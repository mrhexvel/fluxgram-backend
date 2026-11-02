from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from fluxgram_backend.api.context import RequestContextMiddleware
from fluxgram_backend.api.plugin_reputation import router as plugin_reputation_router
from fluxgram_backend.api.rate_limit import InMemoryRateLimiter, RateLimitMiddleware
from fluxgram_backend.application.plugin_reputation import PluginReputationService
from fluxgram_backend.database.connection import Database
from fluxgram_backend.database.plugin_reputation import PostgresReputationRecordReader
from fluxgram_backend.health import install_health_routes
from fluxgram_backend.log_config import configure_logging
from fluxgram_backend.settings import Settings


def create_reputation_api_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    configure_logging(resolved_settings.log_level)
    database = Database(resolved_settings.require_database_url())
    reader = PostgresReputationRecordReader(database.sessions)
    service = PluginReputationService(
        reader,
        resolved_settings.require_reputation_api_authority_key_id(),
        lookup_timeout_seconds=resolved_settings.plugin_reputation_lookup_timeout_seconds,
    )
    limiter = InMemoryRateLimiter(
        resolved_settings.api_rate_limit_requests,
        resolved_settings.api_rate_limit_window_seconds,
        resolved_settings.api_rate_limit_max_clients,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.database = reader
        app.state.plugin_reputation_service = service
        app.state.reputation_api_body_timeout_seconds = (
            resolved_settings.reputation_api_body_timeout_seconds
        )
        try:
            yield
        finally:
            await database.close()

    app = FastAPI(
        title="FluxGram Plugin Reputation API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        RateLimitMiddleware,
        limiter=limiter,
        trusted_proxy_network=resolved_settings.reputation_api_trusted_proxy_network,
    )
    app.add_middleware(RequestContextMiddleware)
    install_health_routes(app, "plugin-reputation-api")
    app.include_router(plugin_reputation_router)
    return app
