from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from fluxgram_backend.api.context import RequestContextMiddleware
from fluxgram_backend.api.errors import install_error_handlers
from fluxgram_backend.api.profiles import ProfileReader
from fluxgram_backend.api.profiles import router as profiles_router
from fluxgram_backend.api.rate_limit import InMemoryRateLimiter, RateLimitMiddleware
from fluxgram_backend.application.profile_queries import ProfileQueryService
from fluxgram_backend.database.connection import Database
from fluxgram_backend.health import DatabaseResource, install_health_routes
from fluxgram_backend.log_config import configure_logging
from fluxgram_backend.settings import Settings


def create_api_app(
    settings: Settings | None = None,
    database: DatabaseResource | None = None,
    profile_reader: ProfileReader | None = None,
    rate_limiter: InMemoryRateLimiter | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    configure_logging(resolved_settings.log_level)
    resolved_database = database or Database(resolved_settings.require_database_url())
    resolved_reader = profile_reader
    if resolved_reader is None and isinstance(resolved_database, Database):
        resolved_reader = ProfileQueryService(
            resolved_database.sessions,
            resolved_settings.api_cache_ttl_seconds,
            resolved_settings.api_negative_cache_ttl_seconds,
        )
    resolved_rate_limiter = rate_limiter or InMemoryRateLimiter(
        resolved_settings.api_rate_limit_requests,
        resolved_settings.api_rate_limit_window_seconds,
        resolved_settings.api_rate_limit_max_clients,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.database = resolved_database
        app.state.profile_reader = resolved_reader

        try:
            yield
        finally:
            await resolved_database.close()

    app = FastAPI(title="FluxGram Appearance API", lifespan=lifespan)
    app.add_middleware(RateLimitMiddleware, limiter=resolved_rate_limiter)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    install_health_routes(app, "api")
    app.include_router(profiles_router)
    return app
