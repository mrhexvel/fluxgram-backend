from aiogram.filters.callback_data import CallbackData


class BadgeChoice(CallbackData, prefix="fgb"):
    index: int


class PositionChoice(CallbackData, prefix="fgp"):
    position: int


class ExpiryChoice(CallbackData, prefix="fge"):
    days: int


class RemoveChoice(CallbackData, prefix="fgr"):
    index: int


class RollbackChoice(CallbackData, prefix="fgo"):
    index: int


class Confirmation(CallbackData, prefix="fgc"):
    action: str
