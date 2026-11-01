from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import EditMessageText, SendMessage, SetMyCommands
from aiogram.methods.base import TelegramMethod
from aiogram.types import Update

from fluxgram_backend.application.plugin_reputation_decisions import (
    ReputationDecision,
    ReputationDecisionState,
)
from fluxgram_backend.application.plugin_reputation_publisher import ReputationPublication
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
from fluxgram_backend.bot.admin.custom_emoji_handlers import (
    CUSTOM_EMOJI_MISSING,
    CUSTOM_EMOJI_PROMPT,
)
from fluxgram_backend.bot.admin.formatting import (
    format_history,
    format_profile,
    parse_archive_sha256,
    parse_developer_key_id,
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
from fluxgram_backend.reputation.protocol import ArchiveStatus, KeyStatus


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


class FakeReputationService:
    def __init__(self) -> None:
        self.calls: list[tuple[OperationContext, ReputationPublication]] = []

    async def enqueue(
        self,
        context: OperationContext,
        publication: ReputationPublication,
    ) -> ReputationDecision:
        replayed = any(call[0].operation_id == context.operation_id for call in self.calls)
        self.calls.append((context, publication))
        return ReputationDecision(
            sequence_id=17,
            decision_operation_id=context.operation_id,
            publication_operation_id="reputation:" + "a" * 64,
            publication=publication,
            state=ReputationDecisionState.PENDING,
            available_at=datetime.now(UTC),
            attempt_count=0,
            lease_token=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error_code=None,
            published_revision=None,
            completed_at=None,
            replayed=replayed,
        )


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
    dispatcher.include_router(
        create_admin_router(appearance, FakeReputationService(), frozenset({1001}))
    )
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
async def test_router_queues_reputation_decision_with_developer_key(
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
    reputation = FakeReputationService()
    dispatcher = Dispatcher()
    dispatcher.include_router(
        create_admin_router(FakeAppearanceService(), reputation, frozenset({1001}))
    )
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI")

    await dispatcher.feed_update(bot, callback_update(100, 1001, "reputation"))
    await dispatcher.feed_update(bot, message_update(101, 1001, "A" * 64))
    await dispatcher.feed_update(
        bot,
        callback_update(102, 1001, ReputationKeyChoice(has_key=True).pack()),
    )
    await dispatcher.feed_update(bot, message_update(103, 1001, "p256:" + "B" * 64))
    await dispatcher.feed_update(
        bot,
        callback_update(
            104,
            1001,
            ReputationStatusChoice(target="archive", value=2).pack(),
        ),
    )
    await dispatcher.feed_update(
        bot,
        callback_update(
            105,
            1001,
            ReputationStatusChoice(target="key", value=2).pack(),
        ),
    )
    await dispatcher.feed_update(
        bot,
        callback_update(106, 1001, ReputationValidityChoice(days=7).pack()),
    )
    await dispatcher.feed_update(
        bot,
        callback_update(107, 1001, Confirmation(action="reputation").pack()),
    )
    await bot.session.close()

    assert len(reputation.calls) == 1
    context, publication = reputation.calls[0]
    assert context.operation_id == "telegram:update:106:reputation"
    assert context.actor_id == 1001
    assert publication.query.archive_sha256 == "a" * 64
    assert publication.query.developer_key_id == "p256:" + "b" * 64
    assert publication.archive_status is ArchiveStatus.MALICIOUS
    assert publication.key_status is KeyStatus.REVOKED
    assert publication.expires_at_millis - publication.issued_at_millis == int(
        timedelta(days=7).total_seconds() * 1000
    )
    assert any(
        "Решение поставлено в очередь" in (getattr(method, "text", "") or "")
        for method in methods
    )
    preview = next(
        method
        for method in methods
        if isinstance(method, EditMessageText)
        and "Подтверждение решения" in (method.text or "")
    )
    assert preview.parse_mode == "HTML"
    assert "<b>Пакет</b>" in (preview.text or "")
    assert "<b>Решение</b>" in (preview.text or "")
    assert "<b>Важно</b>" in (preview.text or "")
    assert "CLEAR" not in (preview.text or "")
    assert "Выпущено" not in (preview.text or "")


@pytest.mark.anyio
async def test_reputation_confirmation_retries_after_telegram_edit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_once = False

    async def call_bot(
        bot: Bot,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        nonlocal failed_once
        if (
            isinstance(method, EditMessageText)
            and "поставлено в очередь" in (method.text or "")
            and not failed_once
        ):
            failed_once = True
            raise RuntimeError("Telegram edit failed")
        return True

    monkeypatch.setattr(Bot, "__call__", call_bot)
    reputation = FakeReputationService()
    dispatcher = Dispatcher()
    dispatcher.include_router(
        create_admin_router(FakeAppearanceService(), reputation, frozenset({1001}))
    )
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI")

    await dispatcher.feed_update(bot, callback_update(200, 1001, "reputation"))
    await dispatcher.feed_update(bot, message_update(201, 1001, "d" * 64))
    await dispatcher.feed_update(
        bot,
        callback_update(202, 1001, ReputationKeyChoice(has_key=False).pack()),
    )
    await dispatcher.feed_update(
        bot,
        callback_update(
            203,
            1001,
            ReputationStatusChoice(target="archive", value=1).pack(),
        ),
    )
    await dispatcher.feed_update(
        bot,
        callback_update(204, 1001, ReputationValidityChoice(days=1).pack()),
    )
    with pytest.raises(RuntimeError, match="Telegram edit failed"):
        await dispatcher.feed_update(
            bot,
            callback_update(205, 1001, Confirmation(action="reputation").pack()),
        )

    await dispatcher.feed_update(
        bot,
        callback_update(206, 1001, Confirmation(action="reputation").pack()),
    )
    await bot.session.close()

    assert len(reputation.calls) == 2
    assert reputation.calls[0][0].operation_id == "telegram:update:204:reputation"
    assert reputation.calls[1][0].operation_id == reputation.calls[0][0].operation_id


@pytest.mark.anyio
async def test_middleware_blocks_non_admin_without_running_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_bot = AsyncMock(return_value=True)
    monkeypatch.setattr(Bot, "__call__", call_bot)
    appearance = FakeAppearanceService()
    dispatcher = Dispatcher()
    dispatcher.include_router(
        create_admin_router(appearance, FakeReputationService(), frozenset({1001}))
    )
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
    dispatcher.include_router(
        create_admin_router(appearance, FakeReputationService(), frozenset({1001}))
    )
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
    assert len(menu_keyboard().inline_keyboard) == 4
    assert parse_archive_sha256("A" * 64) == "a" * 64
    assert parse_archive_sha256("not-a-digest") is None
    assert parse_developer_key_id("P256:" + "B" * 64) == "p256:" + "b" * 64
    assert parse_developer_key_id("b" * 64) is None


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
