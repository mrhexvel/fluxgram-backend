from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, Update

from fluxgram_backend.application.plugin_reputation_decisions import ReputationDecisionError
from fluxgram_backend.application.plugin_reputation_publisher import ReputationPublication
from fluxgram_backend.bot.admin.callbacks import (
    Confirmation,
    ReputationKeyChoice,
    ReputationStatusChoice,
    ReputationValidityChoice,
)
from fluxgram_backend.bot.admin.common import edit_callback
from fluxgram_backend.bot.admin.formatting import (
    format_reputation_decision,
    parse_archive_sha256,
    parse_developer_key_id,
)
from fluxgram_backend.bot.admin.keyboards import (
    cancel_keyboard,
    confirmation_keyboard,
    menu_keyboard,
    reputation_key_keyboard,
    reputation_status_keyboard,
    reputation_validity_keyboard,
)
from fluxgram_backend.bot.admin.service import AdminReputationDecisionService
from fluxgram_backend.bot.admin.states import AdminFlow
from fluxgram_backend.domain.operations import ActorType, OperationContext
from fluxgram_backend.reputation.protocol import (
    ArchiveStatus,
    KeyStatus,
    ReputationQuery,
)


def register_reputation_handlers(
    router: Router,
    reputation: AdminReputationDecisionService,
) -> None:
    @router.callback_query(F.data == "reputation")
    async def choose_archive(query: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminFlow.reputation_archive)
        await edit_callback(query, "Введите SHA-256 архива плагина:", cancel_keyboard())

    @router.message(AdminFlow.reputation_archive)
    async def choose_key_mode(message: Message, state: FSMContext) -> None:
        archive_sha256 = parse_archive_sha256(message.text)
        if archive_sha256 is None:
            await message.answer("Нужны ровно 64 шестнадцатеричных символа SHA-256.")
            return

        await state.update_data(archive_sha256=archive_sha256)
        await state.set_state(AdminFlow.reputation_key_choice)
        await message.answer(
            "Есть ли ключ разработчика?",
            reply_markup=reputation_key_keyboard(),
        )

    @router.callback_query(AdminFlow.reputation_key_choice, ReputationKeyChoice.filter())
    async def choose_key(
        query: CallbackQuery,
        callback_data: ReputationKeyChoice,
        state: FSMContext,
    ) -> None:
        if callback_data.has_key:
            await state.set_state(AdminFlow.reputation_key)
            await edit_callback(
                query,
                "Введите ключ разработчика в формате p256:<64 hex>:",
                cancel_keyboard(),
            )
            return

        await state.update_data(developer_key_id=None, key_status=int(KeyStatus.NOT_APPLICABLE))
        await state.set_state(AdminFlow.reputation_archive_status)
        await edit_callback(
            query,
            "Выберите статус архива:",
            reputation_status_keyboard("archive"),
        )

    @router.message(AdminFlow.reputation_key)
    async def read_developer_key(message: Message, state: FSMContext) -> None:
        developer_key_id = parse_developer_key_id(message.text)
        if developer_key_id is None:
            await message.answer("Нужен ключ в формате p256:<64 шестнадцатеричных символа>.")
            return

        await state.update_data(developer_key_id=developer_key_id)
        await state.set_state(AdminFlow.reputation_archive_status)
        await message.answer(
            "Выберите статус архива:",
            reply_markup=reputation_status_keyboard("archive"),
        )

    @router.callback_query(
        AdminFlow.reputation_archive_status,
        ReputationStatusChoice.filter(),
    )
    async def choose_archive_status(
        query: CallbackQuery,
        callback_data: ReputationStatusChoice,
        state: FSMContext,
    ) -> None:
        if callback_data.target != "archive" or callback_data.value not in (1, 2):
            await query.answer("Выбор устарел.", show_alert=True)
            return

        await state.update_data(archive_status=callback_data.value)
        data = await state.get_data()
        if data.get("developer_key_id") is None:
            await state.set_state(AdminFlow.reputation_validity)
            await edit_callback(
                query,
                "Выберите окно свежести ответа:",
                reputation_validity_keyboard(),
            )
            return

        await state.set_state(AdminFlow.reputation_key_status)
        await edit_callback(
            query,
            "Выберите статус ключа разработчика:",
            reputation_status_keyboard("key"),
        )

    @router.callback_query(AdminFlow.reputation_key_status, ReputationStatusChoice.filter())
    async def choose_key_status(
        query: CallbackQuery,
        callback_data: ReputationStatusChoice,
        state: FSMContext,
    ) -> None:
        if callback_data.target != "key" or callback_data.value not in (1, 2):
            await query.answer("Выбор устарел.", show_alert=True)
            return

        await state.update_data(key_status=callback_data.value)
        await state.set_state(AdminFlow.reputation_validity)
        await edit_callback(
            query,
            "Выберите окно свежести ответа:",
            reputation_validity_keyboard(),
        )

    @router.callback_query(AdminFlow.reputation_validity, ReputationValidityChoice.filter())
    async def preview_decision(
        query: CallbackQuery,
        callback_data: ReputationValidityChoice,
        event_update: Update,
        state: FSMContext,
    ) -> None:
        if callback_data.days not in (1, 7, 30):
            await query.answer("Выбор устарел.", show_alert=True)
            return

        issued_at = datetime.now(UTC)
        expires_at = issued_at + timedelta(days=callback_data.days)
        issued_at_millis = int(issued_at.timestamp() * 1000)
        expires_at_millis = int(expires_at.timestamp() * 1000)
        await state.update_data(
            validity_days=callback_data.days,
            issued_at_millis=issued_at_millis,
            expires_at_millis=expires_at_millis,
            decision_operation_id=f"telegram:update:{event_update.update_id}:reputation",
        )
        publication = _publication(await state.get_data())
        await state.set_state(AdminFlow.reputation_confirm)
        await edit_callback(
            query,
            format_reputation_decision(publication, callback_data.days),
            confirmation_keyboard("reputation"),
            parse_mode="HTML",
        )

    @router.callback_query(
        AdminFlow.reputation_confirm,
        Confirmation.filter(F.action == "reputation"),
    )
    async def confirm_decision(
        query: CallbackQuery,
        state: FSMContext,
    ) -> None:
        data = await state.get_data()
        publication = _publication(data)
        now_millis = int(datetime.now(UTC).timestamp() * 1000)
        if publication.expires_at_millis <= now_millis:
            await state.set_state(AdminFlow.reputation_validity)
            await edit_callback(
                query,
                "Окно свежести истекло. Выберите новое:",
                reputation_validity_keyboard(),
            )
            return

        try:
            decision = await reputation.enqueue(
                OperationContext(
                    str(data["decision_operation_id"]),
                    ActorType.TELEGRAM_ADMIN,
                    query.from_user.id,
                ),
                publication,
            )
        except (ValueError, ReputationDecisionError) as error:
            await edit_callback(query, f"Решение отклонено: {error}", menu_keyboard())
            await state.clear()
            return

        replay = " Повторная доставка." if decision.replayed else ""
        await edit_callback(
            query,
            f"Решение поставлено в очередь. Номер: {decision.sequence_id}. "
            f"Состояние: {decision.state.value}.{replay}",
            menu_keyboard(),
        )
        await state.clear()


def _publication(data: dict[str, Any]) -> ReputationPublication:
    developer_key_value = data.get("developer_key_id")
    developer_key_id = str(developer_key_value) if developer_key_value is not None else None
    return ReputationPublication(
        ReputationQuery(str(data["archive_sha256"]), developer_key_id),
        ArchiveStatus(int(data["archive_status"])),
        KeyStatus(int(data["key_status"])),
        int(data["issued_at_millis"]),
        int(data["expires_at_millis"]),
    )
