from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ReadinessProbe(Protocol):
    async def is_ready(self) -> bool: ...


class DatabaseResource(ReadinessProbe, Protocol):
    async def close(self) -> None: ...


def install_health_routes(app: FastAPI, service: str) -> None:
    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok", "service": service}

    @app.get("/health/ready")
    async def ready(request: Request) -> JSONResponse:
        database: ReadinessProbe = request.app.state.database
        is_ready = await database.is_ready()
        status_code = 200 if is_ready else 503
        status = "ready" if is_ready else "unavailable"

        return JSONResponse(
            status_code=status_code,
            content={"status": status, "service": service},
        )
