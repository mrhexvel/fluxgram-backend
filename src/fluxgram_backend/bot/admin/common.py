from collections.abc import Awaitable

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, Update

from fluxgram_backend.bot.admin.keyboards import menu_keyboard
from fluxgram_backend.domain.operations import (
    ActorType,
    AppearanceOperationError,
    OperationContext,
    OperationResult,
)


def operation_context(update: Update, query: CallbackQuery) -> OperationContext:
    return OperationContext(
        operation_id=f"telegram:update:{update.update_id}",
        actor_type=ActorType.TELEGRAM_ADMIN,
        actor_id=query.from_user.id,
    )


async def edit_callback(
    query: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=keyboard)


async def run_mutation(
    query: CallbackQuery,
    state: FSMContext,
    operation: Awaitable[OperationResult],
    success: str,
) -> None:
    try:
        result = await operation
    except (ValueError, AppearanceOperationError) as error:
        await state.clear()
        await edit_callback(query, f"Операция отклонена: {error}", menu_keyboard())
        return

    await state.clear()
    replay = " Повторная доставка." if result.replayed else ""
    await edit_callback(
        query,
        f"{success}. Revision: {result.revision}.{replay}",
        menu_keyboard(),
    )
