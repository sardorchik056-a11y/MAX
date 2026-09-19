"""
subscription.py — обязательная подписка на каналы.

Идея: пока список обязательных каналов не пуст, ЛЮБОЙ хендлер бота (профиль, игры,
чеки, партнёрка и т.д.) блокируется для пользователя, который подписан не на все каналы
из списка — вместо хендлера показывается экран с кнопками «Подписаться» + «Я подписался».
Блокировка глобальная и не зависит от того, что именно человек нажал или написал —
см. SubscriptionMiddleware, которая регистрируется в main() как outer-миддлварь на
dp.message и dp.callback_query, то есть отрабатывает раньше любого роутера бота.

Админы (ADMIN_IDS) никогда не блокируются — иначе администратор мог бы случайно
закрыть самому себе доступ к админ-панели, которой список каналов и управляется.

Каналы админ добавляет через админ-панель («⚙️ Админ-панель» → «🔒 Обязательная
подписка» → «➕ Добавить канал»): пересылает сообщение из канала ИЛИ присылает
@username/ссылку. При добавлении бот проверяет, что он состоит в канале
АДМИНИСТРАТОРОМ — без этого Telegram не даёт ни проверить подписку конкретного
пользователя (getChatMember), ни надёжно получить пригласительную ссылку.

Список каналов хранится в отдельной базе subscription.db (SQLite, по образцу
bonus.py) — переживает перезапуск бота.

Подключение из main.py (см. комментарии там же):
    import subscription as subscription_module
    subscription_module.ADMIN_IDS = set(ADMIN_IDS)
    subscription_module.set_on_verified(send_start_welcome)
    ...
    dp.include_router(subscription_module.router)
    dp.message.outer_middleware(subscription_module.SubscriptionMiddleware())
    dp.callback_query.outer_middleware(subscription_module.SubscriptionMiddleware())
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    User,
)

from ui import edit_any

# --------------------------------------------------------------------------
# Настройки / заполняется из main.py при старте
# --------------------------------------------------------------------------

DB_PATH = Path(__file__).with_name("subscription.db")
CACHE_TTL = 25.0  # сек. — не долбим Telegram getChatMember на каждое сообщение подряд

# ID администраторов — заполняется из main.py: subscription_module.ADMIN_IDS = set(ADMIN_IDS).
# Админы никогда не проверяются на подписку.
ADMIN_IDS: set[int] = set()

# Вызывается после того, как пользователь подтвердил подписку кнопкой «Я подписался»
# (актуально для /start — см. main.py). Сигнатура: async (bot, chat_id, user, payload) -> None
OnVerified = Callable[[Bot, int, User, Optional[str]], Awaitable[None]]
_on_verified: Optional[OnVerified] = None

log = logging.getLogger("subscription")


def set_on_verified(func: OnVerified) -> None:
    global _on_verified
    _on_verified = func


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# --------------------------------------------------------------------------
# Хранилище каналов (SQLite — переживает перезапуск, см. шапку файла)
# --------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _tx():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def _read():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _db_init() -> None:
    boot = sqlite3.connect(DB_PATH, timeout=10)
    try:
        boot.execute("PRAGMA journal_mode=WAL")  # быстрые коммиты; режим хранится в самом файле
    finally:
        boot.close()

    with _tx() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS required_channels (
                chat_id     INTEGER PRIMARY KEY,
                title       TEXT    NOT NULL,
                username    TEXT,
                invite_link TEXT    NOT NULL,
                added_by    INTEGER NOT NULL,
                added_at    REAL    NOT NULL
            )
            """
        )


_db_init()


def add_channel(chat_id: int, title: str, username: str | None, invite_link: str, added_by: int) -> None:
    """Добавляет канал (или обновляет данные, если такой chat_id уже был добавлен)."""
    with _tx() as c:
        c.execute(
            """
            INSERT INTO required_channels (chat_id, title, username, invite_link, added_by, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                invite_link = excluded.invite_link
            """,
            (chat_id, title, username, invite_link, added_by, time.time()),
        )
    invalidate_cache()


def remove_channel(chat_id: int) -> dict | None:
    """Удаляет канал из списка обязательных. Возвращает его данные (для текста подтверждения)."""
    ch = get_channel(chat_id)
    with _tx() as c:
        c.execute("DELETE FROM required_channels WHERE chat_id = ?", (chat_id,))
    invalidate_cache()
    return ch


def get_channel(chat_id: int) -> dict | None:
    with _read() as c:
        row = c.execute("SELECT * FROM required_channels WHERE chat_id = ?", (chat_id,)).fetchone()
    return dict(row) if row else None


def list_channels() -> list[dict]:
    with _read() as c:
        rows = c.execute("SELECT * FROM required_channels ORDER BY added_at").fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Проверка подписки (getChatMember) с коротким кешем на пользователя
