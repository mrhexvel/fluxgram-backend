from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from fluxgram_backend.api.app import create_api_app
from fluxgram_backend.api.context import request_id_context
from fluxgram_backend.api.rate_limit import InMemoryRateLimiter
from fluxgram_backend.domain.appearance import ProfileDesign
from fluxgram_backend.domain.peer import FluxGramPeerKey
from fluxgram_backend.domain.profile_read import (
    ProfileReadResult,
    RendererBadge,
    RendererProfile,
)
from fluxgram_backend.settings import Settings


class FakeDatabase:
    async def is_ready(self) -> bool:
        return True

    async def close(self) -> None:
        pass


class FakeProfileReader:
    def __init__(self) -> None:
        self.seen_request_ids: list[str | None] = []
        self.expires_at = datetime.now(UTC) + timedelta(minutes=5)

    async def get_profile(self, peer: FluxGramPeerKey) -> ProfileReadResult:
        self.seen_request_ids.append(request_id_context.get())
        return self._result(peer)

    async def get_profiles(
        self,
        peers: Sequence[FluxGramPeerKey],
    ) -> tuple[ProfileReadResult, ...]:
        self.seen_request_ids.append(request_id_context.get())
        return tuple(self._result(peer) for peer in peers)

    def _result(self, peer: FluxGramPeerKey) -> ProfileReadResult:
        if peer.peer_id == 2:
            return ProfileReadResult(peer, 0, self.expires_at, None)

        badge = RendererBadge(
            badge_id="developer",
            custom_emoji_document_id=5373141891321699086,
            title="FluxGram Developer",
            description="Develops FluxGram.",
            particles=True,
            expires_at=None,
        )
        return ProfileReadResult(
            peer=peer,
            revision=7,
            expires_at=self.expires_at,
            profile=RendererProfile((badge,), ProfileDesign(palette_id="midnight")),
        )


def create_client(
    reader: FakeProfileReader,
    limiter: InMemoryRateLimiter | None = None,
) -> TestClient:
    app = create_api_app(Settings(), FakeDatabase(), reader, limiter)
    return TestClient(app)


def test_single_profile_returns_renderer_dto_cache_headers_and_304() -> None:
    reader = FakeProfileReader()

    with create_client(reader) as client:
        response = client.get(
            "/v1/profiles/user/1",
            headers={"X-Request-ID": "stage7-request"},
        )

        assert response.status_code == 200
        assert response.headers["x-request-id"] == "stage7-request"
        assert response.headers["cache-control"].startswith("public, max-age=")
        assert response.headers["etag"].startswith('"')
        assert response.json() == {
            "schema_version": 1,
            "minimum_renderer_version": 1,
            "result": {
                "peer": {"peer_type": "user", "peer_id": "1"},
                "revision": "7",
                "expires_at": response.json()["result"]["expires_at"],
                "profile": {
                    "badges": [
                        {
                            "badge_id": "developer",
                            "custom_emoji_document_id": "5373141891321699086",
                            "title": "FluxGram Developer",
                            "description": "Develops FluxGram.",
                            "particles": True,
                            "expires_at": None,
                        }
                    ],
                    "design": {
                        "palette_id": "midnight",
                    },
                },
            },
        }
        assert reader.seen_request_ids == ["stage7-request"]

        not_modified = client.get(
            "/v1/profiles/user/1",
            headers={"If-None-Match": response.headers["etag"]},
        )

        assert not_modified.status_code == 304
        assert not not_modified.content
        assert not_modified.headers["etag"] == response.headers["etag"]
        assert "cache-control" in not_modified.headers
        assert "x-request-id" in not_modified.headers


def test_batch_preserves_order_and_returns_negative_result() -> None:
    reader = FakeProfileReader()

    with create_client(reader) as client:
        response = client.post(
            "/v1/profiles:batch",
            json={
                "peers": [
                    {"peer_type": "channel", "peer_id": "2"},
                    {"peer_type": "user", "peer_id": "1"},
                ]
            },
        )

    assert response.status_code == 200
    results = response.json()["results"]
    assert [result["peer"] for result in results] == [
        {"peer_type": "channel", "peer_id": "2"},
        {"peer_type": "user", "peer_id": "1"},
    ]
    assert results[0]["revision"] == "0"
    assert results[0]["profile"] is None
    assert "cache-control" in response.headers
    assert "x-request-id" in response.headers


def test_api_rejects_invalid_requests_and_has_no_write_endpoint() -> None:
    reader = FakeProfileReader()

    with create_client(reader) as client:
        invalid_peer = client.get("/v1/profiles/user/4503599627370496")
        duplicate_batch = client.post(
            "/v1/profiles:batch",
            json={
                "peers": [
                    {"peer_type": "user", "peer_id": "1"},
                    {"peer_type": "user", "peer_id": "1"},
                ]
            },
        )
        oversized_batch = client.post(
            "/v1/profiles:batch",
            json={
                "peers": [
                    {"peer_type": "user", "peer_id": str(peer_id)}
                    for peer_id in range(1, 102)
                ]
            },
        )
        write_attempt = client.put("/v1/profiles/user/1", json={})

    assert invalid_peer.status_code == 400
    assert invalid_peer.json()["code"] == "invalid_request"
    assert duplicate_batch.status_code == 400
    assert duplicate_batch.json()["code"] == "invalid_request"
    assert oversized_batch.status_code == 400
    assert oversized_batch.json()["code"] == "invalid_request"
    assert write_attempt.status_code == 405


def test_rate_limiter_is_per_client_and_returns_retry_after() -> None:
    reader = FakeProfileReader()
    limiter = InMemoryRateLimiter(request_limit=1, window_seconds=30, max_clients=10)

    with create_client(reader, limiter) as client:
        assert client.get("/v1/profiles/user/1").status_code == 200
        response = client.get("/v1/profiles/user/1")
        health = client.get("/health/live")

    assert response.status_code == 429
    assert response.json() == {
        "code": "rate_limited",
        "message": "Too many requests",
    }
    assert int(response.headers["retry-after"]) >= 1
    assert "x-request-id" in response.headers
    assert health.status_code == 200


def test_rate_limiter_resets_windows_and_bounds_client_state() -> None:
    now = 0.0
    limiter = InMemoryRateLimiter(
        request_limit=1,
        window_seconds=10,
        max_clients=2,
        clock=lambda: now,
    )

    assert limiter.check("a") is None
    assert limiter.check("b") is None
    assert limiter.check("c") is None
    assert limiter.check("a") is None
    assert limiter.check("a") == 10

    now = 11.0
    assert limiter.check("a") is None
