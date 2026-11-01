import hmac
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from json import JSONDecodeError
from typing import Annotated

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import ValidationError

from fluxgram_backend.application.appearance import AppearanceService
from fluxgram_backend.application.plugin_reputation_decisions import PluginReputationDecisionQueue
from fluxgram_backend.bot.admin import create_admin_router
from fluxgram_backend.bot.admin.service import (
    AdminAppearanceService,
    AdminReputationDecisionService,
)
from fluxgram_backend.database.connection import Database
from fluxgram_backend.health import DatabaseResource, install_health_routes
from fluxgram_backend.log_config import configure_logging
from fluxgram_backend.settings import Settings


def create_bot_app(
    settings: Settings | None = None,
    database: DatabaseResource | None = None,
    bot: Bot | None = None,
    dispatcher: Dispatcher | None = None,
    appearance_service: AdminAppearanceService | None = None,
    reputation_service: AdminReputationDecisionService | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    configure_logging(resolved_settings.log_level)
    resolved_database = database or Database(resolved_settings.require_database_url())
    resolved_bot = bot or Bot(resolved_settings.require_bot_token())
    resolved_dispatcher = dispatcher or Dispatcher()
    webhook_secret = resolved_settings.require_webhook_secret()
    owns_bot = bot is None

    if dispatcher is None:
        if appearance_service is None or reputation_service is None:
            if not isinstance(resolved_database, Database):
                raise RuntimeError("Admin services are required for a custom database")

            if appearance_service is None:
                appearance_service = AppearanceService(resolved_database.sessions)
            if reputation_service is None:
                reputation_service = PluginReputationDecisionQueue(resolved_database.sessions)

        assert appearance_service is not None
        assert reputation_service is not None
        resolved_dispatcher.include_router(
            create_admin_router(
                appearance_service,
                reputation_service,
                resolved_settings.telegram_admin_ids,
            )
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.database = resolved_database
        await resolved_dispatcher.emit_startup(bot=resolved_bot)

        try:
            yield
        finally:
            try:
                await resolved_dispatcher.emit_shutdown(bot=resolved_bot)
            finally:
                try:
                    await resolved_database.close()
                finally:
                    if owns_bot:
                        await resolved_bot.session.close()

    app = FastAPI(title="FluxGram Telegram Bot", lifespan=lifespan)
    install_health_routes(app, "bot")

    @app.post("/webhooks/telegram")
    async def telegram_webhook(
        request: Request,
        secret_token: Annotated[
            str | None,
            Header(alias="X-Telegram-Bot-Api-Secret-Token"),
        ] = None,
    ) -> dict[str, bool]:
        if secret_token is None or not hmac.compare_digest(secret_token, webhook_secret):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        try:
            body = await request.json()
            update = Update.model_validate(body, context={"bot": resolved_bot})
        except (JSONDecodeError, ValidationError, TypeError, ValueError) as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from error

        await resolved_dispatcher.feed_update(resolved_bot, update)
        return {"ok": True}

    return app
