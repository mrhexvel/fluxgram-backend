from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage, SetMyCommands
from aiogram.methods.base import TelegramMethod
from aiogram.types import Update

from fluxgram_backend.bot.admin import (
    BadgeChoice,
    Confirmation,
    ExpiryChoice,
    PositionChoice,
    create_admin_router,
)
from fluxgram_backend.bot.admin.custom_emoji_handlers import (
    CUSTOM_EMOJI_MISSING,
    CUSTOM_EMOJI_PROMPT,
)
from fluxgram_backend.bot.admin.formatting import (
    format_history,
    format_profile,
    parse_user_peer,
)
from fluxgram_backend.bot.admin.keyboards import (
    menu_keyboard,
)
from fluxgram_backend.domain.appearance import BadgeAssignment, BadgeDefinition, ProfileDesign
from fluxgram_backend.domain.operations import (
    AuditEntry,
    OperationContext,
    OperationResult,
    ProfileSummary,
)
from fluxgram_backend.domain.peer import FluxGramPeerKey, PeerType


class FakeAppearanceService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def list_badge_definitions(self) -> tuple[BadgeDefinition, ...]:
        return (BadgeDefinition("founder", 123, "Founder"),)

    async def get_profile(self, peer: FluxGramPeerKey) -> ProfileSummary | None:
        self.calls.append(("profile", peer))
        return None

    async def get_history(
        self,
        peer: FluxGramPeerKey,
        limit: int = 10,
    ) -> tuple[AuditEntry, ...]:
        self.calls.append(("history", peer, limit))
        return ()

    async def assign_badge(
        self,
        context: OperationContext,
        peer: FluxGramPeerKey,
        assignment: BadgeAssignment,
    ) -> OperationResult:
        self.calls.append(("assign", context, peer, assignment))
        return OperationResult(context.operation_id, peer, 1)

    async def remove_badge(
        self,
        context: OperationContext,
        peer: FluxGramPeerKey,
        badge_id: str,
    ) -> OperationResult:
        self.calls.append(("remove", context, peer, badge_id))
        return OperationResult(context.operation_id, peer, 2)

    async def rollback(
        self,
        context: OperationContext,
        original_operation_id: str,
    ) -> OperationResult:
        peer = FluxGramPeerKey(PeerType.USER, 42)
        self.calls.append(("rollback", context, original_operation_id))
        return OperationResult(context.operation_id, peer, 3)


@pytest.mark.anyio
async def test_router_runs_inline_assignment_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    methods: list[TelegramMethod[Any]] = []

    async def call_bot(
        bot: Bot,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        methods.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", call_bot)
    appearance = FakeAppearanceService()
    dispatcher = Dispatcher()
    dispatcher.include_router(create_admin_router(appearance, frozenset({1001})))
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI")

    await dispatcher.emit_startup(bot=bot)
    await dispatcher.feed_update(bot, message_update(1, 1001, "/admin"))
    await dispatcher.feed_update(bot, callback_update(2, 1001, "assign"))
    await dispatcher.feed_update(bot, message_update(3, 1001, "42"))
    await dispatcher.feed_update(
        bot,
        callback_update(4, 1001, BadgeChoice(index=0).pack()),
    )
    await dispatcher.feed_update(
        bot,
        callback_update(5, 1001, PositionChoice(position=0).pack()),
    )
    await dispatcher.feed_update(
        bot,
        callback_update(6, 1001, ExpiryChoice(days=0).pack()),
    )
    await dispatcher.feed_update(
        bot,
        callback_update(7, 1001, Confirmation(action="assign").pack()),
    )
    await dispatcher.emit_shutdown(bot=bot)
    await bot.session.close()

    assign_calls = [call for call in appearance.calls if call[0] == "assign"]
    assert len(assign_calls) == 1
    _, context, peer, assignment = assign_calls[0]
    assert isinstance(context, OperationContext)
    assert context.operation_id == "telegram:update:7"
    assert peer == FluxGramPeerKey(PeerType.USER, 42)
    assert isinstance(assignment, BadgeAssignment)
    assert assignment.badge_id == "founder"
    assert any(type(method).__name__ == "SetMyCommands" for method in methods)


@pytest.mark.anyio
async def test_middleware_blocks_non_admin_without_running_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_bot = AsyncMock(return_value=True)
    monkeypatch.setattr(Bot, "__call__", call_bot)
    appearance = FakeAppearanceService()
    dispatcher = Dispatcher()
    dispatcher.include_router(create_admin_router(appearance, frozenset({1001})))
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI")

    await dispatcher.feed_update(bot, message_update(1, 1002, "/admin"))
    await bot.session.close()

    assert appearance.calls == []
    assert call_bot.await_count == 1


@pytest.mark.anyio
async def test_emoji_id_command_waits_for_custom_emoji_and_returns_document_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    methods: list[TelegramMethod[Any]] = []

    async def call_bot(
        bot: Bot,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        methods.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", call_bot)
    appearance = FakeAppearanceService()
    dispatcher = Dispatcher()
    dispatcher.include_router(create_admin_router(appearance, frozenset({1001})))
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI")

    await dispatcher.emit_startup(bot=bot)
    await dispatcher.feed_update(bot, message_update(20, 1001, "/emoji_id"))
    await dispatcher.feed_update(bot, message_update(21, 1001, "Обычный текст"))
    await dispatcher.feed_update(
        bot,
        custom_emoji_update(22, 1001, ("5373141891321699086", "123456789")),
    )
    answers_after_flow = len([method for method in methods if isinstance(method, SendMessage)])
    await dispatcher.feed_update(
        bot,
        custom_emoji_update(23, 1001, ("999",)),
    )
    await dispatcher.emit_shutdown(bot=bot)
    await bot.session.close()

    answers = [method for method in methods if isinstance(method, SendMessage)]
    commands = next(method for method in methods if isinstance(method, SetMyCommands))

    assert [command.command for command in commands.commands] == ["admin", "emoji_id"]
    assert answers[-3].text == CUSTOM_EMOJI_PROMPT
    assert answers[-2].text == CUSTOM_EMOJI_MISSING
    assert answers[-1].text == "5373141891321699086\n123456789"
    assert len(answers) == answers_after_flow


def test_profile_history_and_menu_helpers() -> None:
    peer = FluxGramPeerKey(PeerType.USER, 42)
    profile = ProfileSummary(
        peer=peer,
        revision=7,
        badges=(BadgeAssignment("founder", 0),),
        design=ProfileDesign(palette_id="midnight"),
    )
    history = (
        AuditEntry(
            operation_id="telegram:update:11",
            operation_type="assign_badge",
            actor_id=1001,
            occurred_at=datetime(2026, 10, 7, tzinfo=UTC),
        ),
    )

    assert parse_user_peer("42") == peer
    assert parse_user_peer("-42") is None
    assert "Revision: 7" in format_profile(peer, profile)
    assert "founder" in format_profile(peer, profile)
    assert "telegram:update:11" in format_history(peer, history)
    assert len(menu_keyboard().inline_keyboard) == 3


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


def custom_emoji_update(
    update_id: int,
    user_id: int,
    document_ids: tuple[str, ...],
) -> Update:
    text = "🙂" * len(document_ids)
    entities = [
        {
            "type": "custom_emoji",
            "offset": index * 2,
            "length": 2,
            "custom_emoji_id": document_id,
        }
        for index, document_id in enumerate(document_ids)
    ]

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
                "entities": entities,
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