# --------------------------------------------------------------------------

_cache: dict[int, tuple[float, list[int]]] = {}

_SUBSCRIBED_STATUSES = {
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.RESTRICTED,
}


def invalidate_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(user_id, None)


async def get_missing_channels(bot: Bot, user_id: int, force: bool = False) -> list[dict]:
    """Возвращает список каналов (см. list_channels()), на которые пользователь ещё
    не подписан. Пустой список = можно пользоваться ботом.

    Канал, который в моменте проверить не удалось (бота выгнали / лишили прав и т.п.),
    в список «недостающих» НЕ попадает — сбой проверки одного канала не должен
    блокировать всех пользователей бота."""
    channels = list_channels()
    if not channels:
        return []

    now = time.monotonic()
    if not force:
        cached = _cache.get(user_id)
        if cached is not None and now - cached[0] < CACHE_TTL:
            missing_ids = set(cached[1])
            return [c for c in channels if c["chat_id"] in missing_ids]

    missing: list[dict] = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["chat_id"], user_id)
            if member.status not in _SUBSCRIBED_STATUSES:
                missing.append(ch)
        except TelegramAPIError as ex:
            log.warning(
                "[subscription] не удалось проверить канал %s для user=%s: %s", ch["chat_id"], user_id, ex
            )
            continue

    _cache[user_id] = (now, [c["chat_id"] for c in missing])
    return missing


# --------------------------------------------------------------------------
# Экран «Доступ ограничен»
# --------------------------------------------------------------------------


def format_required_screen(missing: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🔒 <b>Доступ ограничен</b>\n\n"
        "<i>Чтобы пользоваться ботом, подпишитесь на канал(ы) ниже, а затем нажмите "
        "«Я подписался».</i>"
    )
    rows = [[InlineKeyboardButton(text=f"📢 {c['title']}", url=c["invite_link"])] for c in missing]
    rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="subscription:check")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------
# Глобальный гейт (outer-миддлварь) — блокирует бота без подписки
# --------------------------------------------------------------------------


