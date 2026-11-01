import asyncio
import hashlib
import os
from datetime import timedelta
from typing import cast
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fluxgram_backend.application.plugin_reputation_decisions import (
    PluginReputationDecisionQueue,
    ReputationDecisionConflict,
    ReputationDecisionLeaseLost,
    ReputationDecisionState,
)
from fluxgram_backend.application.plugin_reputation_publisher import (
    PluginReputationPublisher,
    ReputationPublication,
)
from fluxgram_backend.database.migrations import run_migrations
from fluxgram_backend.database.models import (
    AuditLogRecord,
    OperationRecord,
    PluginReputationDecisionRecord,
)
from fluxgram_backend.domain.operations import ActorType, OperationContext
from fluxgram_backend.reputation.protocol import ArchiveStatus, KeyStatus, ReputationQuery
from fluxgram_backend.reputation.signing import AuthoritySignature, SignatureEncoding
from fluxgram_backend.reputation.verification import p256_key_id
from fluxgram_backend.settings import Settings


class FakeAuthoritySigner:
    def __init__(self) -> None:
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.public_key = self.private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    async def get_public_key(self) -> bytes:
        return self.public_key

    async def sign_sha256(self, digest: bytes) -> AuthoritySignature:
        signature = self.private_key.sign(
            digest,
            ec.ECDSA(Prehashed(hashes.SHA256())),
        )
        return AuthoritySignature(signature, SignatureEncoding.DER)


def _publication(identity: str, offset: int = 0) -> ReputationPublication:
    return ReputationPublication(
        ReputationQuery(hashlib.sha256(identity.encode()).hexdigest()),
        ArchiveStatus.CLEAR if offset == 0 else ArchiveStatus.MALICIOUS,
        KeyStatus.NOT_APPLICABLE,
        1_700_000_000_000 + offset,
        1_700_086_400_000 + offset,
    )


def _context(identity: str, suffix: str) -> OperationContext:
    return OperationContext(
        f"reputation-decision:{identity}:{suffix}",
        ActorType.TELEGRAM_ADMIN,
        100_000 + len(suffix),
    )


