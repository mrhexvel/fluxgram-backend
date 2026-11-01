import asyncio
import os
from datetime import timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods.base import TelegramMethod
from aiogram.types import Update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fluxgram_backend.application.appearance import AppearanceService
from fluxgram_backend.application.plugin_reputation_decisions import (
    PluginReputationDecisionQueue,
    ReputationDecisionState,
)
from fluxgram_backend.bot.admin import (
    BadgeChoice,
    Confirmation,
    ExpiryChoice,
    PositionChoice,
    ReputationKeyChoice,
    ReputationStatusChoice,
    ReputationValidityChoice,
    create_admin_router,
)
from fluxgram_backend.database.migrations import run_migrations
from fluxgram_backend.domain.peer import FluxGramPeerKey, PeerType
from fluxgram_backend.reputation.protocol import ArchiveStatus, KeyStatus
from fluxgram_backend.settings import Settings


@pytest.mark.integration
def test_inline_bot_flow_uses_transactional_application_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    run_migrations(Settings.model_validate({"database_url": database_url}))

    async def check() -> None:
        async def call_bot(
            bot: Bot,
            method: TelegramMethod[Any],
            request_timeout: int | None = None,
        ) -> Any:
            return True

        monkeypatch.setattr(Bot, "__call__", call_bot)
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        appearance = AppearanceService(sessions)
        reputation = PluginReputationDecisionQueue(sessions)
        dispatcher = Dispatcher()
        dispatcher.include_router(
            create_admin_router(appearance, reputation, frozenset({1001}))
        )
        bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI")

        try:
            await dispatcher.feed_update(bot, message_update(6001, 1001, "/admin"))
            await dispatcher.feed_update(bot, callback_update(6002, 1001, "assign"))
            await dispatcher.feed_update(bot, message_update(6003, 1001, "53001"))
            await dispatcher.feed_update(
                bot,
                callback_update(6004, 1001, BadgeChoice(index=0).pack()),
            )
            await dispatcher.feed_update(
                bot,
                callback_update(6005, 1001, PositionChoice(position=0).pack()),
            )
            await dispatcher.feed_update(
                bot,
                callback_update(6006, 1001, ExpiryChoice(days=0).pack()),
            )
            await dispatcher.feed_update(
                bot,
                callback_update(6007, 1001, Confirmation(action="assign").pack()),
            )

            profile = await appearance.get_profile(FluxGramPeerKey(PeerType.USER, 53001))
            history = await appearance.get_history(FluxGramPeerKey(PeerType.USER, 53001))

            assert profile is not None
            assert profile.revision == 1
            assert [badge.badge_id for badge in profile.badges] == ["developer"]
            assert history[0].operation_id == "telegram:update:6007"

            await dispatcher.feed_update(
                bot,
                callback_update(6010, 1001, "reputation"),
            )
            await dispatcher.feed_update(bot, message_update(6011, 1001, "c" * 64))
            await dispatcher.feed_update(
                bot,
                callback_update(
                    6012,
                    1001,
                    ReputationKeyChoice(has_key=False).pack(),
                ),
            )
            await dispatcher.feed_update(
                bot,
                callback_update(
                    6013,
                    1001,
                    ReputationStatusChoice(target="archive", value=1).pack(),
                ),
            )
            await dispatcher.feed_update(
                bot,
                callback_update(
                    6014,
                    1001,
                    ReputationValidityChoice(days=1).pack(),
                ),
            )
            await dispatcher.feed_update(
                bot,
                callback_update(
                    6015,
                    1001,
                    Confirmation(action="reputation").pack(),
                ),
            )

            decision = await reputation.get("telegram:update:6014:reputation")
            assert decision is not None
            assert decision.state is ReputationDecisionState.PENDING
            assert decision.publication.query.archive_sha256 == "c" * 64
            assert decision.publication.query.developer_key_id is None
            assert decision.publication.archive_status is ArchiveStatus.CLEAR
            assert decision.publication.key_status is KeyStatus.NOT_APPLICABLE

            claim = await reputation.claim_next("bot-test-cleanup", timedelta(seconds=10))
            assert claim is not None
            assert claim.decision.decision_operation_id == decision.decision_operation_id
            await reputation.mark_failed(
                decision.decision_operation_id,
                claim.decision.lease_token or "",
                "test_cleanup",
            )
        finally:
            await bot.session.close()
            await engine.dispose()

    asyncio.run(check())


def message_update(update_id: int, user_id: int, text: str) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 0,
                "chat": {"id": user_id, "type": "private"},
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "Admin",
                },
                "text": text,
            },
        }
    )


def callback_update(update_id: int, user_id: int, data: str) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "chat_instance": "test",
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "Admin",
                },
                "message": {
                    "message_id": 1,
                    "date": 0,
                    "chat": {"id": user_id, "type": "private"},
                    "text": "Panel",
                },
                "data": data,
            },
        }
    )
