from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from fluxgram_backend.bot.admin.common import edit_callback
from fluxgram_backend.bot.admin.formatting import format_profile, parse_user_peer
from fluxgram_backend.bot.admin.keyboards import cancel_keyboard, menu_keyboard
from fluxgram_backend.bot.admin.service import AdminAppearanceService
from fluxgram_backend.bot.admin.states import AdminFlow


def register_profile_handlers(
    router: Router,
    appearance: AdminAppearanceService,
) -> None:
    @router.callback_query(F.data == "profile")
    async def choose_profile(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminFlow.profile_peer)
        await edit_callback(query, "Введите Telegram ID пользователя:", cancel_keyboard())

    @router.message(AdminFlow.profile_peer)
    async def show_profile(message: Message, state: FSMContext) -> None:
        peer = parse_user_peer(message.text)
        if peer is None:
            await message.answer("Нужен положительный числовой Telegram ID.")
            return

        profile = await appearance.get_profile(peer)
        await state.clear()
        await message.answer(format_profile(peer, profile), reply_markup=menu_keyboard())
