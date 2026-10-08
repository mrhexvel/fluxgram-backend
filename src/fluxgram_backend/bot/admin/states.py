from aiogram.fsm.state import State, StatesGroup


class AdminFlow(StatesGroup):
    custom_emoji = State()
    profile_peer = State()
    assign_peer = State()
    assign_badge = State()
    assign_position = State()
    assign_expiry = State()
    assign_confirm = State()
    remove_peer = State()
    remove_badge = State()
    remove_confirm = State()
    history_peer = State()
    rollback_peer = State()
    rollback_operation = State()
    rollback_confirm = State()
