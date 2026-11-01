import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from ipaddress import IPv4Address, IPv4Network, ip_address

FAILURE_CODES = (
    "decision_expired",
    "decision_not_current",
    "publication_conflict",
    "retry_exhausted",
    "signer_invalid_response",
)
MAX_REQUEST_BYTES = 4096
READ_TIMEOUT_SECONDS = 2
LOOPBACK_NETWORK = IPv4Network("127.0.0.0/8")


class PluginReputationPublisherOperations:
    def __init__(
        self,
        host: str,
        port: int,
        stale_seconds: float,
        allowed_network: IPv4Network = LOOPBACK_NETWORK,
        max_connections: int = 32,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._host = host
        self._port = port
        self._stale_seconds = stale_seconds
        self._allowed_network = allowed_network
        self._max_connections = max_connections
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._running = False
        self._signer_ready = False
        self._active_connections = 0
        self._last_activity: float | None = None
        self._last_activity_unix = 0.0
        self._polls = 0
        self._claims = 0
        self._published = 0
        self._retries = 0
        self._lease_renewals = 0
        self._lease_losses = 0
        self._shutdown_reschedules = 0
        self._failures = {code: 0 for code in FAILURE_CODES}

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[int]:
        server = await asyncio.start_server(
            self._handle_connection,
            self._host,
            self._port,
            limit=MAX_REQUEST_BYTES,
        )
        try:
            sockets = server.sockets
            if not sockets:
                raise RuntimeError("publisher operations server has no listening socket")
            yield int(sockets[0].getsockname()[1])
        finally:
            self._running = False
            server.close()
            await server.wait_closed()

    def worker_started(self) -> None:
        self._running = True

    def worker_stopped(self) -> None:
        self._running = False
        self._signer_ready = False

    def signer_succeeded(self) -> None:
        self._signer_ready = True

    def poll_succeeded(self, claimed: bool) -> None:
        self._polls += 1
        if claimed:
            self._claims += 1
        self._record_activity()

    def lease_renewed(self) -> None:
        self._lease_renewals += 1
        self._record_activity()

    def lease_lost(self) -> None:
        self._lease_losses += 1

    def published(self) -> None:
        self._published += 1
        self._record_activity()

    def failed(self, error_code: str) -> None:
        if error_code not in self._failures:
            raise ValueError("unknown publisher failure code")
        self._failures[error_code] += 1
        self._record_activity()

    def retried(self) -> None:
        self._retries += 1
        self._record_activity()

    def shutdown_rescheduled(self) -> None:
        self._shutdown_reschedules += 1
        self._record_activity()

    def is_live(self) -> bool:
        return self._running

    def is_ready(self) -> bool:
        return (
            self._running
            and self._signer_ready
            and self._last_activity is not None
            and self._monotonic() - self._last_activity <= self._stale_seconds
        )

    def render_metrics(self) -> bytes:
        ready = 1 if self.is_ready() else 0
        running = 1 if self._running else 0
        signer_ready = 1 if self._signer_ready else 0
        lines = [
            "# TYPE fluxgram_plugin_reputation_worker_up gauge",
            f"fluxgram_plugin_reputation_worker_up {running}",
            "# TYPE fluxgram_plugin_reputation_worker_ready gauge",
            f"fluxgram_plugin_reputation_worker_ready {ready}",
            "# TYPE fluxgram_plugin_reputation_worker_signer_ready gauge",
            f"fluxgram_plugin_reputation_worker_signer_ready {signer_ready}",
            "# TYPE fluxgram_plugin_reputation_worker_last_activity_seconds gauge",
            "fluxgram_plugin_reputation_worker_last_activity_seconds "
            f"{self._last_activity_unix:.3f}",
            "# TYPE fluxgram_plugin_reputation_worker_polls_total counter",
            f"fluxgram_plugin_reputation_worker_polls_total {self._polls}",
            "# TYPE fluxgram_plugin_reputation_worker_claims_total counter",
            f"fluxgram_plugin_reputation_worker_claims_total {self._claims}",
            "# TYPE fluxgram_plugin_reputation_worker_published_total counter",
            f"fluxgram_plugin_reputation_worker_published_total {self._published}",
            "# TYPE fluxgram_plugin_reputation_worker_retries_total counter",
            f"fluxgram_plugin_reputation_worker_retries_total {self._retries}",
            "# TYPE fluxgram_plugin_reputation_worker_lease_renewals_total counter",
            f"fluxgram_plugin_reputation_worker_lease_renewals_total {self._lease_renewals}",
            "# TYPE fluxgram_plugin_reputation_worker_lease_losses_total counter",
            f"fluxgram_plugin_reputation_worker_lease_losses_total {self._lease_losses}",
            "# TYPE fluxgram_plugin_reputation_worker_shutdown_reschedules_total counter",
            "fluxgram_plugin_reputation_worker_shutdown_reschedules_total "
            f"{self._shutdown_reschedules}",
            "# TYPE fluxgram_plugin_reputation_worker_failures_total counter",
        ]
        lines.extend(
            f'fluxgram_plugin_reputation_worker_failures_total{{reason="{code}"}} '
            f"{self._failures[code]}"
            for code in FAILURE_CODES
        )
        return ("\n".join(lines) + "\n").encode("ascii")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if not self._peer_allowed(writer) or self._active_connections >= self._max_connections:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            return

        self._active_connections += 1
        try:
            try:
                encoded = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"),
                    timeout=READ_TIMEOUT_SECONDS,
                )
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                await self._respond(writer, 400, "text/plain", b"Bad Request\n")
                return

            if len(encoded) > MAX_REQUEST_BYTES:
                await self._respond(writer, 400, "text/plain", b"Bad Request\n")
                return

            try:
                request_line = encoded.split(b"\r\n", 1)[0].decode("ascii")
                method, target, version = request_line.split(" ")
            except (UnicodeDecodeError, ValueError):
                await self._respond(writer, 400, "text/plain", b"Bad Request\n")
                return

            if version not in ("HTTP/1.0", "HTTP/1.1"):
                await self._respond(writer, 400, "text/plain", b"Bad Request\n")
            elif method != "GET":
                await self._respond(writer, 405, "text/plain", b"Method Not Allowed\n")
            elif target == "/health/live":
                await self._json_status(writer, self.is_live(), "ok", "stopped")
            elif target == "/health/ready":
                await self._json_status(writer, self.is_ready(), "ready", "unavailable")
            elif target == "/metrics":
                await self._respond(
                    writer,
                    200,
                    "text/plain; version=0.0.4; charset=utf-8",
                    self.render_metrics(),
                )
            else:
                await self._respond(writer, 404, "text/plain", b"Not Found\n")
        except (ConnectionError, OSError):
            pass
        finally:
            self._active_connections -= 1
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def _json_status(
        self,
        writer: asyncio.StreamWriter,
        healthy: bool,
        healthy_status: str,
        unhealthy_status: str,
    ) -> None:
        body = json.dumps(
            {
                "status": healthy_status if healthy else unhealthy_status,
                "service": "plugin-reputation-publisher",
            },
            separators=(",", ":"),
        ).encode("ascii")
        await self._respond(writer, 200 if healthy else 503, "application/json", body)

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        content_type: str,
        body: bytes,
    ) -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            503: "Service Unavailable",
        }[status]
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(headers + body)
        await writer.drain()

    def _record_activity(self) -> None:
        self._last_activity = self._monotonic()
        self._last_activity_unix = self._wall_clock()

    def _peer_allowed(self, writer: asyncio.StreamWriter) -> bool:
        peer = writer.get_extra_info("peername")
        if not isinstance(peer, tuple) or not peer or not isinstance(peer[0], str):
            return False
        try:
            address = ip_address(peer[0])
        except ValueError:
            return False
        return isinstance(address, IPv4Address) and (
            address.is_loopback or address in self._allowed_network
        )
