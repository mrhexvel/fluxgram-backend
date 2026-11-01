import asyncio
import os
import ssl
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fluxgram_backend.application.plugin_reputation_decisions import PluginReputationDecisionQueue
from fluxgram_backend.application.plugin_reputation_publisher import PluginReputationPublisher
from fluxgram_backend.publisher import composition
from fluxgram_backend.publisher.settings import PluginReputationPublisherSettings


class FakeTlsContext:
    verify_mode = ssl.CERT_REQUIRED
    check_hostname = True


class FakeClient:
    instances: ClassVar[list["FakeClient"]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.follow_redirects = cast(bool, kwargs["follow_redirects"])
        self.closed = False
        self.instances.append(self)

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True


class FakeDatabase:
    instances: ClassVar[list["FakeDatabase"]] = []

    def __init__(self, url: str) -> None:
        self.url = url
        self.sessions = cast(async_sessionmaker[AsyncSession], object())
        self.closed = False
        self.instances.append(self)

    async def close(self) -> None:
        self.closed = True


def _settings(tmp_path: Path) -> PluginReputationPublisherSettings:
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test-ca", encoding="ascii")
    ca_file.chmod(0o600)
    return PluginReputationPublisherSettings.model_validate(
        {
            "plugin_reputation_authority_key_id": "p256:" + "a" * 64,
            "vault_address": "https://vault.example:8200",
            "vault_transit_mount": "transit",
            "vault_transit_key_name": "plugin-reputation",
            "vault_transit_key_version": 2,
            "vault_agent_token_file": str(tmp_path / "token"),
            "vault_ca_file": str(ca_file),
            "vault_signer_timeout_seconds": 7,
        }
    )


def test_publisher_composition_owns_bounded_tls_client_and_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    FakeClient.instances.clear()
    FakeDatabase.instances.clear()
    ca_data_values: list[str] = []

    def create_tls(*, cadata: str) -> FakeTlsContext:
        ca_data_values.append(cadata)
        return FakeTlsContext()

    monkeypatch.setattr(
        "fluxgram_backend.publisher.composition.ssl.create_default_context",
        create_tls,
    )
    monkeypatch.setattr(
        "fluxgram_backend.publisher.composition.httpx.AsyncClient",
        FakeClient,
    )
    monkeypatch.setattr(composition, "Database", FakeDatabase)
    settings = _settings(tmp_path)

    async def run() -> None:
        async with composition.open_plugin_reputation_publisher(
            "postgresql+asyncpg://publisher:secret@db/fluxgram",
            settings,
        ) as publisher:
            assert isinstance(publisher, PluginReputationPublisher)
            assert not FakeClient.instances[0].closed
            assert not FakeDatabase.instances[0].closed

    asyncio.run(run())

    client = FakeClient.instances[0]
    assert client.closed
    assert FakeDatabase.instances[0].closed
    assert ca_data_values == ["test-ca"]
    assert client.kwargs["verify"].verify_mode == ssl.CERT_REQUIRED
    assert client.kwargs["verify"].check_hostname
    assert client.kwargs["follow_redirects"] is False
    assert client.kwargs["trust_env"] is False
    assert client.kwargs["http2"] is False
    assert cast(httpx.Timeout, client.kwargs["timeout"]).connect == 7
    assert cast(httpx.Limits, client.kwargs["limits"]).max_connections == 2
    assert cast(httpx.Limits, client.kwargs["limits"]).max_keepalive_connections == 1


def test_publisher_runtime_exposes_queue_and_publisher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    FakeClient.instances.clear()
    FakeDatabase.instances.clear()
    monkeypatch.setattr(
        "fluxgram_backend.publisher.composition.ssl.create_default_context",
        lambda *, cadata: FakeTlsContext(),
    )
    monkeypatch.setattr(
        "fluxgram_backend.publisher.composition.httpx.AsyncClient",
        FakeClient,
    )
    monkeypatch.setattr(composition, "Database", FakeDatabase)

    async def run() -> None:
        async with composition.open_plugin_reputation_publisher_runtime(
            "postgresql+asyncpg://publisher:secret@db/fluxgram",
            _settings(tmp_path),
        ) as runtime:
            assert isinstance(runtime.queue, PluginReputationDecisionQueue)
            assert isinstance(runtime.publisher, PluginReputationPublisher)

    asyncio.run(run())


def test_publisher_composition_closes_resources_after_consumer_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    FakeClient.instances.clear()
    FakeDatabase.instances.clear()
    monkeypatch.setattr(
        "fluxgram_backend.publisher.composition.ssl.create_default_context",
        lambda *, cadata: FakeTlsContext(),
    )
    monkeypatch.setattr(
        "fluxgram_backend.publisher.composition.httpx.AsyncClient",
        FakeClient,
    )
    monkeypatch.setattr(composition, "Database", FakeDatabase)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="consumer failed"):
            async with composition.open_plugin_reputation_publisher(
                "postgresql+asyncpg://publisher:secret@db/fluxgram",
                _settings(tmp_path),
            ):
                raise RuntimeError("consumer failed")

    asyncio.run(run())

    assert FakeClient.instances[0].closed
    assert FakeDatabase.instances[0].closed


def test_publisher_composition_rejects_non_verifying_tls_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = FakeTlsContext()
    context.check_hostname = False
    monkeypatch.setattr(
        "fluxgram_backend.publisher.composition.ssl.create_default_context",
        lambda *, cadata: context,
    )

    async def run() -> None:
        with pytest.raises(RuntimeError, match="hostnames"):
            async with composition.open_plugin_reputation_publisher(
                "postgresql+asyncpg://publisher:secret@db/fluxgram",
                _settings(tmp_path),
            ):
                pass

    asyncio.run(run())


def test_publisher_composition_rejects_unsafe_ca_file(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.vault_ca_file.chmod(0o666)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="CA file"):
            async with composition.open_plugin_reputation_publisher(
                "postgresql+asyncpg://publisher:secret@db/fluxgram",
                settings,
            ):
                pass

    asyncio.run(run())


def test_publisher_composition_rejects_untrusted_ca_file_types(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    original = settings.vault_ca_file
    symlink = tmp_path / "ca-link.pem"
    hard_link = tmp_path / "ca-hard-link.pem"
    oversized = tmp_path / "ca-oversized.pem"
    symlink.symlink_to(original)
    os.link(original, hard_link)
    oversized.write_bytes(b"x" * (1024 * 1024 + 1))
    oversized.chmod(0o600)

    async def reject(path: Path) -> None:
        settings.vault_ca_file = path
        with pytest.raises(RuntimeError, match="CA file"):
            async with composition.open_plugin_reputation_publisher(
                "postgresql+asyncpg://publisher:secret@db/fluxgram",
                settings,
            ):
                pass

    asyncio.run(reject(symlink))
    asyncio.run(reject(hard_link))
    asyncio.run(reject(oversized))
