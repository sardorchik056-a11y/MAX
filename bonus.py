"""
bonus.py — бонусный баланс (💎) и бонусные чеки.

Как это работает:

    1. Админ создаёт бонусный чек командой  /addcheck <сумма> [активаций]  и получает ссылку
       вида t.me/<bot>?start=bcheck_<КОД>.
    2. Игрок активирует чек — бонус зачисляется на БОНУСНЫЙ баланс (не на реальный) и
       не может быть выведен.
    3. Чтобы бонус перешёл на реальный баланс, нужно сделать ставки на сумму
       WAGER_MULT (×3) от начального бонуса. Пример: бонус $0.50 → ставок на $1.50.
       В зачёт идут ставки, сделанные ПОСЛЕ получения бонуса.
    4. Как только отыгрыш выполнен, сумма бонуса автоматически переводится на реальный
       баланс, а игрок получает уведомление.

Если бонусов несколько — они отыгрываются по очереди (первый полученный — первым): ставки
сначала засчитываются в самый старый бонус, излишек переходит к следующему.

Как модуль узнаёт о ставках:
    install_bet_hook() оборачивает storage.log_game_round — эту функцию игры вызывают после
    каждого раунда. Хук надо установить ДО `import games` (это сделано в main.py), тогда
    games.py получает уже обёрнутую функцию. Саму games.py менять не нужно.

Данные лежат в отдельной базе bonus.db (SQLite), реальный баланс меняется только через
storage.adjust_balance в момент перевода.
"""

from __future__ import annotations

import asyncio
import functools
import html
import inspect
import logging
import secrets
import sqlite3
import string
import time
from contextlib import contextmanager
from pathlib import Path

from aiogram import Bot

import storage
from storage import adjust_balance, get_profile_stats

# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------

WAGER_MULT = 3.0                 # во сколько раз нужно «прокрутить» бонус ставками
BONUS_EMOJI_ID = "5427168083074628963"   # 💎 бонусные доллары

# Тип транзакции для перевода бонуса на реальный баланс (storage.adjust_balance).
# "admin_grant" — известный storage тип, не считается пополнением. Если в storage.py есть
# свой тип для бонусов — впишите его сюда.
BONUS_TRANSFER_KIND = "admin_grant"

MAX_CHECK_AMOUNT = 10_000.0
MAX_CHECK_ACTIVATIONS = 100_000

DB_PATH = Path(__file__).with_name("bonus.db")

# Кому слать уведомления о сбоях переводов. Заполняется из main.py (ADMIN_IDS).
ALERT_ADMIN_IDS: set[int] = set()

log = logging.getLogger("bonus")

BONUS_ICON = f'<tg-emoji emoji-id="{BONUS_EMOJI_ID}">💎</tg-emoji>'

_bot: Bot | None = None


def set_bot(bot: Bot) -> None:
    """Бот нужен для уведомлений об отыгрыше. Вызывается из main() при старте."""
    global _bot
    _bot = bot


def _fmt_usd(value: float) -> str:
    return f"${value:,.2f}"


# --------------------------------------------------------------------------
# База (SQLite)
# --------------------------------------------------------------------------


