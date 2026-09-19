"""
bonus.py — бонусный баланс (💎), бонусные чеки и отыгрыш.

Правила:

    1. Админ создаёт бонусный чек командой  /addcheck <сумма> [активаций]  и получает ссылку
       вида t.me/<bot>?start=bcheck_<КОД>. Игрок активирует чек — бонус попадает на БОНУСНЫЙ
       баланс (не на реальный) и не может быть выведен.
    2. С бонусного баланса можно играть:
         • автоматически — если реальный баланс меньше $0.10 (см. games.BettingGame.take_bet);
         • вручную — командой «0.1 бонус» (ставка и режим «бонус» запоминаются до «0.1$»).
    3. Проигрыш списывается с бонусного баланса, ВЫИГРЫШ бонусной ставки зачисляется на
       бонусный же баланс. Реальный баланс бонусные раунды не затрагивают.
    4. Отыграть нужно WAGER_MULT (×3) от начальной суммы бонуса. В зачёт идут только ставки,
       сделанные с бонусного баланса. Пример: бонус $0.50 → бонусных ставок на $1.50.
    5. Как только отыгрыш выполнен, на РЕАЛЬНЫЙ баланс переходит только начальная сумма бонуса
       (а если на бонусном баланса осталось меньше — сколько осталось). Всё, что выиграно сверх
       начальной суммы, аннулируется, бонусный баланс обнуляется.
    6. Если после раунда на бонусном балансе осталось меньше MIN_BONUS_BALANCE ($0.10) —
       отыгрыш отменяется, остаток обнуляется (играть на него уже нельзя — минимальная ставка $0.10).

Несколько бонусов складываются в один кошелёк: суммы, требуемый отыгрыш и начальные суммы
суммируются, прогресс отыгрыша общий.

Как модуль узнаёт о ставках: games.py вызывает try_spend() при постановке ставки, settle() после
раунда и refund() при сбое игры. Хука на storage.log_game_round больше нет — реальные ставки
бонус не отыгрывают, а бонусные раунды в оборот/топ не попадают.

Данные лежат в отдельной базе bonus.db (SQLite). Реальный баланс меняется только через
storage.adjust_balance в момент перевода.
"""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import sqlite3
import string
import time
from contextlib import contextmanager
from pathlib import Path

from aiogram import Bot

from storage import adjust_balance, get_profile_stats

# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------

