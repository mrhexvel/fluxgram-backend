from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, Update

from fluxgram_backend.bot.admin.callbacks import (
    BadgeChoice,
    Confirmation,
    ExpiryChoice,
    PositionChoice,
    RemoveChoice,
)
from fluxgram_backend.bot.admin.common import edit_callback, operation_context, run_mutation
from fluxgram_backend.bot.admin.formatting import NEWLINE, parse_user_peer
from fluxgram_backend.bot.admin.keyboards import (
    badge_keyboard,
    cancel_keyboard,
    confirmation_keyboard,
    expiry_keyboard,
    menu_keyboard,
    position_keyboard,
    remove_keyboard,
)
from fluxgram_backend.bot.admin.service import AdminAppearanceService
from fluxgram_backend.bot.admin.states import AdminFlow
from fluxgram_backend.domain.appearance import BadgeAssignment
from fluxgram_backend.domain.peer import FluxGramPeerKey, PeerType


def register_badge_handlers(
    router: Router,
    appearance: AdminAppearanceService,
) -> None:
    @router.callback_query(F.data == "assign")
    async def choose_assign_peer(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminFlow.assign_peer)
        await edit_callback(query, "Введите Telegram ID пользователя:", cancel_keyboard())

    @router.message(AdminFlow.assign_peer)
    async def choose_badge(message: Message, state: FSMContext) -> None:
        peer = parse_user_peer(message.text)
        if peer is None:
            await message.answer("Нужен положительный числовой Telegram ID.")
            return

        definitions = await appearance.list_badge_definitions()
        if not definitions:
            await state.clear()
            await message.answer("Нет доступных бейджей.", reply_markup=menu_keyboard())
            return

        await state.update_data(
            peer_id=peer.peer_id,
            badge_ids=[definition.badge_id for definition in definitions],
        )
        await state.set_state(AdminFlow.assign_badge)
        await message.answer("Выберите бейдж:", reply_markup=badge_keyboard(definitions))

    @router.callback_query(AdminFlow.assign_badge, BadgeChoice.filter())
    async def choose_position(
        query: CallbackQuery,
        callback_data: BadgeChoice,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        badge_ids = list(data["badge_ids"])
        if not 0 <= callback_data.index < len(badge_ids):
            await query.answer("Список бейджей устарел.", show_alert=True)
            return

        await state.update_data(badge_id=badge_ids[callback_data.index])
        await state.set_state(AdminFlow.assign_position)
        await edit_callback(query, "Выберите позицию бейджа:", position_keyboard())

    @router.callback_query(AdminFlow.assign_position, PositionChoice.filter())
    async def choose_expiry(
        query: CallbackQuery,
        callback_data: PositionChoice,
        state: FSMContext,
    ) -> None:
        await state.update_data(position=callback_data.position)
        await state.set_state(AdminFlow.assign_expiry)
        await edit_callback(query, "Выберите срок действия:", expiry_keyboard())

    @router.callback_query(AdminFlow.assign_expiry, ExpiryChoice.filter())
    async def preview_assignment(
        query: CallbackQuery,
        callback_data: ExpiryChoice,
        state: FSMContext,
    ) -> None:
        expires_at = None
        if callback_data.days > 0:
            expires_at = datetime.now(UTC) + timedelta(days=callback_data.days)

        await state.update_data(
            expires_at=expires_at.isoformat() if expires_at is not None else None
        )
        await state.set_state(AdminFlow.assign_confirm)
        data = await state.get_data()
        text = NEWLINE.join(
            (
                "Подтвердите назначение:",
                f"User ID: {data['peer_id']}",
                f"Бейдж: {data['badge_id']}",
                f"Позиция: {data['position']}",
                f"Срок: {expires_at.isoformat() if expires_at is not None else 'без срока'}",
            )
        )
        await edit_callback(query, text, confirmation_keyboard("assign"))

    @router.callback_query(AdminFlow.assign_confirm, Confirmation.filter(F.action == "assign"))
    async def confirm_assignment(
        query: CallbackQuery,
        event_update: Update,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        peer = FluxGramPeerKey(PeerType.USER, int(data["peer_id"]))
        expires_value = data.get("expires_at")
        assignment = BadgeAssignment(
            badge_id=str(data["badge_id"]),
            position=int(data["position"]),
            expires_at=(
                datetime.fromisoformat(str(expires_value)) if expires_value is not None else None
            ),
        )
        await run_mutation(
            query,
            state,
            appearance.assign_badge(operation_context(event_update, query), peer, assignment),
            "Бейдж назначен",
        )

    @router.callback_query(F.data == "remove")
    async def choose_remove_peer(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminFlow.remove_peer)
        await edit_callback(query, "Введите Telegram ID пользователя:", cancel_keyboard())

    @router.message(AdminFlow.remove_peer)
    async def choose_removed_badge(message: Message, state: FSMContext) -> None:
        peer = parse_user_peer(message.text)
        if peer is None:
            await message.answer("Нужен положительный числовой Telegram ID.")
            return

        profile = await appearance.get_profile(peer)
        if profile is None or not profile.badges:
            await state.clear()
            await message.answer("Бейджи пользователю не назначены.", reply_markup=menu_keyboard())
            return

        badge_ids = [badge.badge_id for badge in profile.badges]
        await state.update_data(peer_id=peer.peer_id, badge_ids=badge_ids)
        await state.set_state(AdminFlow.remove_badge)
        await message.answer("Выберите бейдж:", reply_markup=remove_keyboard(badge_ids))

    @router.callback_query(AdminFlow.remove_badge, RemoveChoice.filter())
    async def preview_removal(
        query: CallbackQuery,
        callback_data: RemoveChoice,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        badge_ids = list(data["badge_ids"])
        if not 0 <= callback_data.index < len(badge_ids):
            await query.answer("Список бейджей устарел.", show_alert=True)
            return

        badge_id = badge_ids[callback_data.index]
        await state.update_data(badge_id=badge_id)
        await state.set_state(AdminFlow.remove_confirm)
        text = NEWLINE.join(
            (
                "Подтвердите удаление:",
                f"User ID: {data['peer_id']}",
                f"Бейдж: {badge_id}",
            )
        )
        await edit_callback(query, text, confirmation_keyboard("remove"))

    @router.callback_query(AdminFlow.remove_confirm, Confirmation.filter(F.action == "remove"))
    async def confirm_removal(
        query: CallbackQuery,
        event_update: Update,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        peer = FluxGramPeerKey(PeerType.USER, int(data["peer_id"]))
        await run_mutation(
            query,
            state,
            appearance.remove_badge(
                operation_context(event_update, query),
                peer,
                str(data["badge_id"]),
            ),
            "Бейдж удалён",
        )
