from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject


class AdminAccessMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: frozenset[int]) -> None:
        self._admin_ids = admin_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat: Chat | None
        if isinstance(event, Message):
            user = event.from_user
            chat = event.chat
        elif isinstance(event, CallbackQuery):
            user = event.from_user
            chat = event.message.chat if isinstance(event.message, Message) else None
        else:
            return await handler(event, data)

        if user is None or user.id not in self._admin_ids:
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ запрещён.", show_alert=True)
            else:
                await event.answer("Доступ запрещён.")
            return None

        if chat is None or chat.type != ChatType.PRIVATE:
            if isinstance(event, CallbackQuery):
                await event.answer("Только личный чат.", show_alert=True)
            else:
                await event.answer("Административная панель доступна только в личном чате.")
            return None

        return await handler(event, data)
