import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fluxgram_backend.application.plugin_reputation import SignedReputationRecord
from fluxgram_backend.database.models import OperationRecord, PluginReputationRecord
from fluxgram_backend.database.plugin_reputation import signed_record_from_row
from fluxgram_backend.domain.operations import OperationContext
from fluxgram_backend.reputation.protocol import (
    ALGORITHM_ECDSA_P256_SHA256,
    ArchiveStatus,
    KeyStatus,
    ReputationQuery,
    ReputationResponse,
    SignatureEnvelope,
)
from fluxgram_backend.reputation.signing import (
    AuthoritySignature,
    AuthoritySigner,
    AuthoritySigningError,
    normalize_p256_signature,
)
from fluxgram_backend.reputation.verification import (
    VerificationError,
    canonical_message,
    p256_key_id,
    verify_signed_record,
)

OPERATION_TYPE = "publish_plugin_reputation"


class ReputationPublicationError(Exception):
    pass


class ReputationPublicationConflict(ReputationPublicationError):
    pass


@dataclass(frozen=True, slots=True)
class ReputationPublication:
    query: ReputationQuery
    archive_status: ArchiveStatus
    key_status: KeyStatus
    issued_at_millis: int
    expires_at_millis: int

    def __post_init__(self) -> None:
        ReputationResponse(
            self.query.archive_sha256,
            self.query.developer_key_id,
            0,
            self.issued_at_millis,
            self.expires_at_millis,
            self.archive_status,
            self.key_status,
        )


@dataclass(frozen=True, slots=True)
class PublishedReputationRecord:
    operation_id: str
    revision: int
    signed_record: SignedReputationRecord
    replayed: bool = False


