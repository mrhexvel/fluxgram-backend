import asyncio
import hashlib

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fluxgram_backend.application.plugin_reputation import (
    ReputationReadError,
    SignedReputationRecord,
)
from fluxgram_backend.database.models import PluginReputationRecord
from fluxgram_backend.reputation.protocol import (
    ProtocolError,
    ReputationQuery,
    ReputationResponse,
    SignatureEnvelope,
)


class PostgresReputationRecordReader:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_record(self, query: ReputationQuery) -> SignedReputationRecord | None:
        developer_key_id = query.developer_key_id or ""

        try:
            async with self._sessions() as session:
                record = await session.scalar(
                    select(PluginReputationRecord)
                    .where(
                        PluginReputationRecord.archive_sha256 == query.archive_sha256,
                        PluginReputationRecord.developer_key_id == developer_key_id,
                    )
                    .order_by(PluginReputationRecord.revision.desc())
                    .limit(1)
                )
        except SQLAlchemyError as error:
            raise ReputationReadError("Reputation storage lookup failed") from error

        if record is None:
            return None

        return signed_record_from_row(record)

    async def is_ready(self, timeout_seconds: float = 2.0) -> bool:
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._sessions() as session:
                    ready = await session.scalar(text(READ_ONLY_CAPABILITY_QUERY))
        except (TimeoutError, SQLAlchemyError):
            return False
        return ready is True


READ_ONLY_CAPABILITY_QUERY = """
SELECT
    has_table_privilege(current_user, 'plugin_reputation_records', 'SELECT')
    AND NOT has_database_privilege(current_user, current_database(), 'CREATE')
    AND NOT has_database_privilege(current_user, current_database(), 'TEMPORARY')
    AND has_schema_privilege(current_user, 'public', 'USAGE')
    AND NOT has_schema_privilege(current_user, 'public', 'CREATE')
    AND (
        SELECT
            NOT rolsuper
            AND NOT rolcreaterole
            AND NOT rolcreatedb
            AND NOT rolreplication
            AND NOT rolbypassrls
        FROM pg_roles
        WHERE rolname = current_user
    )
    AND NOT EXISTS (
        SELECT 1
        FROM pg_auth_members membership
        JOIN pg_roles member_role ON member_role.oid = membership.member
        WHERE member_role.rolname = current_user
    )
    AND NOT EXISTS (
        SELECT 1
        FROM unnest(ARRAY[
            'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER', 'MAINTAIN'
        ]) AS denied(privilege)
        WHERE has_table_privilege(current_user, 'plugin_reputation_records', denied.privilege)
    )
    AND NOT EXISTS (
        SELECT 1
        FROM unnest(ARRAY['INSERT', 'UPDATE', 'REFERENCES']) AS denied(privilege)
        WHERE has_any_column_privilege(
            current_user,
            'plugin_reputation_records',
            denied.privilege
        )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM unnest(ARRAY[
            'plugin_reputation_decisions', 'operations', 'audit_log'
        ]) AS denied_table(name)
        CROSS JOIN unnest(ARRAY[
            'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER',
            'MAINTAIN'
        ]) AS denied_privilege(name)
        WHERE has_table_privilege(
            current_user,
            denied_table.name,
            denied_privilege.name
        )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM unnest(ARRAY[
            'plugin_reputation_decisions', 'operations', 'audit_log'
        ]) AS denied_table(name)
        CROSS JOIN unnest(ARRAY[
            'SELECT', 'INSERT', 'UPDATE', 'REFERENCES'
        ]) AS denied_privilege(name)
        WHERE has_any_column_privilege(
            current_user,
            denied_table.name,
            denied_privilege.name
        )
    )
"""


def signed_record_from_row(record: PluginReputationRecord) -> SignedReputationRecord:
    response = ReputationResponse.decode(record.response)
    envelope = SignatureEnvelope.decode(record.envelope)
    developer_key_id = record.developer_key_id or None
    authority_key_id = "p256:" + hashlib.sha256(envelope.public_key).hexdigest()

    if (
        response.archive_sha256 != record.archive_sha256
        or response.developer_key_id != developer_key_id
        or response.revision != record.revision
        or response.issued_at_millis != record.issued_at_millis
        or response.expires_at_millis != record.expires_at_millis
        or int(response.archive_status) != record.archive_status
        or int(response.key_status) != record.key_status
        or authority_key_id != record.authority_key_id
    ):
        raise ProtocolError("Stored reputation metadata does not match signed bytes")

    return SignedReputationRecord(record.response, record.envelope)
