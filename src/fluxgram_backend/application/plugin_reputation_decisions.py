import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from fluxgram_backend.application.plugin_reputation_publisher import (
    PublishedReputationRecord,
    ReputationPublication,
)
from fluxgram_backend.database.models import (
    OperationRecord,
    PluginReputationDecisionRecord,
    PluginReputationRecord,
)
from fluxgram_backend.domain.operations import ActorType, OperationContext
from fluxgram_backend.reputation.protocol import ArchiveStatus, KeyStatus, ReputationQuery

OPERATION_TYPE = "queue_plugin_reputation"
WORKER_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_LEASE_SECONDS = 900.0
MAX_RETRY_DELAY_SECONDS = 86_400.0


class ReputationDecisionError(Exception):
    pass


class ReputationDecisionConflict(ReputationDecisionError):
    pass


class ReputationDecisionLeaseLost(ReputationDecisionError):
    pass


class ReputationDecisionState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PUBLISHED = "published"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReputationDecision:
    sequence_id: int
    decision_operation_id: str
    publication_operation_id: str
    publication: ReputationPublication
    state: ReputationDecisionState
    available_at: datetime
    attempt_count: int
    lease_token: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error_code: str | None
    published_revision: int | None
    completed_at: datetime | None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ClaimedReputationDecision:
    decision: ReputationDecision

    @property
    def publication_context(self) -> OperationContext:
        return OperationContext(
            self.decision.publication_operation_id,
            ActorType.SYSTEM,
            None,
        )


