import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp


@dataclass(slots=True)
class ClientWindow:
    started_at: float
    requests: int


class InMemoryRateLimiter:
    def __init__(
        self,
        request_limit: int,
        window_seconds: int,
        max_clients: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._request_limit = request_limit
        self._window_seconds = window_seconds
        self._max_clients = max_clients
        self._clock = clock or time.monotonic
        self._clients: OrderedDict[str, ClientWindow] = OrderedDict()
        self._lock = Lock()

    def check(self, client: str) -> int | None:
        now = self._clock()

        with self._lock:
            window = self._clients.get(client)
            if window is None or now - window.started_at >= self._window_seconds:
                self._store_new_window(client, now)
                return None

            self._clients.move_to_end(client)
            if window.requests >= self._request_limit:
                remaining = self._window_seconds - (now - window.started_at)
                return max(1, math.ceil(remaining))

            window.requests += 1
            return None

    def _store_new_window(self, client: str, now: float) -> None:
        if client not in self._clients and len(self._clients) >= self._max_clients:
            self._discard_expired(now)

        if client not in self._clients and len(self._clients) >= self._max_clients:
            self._clients.popitem(last=False)

        self._clients[client] = ClientWindow(started_at=now, requests=1)
        self._clients.move_to_end(client)

    def _discard_expired(self, now: float) -> None:
        expired = [
            client
            for client, window in self._clients.items()
            if now - window.started_at >= self._window_seconds
        ]
        for client in expired:
            del self._clients[client]


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, limiter: InMemoryRateLimiter) -> None:
        super().__init__(app)
        self._limiter = limiter

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)

        client = request.client.host if request.client is not None else "unknown"
        retry_after = self._limiter.check(client)
        if retry_after is None:
            return await call_next(request)

        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={
                "code": "rate_limited",
                "message": "Too many requests",
            },
        )
