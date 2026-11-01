import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fluxgram_backend.application.plugin_reputation import SignedReputationRecord
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

        if record is None:
            return None

        return signed_record_from_row(record)


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
