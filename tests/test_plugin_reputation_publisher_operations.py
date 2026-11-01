import asyncio
import sys
from collections.abc import Callable
from contextlib import suppress

import pytest

from fluxgram_backend.publisher.operations import PluginReputationPublisherOperations


class Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


async def request(port: int, target: str, method: str = "GET") -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"{method} {target} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode(
            "ascii"
        )
    )
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    status = int(response.split(b" ", 2)[1])
    return status, response.split(b"\r\n\r\n", 1)[1]


def operations(
    monotonic: Callable[[], float],
    wall_clock: Callable[[], float] = lambda: 1_800_000_000.0,
) -> PluginReputationPublisherOperations:
    return PluginReputationPublisherOperations(
        "127.0.0.1",
        0,
        10,
        monotonic=monotonic,
        wall_clock=wall_clock,
    )


@pytest.mark.anyio
async def test_operations_health_requires_recent_worker_activity() -> None:
    clock = Clock(100)
    state = operations(clock)

    async with state.serve() as port:
        assert (await request(port, "/health/live"))[0] == 503
        assert (await request(port, "/health/ready"))[0] == 503

        state.worker_started()
        assert (await request(port, "/health/live"))[0] == 200
        assert (await request(port, "/health/ready"))[0] == 503

        state.poll_succeeded(False)
        assert (await request(port, "/health/ready"))[0] == 503

        state.signer_succeeded()
        assert (await request(port, "/health/ready"))[0] == 200

        clock.value += 11
        assert (await request(port, "/health/ready"))[0] == 503


@pytest.mark.anyio
async def test_operations_metrics_are_bounded_and_do_not_include_decision_identity() -> None:
    state = operations(Clock(100))
    state.worker_started()
    state.signer_succeeded()
    state.poll_succeeded(True)
    state.lease_renewed()
    state.published()
    state.retried()
    state.lease_lost()
    state.failed("decision_expired")
    state.shutdown_rescheduled()

    async with state.serve() as port:
        status, body = await request(port, "/metrics")

    assert status == 200
    assert b"fluxgram_plugin_reputation_worker_ready 1" in body
    assert b"fluxgram_plugin_reputation_worker_signer_ready 1" in body
    assert b"fluxgram_plugin_reputation_worker_claims_total 1" in body
    assert b'failures_total{reason="decision_expired"} 1' in body
    assert b"archive" not in body
    assert b"decision_id" not in body


@pytest.mark.anyio
async def test_operations_server_rejects_unknown_method_path_and_oversized_request() -> None:
    state = operations(Clock(100))
    state.worker_started()
    state.signer_succeeded()
    state.poll_succeeded(False)

    async with state.serve() as port:
        assert (await request(port, "/missing"))[0] == 404
        assert (await request(port, "/health/live", "POST"))[0] == 405

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /health/live HTTP/1.1\r\nX-Fill: " + b"a" * 5000 + b"\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()

    assert int(response.split(b" ", 2)[1]) == 400


def test_operations_reject_unknown_failure_label() -> None:
    with pytest.raises(ValueError, match="unknown publisher failure"):
        operations(Clock(100)).failed("dynamic-label")


@pytest.mark.anyio
async def test_operations_server_limits_slow_connections() -> None:
    state = PluginReputationPublisherOperations(
        "127.0.0.1",
        0,
        10,
        max_connections=1,
    )
    state.worker_started()
    state.signer_succeeded()
    state.poll_succeeded(False)

    async with state.serve() as port:
        first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0)
        second_reader, second_writer = await asyncio.open_connection("127.0.0.1", port)
        second_writer.write(b"GET /health/live HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await second_writer.drain()

        response = b""
        with suppress(ConnectionResetError):
            response = await second_reader.read()
        assert response == b""

        second_writer.close()
        with suppress(ConnectionResetError):
            await second_writer.wait_closed()
        first_writer.close()
        await first_writer.wait_closed()
        await first_reader.read()


@pytest.mark.anyio
async def test_healthcheck_command_uses_ready_endpoint() -> None:
    state = operations(Clock(100))
    state.worker_started()
    state.signer_succeeded()
    state.poll_succeeded(False)

    async with state.serve() as port:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "fluxgram_backend.publisher.healthcheck",
            "--port",
            str(port),
        )
        assert await process.wait() == 0