class PluginReputationPublisher:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        signer: AuthoritySigner,
        authority_key_id: str,
        signer_timeout_seconds: float = 10.0,
    ) -> None:
        if (
            not math.isfinite(signer_timeout_seconds)
            or signer_timeout_seconds <= 0
            or signer_timeout_seconds > 60
        ):
            raise ValueError("signer timeout must be between 0 and 60 seconds")

        self._sessions = sessions
        self._signer = signer
        self._authority_key_id = authority_key_id
        self._signer_timeout_seconds = signer_timeout_seconds

    async def publish(
        self,
        context: OperationContext,
        publication: ReputationPublication,
    ) -> PublishedReputationRecord:
        fingerprint = self._fingerprint(context, publication)

        async with self._sessions.begin() as session:
            replay = await self._find_replay(session, context, publication, fingerprint)
            if replay is not None:
                return replay

            await self._lock_identity(session, publication.query)
            revision = await self._next_revision(session, publication.query)
            response = ReputationResponse(
                publication.query.archive_sha256,
                publication.query.developer_key_id,
                revision,
                publication.issued_at_millis,
                publication.expires_at_millis,
                publication.archive_status,
                publication.key_status,
            )
            signed_record = await self._sign(response, publication.query)
            result = self._result_value(publication.query, revision)

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
            session.add(
                PluginReputationRecord(
                    archive_sha256=publication.query.archive_sha256,
                    developer_key_id=publication.query.developer_key_id or "",
                    revision=revision,
                    issued_at_millis=publication.issued_at_millis,
                    expires_at_millis=publication.expires_at_millis,
                    archive_status=int(publication.archive_status),
                    key_status=int(publication.key_status),
                    authority_key_id=self._authority_key_id,
                    response=signed_record.response,
                    envelope=signed_record.envelope,
                    operation_id=context.operation_id,
                )
            )
            await session.flush()

            return PublishedReputationRecord(context.operation_id, revision, signed_record)

    async def _find_replay(
        self,
        session: AsyncSession,
        context: OperationContext,
        publication: ReputationPublication,
        fingerprint: str,
    ) -> PublishedReputationRecord | None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:operation_id, 0))"),
            {"operation_id": f"operation:{context.operation_id}"},
        )
        operation = await session.get(OperationRecord, context.operation_id)
        if operation is None:
            return None

        if (
            operation.operation_type != OPERATION_TYPE
            or operation.request_fingerprint != fingerprint
        ):
            raise ReputationPublicationConflict(
                "operation_id was already used for a different intent"
            )

        record = await session.scalar(
            select(PluginReputationRecord).where(
                PluginReputationRecord.operation_id == context.operation_id
            )
        )
        if record is None:
            raise ReputationPublicationConflict("published operation has no reputation record")

        expected_result = self._result_value(publication.query, record.revision)
        if operation.result != expected_result:
            raise ReputationPublicationConflict("published operation result is inconsistent")

        try:
            signed_record = signed_record_from_row(record)
        except ValueError as error:
            raise ReputationPublicationConflict(
                "published reputation metadata is inconsistent"
            ) from error

        try:
            response = ReputationResponse.decode(signed_record.response)
            expected_response = ReputationResponse(
                publication.query.archive_sha256,
                publication.query.developer_key_id,
                record.revision,
                publication.issued_at_millis,
                publication.expires_at_millis,
                publication.archive_status,
                publication.key_status,
            )
            if response != expected_response:
                raise ReputationPublicationConflict(
                    "published reputation record does not match operation intent"
                )

            verify_signed_record(
                publication.query,
                signed_record.response,
                signed_record.envelope,
                self._authority_key_id,
                publication.issued_at_millis,
            )
        except (ValueError, VerificationError) as error:
            raise ReputationPublicationConflict("published reputation record is invalid") from error

        return PublishedReputationRecord(
            context.operation_id,
            record.revision,
            signed_record,
            replayed=True,
        )

    async def _sign(
        self,
        response: ReputationResponse,
        query: ReputationQuery,
    ) -> SignedReputationRecord:
        try:
            async with asyncio.timeout(self._signer_timeout_seconds):
                public_key = await self._signer.get_public_key()
                if not isinstance(public_key, bytes) or not 1 <= len(public_key) <= 256:
                    raise AuthoritySigningError("Authority returned malformed public key")
                if p256_key_id(public_key) != self._authority_key_id:
                    raise AuthoritySigningError(
                        "Authority public key does not match configured pin"
                    )

                message = canonical_message(response, ALGORITHM_ECDSA_P256_SHA256, public_key)
                digest = hashlib.sha256(message).digest()
                authority_signature = await self._signer.sign_sha256(digest)
                if not isinstance(authority_signature, AuthoritySignature):
                    raise AuthoritySigningError("Authority returned invalid signature result")

                signature = normalize_p256_signature(authority_signature)
                envelope = SignatureEnvelope(
                    ALGORITHM_ECDSA_P256_SHA256,
                    public_key,
                    signature,
                ).encode()
                encoded_response = response.encode()
                verify_signed_record(
                    query,
                    encoded_response,
                    envelope,
                    self._authority_key_id,
                    response.issued_at_millis,
                )
        except TimeoutError as error:
            raise ReputationPublicationError("Authority signer timed out") from error
        except (AuthoritySigningError, ValueError, VerificationError) as error:
            raise ReputationPublicationError("Authority signature validation failed") from error
        except Exception as error:
            raise ReputationPublicationError("Authority signer failed") from error

        return SignedReputationRecord(encoded_response, envelope)

    @staticmethod
    async def _lock_identity(session: AsyncSession, query: ReputationQuery) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
            {
                "identity": (
                    f"reputation_identity:{query.archive_sha256}:"
                    f"{query.developer_key_id or ''}"
                )
            },
        )

    @staticmethod
    async def _next_revision(session: AsyncSession, query: ReputationQuery) -> int:
        previous = await session.scalar(
            select(func.max(PluginReputationRecord.revision)).where(
                PluginReputationRecord.archive_sha256 == query.archive_sha256,
                PluginReputationRecord.developer_key_id == (query.developer_key_id or ""),
            )
        )
        return 0 if previous is None else previous + 1

    @staticmethod
    def _fingerprint(context: OperationContext, publication: ReputationPublication) -> str:
        value = {
            "operation_type": OPERATION_TYPE,
            "actor_type": context.actor_type.value,
            "actor_id": context.actor_id,
            "intent": {
                "archive_sha256": publication.query.archive_sha256,
                "developer_key_id": publication.query.developer_key_id,
                "archive_status": int(publication.archive_status),
                "key_status": int(publication.key_status),
                "issued_at_millis": publication.issued_at_millis,
                "expires_at_millis": publication.expires_at_millis,
            },
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _result_value(query: ReputationQuery, revision: int) -> dict[str, Any]:
        return {
            "archive_sha256": query.archive_sha256,
            "developer_key_id": query.developer_key_id,
            "revision": revision,
        }
