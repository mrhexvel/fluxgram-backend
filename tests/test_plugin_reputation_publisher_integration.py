import asyncio
import hashlib
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fluxgram_backend.application.plugin_reputation_publisher import (
    PluginReputationPublisher,
    ReputationPublication,
    ReputationPublicationConflict,
    ReputationPublicationError,
    ReputationPublicationExpired,
)
from fluxgram_backend.database.migrations import run_migrations
from fluxgram_backend.database.models import (
    AuditLogRecord,
    OperationRecord,
    PluginReputationRecord,
)
from fluxgram_backend.domain.operations import ActorType, OperationContext
from fluxgram_backend.reputation.protocol import (
    ArchiveStatus,
    KeyStatus,
    ReputationQuery,
    ReputationResponse,
    SignatureEnvelope,
)
from fluxgram_backend.reputation.signing import AuthoritySignature, SignatureEncoding
from fluxgram_backend.reputation.verification import (
    canonical_message,
    p256_key_id,
    verify_signed_record,
)
from fluxgram_backend.settings import Settings


class FakeAuthoritySigner:
    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        self.private_key = private_key
        self.public_key = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.sign_count = 0
        self.digests: list[bytes] = []

    async def get_public_key(self) -> bytes:
        return self.public_key

    async def sign_sha256(self, digest: bytes) -> AuthoritySignature:
        self.sign_count += 1
        self.digests.append(bytes(digest))
        signature = self.private_key.sign(
            digest,
            ec.ECDSA(Prehashed(hashes.SHA256())),
        )
        return AuthoritySignature(signature, SignatureEncoding.DER)


class InvalidAuthoritySigner(FakeAuthoritySigner):
    async def sign_sha256(self, digest: bytes) -> AuthoritySignature:
        self.sign_count += 1
        self.digests.append(bytes(digest))
        return AuthoritySignature(
            b"0\x06\x02\x01\x00\x02\x01\x01",
            SignatureEncoding.DER,
        )


class SlowAuthoritySigner(FakeAuthoritySigner):
    async def sign_sha256(self, digest: bytes) -> AuthoritySignature:
        await asyncio.sleep(1)
        return await super().sign_sha256(digest)


@pytest.mark.integration
def test_publisher_is_idempotent_revisioned_audited_and_rolls_back_signing_failure() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    run_migrations(Settings.model_validate({"database_url": database_url}))
    identity = uuid4().hex
    archive_sha256 = hashlib.sha256(identity.encode()).hexdigest()
    query = ReputationQuery(archive_sha256)
    now_millis = int(datetime.now(UTC).timestamp() * 1000)
    first_publication = ReputationPublication(
        query,
        ArchiveStatus.CLEAR,
        KeyStatus.NOT_APPLICABLE,
        now_millis - 2_000,
        now_millis + 86_400_000,
    )
    second_publication = ReputationPublication(
        query,
        ArchiveStatus.MALICIOUS,
        KeyStatus.NOT_APPLICABLE,
        now_millis - 1_000,
        now_millis + 86_400_001,
    )

    async def check() -> None:
        engine = create_async_engine(database_url)
        connection = await engine.connect()
        transaction = await connection.begin()
        sessions = async_sessionmaker(
            connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        signer = FakeAuthoritySigner(ec.generate_private_key(ec.SECP256R1()))
        authority_key_id = p256_key_id(signer.public_key)
        publisher = PluginReputationPublisher(sessions, signer, authority_key_id)
        first_context = OperationContext(
            f"reputation-publisher:{identity}:0",
            ActorType.SYSTEM,
            None,
        )

        try:
            first = await publisher.publish(first_context, first_publication)
            replay = await publisher.publish(first_context, first_publication)

            assert first.revision == 0
            assert not first.replayed
            assert replay.revision == 0
            assert replay.replayed
            assert replay.signed_record == first.signed_record
            assert signer.sign_count == 1
            first_response = ReputationResponse.decode(first.signed_record.response)
            first_envelope = SignatureEnvelope.decode(first.signed_record.envelope)
            expected_digest = hashlib.sha256(
                canonical_message(
                    first_response,
                    first_envelope.algorithm,
                    first_envelope.public_key,
                )
            ).digest()
            assert signer.digests == [expected_digest]

            with pytest.raises(ReputationPublicationConflict, match="different intent"):
                await publisher.publish(first_context, second_publication)

            second = await publisher.publish(
                OperationContext(
                    f"reputation-publisher:{identity}:1",
                    ActorType.SYSTEM,
                    None,
                ),
                second_publication,
            )

            assert second.revision == 1
            assert signer.sign_count == 2
            verify_signed_record(
                query,
                second.signed_record.response,
                second.signed_record.envelope,
                authority_key_id,
                second_publication.issued_at_millis,
            )

            invalid_operation_id = f"reputation-publisher:{identity}:invalid"
            invalid_publisher = PluginReputationPublisher(
                sessions,
                InvalidAuthoritySigner(signer.private_key),
                authority_key_id,
            )
            with pytest.raises(ReputationPublicationError, match="validation failed"):
                await invalid_publisher.publish(
                    OperationContext(invalid_operation_id, ActorType.SYSTEM, None),
                    ReputationPublication(
                        query,
                        ArchiveStatus.CLEAR,
                        KeyStatus.NOT_APPLICABLE,
                        now_millis - 900,
                        now_millis + 86_400_002,
                    ),
                )

            timeout_operation_id = f"reputation-publisher:{identity}:timeout"
            timeout_publisher = PluginReputationPublisher(
                sessions,
                SlowAuthoritySigner(signer.private_key),
                authority_key_id,
                signer_timeout_seconds=0.01,
            )
            with pytest.raises(ReputationPublicationError, match="timed out"):
                await timeout_publisher.publish(
                    OperationContext(timeout_operation_id, ActorType.SYSTEM, None),
                    ReputationPublication(
                        query,
                        ArchiveStatus.CLEAR,
                        KeyStatus.NOT_APPLICABLE,
                        now_millis - 800,
                        now_millis + 86_400_003,
                    ),
                )

            expired_operation_id = f"reputation-publisher:{identity}:expired"
            with pytest.raises(ReputationPublicationExpired):
                await publisher.publish(
                    OperationContext(expired_operation_id, ActorType.SYSTEM, None),
                    ReputationPublication(
                        query,
                        ArchiveStatus.MALICIOUS,
                        KeyStatus.NOT_APPLICABLE,
                        now_millis - 2_000,
                        now_millis - 1_000,
                    ),
                )

            async with sessions() as session:
                record_count = await session.scalar(
                    select(func.count()).select_from(PluginReputationRecord).where(
                        PluginReputationRecord.archive_sha256 == archive_sha256
                    )
                )
                audit_count = await session.scalar(
                    select(func.count()).select_from(AuditLogRecord).where(
                        AuditLogRecord.resource_key["archive_sha256"].as_string()
                        == archive_sha256
                    )
                )
                failed_operation = await session.get(OperationRecord, invalid_operation_id)
                timeout_operation = await session.get(OperationRecord, timeout_operation_id)
                expired_operation = await session.get(OperationRecord, expired_operation_id)

            assert record_count == 2
            assert audit_count == 2
            assert failed_operation is None
            assert timeout_operation is None
            assert expired_operation is None
        finally:
            await transaction.rollback()
            await connection.close()
            await engine.dispose()

    asyncio.run(check())
