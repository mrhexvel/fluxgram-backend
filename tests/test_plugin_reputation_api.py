import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from fluxgram_backend.api.app import create_api_app
from fluxgram_backend.api.plugin_reputation import _read_body
from fluxgram_backend.api.reputation_app import create_reputation_api_app
from fluxgram_backend.application.plugin_reputation import (
    PluginReputationService,
    ReputationReadError,
    SignedReputationRecord,
)
from fluxgram_backend.reputation.protocol import ReputationQuery
from fluxgram_backend.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


class FakeDatabase:
    async def is_ready(self) -> bool:
        return True

    async def close(self) -> None:
        pass


class FakeRecordReader:
    def __init__(self, record: SignedReputationRecord | None) -> None:
        self.record = record
        self.queries: list[ReputationQuery] = []

    async def get_record(self, query: ReputationQuery) -> SignedReputationRecord | None:
        self.queries.append(query)
        return self.record


class FailingRecordReader:
    async def get_record(self, query: ReputationQuery) -> SignedReputationRecord | None:
        raise ReputationReadError("database unavailable")


class SlowRecordReader:
    async def get_record(self, query: ReputationQuery) -> SignedReputationRecord | None:
        await asyncio.sleep(1)
        return None


def load_golden() -> dict[str, str]:
    path = ROOT / "fixtures/plugin-reputation/v1/golden.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def create_client(
    reader: FakeRecordReader,
    golden: dict[str, str],
) -> TestClient:
    service = PluginReputationService(
        reader,
        golden["authority_key_id"],
        clock_millis=lambda: 1_700_043_200_000,
    )
    app = create_api_app(
        Settings(),
        FakeDatabase(),
        plugin_reputation_service=service,
    )
    return TestClient(app)


def test_route_is_absent_without_explicit_service() -> None:
    with TestClient(create_api_app(Settings(), FakeDatabase())) as client:
        response = client.post(
            "/v1/plugin-reputation/query",
            content=b"",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert response.status_code == 404


def test_route_is_wired_from_production_settings() -> None:
    golden = load_golden()
    settings = Settings.model_validate(
        {
            "database_url": "postgresql+asyncpg://user:secret@database/fluxgram",
            "reputation_api_authority_key_id": golden["authority_key_id"],
        }
    )
    app = create_reputation_api_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/v1/plugin-reputation/query",
            content=b"",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert response.status_code == 400


def test_reputation_app_does_not_publish_profile_routes() -> None:
    golden = load_golden()
    settings = Settings.model_validate(
        {
            "database_url": "postgresql+asyncpg://user:secret@database/fluxgram",
            "reputation_api_authority_key_id": golden["authority_key_id"],
        }
    )

    with TestClient(create_reputation_api_app(settings)) as client:
        response = client.get("/v1/profiles/user/1")
        docs = client.get("/docs")
        schema = client.get("/openapi.json")

    assert response.status_code == 404
    assert docs.status_code == 404
    assert schema.status_code == 404


def test_request_body_read_has_wall_clock_timeout() -> None:
    async def receive() -> dict[str, object]:
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/plugin-reputation/query",
            "headers": [],
        },
        receive,
    )

    with pytest.raises(TimeoutError):
        asyncio.run(_read_body(request, 0.01))


def test_endpoint_returns_exact_verified_frame() -> None:
    golden = load_golden()
    reader = FakeRecordReader(
        SignedReputationRecord(
            bytes.fromhex(golden["response"]),
            bytes.fromhex(golden["envelope"]),
        )
    )

    with create_client(reader, golden) as client:
        response = client.post(
            "/v1/plugin-reputation/query",
            content=bytes.fromhex(golden["query"]),
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Encoding": "identity",
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content == bytes.fromhex(golden["frame"])
    assert reader.queries == [
        ReputationQuery(golden["archive_sha256"], golden["developer_key_id"])
    ]


def test_endpoint_returns_not_found_without_synthesizing_clear() -> None:
    golden = load_golden()
    reader = FakeRecordReader(None)

    with create_client(reader, golden) as client:
        response = client.post(
            "/v1/plugin-reputation/query",
            content=bytes.fromhex(golden["query"]),
            headers={"Content-Type": "application/octet-stream"},
        )

    assert response.status_code == 404
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"


def test_endpoint_rejects_invalid_transport_inputs() -> None:
    golden = load_golden()
    reader = FakeRecordReader(None)
    valid_query = bytes.fromhex(golden["query"])

    with create_client(reader, golden) as client:
        wrong_type = client.post(
            "/v1/plugin-reputation/query",
            content=valid_query,
            headers={"Content-Type": "application/json"},
        )
        compressed = client.post(
            "/v1/plugin-reputation/query",
            content=valid_query,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Encoding": "gzip",
            },
        )
        malformed = client.post(
            "/v1/plugin-reputation/query",
            content=valid_query[:-1],
            headers={"Content-Type": "application/octet-stream"},
        )
        oversized = client.post(
            "/v1/plugin-reputation/query",
            content=b"x" * 77,
            headers={"Content-Type": "application/octet-stream"},
        )

    assert wrong_type.status_code == 415
    assert compressed.status_code == 415
    assert malformed.status_code == 400
    assert oversized.status_code == 413
    assert reader.queries == []


def test_endpoint_bounds_chunked_body_before_copy() -> None:
    golden = load_golden()
    reader = FakeRecordReader(None)

    def chunks() -> Iterator[bytes]:
        yield b"x" * 76
        yield b"x"

    with create_client(reader, golden) as client:
        response = client.post(
            "/v1/plugin-reputation/query",
            content=chunks(),
            headers={"Content-Type": "application/octet-stream"},
        )

    assert response.status_code == 413
    assert reader.queries == []


def test_endpoint_fails_closed_for_invalid_stored_record() -> None:
    golden = load_golden()
    envelope = bytearray.fromhex(golden["envelope"])
    envelope[-1] ^= 1
    reader = FakeRecordReader(
        SignedReputationRecord(bytes.fromhex(golden["response"]), bytes(envelope))
    )

    with create_client(reader, golden) as client:
        response = client.post(
            "/v1/plugin-reputation/query",
            content=bytes.fromhex(golden["query"]),
            headers={"Content-Type": "application/octet-stream"},
        )

    assert response.status_code == 503
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"


def test_endpoint_fails_closed_when_storage_is_unavailable() -> None:
    golden = load_golden()
    service = PluginReputationService(
        FailingRecordReader(),
        golden["authority_key_id"],
    )
    app = create_api_app(
        Settings(),
        FakeDatabase(),
        plugin_reputation_service=service,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/plugin-reputation/query",
            content=bytes.fromhex(golden["query"]),
            headers={"Content-Type": "application/octet-stream"},
        )

    assert response.status_code == 503
    assert response.content == b""


def test_endpoint_bounds_storage_lookup_time() -> None:
    golden = load_golden()
    service = PluginReputationService(
        SlowRecordReader(),
        golden["authority_key_id"],
        lookup_timeout_seconds=0.01,
    )
    app = create_api_app(
        Settings(),
        FakeDatabase(),
        plugin_reputation_service=service,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/plugin-reputation/query",
            content=bytes.fromhex(golden["query"]),
            headers={"Content-Type": "application/octet-stream"},
        )

    assert response.status_code == 503
    assert response.content == b""
