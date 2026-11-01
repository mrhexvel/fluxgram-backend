import json
from collections.abc import Iterator
from pathlib import Path

from fastapi.testclient import TestClient

from fluxgram_backend.api.app import create_api_app
from fluxgram_backend.application.plugin_reputation import (
    PluginReputationService,
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
