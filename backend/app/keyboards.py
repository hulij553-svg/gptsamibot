"""Telegram keyboards."""

from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder, WebAppInfo

from app.config import settings


def main_menu_kb(has_api_key: bool = False, webapp_url: str = "") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if webapp_url:
        builder.button(text="🖼 Генератор", web_app=WebAppInfo(url=webapp_url))
    if has_api_key:
        builder.button(text="👤 Профиль", callback_data="profile")
        builder.button(text="📜 История", callback_data="history")
    else:
        builder.button(text="🔑 Подключить API-ключ", callback_data="set_key")
    builder.adjust(1, 2)
    return builder.as_markup()


def profile_kb(has_api_key: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔑 Сменить ключ", callback_data="set_key")
    builder.button(text="📜 История", callback_data="history")
    builder.button(text="🏠 Меню", callback_data="main_menu")
    builder.adjust(2, 1)
    return builder.as_markup()


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()
