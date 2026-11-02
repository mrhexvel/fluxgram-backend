import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fluxgram_backend.application.plugin_reputation_decisions import (
    PluginReputationDecisionQueue,
    ReputationDecisionState,
)
from fluxgram_backend.application.plugin_reputation_publisher import ReputationPublication
from fluxgram_backend.database.models import PluginReputationRecord
from fluxgram_backend.domain.operations import ActorType, OperationContext
from fluxgram_backend.reputation.protocol import (
    MAX_FRAME_BYTES,
    ArchiveStatus,
    KeyStatus,
    ReputationQuery,
    ReputationResponse,
    TransportFrame,
)
from fluxgram_backend.reputation.verification import verify_signed_record

DATABASE_URL = "postgresql+asyncpg://fluxgram:fluxgram@postgres:5432/fluxgram"
AUTHORITY_KEY_ID_FILE = Path("/vault/config/authority-key-id")


async def verify_deployment() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    queue = PluginReputationDecisionQueue(sessions)
    identity = uuid4().hex
    operation_id = f"local-reputation-smoke:{identity}"
    archive_sha256 = hashlib.sha256(operation_id.encode("ascii")).hexdigest()
    query = ReputationQuery(archive_sha256)

    try:
        now_millis = await queue.current_time_millis()
        publication = ReputationPublication(
            query,
            ArchiveStatus.CLEAR,
            KeyStatus.NOT_APPLICABLE,
            now_millis - 1_000,
            now_millis + int(timedelta(days=1).total_seconds() * 1000),
        )
        await queue.enqueue(
            OperationContext(operation_id, ActorType.TELEGRAM_ADMIN, 1),
            publication,
        )

        deadline = asyncio.get_running_loop().time() + 30
        decision = None
        while asyncio.get_running_loop().time() < deadline:
            decision = await queue.get(operation_id)
            if decision is not None and decision.state in (
                ReputationDecisionState.PUBLISHED,
                ReputationDecisionState.FAILED,
            ):
                break
            await asyncio.sleep(0.2)

        if decision is None or decision.state is not ReputationDecisionState.PUBLISHED:
            raise RuntimeError("local reputation decision was not published")

        async with sessions() as session:
            record = await session.scalar(
                select(PluginReputationRecord).where(
                    PluginReputationRecord.archive_sha256 == archive_sha256,
                    PluginReputationRecord.developer_key_id == "",
                    PluginReputationRecord.revision == decision.published_revision,
                )
            )
        if record is None:
            raise RuntimeError("published local reputation record is missing")

        authority_key_id = AUTHORITY_KEY_ID_FILE.read_text(encoding="ascii")
        verify_signed_record(
            query,
            record.response,
            record.envelope,
            authority_key_id,
            await queue.current_time_millis(),
        )
        response = ReputationResponse.decode(record.response)
        if (
            response.archive_status is not ArchiveStatus.CLEAR
            or response.key_status is not KeyStatus.NOT_APPLICABLE
            or response.issued_at_millis != publication.issued_at_millis
            or response.expires_at_millis != publication.expires_at_millis
        ):
            raise RuntimeError("published local reputation record changed the decision")

        frame = await asyncio.to_thread(query_public_api, query)
        if frame.response != record.response or frame.envelope != record.envelope:
            raise RuntimeError("public API changed the signed reputation record")

        unknown_query = ReputationQuery(hashlib.sha256(identity.encode("ascii")).hexdigest())
        await asyncio.to_thread(require_unknown_identity, unknown_query)
    finally:
        await engine.dispose()

    print("Local Vault publisher and public API verification passed")


def query_public_api(query: ReputationQuery) -> TransportFrame:
    request = Request(
        "http://public-api:8080/v1/plugin-reputation/query",
        data=query.encode(),
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("public reputation API returned an unexpected status")
        encoded = response.read(MAX_FRAME_BYTES + 1)
        if len(encoded) > MAX_FRAME_BYTES:
            raise RuntimeError("public reputation API response exceeds the transport limit")
        return TransportFrame.decode(encoded)


def require_unknown_identity(query: ReputationQuery) -> None:
    request = Request(
        "http://public-api:8080/v1/plugin-reputation/query",
        data=query.encode(),
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        urlopen(request, timeout=5)
    except HTTPError as error:
        if error.code == 404 and error.read() == b"":
            return
        raise RuntimeError("public reputation API returned an unexpected error") from error
    raise RuntimeError("public reputation API synthesized an unknown reputation response")


if __name__ == "__main__":
    asyncio.run(verify_deployment())