class SubscriptionMiddleware(BaseMiddleware):
    """Блокирует любой хендлер для не подписанного пользователя — вместо него
    показывает экран «Доступ ограничен». Пропускает без проверки: админов, команду
    /start (она сама решает, что показать — см. cmd_start в main.py) и нажатие
    кнопки «Я подписался»."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or _is_admin(user.id):
            return await handler(event, data)

        if isinstance(event, Message):
            if (event.text or "").startswith("/start"):
                return await handler(event, data)

            missing = await get_missing_channels(data["bot"], user.id)
            if not missing:
                return await handler(event, data)

            text, markup = format_required_screen(missing)
            await event.answer(text, reply_markup=markup)
            return None

        if isinstance(event, CallbackQuery):
            if event.data == "subscription:check":
                return await handler(event, data)

            missing = await get_missing_channels(data["bot"], user.id)
            if not missing:
                return await handler(event, data)

            text, markup = format_required_screen(missing)
            await event.answer("🔒 Сначала подпишитесь — кнопки ниже.", show_alert=True)
            if event.message is not None:
                try:
                    await edit_any(event.message, text, reply_markup=markup)
                except Exception:
                    pass
            return None

        return await handler(event, data)


# --------------------------------------------------------------------------
# Роутер: кнопка «Я подписался» + админ-панель управления каналами
# --------------------------------------------------------------------------

router = Router(name="subscription")


@router.callback_query(F.data == "subscription:check")
async def subscription_check(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    missing = await get_missing_channels(bot, callback.from_user.id, force=True)
    if missing:
        text, markup = format_required_screen(missing)
        await callback.answer("❗ Вы подписаны ещё не на все каналы.", show_alert=True)
        try:
            await edit_any(callback.message, text, reply_markup=markup)
        except Exception:
            pass
        return

    await callback.answer("✅ Подписка подтверждена!")

    data = await state.get_data()
    payload = data.get("pending_start_payload")
    await state.clear()

    try:
        await callback.message.delete()
    except Exception:
        pass

    if _on_verified is not None:
        await _on_verified(bot, callback.message.chat.id, callback.from_user, payload)


class SubsStates(StatesGroup):
    add_channel = State()


def _tree(lines: list[str]) -> str:
    if not lines:
        return ""
    if len(lines) == 1:
        return f"└ {lines[0]}"
    return "\n".join([f"┌ {lines[0]}", *[f"├ {l}" for l in lines[1:-1]], f"└ {lines[-1]}"])


def format_subs_menu_text() -> str:
    channels = list_channels()
    header = "🔒 <b>Обязательная подписка</b>\n\n"

    if not channels:
        return (
            header
            + "<i>Каналов не добавлено — подписка не требуется, бот доступен всем.</i>\n\n"
            "Нажмите «Добавить канал», чтобы включить проверку."
        )

    lines = []
    for ch in channels:
        username_part = f" (@{ch['username']})" if ch["username"] else ""
        lines.append(f"<b>{ch['title']}</b>{username_part}")

    return (
        header
        + "<i>Пока список не пуст, пользоваться ботом смогут только те, кто подписан "
        "на ВСЕ каналы ниже (кроме администраторов).</i>\n\n"
        f"{_tree(lines)}"
    )


def subs_menu_keyboard() -> InlineKeyboardMarkup:
    channels = list_channels()
    rows = [
        [InlineKeyboardButton(text=f"🗑 {c['title'][:30]}", callback_data=f"admin:subs:remove:{c['chat_id']}")]
        for c in channels
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="admin:subs:add")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subs_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin:subs:cancel")]]
    )


@router.callback_query(F.data == "admin:subs")
async def subs_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return
    await state.clear()
    await edit_any(callback.message, format_subs_menu_text(), reply_markup=subs_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:subs:add")
async def subs_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return
    await state.set_state(SubsStates.add_channel)
    await edit_any(
        callback.message,
        "➕ <b>Добавление канала</b>\n\n"
        "Перешлите сюда любое сообщение из канала, либо отправьте его @username или ссылку "
        "вида t.me/username.\n\n"
        "⚠️ <b>Бот должен быть администратором этого канала</b> — без этого Telegram не даёт "
        "проверять подписку. Добавьте бота в администраторы канала заранее.\n\n"
        "<i>Приватный канал без @username? Просто перешлите из него любое сообщение.</i>",
        reply_markup=subs_cancel_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:subs:cancel")
async def subs_add_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_any(callback.message, format_subs_menu_text(), reply_markup=subs_menu_keyboard())
    await callback.answer("Отменено")


async def _resolve_chat(bot: Bot, raw: str):
    raw = raw.strip()
    if "t.me/" in raw:
        raw = raw.split("t.me/", 1)[1]
    raw = raw.split("?", 1)[0].strip("/ ")
    if raw.startswith("@"):
        ref: str | int = raw
    elif raw.lstrip("-").isdigit():
        ref = int(raw)
    else:
        ref = f"@{raw}"
    return await bot.get_chat(ref)


@router.message(SubsStates.add_channel)
async def subs_add_receive(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return

    forward_chat = message.forward_from_chat
    raw = (message.text or "").strip()

    if forward_chat is None and not raw:
        await message.answer("Отправьте @username канала, ссылку на него, или перешлите сообщение из канала:")
        return

    try:
        chat = await bot.get_chat(forward_chat.id) if forward_chat is not None else await _resolve_chat(bot, raw)
    except TelegramAPIError as ex:
        await message.answer(
            f"❌ Не удалось найти канал: {ex}\n\n"
            "Проверьте ссылку/username, убедитесь, что бот в него добавлен, и попробуйте снова:"
        )
        return

    if chat.type not in ("channel", "supergroup", "group"):
        await message.answer(
            "❌ Это не канал и не группа. Пришлите другой @username/ссылку, либо перешлите сообщение "
            "из нужного канала:"
        )
        return

    try:
        member = await bot.get_chat_member(chat.id, bot.id)
    except TelegramAPIError:
        await message.answer(
            f"❌ Бот не состоит в «{chat.title}». Добавьте бота в канал администратором и попробуйте снова:"
        )
        return

    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.answer(
            f"❌ Бот добавлен в «{chat.title}», но НЕ является администратором.\n"
            "Выдайте боту права администратора в канале и повторите попытку:"
        )
        return

    invite_link = chat.invite_link
    if not invite_link:
        try:
            invite_link = await bot.export_chat_invite_link(chat.id)
        except TelegramAPIError:
            invite_link = None
    if not invite_link and chat.username:
        invite_link = f"https://t.me/{chat.username}"

    if not invite_link:
        await message.answer(
            f"❌ Не удалось получить пригласительную ссылку для «{chat.title}».\n"
            "Выдайте боту право «Приглашение пользователей по ссылке» в правах администратора "
            "и повторите попытку:"
        )
        return

    add_channel(chat.id, chat.title or "Канал", chat.username, invite_link, message.from_user.id)
    await state.clear()

    await message.answer(
        f"✅ Канал «{chat.title}» добавлен в обязательную подписку.",
        reply_markup=subs_menu_keyboard(),
    )


@router.callback_query(F.data.startswith("admin:subs:remove:"))
async def subs_remove(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return

    chat_id = int(callback.data.split(":", 3)[3])
    ch = remove_channel(chat_id)

    await edit_any(callback.message, format_subs_menu_text(), reply_markup=subs_menu_keyboard())
    await callback.answer(f"Канал «{ch['title']}» убран" if ch else "Канал уже убран")