class PluginReputationDecisionQueue:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def enqueue(
        self,
        context: OperationContext,
        publication: ReputationPublication,
    ) -> ReputationDecision:
        if context.actor_type is not ActorType.TELEGRAM_ADMIN:
            raise ValueError("reputation decisions require a Telegram administrator")

        publication_operation_id = self._publication_operation_id(context.operation_id)
        fingerprint = self._fingerprint(context, publication, publication_operation_id)
        result = self._operation_result(publication, publication_operation_id)

        async with self._sessions.begin() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:operation_id, 0))"),
                {"operation_id": f"operation:{context.operation_id}"},
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
                {
                    "identity": (
                        "reputation_decision_identity:"
                        f"{publication.query.archive_sha256}:"
                        f"{publication.query.developer_key_id or ''}"
                    )
                },
            )
            operation = await session.get(OperationRecord, context.operation_id)
            if operation is not None:
                return await self._replay(
                    session,
                    operation,
                    context,
                    fingerprint,
                    result,
                )

            session.add(
                OperationRecord(
                    operation_id=context.operation_id,
                    operation_type=OPERATION_TYPE,
                    request_fingerprint=fingerprint,
                    actor_type=context.actor_type.value,
                    actor_id=context.actor_id,
                    rollbackable=False,
                    reverts_operation_id=None,
                    result=result,
                )
            )
            await session.flush()

            record = PluginReputationDecisionRecord(
                decision_operation_id=context.operation_id,
                publication_operation_id=publication_operation_id,
                archive_sha256=publication.query.archive_sha256,
                developer_key_id=publication.query.developer_key_id or "",
                issued_at_millis=publication.issued_at_millis,
                expires_at_millis=publication.expires_at_millis,
                archive_status=int(publication.archive_status),
                key_status=int(publication.key_status),
            )
            session.add(record)
            await session.flush()
            await session.refresh(record)

            return self._decision(record)

    async def get(self, decision_operation_id: str) -> ReputationDecision | None:
        async with self._sessions() as session:
            record = await session.scalar(
                select(PluginReputationDecisionRecord).where(
                    PluginReputationDecisionRecord.decision_operation_id
                    == decision_operation_id
                )
            )
            if record is None:
                return None

            return self._decision(record)

    async def claim_next(
        self,
        worker_id: str,
        lease_duration: timedelta,
    ) -> ClaimedReputationDecision | None:
        self._validate_worker_id(worker_id)
        self._validate_duration(lease_duration, MAX_LEASE_SECONDS, "lease duration")

        candidate = aliased(PluginReputationDecisionRecord)
        earlier = aliased(PluginReputationDecisionRecord)
        now = func.transaction_timestamp()
        claimable = or_(
            and_(
                candidate.state == ReputationDecisionState.PENDING.value,
                candidate.available_at <= now,
            ),
            and_(
                candidate.state == ReputationDecisionState.CLAIMED.value,
                candidate.lease_expires_at <= now,
            ),
        )
        earlier_active = exists(
            select(1).where(
                earlier.archive_sha256 == candidate.archive_sha256,
                earlier.developer_key_id == candidate.developer_key_id,
                earlier.sequence_id < candidate.sequence_id,
                earlier.state.in_(
                    (
                        ReputationDecisionState.PENDING.value,
                        ReputationDecisionState.CLAIMED.value,
                    )
                ),
            )
        )

        async with self._sessions.begin() as session:
            record = await session.scalar(
                select(candidate)
                .where(claimable, ~earlier_active)
                .order_by(candidate.sequence_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if record is None:
                return None

            database_now = await session.scalar(select(func.transaction_timestamp()))
            if database_now is None:
                raise ReputationDecisionError("database time is unavailable")

            record.state = ReputationDecisionState.CLAIMED.value
            record.lease_token = str(uuid4())
            record.lease_owner = worker_id
            record.lease_expires_at = database_now + lease_duration
            record.attempt_count += 1
            record.last_error_code = None
            await session.flush()
            await session.refresh(record)

            return ClaimedReputationDecision(self._decision(record))

    async def renew_claim(
        self,
        decision_operation_id: str,
        lease_token: str,
        lease_duration: timedelta,
    ) -> ClaimedReputationDecision:
        self._validate_duration(lease_duration, MAX_LEASE_SECONDS, "lease duration")

        async with self._sessions.begin() as session:
            record = await self._lock_claim(session, decision_operation_id, lease_token)
            database_now = await session.scalar(select(func.transaction_timestamp()))
            if database_now is None:
                raise ReputationDecisionError("database time is unavailable")

            record.lease_expires_at = database_now + lease_duration
            await session.flush()
            await session.refresh(record)

            return ClaimedReputationDecision(self._decision(record))

    async def reschedule(
        self,
        decision_operation_id: str,
        lease_token: str,
        retry_delay: timedelta,
        error_code: str,
    ) -> ReputationDecision:
        self._validate_duration(
            retry_delay,
            MAX_RETRY_DELAY_SECONDS,
            "retry delay",
            allow_zero=True,
        )
        self._validate_error_code(error_code)

        async with self._sessions.begin() as session:
            record = await self._lock_claim(session, decision_operation_id, lease_token)
            database_now = await session.scalar(select(func.transaction_timestamp()))
            if database_now is None:
                raise ReputationDecisionError("database time is unavailable")

            record.state = ReputationDecisionState.PENDING.value
            record.available_at = database_now + retry_delay
            record.lease_token = None
            record.lease_owner = None
            record.lease_expires_at = None
            record.last_error_code = error_code
            await session.flush()
            await session.refresh(record)

            return self._decision(record)

    async def mark_failed(
        self,
        decision_operation_id: str,
        lease_token: str,
        error_code: str,
    ) -> ReputationDecision:
        self._validate_error_code(error_code)

        async with self._sessions.begin() as session:
            record = await self._lock_claim(session, decision_operation_id, lease_token)
            database_now = await session.scalar(select(func.transaction_timestamp()))
            if database_now is None:
                raise ReputationDecisionError("database time is unavailable")

            record.state = ReputationDecisionState.FAILED.value
            record.lease_token = None
            record.lease_owner = None
            record.lease_expires_at = None
            record.last_error_code = error_code
            record.completed_at = database_now
            await session.flush()
            await session.refresh(record)

            return self._decision(record)

    async def mark_published(
        self,
        decision_operation_id: str,
        lease_token: str,
        result: PublishedReputationRecord,
    ) -> ReputationDecision:
        async with self._sessions.begin() as session:
            decision = await self._lock_claim(session, decision_operation_id, lease_token)
            if result.operation_id != decision.publication_operation_id:
                raise ReputationDecisionConflict("publication operation does not match decision")

            publication = await session.scalar(
                select(PluginReputationRecord).where(
                    PluginReputationRecord.operation_id == result.operation_id,
                    PluginReputationRecord.archive_sha256 == decision.archive_sha256,
                    PluginReputationRecord.developer_key_id == decision.developer_key_id,
                    PluginReputationRecord.revision == result.revision,
                )
            )
            if publication is None:
                raise ReputationDecisionConflict("published record does not match decision")

            database_now = await session.scalar(select(func.transaction_timestamp()))
            if database_now is None:
                raise ReputationDecisionError("database time is unavailable")

            decision.state = ReputationDecisionState.PUBLISHED.value
            decision.lease_token = None
            decision.lease_owner = None
            decision.lease_expires_at = None
            decision.last_error_code = None
            decision.published_revision = result.revision
            decision.completed_at = database_now
            await session.flush()
            await session.refresh(decision)

            return self._decision(decision)

    async def _replay(
        self,
        session: AsyncSession,
        operation: OperationRecord,
        context: OperationContext,
        fingerprint: str,
        result: dict[str, object],
    ) -> ReputationDecision:
        if (
            operation.operation_type != OPERATION_TYPE
            or operation.request_fingerprint != fingerprint
            or operation.actor_type != context.actor_type.value
            or operation.actor_id != context.actor_id
            or operation.result != result
        ):
            raise ReputationDecisionConflict(
                "operation_id was already used for a different decision"
            )

        record = await session.scalar(
            select(PluginReputationDecisionRecord).where(
                PluginReputationDecisionRecord.decision_operation_id == context.operation_id
            )
        )
        if record is None:
            raise ReputationDecisionConflict("decision operation has no queue record")

        decision = self._decision(record)
        return ReputationDecision(
            sequence_id=decision.sequence_id,
            decision_operation_id=decision.decision_operation_id,
            publication_operation_id=decision.publication_operation_id,
            publication=decision.publication,
            state=decision.state,
            available_at=decision.available_at,
            attempt_count=decision.attempt_count,
            lease_token=decision.lease_token,
            lease_owner=decision.lease_owner,
            lease_expires_at=decision.lease_expires_at,
            last_error_code=decision.last_error_code,
            published_revision=decision.published_revision,
            completed_at=decision.completed_at,
            replayed=True,
        )

    @staticmethod
    async def _lock_claim(
        session: AsyncSession,
        decision_operation_id: str,
        lease_token: str,
    ) -> PluginReputationDecisionRecord:
        record = await session.scalar(
            select(PluginReputationDecisionRecord)
            .where(
                PluginReputationDecisionRecord.decision_operation_id
                == decision_operation_id,
                PluginReputationDecisionRecord.state
                == ReputationDecisionState.CLAIMED.value,
                PluginReputationDecisionRecord.lease_token == lease_token,
                PluginReputationDecisionRecord.lease_expires_at
                > func.transaction_timestamp(),
            )
            .with_for_update()
        )
        if record is None:
            raise ReputationDecisionLeaseLost("decision lease is no longer active")

        return record

    @staticmethod
    def _decision(record: PluginReputationDecisionRecord) -> ReputationDecision:
        publication = ReputationPublication(
            ReputationQuery(
                record.archive_sha256,
                record.developer_key_id or None,
            ),
            ArchiveStatus(record.archive_status),
            KeyStatus(record.key_status),
            record.issued_at_millis,
            record.expires_at_millis,
        )
        return ReputationDecision(
            sequence_id=record.sequence_id,
            decision_operation_id=record.decision_operation_id,
            publication_operation_id=record.publication_operation_id,
            publication=publication,
            state=ReputationDecisionState(record.state),
            available_at=record.available_at,
            attempt_count=record.attempt_count,
            lease_token=record.lease_token,
            lease_owner=record.lease_owner,
            lease_expires_at=record.lease_expires_at,
            last_error_code=record.last_error_code,
            published_revision=record.published_revision,
            completed_at=record.completed_at,
        )

    @staticmethod
    def _publication_operation_id(decision_operation_id: str) -> str:
        digest = hashlib.sha256(decision_operation_id.encode()).hexdigest()
        return f"reputation:{digest}"

    @staticmethod
    def _operation_result(
        publication: ReputationPublication,
        publication_operation_id: str,
    ) -> dict[str, object]:
        return {
            "archive_sha256": publication.query.archive_sha256,
            "developer_key_id": publication.query.developer_key_id,
            "archive_status": int(publication.archive_status),
            "key_status": int(publication.key_status),
            "issued_at_millis": publication.issued_at_millis,
            "expires_at_millis": publication.expires_at_millis,
            "publication_operation_id": publication_operation_id,
        }

    @classmethod
    def _fingerprint(
        cls,
        context: OperationContext,
        publication: ReputationPublication,
        publication_operation_id: str,
    ) -> str:
        value = {
            "operation_type": OPERATION_TYPE,
            "actor_type": context.actor_type.value,
            "actor_id": context.actor_id,
            "intent": cls._operation_result(publication, publication_operation_id),
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _validate_worker_id(worker_id: str) -> None:
        if WORKER_ID_PATTERN.fullmatch(worker_id) is None:
            raise ValueError("worker_id is invalid")

    @staticmethod
    def _validate_error_code(error_code: str) -> None:
        if ERROR_CODE_PATTERN.fullmatch(error_code) is None:
            raise ValueError("error_code is invalid")

    @staticmethod
    def _validate_duration(
        value: timedelta,
        maximum_seconds: float,
        label: str,
        allow_zero: bool = False,
    ) -> None:
        seconds = value.total_seconds()
        if (
            not math.isfinite(seconds)
            or seconds < 0
            or (not allow_zero and seconds == 0)
            or seconds > maximum_seconds
        ):
            raise ValueError(f"{label} is outside the supported range")
