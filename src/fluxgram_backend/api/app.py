from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from fluxgram_backend.database.connection import Database
from fluxgram_backend.health import DatabaseResource, install_health_routes
from fluxgram_backend.log_config import configure_logging
from fluxgram_backend.settings import Settings


def create_api_app(
    settings: Settings | None = None,
    database: DatabaseResource | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    configure_logging(resolved_settings.log_level)
    resolved_database = database or Database(resolved_settings.require_database_url())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.database = resolved_database

        try:
            yield
        finally:
            await resolved_database.close()

    app = FastAPI(title="FluxGram Appearance API", lifespan=lifespan)
    install_health_routes(app, "api")
    return app