@contextmanager
def _tx():
    """Атомарная транзакция (BEGIN IMMEDIATE — параллельные записи выстраиваются в очередь)."""
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
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
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _db_init() -> None:
    with _tx() as c:
        # бонусные чеки, созданные админом
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS bonus_checks (
                code            TEXT PRIMARY KEY,
                amount          REAL    NOT NULL,
                max_activations INTEGER NOT NULL,
                used            INTEGER NOT NULL DEFAULT 0,
                active          INTEGER NOT NULL DEFAULT 1,
                created_by      INTEGER NOT NULL,
                created_at      REAL    NOT NULL
            )
            """
        )
        # кто какой чек активировал (один игрок — одна активация чека)
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS bonus_activations (
                code    TEXT    NOT NULL,
                user_id INTEGER NOT NULL,
                at      REAL    NOT NULL,
                PRIMARY KEY (code, user_id)
            )
            """
        )
        # полученные бонусы и прогресс отыгрыша.
        # status: active -> paying -> done | failed
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS bonus_grants (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                amount     REAL    NOT NULL,
                required   REAL    NOT NULL,
                wagered    REAL    NOT NULL DEFAULT 0,
                status     TEXT    NOT NULL DEFAULT 'active',
                source     TEXT,
                created_at REAL    NOT NULL,
                done_at    REAL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_grants_user ON bonus_grants (user_id, status)")


_db_init()

# Игроки с неотыгранным бонусом — чтобы обычные ставки остальных игроков не ходили в базу.
with _read() as _c:
    _users_with_bonus: set[int] = {
        int(r[0]) for r in _c.execute("SELECT DISTINCT user_id FROM bonus_grants WHERE status = 'active'")
    }
    _stuck = _c.execute("SELECT COUNT(*) FROM bonus_grants WHERE status IN ('paying', 'failed')").fetchone()[0]
if _stuck:
    log.warning("[bonus] в bonus_grants есть %s записей со статусом paying/failed — проверьте вручную", _stuck)


# --------------------------------------------------------------------------
# Чеки и выдача бонуса
# --------------------------------------------------------------------------


def create_check(admin_id: int, amount: float, max_activations: int) -> str:
    """Создаёт бонусный чек, возвращает его код."""
    alphabet = string.ascii_uppercase + string.digits
    with _tx() as c:
        while True:
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            if not c.execute("SELECT 1 FROM bonus_checks WHERE code = ?", (code,)).fetchone():
                break
        c.execute(
            "INSERT INTO bonus_checks (code, amount, max_activations, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (code, amount, max_activations, admin_id, time.time()),
        )
    return code


def grant_bonus(user_id: int, amount: float, source: str = "manual") -> None:
    """Зачисляет бонус на бонусный баланс (с требованием отыгрыша ×WAGER_MULT)."""
    with _tx() as c:
        _insert_grant(c, user_id, amount, source)
    _users_with_bonus.add(user_id)


def _insert_grant(c: sqlite3.Connection, user_id: int, amount: float, source: str) -> None:
    c.execute(
        "INSERT INTO bonus_grants (user_id, amount, required, source, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, amount, round(amount * WAGER_MULT, 2), source, time.time()),
    )


def activate_check(user_id: int, code: str) -> tuple[bool, str, float]:
    """Активация бонусного чека. Возвращает (успех, сообщение, сумма бонуса)."""
    code = code.strip().upper()
    with _tx() as c:
        check = c.execute("SELECT * FROM bonus_checks WHERE code = ?", (code,)).fetchone()
        if check is None or not check["active"]:
            return False, "Бонусный чек не найден или уже недействителен.", 0.0
        if check["used"] >= check["max_activations"]:
            c.execute("UPDATE bonus_checks SET active = 0 WHERE code = ?", (code,))
            return False, "Все активации этого чека уже использованы.", 0.0
        if c.execute("SELECT 1 FROM bonus_activations WHERE code = ? AND user_id = ?", (code, user_id)).fetchone():
            return False, "Вы уже активировали этот чек.", 0.0

        c.execute("INSERT INTO bonus_activations (code, user_id, at) VALUES (?, ?, ?)", (code, user_id, time.time()))
        used = check["used"] + 1
        c.execute(
            "UPDATE bonus_checks SET used = ?, active = ? WHERE code = ?",
            (used, 1 if used < check["max_activations"] else 0, code),
        )
        amount = float(check["amount"])
        _insert_grant(c, user_id, amount, f"check:{code}")
    _users_with_bonus.add(user_id)
    return True, "Бонусный чек активирован!", amount


async def check_link(bot: Bot, code: str) -> str:
    me = await bot.me()
    return f"https://t.me/{me.username}?start=bcheck_{code}"


def activation_text(amount: float) -> str:
    """Текст после успешной активации (для приветствия в main.py)."""
    return (
        f"{BONUS_ICON} <b>Бонусный чек активирован!</b>\n"
        f"┌ Бонус: <b>{_fmt_usd(amount)}</b>\n"
        f"├ Нужно поставить: <b>{_fmt_usd(amount * WAGER_MULT)}</b> (×{WAGER_MULT:g})\n"
        f"└ После отыгрыша бонус автоматически перейдёт на реальный баланс\n\n"
    )


def get_summary(user_id: int) -> dict:
    """Сводка по бонусному балансу: balance, required, wagered, remaining."""
    with _read() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(amount), 0), COALESCE(SUM(required), 0), COALESCE(SUM(wagered), 0) "
            "FROM bonus_grants WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchone()
    balance, required, wagered = float(row[0]), float(row[1]), float(row[2])
    return {
        "balance": balance,
        "required": required,
        "wagered": wagered,
        "remaining": max(required - wagered, 0.0),
    }


