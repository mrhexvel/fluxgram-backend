import asyncio
import hashlib
import os
from uuid import uuid4

import pytest
from alembic import command
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fluxgram_backend.database.migrations import build_migration_config
from fluxgram_backend.database.models import (
    AuditLogRecord,
    OperationRecord,
    PluginReputationRecord,
)
from fluxgram_backend.database.plugin_reputation import PostgresReputationRecordReader
from fluxgram_backend.reputation.protocol import (
    ArchiveStatus,
    KeyStatus,
    ReputationQuery,
    ReputationResponse,
    SignatureEnvelope,
)
from fluxgram_backend.reputation.verification import P256_ORDER, canonical_message
from fluxgram_backend.settings import Settings


@pytest.mark.integration
def test_reputation_repository_returns_latest_exact_record_and_writes_audit() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    settings = Settings.model_validate({"database_url": database_url})
    config = build_migration_config(settings)
    command.upgrade(config, "head")
    identity = uuid4().hex
    archive_sha256 = hashlib.sha256(identity.encode()).hexdigest()
    operation_prefix = f"reputation:{identity}"
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    authority_key_id = "p256:" + hashlib.sha256(public_key).hexdigest()

    async def check() -> None:
        engine = create_async_engine(database_url)
        connection = await engine.connect()
        transaction = await connection.begin()
        sessions = async_sessionmaker(
            connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )

        try:
            response_bytes = b""
            envelope_bytes = b""

            for revision, archive_status in (
                (0, ArchiveStatus.CLEAR),
                (1, ArchiveStatus.MALICIOUS),
            ):
                operation_id = f"{operation_prefix}:{revision}"
                response = ReputationResponse(
                    archive_sha256,
                    None,
                    revision,
                    1_700_000_000_000 + revision,
                    1_700_086_400_000 + revision,
                    archive_status,
                    KeyStatus.NOT_APPLICABLE,
                )
                response_bytes = response.encode()
                envelope_bytes = sign_response(response, private_key, public_key)

                async with sessions.begin() as session:
                    session.add(
                        OperationRecord(
                            operation_id=operation_id,
                            operation_type="publish_plugin_reputation",
                            request_fingerprint=hashlib.sha256(response_bytes).hexdigest(),
                            actor_type="system",
                            actor_id=None,
                            rollbackable=False,
                            reverts_operation_id=None,
                            result={
                                "archive_sha256": archive_sha256,
                                "developer_key_id": None,
                                "revision": revision,
                            },
                        )
                    )
                    session.add(
                        PluginReputationRecord(
                            archive_sha256=archive_sha256,
                            developer_key_id="",
                            revision=revision,
                            issued_at_millis=response.issued_at_millis,
                            expires_at_millis=response.expires_at_millis,
                            archive_status=int(response.archive_status),
                            key_status=int(response.key_status),
                            authority_key_id=authority_key_id,
                            response=response_bytes,
                            envelope=envelope_bytes,
                            operation_id=operation_id,
                        )
                    )

            reader = PostgresReputationRecordReader(sessions)
            record = await reader.get_record(ReputationQuery(archive_sha256))

            assert record is not None
            assert record.response == response_bytes
            assert record.envelope == envelope_bytes

            async with sessions() as session:
                audits = tuple(
                    await session.scalars(
                        select(AuditLogRecord)
                        .where(AuditLogRecord.operation_id.like(f"{operation_prefix}:%"))
                        .order_by(AuditLogRecord.operation_id)
                    )
                )

            assert len(audits) == 2
            assert all(audit.resource_type == "plugin_reputation" for audit in audits)
            assert audits[1].before_value is None
            assert audits[1].after_value is not None
            assert audits[1].after_value["revision"] == 1
            assert audits[1].after_value["response_sha256"] == hashlib.sha256(
                response_bytes
            ).hexdigest()
            assert audits[1].after_value["envelope_sha256"] == hashlib.sha256(
                envelope_bytes
            ).hexdigest()

            invalid_operation_id = f"{operation_prefix}:invalid"
            invalid_response = ReputationResponse(
                archive_sha256,
                None,
                2,
                1_700_000_000_002,
                1_700_086_400_002,
                ArchiveStatus.CLEAR,
                KeyStatus.NOT_APPLICABLE,
            )
            with pytest.raises(DBAPIError, match="operation is invalid"):
                async with sessions.begin() as session:
                    session.add(
                        OperationRecord(
                            operation_id=invalid_operation_id,
                            operation_type="publish_plugin_reputation",
                            request_fingerprint="0" * 64,
                            actor_type="system",
                            actor_id=None,
                            rollbackable=False,
                            reverts_operation_id=None,
                            result={"revision": 2},
                        )
                    )
                    session.add(
                        PluginReputationRecord(
                            archive_sha256=archive_sha256,
                            developer_key_id="",
                            revision=2,
                            issued_at_millis=invalid_response.issued_at_millis,
                            expires_at_millis=invalid_response.expires_at_millis,
                            archive_status=int(invalid_response.archive_status),
                            key_status=int(invalid_response.key_status),
                            authority_key_id=authority_key_id,
                            response=invalid_response.encode(),
                            envelope=sign_response(invalid_response, private_key, public_key),
                            operation_id=invalid_operation_id,
                        )
                    )

            with pytest.raises(DBAPIError, match="operation is immutable"):
                async with sessions.begin() as session:
                    await session.execute(
                        text(
                            "UPDATE operations SET actor_type = 'telegram_admin', actor_id = 1 "
                            "WHERE operation_id = :operation_id"
                        ),
                        {"operation_id": f"{operation_prefix}:1"},
                    )
        finally:
            await transaction.rollback()
            await connection.close()
            await engine.dispose()

    asyncio.run(check())


def sign_response(
    response: ReputationResponse,
    private_key: ec.EllipticCurvePrivateKey,
    public_key: bytes,
) -> bytes:
    message = canonical_message(response, 1, public_key)
    signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature)
    if s > P256_ORDER // 2:
        s = P256_ORDER - s

    return SignatureEnvelope(1, public_key, encode_dss_signature(r, s)).encode()
