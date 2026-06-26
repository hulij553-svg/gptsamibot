"""Telegram bot handlers — image MVP с вводом API-ключа (как в оригинале).

Пользователь подключает свой AIGate-ключ (через /setkey или кнопку), генерации
списываются с его баланса. Общего ключа/лимитов/тарифов пока нет — это позже.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User

from app import keyboards as kb
from app.api_client import AIGateClient, AIGateError, format_balance
from app.config import settings
from app.database import db
from app.states import ProfileStates

router = Router()


async def ensure_user(from_user: User):
    user = await db.get_user(from_user.id)
    if not user:
        await db.create_user(from_user.id, from_user.username, from_user.full_name)
        user = await db.get_user(from_user.id)
    return user


def webapp_url() -> str:
    return settings.WEBAPP_URL or ""


async def start_text(from_user: User) -> tuple[str, bool]:
    user = await ensure_user(from_user)
    has_key = bool(user and user.get("api_key"))
    status = "ключ подключён" if has_key else "ключ ещё не подключён"
    text = (
        "<b>sami studio</b>\n\n"
        "Генерация картинок через <code>gpt-image-2</code>.\n"
        "Генерации списываются с баланса вашего API-ключа.\n\n"
        f"<b>Статус:</b> {status}\n\n"
        "<b>Как начать:</b>\n"
        "1. Зарегистрируйтесь и пополните баланс на aigate.shop\n"
        "2. Получите API-ключ в кабинете.\n"
        "3. Нажмите «Подключить API-ключ» или отправьте /setkey.\n"
        "4. Откройте «Генератор» и создайте картинку.\n\n"
        "Готовые фото приходят прямо в этот чат."
    )
    return text, has_key


@router.message(Command("start"))
async def cmd_start(message: Message):
    text, has_key = await start_text(message.from_user)
    await message.answer(text, reply_markup=kb.main_menu_kb(has_key, webapp_url()))


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Команды</b>\n"
        "/start — главное меню\n"
        "/setkey — подключить API-ключ\n"
        "/generate — открыть Mini App\n"
        "/profile — баланс и статус ключа\n"
        "/history — последние генерации"
    )


@router.message(Command("generate"))
async def cmd_generate(message: Message):
    user = await ensure_user(message.from_user)
    has_key = bool(user and user.get("api_key"))
    if not has_key:
        await message.answer("Сначала подключите API-ключ.", reply_markup=kb.main_menu_kb(False, webapp_url()))
        return
    await message.answer(
        "Генератор открывается в Mini App — там выбор размера, качества и количества.",
        reply_markup=kb.main_menu_kb(has_key, webapp_url()),
    )


@router.message(Command("setkey"))
async def cmd_setkey(message: Message, state: FSMContext):
    await ensure_user(message.from_user)
    await state.set_state(ProfileStates.waiting_api_key)
    await message.answer(
        "Отправьте API-ключ. Я проверю его через баланс и сохраню только на backend.",
        reply_markup=kb.cancel_kb(),
    )


@router.message(StateFilter(ProfileStates.waiting_api_key), F.text)
async def process_key(message: Message, state: FSMContext):
    key = message.text.strip()
    if key.lower() in {"отмена", "cancel"}:
        await state.clear()
        await message.answer("Отменено.", reply_markup=kb.remove_kb())
        await cmd_start(message)
        return

    # Стираем сообщение с ключом из чата сразу — не висит в истории.
    try:
        await message.delete()
    except Exception:
        pass

    checking = await message.answer("Проверяю ключ…")
    try:
        balance = await AIGateClient(key).get_balance()
        await db.set_user_api_key(message.from_user.id, key)
        await checking.edit_text(
            f"✅ Ключ подключён.\n\n{format_balance(balance)}",
            reply_markup=kb.profile_kb(True),
        )
    except AIGateError as exc:
        await checking.edit_text(f"❌ Ключ не прошёл проверку: {exc}")
    finally:
        await state.clear()
        await message.answer("Готово.", reply_markup=kb.remove_kb())


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    await send_profile(message, message.from_user)


async def send_profile(message: Message, from_user: User):
    user = await ensure_user(from_user)
    if not user or not user.get("api_key"):
        await message.answer("API-ключ пока не подключён.", reply_markup=kb.profile_kb(False))
        return
    try:
        balance = await AIGateClient(user["api_key"]).get_balance()
        await message.answer(f"<b>Профиль</b>\n\n{format_balance(balance)}", reply_markup=kb.profile_kb(True))
    except AIGateError as exc:
        await message.answer(f"Не удалось получить баланс: {exc}", reply_markup=kb.profile_kb(True))


@router.message(Command("history"))
async def cmd_history(message: Message):
    await send_history(message, message.from_user)


async def send_history(message: Message, from_user: User):
    await ensure_user(from_user)
    jobs = await db.get_user_jobs(from_user.id, 10)
    has_key = bool((await db.get_user(from_user.id)) or {}).get("api_key")
    if not jobs:
        await message.answer("История пока пустая.", reply_markup=kb.main_menu_kb(has_key, webapp_url()))
        return
    icons = {"queued": "⏳", "generating": "🔄", "saving": "💾", "done": "✅", "failed": "❌", "partial": "⚠️"}
    lines = ["<b>Последние генерации</b>"]
    for j in jobs:
        icon = icons.get(j["status"], j["status"])
        lines.append(f"{icon} #{j['id'][:8]} — {j.get('n_done', 0)}/{j.get('n_requested', 0)} · {j.get('size')} · {j.get('quality')}")
    await message.answer("\n".join(lines), reply_markup=kb.main_menu_kb(has_key, webapp_url()))


# ---------------- callbacks ----------------

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.answer()
    text, has_key = await start_text(callback.from_user)
    await callback.message.edit_text(text, reply_markup=kb.main_menu_kb(has_key, webapp_url()))


@router.callback_query(F.data.in_({"set_key", "change_key"}))
async def cb_setkey(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(ProfileStates.waiting_api_key)
    await callback.message.answer(
        "Отправьте API-ключ.",
        reply_markup=kb.cancel_kb(),
    )


@router.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    await callback.answer()
    user = await ensure_user(callback.from_user)
    if not user or not user.get("api_key"):
        await callback.message.edit_text("API-ключ пока не подключён.", reply_markup=kb.profile_kb(False))
        return
    try:
        balance = await AIGateClient(user["api_key"]).get_balance()
        await callback.message.edit_text(
            f"<b>Профиль</b>\n\n{format_balance(balance)}", reply_markup=kb.profile_kb(True)
        )
    except AIGateError as exc:
        await callback.message.edit_text(f"Не удалось получить баланс: {exc}", reply_markup=kb.profile_kb(True))


@router.callback_query(F.data == "history")
async def cb_history(callback: CallbackQuery):
    await callback.answer()
    await ensure_user(callback.from_user)
    jobs = await db.get_user_jobs(callback.from_user.id, 10)
    has_key = bool((await db.get_user(callback.from_user.id)) or {}).get("api_key")
    if not jobs:
        await callback.message.edit_text("История пока пустая.", reply_markup=kb.main_menu_kb(has_key, webapp_url()))
        return
    icons = {"queued": "⏳", "generating": "🔄", "saving": "💾", "done": "✅", "failed": "❌", "partial": "⚠️"}
    lines = ["<b>Последние генерации</b>"]
    for j in jobs:
        icon = icons.get(j["status"], j["status"])
        lines.append(f"{icon} #{j['id'][:8]} — {j.get('n_done', 0)}/{j.get('n_requested', 0)} · {j.get('size')} · {j.get('quality')}")
    await callback.message.edit_text("\n".join(lines), reply_markup=kb.main_menu_kb(has_key, webapp_url()))


# ---------------- admin ----------------

async def _resolve_user(arg: str):
    if arg.isdigit():
        return await db.get_user(int(arg))
    all_users = await db.get_all_users()
    return next((u for u in all_users if u.get("username") and u["username"].lower() == arg.lower()), None)


@router.message(Command("allow"))
async def cmd_allow(message: Message):
    if not message.from_user or message.from_user.id not in settings.ADMIN_IDS:
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /allow 123456789 или /allow @username")
        return
    user = await _resolve_user(parts[1].lstrip("@"))
    if user:
        await db.set_user_allowed(int(user["telegram_id"]), True)
        name = user.get("username") or user.get("full_name") or str(user["telegram_id"])
        await message.answer(f"✅ Доступ открыт: {name}")
    else:
        await message.answer("❌ Пользователь не найден. Он должен хотя бы раз открыть бота.")


@router.message(Command("deny"))
async def cmd_deny(message: Message):
    if not message.from_user or message.from_user.id not in settings.ADMIN_IDS:
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /deny 123456789 или /deny @username")
        return
    user = await _resolve_user(parts[1].lstrip("@"))
    if user:
        await db.set_user_allowed(int(user["telegram_id"]), False)
        name = user.get("username") or user.get("full_name") or str(user["telegram_id"])
        await message.answer(f"🚫 Доступ закрыт: {name}")
    else:
        await message.answer("❌ Пользователь не найден.")


@router.message(Command("users"))
async def cmd_users(message: Message):
    if not message.from_user or message.from_user.id not in settings.ADMIN_IDS:
        return
    all_users = await db.get_all_users()
    if not all_users:
        await message.answer("Нет пользователей.")
        return
    lines = []
    for u in all_users:
        status = "✅" if u.get("is_allowed") else "🚫"
        key = "🔑" if u.get("api_key") else "—"
        name = f"@{u['username']}" if u.get("username") else u.get("full_name") or "—"
        lines.append(f"{status} {key} {name} ({u['telegram_id']})")
    await message.answer("Пользователи:\n" + "\n".join(lines))


@router.message(F.text.lower().in_({"отмена", "cancel"}))
async def universal_cancel(message: Message, state: FSMContext):
    if await state.get_state():
        await state.clear()
        await message.answer("Отменено.", reply_markup=kb.remove_kb())
        await cmd_start(message)