# --------------------------------------------------------------------------
# Отыгрыш: учёт ставок и перевод на реальный баланс
# --------------------------------------------------------------------------


def _apply_bet(user_id: int, bet: float) -> list[tuple[int, float, float]]:
    """Засчитывает ставку в отыгрыш (старые бонусы — первыми, излишек — следующему).
    Возвращает завершённые бонусы: [(grant_id, amount, required)] — они помечены 'paying'."""
    completed: list[tuple[int, float, float]] = []
    with _tx() as c:
        rows = c.execute(
            "SELECT id, amount, required, wagered FROM bonus_grants "
            "WHERE user_id = ? AND status = 'active' ORDER BY id",
            (user_id,),
        ).fetchall()
        left = bet
        for row in rows:
            if left <= 1e-9:
                break
            take = min(left, row["required"] - row["wagered"])
            left -= take
            wagered = row["wagered"] + take
            if wagered >= row["required"] - 1e-9:
                c.execute(
                    "UPDATE bonus_grants SET wagered = ?, status = 'paying', done_at = ? WHERE id = ?",
                    (row["required"], time.time(), row["id"]),
                )
                completed.append((row["id"], float(row["amount"]), float(row["required"])))
            else:
                c.execute("UPDATE bonus_grants SET wagered = ? WHERE id = ?", (round(wagered, 6), row["id"]))
        still_active = c.execute(
            "SELECT 1 FROM bonus_grants WHERE user_id = ? AND status = 'active' LIMIT 1", (user_id,)
        ).fetchone()
    if not still_active:
        _users_with_bonus.discard(user_id)
    return completed


def _set_grant_status(grant_id: int, status: str) -> None:
    with _tx() as c:
        c.execute("UPDATE bonus_grants SET status = ? WHERE id = ?", (status, grant_id))


async def _alert_admins(text: str) -> None:
    if _bot is None:
        return
    for admin_id in ALERT_ADMIN_IDS:
        try:
            await _bot.send_message(admin_id, f"<b>[Бонусы]</b> {text}")
        except Exception as ex:
            log.warning("[bonus] не удалось уведомить админа %s: %s", admin_id, ex)


