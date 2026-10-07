from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, CallbackQuery, Message

from fluxgram_backend.bot.admin.badge_handlers import register_badge_handlers
from fluxgram_backend.bot.admin.common import edit_callback
from fluxgram_backend.bot.admin.history_handlers import register_history_handlers
from fluxgram_backend.bot.admin.keyboards import menu_keyboard
from fluxgram_backend.bot.admin.middleware import AdminAccessMiddleware
from fluxgram_backend.bot.admin.profile_handlers import register_profile_handlers
from fluxgram_backend.bot.admin.service import AdminAppearanceService


def create_admin_router(
    appearance: AdminAppearanceService,
    admin_ids: frozenset[int],
) -> Router:
    router = Router(name="fluxgram_admin")
    middleware = AdminAccessMiddleware(admin_ids)
    router.message.middleware(middleware)
    router.callback_query.middleware(middleware)

    @router.startup()
    async def register_commands(bot: Bot) -> None:
        await bot.set_my_commands(
            [BotCommand(command="admin", description="Открыть панель FluxGram")]
        )

    @router.message(Command("admin"))
    async def open_admin(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Панель FluxGram", reply_markup=menu_keyboard())

    @router.callback_query(F.data == "menu")
    async def return_to_menu(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await edit_callback(query, "Панель FluxGram", menu_keyboard())

    register_profile_handlers(router, appearance)
    register_badge_handlers(router, appearance)
    register_history_handlers(router, appearance)
    return router