WAGER_MULT = 3.0                 # во сколько раз нужно «прокрутить» бонус ставками
MIN_BONUS_BALANCE = 0.1          # ниже этого остатка отыгрыш отменяется, бонус обнуляется
NOTIFY_DELAY = 4.0               # сек. — уведомление уходит ПОСЛЕ сообщения с результатом раунда (оно на +3с)
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


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Запись бонусной ставки идёт прямо в цикле событий бота, поэтому коммит должен быть быстрым.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _tx():
    """Атомарная транзакция (BEGIN IMMEDIATE — параллельные записи выстраиваются в очередь)."""
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
        # история полученных бонусов.
        # status: active -> paying -> done | failed   (отыграно)
        #         active -> cancelled                 (баланс упал ниже MIN_BONUS_BALANCE)
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
        # живой бонусный кошелёк игрока (одна строка на игрока, пока у него есть активный бонус):
        #   balance  — текущий бонусный баланс (с него играют, на него падает выигрыш)
        #   initial  — сумма выданных бонусов (столько перейдёт на реальный после отыгрыша)
        #   required — сколько всего нужно поставить, wagered — сколько уже поставлено
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS bonus_wallets (
                user_id    INTEGER PRIMARY KEY,
                balance    REAL NOT NULL,
                initial    REAL NOT NULL,
                required   REAL NOT NULL,
                wagered    REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            )
            """
        )
        # Миграция со старой схемы (бонус был статичной суммой в bonus_grants): активные гранты
        # без кошелька превращаются в кошелёк. Повторный запуск ничего не меняет.
        c.execute(
            """
            INSERT INTO bonus_wallets (user_id, balance, initial, required, wagered, updated_at)
            SELECT user_id, SUM(amount), SUM(amount), SUM(required), SUM(wagered), ?
            FROM bonus_grants
            WHERE status = 'active' AND user_id NOT IN (SELECT user_id FROM bonus_wallets)
            GROUP BY user_id
            """,
            (time.time(),),
        )


_db_init()

# Игроки с бонусным кошельком — чтобы обычные игроки не ходили в базу на каждой ставке/меню.
with _read() as _c:
    _users_with_bonus: set[int] = {int(r[0]) for r in _c.execute("SELECT user_id FROM bonus_wallets")}
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
    required = round(amount * WAGER_MULT, 2)
    now = time.time()
    c.execute(
        "INSERT INTO bonus_grants (user_id, amount, required, source, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, amount, required, source, now),
    )
    # новый бонус добавляется к кошельку (если он уже есть) — прогресс отыгрыша общий
    c.execute(
        """
        INSERT INTO bonus_wallets (user_id, balance, initial, required, wagered, updated_at)
        VALUES (?, ?, ?, ?, 0, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            balance    = balance  + excluded.balance,
            initial    = initial  + excluded.initial,
            required   = required + excluded.required,
            updated_at = excluded.updated_at
        """,
        (user_id, amount, amount, required, now),
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
        f"├ Играть на бонус: отправьте в чат, например, <code>0.1 бонус</code>\n"
        f"└ Если реальный баланс меньше <b>{_fmt_usd(MIN_BONUS_BALANCE)}</b> — игры идут на бонус автоматически\n\n"
        f"<i>После отыгрыша на реальный баланс перейдёт только начальная сумма бонуса. "
        f"Если бонусный баланс упадёт ниже {_fmt_usd(MIN_BONUS_BALANCE)}, отыгрыш отменяется, а остаток сгорает.</i>\n\n"
    )


def get_summary(user_id: int) -> dict:
    """Сводка по бонусному кошельку: active, balance, initial, required, wagered, remaining."""
    empty = {"active": False, "balance": 0.0, "initial": 0.0, "required": 0.0, "wagered": 0.0, "remaining": 0.0}
    if user_id not in _users_with_bonus:
        return empty
    with _read() as c:
        row = c.execute(
            "SELECT balance, initial, required, wagered FROM bonus_wallets WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return empty
    return {
        "active": True,
        "balance": float(row["balance"]),
        "initial": float(row["initial"]),
        "required": float(row["required"]),
        "wagered": float(row["wagered"]),
        "remaining": max(float(row["required"]) - float(row["wagered"]), 0.0),
    }


# --------------------------------------------------------------------------
# Игра на бонус: списание ставки, расчёт раунда, возврат
# --------------------------------------------------------------------------


def try_spend(user_id: int, amount: float) -> bool:
    """Списывает ставку с бонусного баланса. False — если бонуса нет или его не хватает."""
    if amount <= 0 or user_id not in _users_with_bonus:
        return False
    with _tx() as c:
        row = c.execute("SELECT balance FROM bonus_wallets WHERE user_id = ?", (user_id,)).fetchone()
        if row is None or row["balance"] < amount - 1e-9:
            return False
        c.execute(
            "UPDATE bonus_wallets SET balance = ?, updated_at = ? WHERE user_id = ?",
            (max(round(row["balance"] - amount, 6), 0.0), time.time(), user_id),
        )
    return True


def refund(user_id: int, amount: float) -> None:
    """Возвращает ставку на бонусный баланс (игра не состоялась). Отыгрыш не засчитывается."""
    with _tx() as c:
        cur = c.execute(
            "UPDATE bonus_wallets SET balance = ROUND(balance + ?, 6), updated_at = ? WHERE user_id = ?",
            (amount, time.time(), user_id),
        )
        if cur.rowcount == 0:
            log.warning("[bonus] возврат %.2f USD user=%s не выполнен: кошелька нет", amount, user_id)


def _close_wallet(c: sqlite3.Connection, user_id: int, status: str) -> list[int]:
    """Закрывает кошелёк: гранты получают status, строка кошелька удаляется. Возвращает id грантов."""
    ids = [int(r[0]) for r in c.execute("SELECT id FROM bonus_grants WHERE user_id = ? AND status = 'active'", (user_id,))]
    c.execute(
        "UPDATE bonus_grants SET status = ?, done_at = ? WHERE user_id = ? AND status = 'active'",
        (status, time.time(), user_id),
    )
    c.execute("DELETE FROM bonus_wallets WHERE user_id = ?", (user_id,))
    return ids


def _set_grants_status(ids: list[int], status: str) -> None:
    if not ids:
        return
    try:
        with _tx() as c:
            c.executemany("UPDATE bonus_grants SET status = ? WHERE id = ?", [(status, i) for i in ids])
    except Exception:
        log.exception("[bonus] не удалось выставить статус %s грантам %s", status, ids)


def settle(user_id: int, bet: float, win: float) -> dict | None:
    """Учитывает сыгранный на бонус раунд: win (0 при проигрыше) падает на бонусный баланс,
    bet идёт в отыгрыш.

    Возвращает событие, если кошелёк закрылся, иначе None:
      {"kind": "done",      "required", "transfer", "burned", ...}  — отыграно, transfer ушёл на реальный
      {"kind": "cancelled", "burned", "wagered", "required"}        — баланс < MIN_BONUS_BALANCE

    Событие надо передать в notify_event(). Перевод на реальный баланс уже выполнен внутри."""
    event: dict | None = None
    with _tx() as c:
        w = c.execute(
            "SELECT balance, initial, required, wagered FROM bonus_wallets WHERE user_id = ?", (user_id,)
        ).fetchone()
        if w is None:
            log.warning("[bonus] раунд user=%s bet=%.2f win=%.2f не учтён: кошелька нет", user_id, bet, win)
            return None

        required = float(w["required"])
        balance = round(float(w["balance"]) + win, 6)
        wagered = min(float(w["wagered"]) + bet, required)

        if balance < MIN_BONUS_BALANCE - 1e-9:
            # Остаток слишком мал для ставки — отыгрыш отменяется, бонус сгорает.
            # Проверяется первым: даже если этой ставкой отыгрыш добит, при остатке < MIN_BONUS_BALANCE
            # переводить нечего (и не должно приходить «отыграно, +$0.00»).
            event = {
                "kind": "cancelled",
                "burned": round(balance, 2),
                "wagered": wagered,
                "required": required,
                "ids": _close_wallet(c, user_id, "cancelled"),
            }
        elif wagered >= required - 1e-9:
            # Отыграно: на реальный — только начальная сумма (или меньше, если остаток меньше).
            transfer = round(min(float(w["initial"]), balance), 2)
            event = {
                "kind": "done",
                "required": required,
                "transfer": transfer,
                "burned": round(max(balance - transfer, 0.0), 2),
                "ids": _close_wallet(c, user_id, "paying"),
            }
        else:
            c.execute(
                "UPDATE bonus_wallets SET balance = ?, wagered = ?, updated_at = ? WHERE user_id = ?",
                (balance, round(wagered, 6), time.time(), user_id),
            )

    if event is None:
        return None

    _users_with_bonus.discard(user_id)
    if event["kind"] == "done":
        _pay_out(user_id, event)
    return event


def _pay_out(user_id: int, event: dict) -> None:
    """Переводит отыгранную начальную сумму на реальный баланс."""
    transfer = event["transfer"]
    try:
        if transfer > 0:
            adjust_balance(user_id, transfer, BONUS_TRANSFER_KIND)
    except Exception:
        event["failed"] = True
        _set_grants_status(event["ids"], "failed")
        log.critical("[bonus] НЕ УДАЛОСЬ перевести %.2f USD user=%s (гранты %s)", transfer, user_id, event["ids"], exc_info=True)
        return
    _set_grants_status(event["ids"], "done")
    log.info(
        "[bonus] user=%s: бонус отыгран, на реальный баланс переведено %.2f USD (аннулировано %.2f)",
        user_id, transfer, event["burned"],
    )


# --------------------------------------------------------------------------
# Уведомления
# --------------------------------------------------------------------------

_tasks: set[asyncio.Task] = set()


def notify_event(user_id: int, event: dict | None) -> None:
    """Планирует уведомление о закрытии кошелька. Безопасно вызывать из синхронного кода
    внутри работающего event loop; ничего не бросает."""
    if event is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("[bonus] уведомление user=%s не отправлено: нет запущенного event loop", user_id)
        return
    task = loop.create_task(_send_event(user_id, event))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _alert_admins(text: str) -> None:
    if _bot is None:
        return
    for admin_id in ALERT_ADMIN_IDS:
        try:
            await _bot.send_message(admin_id, f"<b>[Бонусы]</b> {text}")
        except Exception as ex:
            log.warning("[bonus] не удалось уведомить админа %s: %s", admin_id, ex)


def _event_text(user_id: int, event: dict) -> str:
    if event["kind"] == "done":
        lines = [
            f"┌ Отыграно ставками: <b>{_fmt_usd(event['required'])}</b>",
            f"├ Переведено на реальный баланс: <b>+{_fmt_usd(event['transfer'])}</b>",
        ]
        if event["burned"] >= 0.01:
            lines.append(f"├ Выигрыш сверх начальной суммы аннулирован: <b>{_fmt_usd(event['burned'])}</b>")
        lines.append(f"└ Баланс: <b>{_fmt_usd(get_profile_stats(user_id)['balance'])}</b>")
        return f"{BONUS_ICON} <b>Бонус отыгран!</b>\n\n" + "\n".join(lines)

    return (
        f"{BONUS_ICON} <b>Отыгрыш бонуса отменён</b>\n\n"
        f"┌ Бонусный баланс опустился ниже <b>{_fmt_usd(MIN_BONUS_BALANCE)}</b>\n"
        f"├ Остаток аннулирован: <b>{_fmt_usd(event['burned'])}</b>\n"
        f"└ Было отыграно: <b>{_fmt_usd(event['wagered'])}</b> из <b>{_fmt_usd(event['required'])}</b>\n\n"
        "<i>Реальный баланс не затронут. Чтобы играть на реальные средства, отправьте в чат, например, 0.1$</i>"
    )


async def _send_event(user_id: int, event: dict) -> None:
    try:
        if event.get("failed"):
            await _alert_admins(
                f"перевод бонуса не выполнен: user=<code>{user_id}</code> {_fmt_usd(event['transfer'])}, "
                f"гранты <code>{event['ids']}</code> (статус failed). Нужен ручной разбор!"
            )
            return
        await asyncio.sleep(NOTIFY_DELAY)  # не раньше результата раунда, чтобы не спойлерить анимацию
        if _bot is None:
            return
        await _bot.send_message(user_id, _event_text(user_id, event))
    except Exception as ex:
        log.warning("[bonus] не удалось уведомить user=%s: %s", user_id, html.escape(repr(ex)))
