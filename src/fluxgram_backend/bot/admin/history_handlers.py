from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, Update

from fluxgram_backend.bot.admin.callbacks import Confirmation, RollbackChoice
from fluxgram_backend.bot.admin.common import edit_callback, operation_context, run_mutation
from fluxgram_backend.bot.admin.formatting import NEWLINE, format_history, parse_user_peer
from fluxgram_backend.bot.admin.keyboards import (
    cancel_keyboard,
    confirmation_keyboard,
    menu_keyboard,
    rollback_keyboard,
)
from fluxgram_backend.bot.admin.service import AdminAppearanceService
from fluxgram_backend.bot.admin.states import AdminFlow


def register_history_handlers(
    router: Router,
    appearance: AdminAppearanceService,
) -> None:
    @router.callback_query(F.data == "history")
    async def choose_history_peer(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminFlow.history_peer)
        await edit_callback(query, "Введите Telegram ID пользователя:", cancel_keyboard())

    @router.message(AdminFlow.history_peer)
    async def show_history(message: Message, state: FSMContext) -> None:
        peer = parse_user_peer(message.text)
        if peer is None:
            await message.answer("Нужен положительный числовой Telegram ID.")
            return

        history = await appearance.get_history(peer)
        await state.clear()
        await message.answer(format_history(peer, history), reply_markup=menu_keyboard())

    @router.callback_query(F.data == "rollback")
    async def choose_rollback_peer(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminFlow.rollback_peer)
        await edit_callback(query, "Введите Telegram ID пользователя:", cancel_keyboard())

    @router.message(AdminFlow.rollback_peer)
    async def choose_rollback_operation(message: Message, state: FSMContext) -> None:
        peer = parse_user_peer(message.text)
        if peer is None:
            await message.answer("Нужен положительный числовой Telegram ID.")
            return

        history = await appearance.get_history(peer)
        if not history:
            await state.clear()
            await message.answer("История пользователя пуста.", reply_markup=menu_keyboard())
            return

        operation_ids = [entry.operation_id for entry in history]
        await state.update_data(peer_id=peer.peer_id, operation_ids=operation_ids)
        await state.set_state(AdminFlow.rollback_operation)
        await message.answer(
            "Выберите операцию для отката:",
            reply_markup=rollback_keyboard(history),
        )

    @router.callback_query(AdminFlow.rollback_operation, RollbackChoice.filter())
    async def preview_rollback(
        query: CallbackQuery,
        callback_data: RollbackChoice,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        operation_ids = list(data["operation_ids"])
        if not 0 <= callback_data.index < len(operation_ids):
            await query.answer("История устарела.", show_alert=True)
            return

        operation_id = operation_ids[callback_data.index]
        await state.update_data(operation_id=operation_id)
        await state.set_state(AdminFlow.rollback_confirm)
        text = NEWLINE.join(
            (
                "Подтвердите откат:",
                f"User ID: {data['peer_id']}",
                f"Операция: {operation_id}",
            )
        )
        await edit_callback(query, text, confirmation_keyboard("rollback"))

    @router.callback_query(
        AdminFlow.rollback_confirm,
        Confirmation.filter(F.action == "rollback"),
    )
    async def confirm_rollback(
        query: CallbackQuery,
        event_update: Update,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        await run_mutation(
            query,
            state,
            appearance.rollback(
                operation_context(event_update, query),
                str(data["operation_id"]),
            ),
            "Операция отменена",
        )