def _decision_result(
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


def test_reputation_decision_queue_rejects_invalid_boundaries_before_database_access() -> None:
    sessions = cast(async_sessionmaker[AsyncSession], None)
    queue = PluginReputationDecisionQueue(sessions)
    publication = _publication("boundary")

    with pytest.raises(ValueError, match="Telegram administrator"):
        asyncio.run(
            queue.enqueue(
                OperationContext("system-decision", ActorType.SYSTEM, None),
                publication,
            )
        )
    with pytest.raises(ValueError, match="worker_id"):
        asyncio.run(queue.claim_next("invalid worker", timedelta(seconds=10)))
    with pytest.raises(ValueError, match="lease duration"):
        asyncio.run(queue.claim_next("worker", timedelta(0)))
    with pytest.raises(ValueError, match="error_code"):
        asyncio.run(queue.mark_failed("decision", "lease", "Invalid Error"))


@pytest.mark.integration
def test_reputation_decision_queue_is_idempotent_fenced_and_fifo() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    run_migrations(Settings.model_validate({"database_url": database_url}))
    identity = uuid4().hex
    first_publication = _publication(f"{identity}:a")
    second_publication = _publication(f"{identity}:a", 1)
    other_publication = _publication(f"{identity}:b")
    first_context = _context(identity, "a1")
    second_context = _context(identity, "a2")
    other_context = _context(identity, "b1")

    async def check() -> None:
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        queue = PluginReputationDecisionQueue(sessions)
        signer = FakeAuthoritySigner()
        publisher = PluginReputationPublisher(
            sessions,
            signer,
            p256_key_id(signer.public_key),
        )

        try:
            first = await queue.enqueue(first_context, first_publication)
            replay = await queue.enqueue(first_context, first_publication)
            second = await queue.enqueue(second_context, second_publication)
            other = await queue.enqueue(other_context, other_publication)

            assert replay.replayed
            assert replay.sequence_id == first.sequence_id
            assert first.publication_operation_id != first.decision_operation_id
            with pytest.raises(ReputationDecisionConflict, match="different decision"):
                await queue.enqueue(first_context, second_publication)

            first_claim = await queue.claim_next("publisher-1", timedelta(milliseconds=100))
            other_claim = await queue.claim_next("publisher-2", timedelta(seconds=10))
            blocked = await queue.claim_next("publisher-3", timedelta(seconds=10))

            assert first_claim is not None
            assert other_claim is not None
            assert first_claim.decision.sequence_id == first.sequence_id
            assert other_claim.decision.sequence_id == other.sequence_id
            assert blocked is None

            await queue.mark_failed(
                other.decision_operation_id,
                other_claim.decision.lease_token or "",
                "invalid_decision",
            )
            await asyncio.sleep(0.15)
            reclaimed = await queue.claim_next("publisher-3", timedelta(seconds=10))
            assert reclaimed is not None
            assert reclaimed.decision.sequence_id == first.sequence_id
            assert reclaimed.decision.attempt_count == 2
            assert reclaimed.decision.lease_token != first_claim.decision.lease_token

            with pytest.raises(ReputationDecisionLeaseLost):
                await queue.renew_claim(
                    first.decision_operation_id,
                    first_claim.decision.lease_token or "",
                    timedelta(seconds=10),
                )

            published = await publisher.publish(
                reclaimed.publication_context,
                reclaimed.decision.publication,
            )
            completed = await queue.mark_published(
                first.decision_operation_id,
                reclaimed.decision.lease_token or "",
                published,
            )
            assert completed.state is ReputationDecisionState.PUBLISHED
            assert completed.published_revision == 0

            second_claim = await queue.claim_next("publisher-4", timedelta(seconds=10))
            assert second_claim is not None
            assert second_claim.decision.sequence_id == second.sequence_id
            rescheduled = await queue.reschedule(
                second.decision_operation_id,
                second_claim.decision.lease_token or "",
                timedelta(0),
                "signer_unavailable",
            )
            assert rescheduled.state is ReputationDecisionState.PENDING
            assert rescheduled.last_error_code == "signer_unavailable"
            stored = await queue.get(second.decision_operation_id)
            assert stored == rescheduled

            with pytest.raises(ReputationDecisionLeaseLost):
                await queue.mark_failed(
                    second.decision_operation_id,
                    second_claim.decision.lease_token or "",
                    "stale_worker",
                )

            final_claim = await queue.claim_next("publisher-5", timedelta(seconds=10))
            assert final_claim is not None
            assert final_claim.decision.sequence_id == second.sequence_id
            await queue.mark_failed(
                second.decision_operation_id,
                final_claim.decision.lease_token or "",
                "invalid_decision",
            )

            async with sessions() as session:
                audit = await session.scalar(
                    select(AuditLogRecord).where(
                        AuditLogRecord.operation_id == first.decision_operation_id
                    )
                )
                assert audit is not None
                assert audit.resource_type == "plugin_reputation_decision"
                assert audit.actor_type == ActorType.TELEGRAM_ADMIN.value
                assert audit.actor_id == first_context.actor_id

            with pytest.raises(DBAPIError, match="intent is immutable"):
                async with engine.begin() as connection:
                    await connection.execute(
                        update(PluginReputationDecisionRecord)
                        .where(
                            PluginReputationDecisionRecord.sequence_id == first.sequence_id
                        )
                        .values(archive_status=int(ArchiveStatus.MALICIOUS))
                    )
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.integration
def test_reputation_decision_claims_are_unique_across_workers() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    run_migrations(Settings.model_validate({"database_url": database_url}))
    identity = uuid4().hex

    async def check() -> None:
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        queue = PluginReputationDecisionQueue(sessions)

        try:
            first = await queue.enqueue(_context(identity, "c1"), _publication(f"{identity}:c"))
            second = await queue.enqueue(_context(identity, "d1"), _publication(f"{identity}:d"))
            claims = await asyncio.gather(
                queue.claim_next("publisher-a", timedelta(seconds=10)),
                queue.claim_next("publisher-b", timedelta(seconds=10)),
            )
            claimed_ids = {
                claim.decision.sequence_id for claim in claims if claim is not None
            }

            assert claimed_ids == {first.sequence_id, second.sequence_id}
            for claim in claims:
                assert claim is not None
                await queue.mark_failed(
                    claim.decision.decision_operation_id,
                    claim.decision.lease_token or "",
                    "invalid_decision",
                )
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.integration
def test_concurrent_enqueues_preserve_fifo_for_exact_identity() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    run_migrations(Settings.model_validate({"database_url": database_url}))
    identity = uuid4().hex
    first_publication = _publication(identity)
    second_publication = _publication(identity, 1)

    async def check() -> None:
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        queue = PluginReputationDecisionQueue(sessions)
        blocker = await engine.connect()
        transaction = await blocker.begin()
        lock_key = (
            "reputation_decision_identity:"
            f"{first_publication.query.archive_sha256}:"
        )

        try:
            await blocker.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
                {"identity": lock_key},
            )
            first_task = asyncio.create_task(
                queue.enqueue(_context(identity, "fifo-1"), first_publication)
            )
            await asyncio.sleep(0.05)
            second_task = asyncio.create_task(
                queue.enqueue(_context(identity, "fifo-2"), second_publication)
            )
            await asyncio.sleep(0.05)
            await transaction.commit()
            first, second = await asyncio.gather(first_task, second_task)

            assert first.sequence_id < second.sequence_id
            first_claim = await queue.claim_next("publisher-fifo", timedelta(seconds=10))
            assert first_claim is not None
            assert first_claim.decision.sequence_id == first.sequence_id
            await queue.mark_failed(
                first.decision_operation_id,
                first_claim.decision.lease_token or "",
                "invalid_decision",
            )
            second_claim = await queue.claim_next("publisher-fifo", timedelta(seconds=10))
            assert second_claim is not None
            assert second_claim.decision.sequence_id == second.sequence_id
            await queue.mark_failed(
                second.decision_operation_id,
                second_claim.decision.lease_token or "",
                "invalid_decision",
            )
        finally:
            if transaction.is_active:
                await transaction.rollback()
            await blocker.close()
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.integration
def test_database_trigger_allocates_sequence_after_identity_lock() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    run_migrations(Settings.model_validate({"database_url": database_url}))
    identity = uuid4().hex
    first_publication = _publication(identity)
    second_publication = _publication(identity, 1)
    first_decision_id = f"direct-decision:{identity}:1"
    second_decision_id = f"direct-decision:{identity}:2"
    first_publication_id = f"direct-publication:{identity}:1"
    second_publication_id = f"direct-publication:{identity}:2"

    async def check() -> None:
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        queue = PluginReputationDecisionQueue(sessions)

        async with sessions.begin() as session:
            for decision_id, publication_id, publication in (
                (first_decision_id, first_publication_id, first_publication),
                (second_decision_id, second_publication_id, second_publication),
            ):
                session.add(
                    OperationRecord(
                        operation_id=decision_id,
                        operation_type="queue_plugin_reputation",
                        request_fingerprint="a" * 64,
                        actor_type=ActorType.TELEGRAM_ADMIN.value,
                        actor_id=123456,
                        rollbackable=False,
                        reverts_operation_id=None,
                        result=_decision_result(publication, publication_id),
                    )
                )

        async def insert_decision(
            decision_id: str,
            publication_id: str,
            publication: ReputationPublication,
        ) -> int:
            async with sessions.begin() as session:
                record = PluginReputationDecisionRecord(
                    decision_operation_id=decision_id,
                    publication_operation_id=publication_id,
                    archive_sha256=publication.query.archive_sha256,
                    developer_key_id=publication.query.developer_key_id or "",
                    issued_at_millis=publication.issued_at_millis,
                    expires_at_millis=publication.expires_at_millis,
                    archive_status=int(publication.archive_status),
                    key_status=int(publication.key_status),
                )
                session.add(record)
                await session.flush()
                return record.sequence_id

        blocker = await engine.connect()
        transaction = await blocker.begin()
        lock_key = (
            "reputation_decision_identity:"
            f"{first_publication.query.archive_sha256}:"
        )

        try:
            await blocker.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
                {"identity": lock_key},
            )
            first_task = asyncio.create_task(
                insert_decision(
                    first_decision_id,
                    first_publication_id,
                    first_publication,
                )
            )
            await asyncio.sleep(0.05)
            second_task = asyncio.create_task(
                insert_decision(
                    second_decision_id,
                    second_publication_id,
                    second_publication,
                )
            )
            await asyncio.sleep(0.05)
            await transaction.commit()
            first_sequence, second_sequence = await asyncio.gather(
                first_task,
                second_task,
            )

            assert first_sequence < second_sequence
            for expected_sequence in (first_sequence, second_sequence):
                claim = await queue.claim_next("publisher-direct", timedelta(seconds=10))
                assert claim is not None
                assert claim.decision.sequence_id == expected_sequence
                await queue.mark_failed(
                    claim.decision.decision_operation_id,
                    claim.decision.lease_token or "",
                    "invalid_decision",
                )
        finally:
            if transaction.is_active:
                await transaction.rollback()
            await blocker.close()
            await engine.dispose()

    asyncio.run(check())
