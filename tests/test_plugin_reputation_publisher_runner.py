import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from fluxgram_backend.publisher import runner


class FakePublisher:
    def __init__(self) -> None:
        self.checks = 0

    async def check_signer(self) -> None:
        self.checks += 1
        if self.checks > 1:
            raise RuntimeError("signer probe failed")


class FakeOperations:
    instance: "FakeOperations | None" = None

    def __init__(self, *_args: object) -> None:
        self.started = False
        self.stopped = False
        FakeOperations.instance = self

    @asynccontextmanager
    async def serve(self) -> Any:
        yield 8090

    def signer_succeeded(self) -> None:
        pass

    def worker_started(self) -> None:
        self.started = True

    def worker_stopped(self) -> None:
        self.stopped = True


class FakeWorker:
    instance: "FakeWorker | None" = None

    def __init__(self, *_args: object) -> None:
        self.finished = False
        FakeWorker.instance = self

    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()
        self.finished = True


@pytest.mark.anyio
async def test_runner_cleans_worker_after_signer_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = FakePublisher()

    @asynccontextmanager
    async def open_runtime(*_args: object) -> Any:
        yield SimpleNamespace(queue=object(), publisher=publisher)

    monkeypatch.setattr(runner, "PluginReputationPublisherOperations", FakeOperations)
    monkeypatch.setattr(runner, "PluginReputationPublisherWorker", FakeWorker)
    monkeypatch.setattr(runner, "open_plugin_reputation_publisher_runtime", open_runtime)
    publisher_settings = SimpleNamespace(vault_signer_timeout_seconds=0.1)
    worker_settings = SimpleNamespace(
        plugin_reputation_operations_host="127.0.0.1",
        plugin_reputation_operations_port=8090,
        plugin_reputation_readiness_stale_seconds=1.0,
        plugin_reputation_operations_allowed_network=object(),
        plugin_reputation_operations_max_connections=1,
        plugin_reputation_worker_id="worker",
        plugin_reputation_worker_lease_seconds=2.0,
        plugin_reputation_worker_renewal_seconds=0.5,
        plugin_reputation_worker_poll_seconds=0.1,
        plugin_reputation_worker_retry_initial_seconds=0.1,
        plugin_reputation_worker_retry_max_seconds=1.0,
        plugin_reputation_worker_max_attempts=2,
        plugin_reputation_signer_probe_seconds=0.01,
        plugin_reputation_worker_shutdown_grace_seconds=1.0,
    )

    with pytest.raises(RuntimeError, match="signer probe failed"):
        await runner.run_plugin_reputation_publisher(
            "postgresql+asyncpg://local",
            publisher_settings,
            worker_settings,
        )

    assert FakeWorker.instance is not None
    assert FakeWorker.instance.finished
    assert FakeOperations.instance is not None
    assert FakeOperations.instance.started
    assert FakeOperations.instance.stopped
