from aiogram import Router
from aiogram.enums import MessageEntityType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from fluxgram_backend.bot.admin.states import AdminFlow

CUSTOM_EMOJI_PROMPT = "Отправьте сообщение с кастомным эмодзи."  # noqa: RUF001
CUSTOM_EMOJI_MISSING = "В сообщении нет кастомного эмодзи. Попробуйте ещё раз."  # noqa: RUF001


def register_custom_emoji_handlers(router: Router) -> None:
    @router.message(Command("emoji_id"))
    async def request_custom_emoji(message: Message, state: FSMContext) -> None:
        await state.set_state(AdminFlow.custom_emoji)
        await message.answer(CUSTOM_EMOJI_PROMPT)

    @router.message(AdminFlow.custom_emoji)
    async def show_custom_emoji_ids(message: Message, state: FSMContext) -> None:
        document_ids = custom_emoji_document_ids(message)

        if not document_ids:
            await message.answer(CUSTOM_EMOJI_MISSING)
            return

        await state.clear()
        await message.answer("\n".join(document_ids))


def custom_emoji_document_ids(message: Message) -> tuple[str, ...]:
    entities = tuple(message.entities or ()) + tuple(message.caption_entities or ())
    document_ids: list[str] = []

    for entity in entities:
        if entity.type != MessageEntityType.CUSTOM_EMOJI or entity.custom_emoji_id is None:
            continue
        if entity.custom_emoji_id not in document_ids:
            document_ids.append(entity.custom_emoji_id)

    return tuple(document_ids)
