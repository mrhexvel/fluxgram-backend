from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from fluxgram_backend.bot.admin.callbacks import (
    BadgeChoice,
    Confirmation,
    ExpiryChoice,
    PositionChoice,
    RemoveChoice,
    ReputationKeyChoice,
    ReputationStatusChoice,
    ReputationValidityChoice,
    RollbackChoice,
)
from fluxgram_backend.domain.appearance import BadgeDefinition
from fluxgram_backend.domain.operations import AuditEntry


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Профиль", callback_data="profile"),
                InlineKeyboardButton(text="История", callback_data="history"),
            ],
            [
                InlineKeyboardButton(text="Назначить бейдж", callback_data="assign"),
                InlineKeyboardButton(text="Удалить бейдж", callback_data="remove"),
            ],
            [InlineKeyboardButton(text="Откатить операцию", callback_data="rollback")],
            [InlineKeyboardButton(text="Репутация плагина", callback_data="reputation")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu")]]
    )


def badge_keyboard(definitions: tuple[BadgeDefinition, ...]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=definition.title,
                callback_data=BadgeChoice(index=index).pack(),
            )
        ]
        for index, definition in enumerate(definitions)
    ]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def position_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=str(position),
                callback_data=PositionChoice(position=position).pack(),
            )
            for position in range(start, min(start + 4, 8))
        ]
        for start in (0, 4)
    ]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def expiry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Без срока", callback_data=ExpiryChoice(days=0).pack())],
            [
                InlineKeyboardButton(text="30 дней", callback_data=ExpiryChoice(days=30).pack()),
                InlineKeyboardButton(text="1 год", callback_data=ExpiryChoice(days=365).pack()),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="menu")],
        ]
    )


def remove_keyboard(badge_ids: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=badge_id, callback_data=RemoveChoice(index=index).pack())]
        for index, badge_id in enumerate(badge_ids)
    ]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rollback_keyboard(history: tuple[AuditEntry, ...]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{entry.operation_type}: {entry.operation_id}",
                callback_data=RollbackChoice(index=index).pack(),
            )
        ]
        for index, entry in enumerate(history)
    ]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirmation_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить",
                    callback_data=Confirmation(action=action).pack(),
                ),
                InlineKeyboardButton(text="Отмена", callback_data="menu"),
            ]
        ]
    )


def reputation_key_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Без ключа разработчика",
                    callback_data=ReputationKeyChoice(has_key=False).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="Указать ключ разработчика",
                    callback_data=ReputationKeyChoice(has_key=True).pack(),
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data="menu")],
        ]
    )


def reputation_status_keyboard(target: str) -> InlineKeyboardMarkup:
    second_text = "Вредоносный" if target == "archive" else "Отозван"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Нет известного отзыва",
                    callback_data=ReputationStatusChoice(target=target, value=1).pack(),
                ),
                InlineKeyboardButton(
                    text=second_text,
                    callback_data=ReputationStatusChoice(target=target, value=2).pack(),
                ),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="menu")],
        ]
    )


def reputation_validity_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{days} дн.",
                    callback_data=ReputationValidityChoice(days=days).pack(),
                )
                for days in (1, 7, 30)
            ],
            [InlineKeyboardButton(text="Назад", callback_data="menu")],
        ]
    )