async def _transfer_to_real(user_id: int, grant_id: int, amount: float, required: float) -> None:
    try:
        new_balance = adjust_balance(user_id, amount, BONUS_TRANSFER_KIND)
    except Exception as ex:
        await asyncio.to_thread(_set_grant_status, grant_id, "failed")
        log.critical("[bonus] НЕ УДАЛОСЬ перевести %.2f USD user=%s (grant %s)", amount, user_id, grant_id, exc_info=True)
        await _alert_admins(
            f"перевод бонуса #{grant_id} не выполнен: user=<code>{user_id}</code> {_fmt_usd(amount)} — "
            f"<code>{html.escape(repr(ex))}</code>. Нужен ручной разбор!"
        )
        return

    await asyncio.to_thread(_set_grant_status, grant_id, "done")
    log.info("[bonus] user=%s: бонус %.2f USD отыгран и переведён на реальный баланс (баланс %.2f)", user_id, amount, new_balance)

    if _bot is None:
        return
    try:
        await _bot.send_message(
            user_id,
            f"{BONUS_ICON} <b>Бонус отыгран!</b>\n\n"
            f"┌ Отыграно ставками: <b>{_fmt_usd(required)}</b>\n"
            f"├ Переведено на реальный баланс: <b>+{_fmt_usd(amount)}</b>\n"
            f"└ Баланс: <b>{_fmt_usd(get_profile_stats(user_id)['balance'])}</b>\n\n"
            "<i>Теперь эти средства можно использовать без ограничений. Удачной игры!</i>",
        )
    except Exception as ex:
        log.warning("[bonus] не удалось уведомить user=%s: %s", user_id, ex)


async def on_bet(user_id: int, bet: float) -> None:
    """Учитывает ставку игрока. Никогда не бросает исключений."""
    try:
        if bet <= 0 or user_id not in _users_with_bonus:
            return
        completed = await asyncio.to_thread(_apply_bet, user_id, bet)
        for grant_id, amount, required in completed:
            await _transfer_to_real(user_id, grant_id, amount, required)
    except Exception:
        log.exception("[bonus] сбой учёта ставки user=%s bet=%s", user_id, bet)


# --------------------------------------------------------------------------
# Хук на ставки (storage.log_game_round)
# --------------------------------------------------------------------------

_tasks: set[asyncio.Task] = set()


def _schedule_on_bet(user_id: int, bet: float) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("[bonus] ставка user=%s не учтена: нет запущенного event loop", user_id)
        return
    task = loop.create_task(on_bet(user_id, bet))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _rounds_count(user_id: int) -> int:
    return len(storage.USER_GAME_ROUNDS.get(user_id, ()))


def _detect_bet(user_id: int, before: int, args: tuple, kwargs: dict) -> float:
    """Сумма ставки только что залогированного раунда."""
    rounds = list(storage.USER_GAME_ROUNDS.get(user_id, ()))
    if len(rounds) > before:  # основной путь: берём новые раунды из хранилища
        return sum(float(r.get("bet") or 0) for r in rounds[before:])
    bet = kwargs.get("bet")  # запасной путь — ставка в аргументах вызова
    if bet is None and len(args) > 1 and isinstance(args[1], (int, float)):
        bet = args[1]
    return float(bet or 0)


def install_bet_hook() -> None:
    """Оборачивает storage.log_game_round, чтобы каждая ставка шла в отыгрыш бонуса.
    Вызывать ДО `import games` (games.py берёт log_game_round из storage)."""
    original = storage.log_game_round
    if getattr(original, "_bonus_hooked", False):
        return

    def _user_id(args: tuple, kwargs: dict) -> int | None:
        uid = kwargs.get("user_id", args[0] if args else None)
        return uid if isinstance(uid, int) else None

    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def hooked(*args, **kwargs):
            uid = _user_id(args, kwargs)
            before = _rounds_count(uid) if uid is not None else 0
            result = await original(*args, **kwargs)
            if uid is not None and uid in _users_with_bonus:
                _schedule_on_bet(uid, _detect_bet(uid, before, args, kwargs))
            return result

    else:

        @functools.wraps(original)
        def hooked(*args, **kwargs):
            uid = _user_id(args, kwargs)
            before = _rounds_count(uid) if uid is not None else 0
            result = original(*args, **kwargs)
            if uid is not None and uid in _users_with_bonus:
                _schedule_on_bet(uid, _detect_bet(uid, before, args, kwargs))
            return result

    hooked._bonus_hooked = True  # type: ignore[attr-defined]
    storage.log_game_round = hooked
    log.info("[bonus] хук на ставки установлен (storage.log_game_round)")
