from aiogram.fsm.state import State, StatesGroup


class ProfileStates(StatesGroup):
    waiting_api_key = State()
