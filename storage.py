"""
storage.py — единое хранилище профилей, баланса, транзакций и истории
игровых раундов. In-memory кэш + постоянное хранение в SQLite (storage.db),
так что баланс и история переживают перезапуск бота.

ПОЧЕМУ ЭТОТ ФАЙЛ ВООБЩЕ ПОЯВИЛСЯ
---------------------------------
Раньше USER_PROFILES и связанные функции (get_profile_stats, adjust_balance
и т.д.) были определены прямо в main.py, а games.py обращался к ним через
`from main import ...` внутри своих методов (см. BettingGame.get_balance и
похожие). Из-за этого баланс в профиле и баланс в играх расходились:

  1. Вы запускаете `python main.py` — интерпретатор загружает этот файл как
     модуль с именем "__main__" и создаёт словарь USER_PROFILES внутри него.
  2. Когда где-то внутри выполнения вызывается `from main import ...`
     (например, из games.py), Python видит, что модуля с именем "main" ещё
     нет в sys.modules, и ЗАНОВО выполняет весь файл main.py — на этот раз
     под именем "main". Все top-level переменные, включая USER_PROFILES,
     создаются повторно, уже второй, независимый экземпляр.
  3. В результате получаются ДВА словаря USER_PROFILES: один живёт в
     sys.modules["__main__"] (его использует main.py «сам с собой»), другой
     в sys.modules["main"] (его использует games.py). Баланс, изменённый
     через один словарь, не виден через другой — отсюда и расхождение.

Чтобы это исправить, всё хранилище вынесено в отдельный модуль storage.py.
Он НЕ импортирует ни main.py, ни games.py, поэтому какой бы файл его ни
импортировал и сколько бы раз — Python подгрузит storage.py только один
раз (под именем "storage") и будет переиспользовать один и тот же модуль
из sys.modules. Соответственно и словари USER_PROFILES / USER_TRANSACTIONS /
USER_GAME_ROUNDS внутри него — единственные в своём роде, общие для
main.py и games.py. Баланс теперь всегда один.

ПОЧЕМУ ТЕПЕРЬ ЕЩЁ И SQLITE
---------------------------------
Раньше USER_PROFILES / USER_TRANSACTIONS / USER_GAME_ROUNDS были ПРОСТО
словарями в памяти процесса — при перезапуске бота (деплой, падение,
`Ctrl+C`) все балансы, история пополнений/выводов и статистика игр
обнулялись.

Теперь это по-прежнему обычные словари в памяти (весь код бота, который
делает `profile["balance"] += ...` или
`USER_TRANSACTIONS.setdefault(uid, []).append(...)`, работает БЕЗ ИЗМЕНЕНИЙ
и по-прежнему видит мгновенные изменения через общий объект), но каждое
изменение сразу же пишется («write-through») в SQLite-базу storage.db
рядом с этим файлом. При старте бота эта база читается целиком обратно в
USER_PROFILES / USER_TRANSACTIONS / USER_GAME_ROUNDS, так что после
перезапуска ничего не пропадает.

Работает это благодаря тому, что значения в этих словарях — не «голые»
dict/list, а маленькие подклассы _ProfileDict / _TxList / _RoundList,
которые перехватывают `__setitem__` / `append` и синхронно пишут
изменение в БД, прежде чем (или сразу после того как) применить его к
самому объекту в памяти. Остальной код бота ничего об этом не знает и не
должен знать — он просто продолжает работать с обычными dict/list.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).with_name("storage.db")


# --------------------------------------------------------------------------
# База (SQLite) — тот же паттерн подключения, что и в bonus.py: свежее
# соединение на каждую операцию (sqlite3.Connection не расшаривается между
# потоками), WAL для быстрых коммитов, BEGIN IMMEDIATE для записи, чтобы
# параллельные записи не гонялись за блокировкой, а просто вставали в очередь.
# --------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _tx():
    """Атомарная транзакция записи (BEGIN IMMEDIATE)."""
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
        boot.execute("PRAGMA journal_mode=WAL")  # режим хранится в самом файле БД
    finally:
        boot.close()

    with _tx() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 0
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                ts        REAL NOT NULL,
                amount    REAL NOT NULL,
                type      TEXT NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id)")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS game_rounds (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                ts        REAL NOT NULL,
                bet       REAL NOT NULL,
                win       REAL NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_game_rounds_user ON game_rounds(user_id)")


def _db_set_balance(user_id: int, balance: float) -> None:
    with _tx() as c:
        c.execute(
            """
            INSERT INTO profiles (user_id, balance) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance
            """,
            (user_id, balance),
        )


def _db_insert_transaction(user_id: int, tx: dict) -> None:
    with _tx() as c:
        c.execute(
            "INSERT INTO transactions (user_id, ts, amount, type) VALUES (?, ?, ?, ?)",
            (user_id, tx["timestamp"].timestamp(), tx["amount"], tx["type"]),
        )


def _db_insert_round(user_id: int, round_: dict) -> None:
    with _tx() as c:
        c.execute(
            "INSERT INTO game_rounds (user_id, ts, bet, win) VALUES (?, ?, ?, ?)",
            (user_id, round_["timestamp"].timestamp(), round_["bet"], round_["win"]),
        )


# --------------------------------------------------------------------------
# Профили пользователей
# --------------------------------------------------------------------------
# USER_PROFILES[user_id] — словарь с полем "balance" (число) и подтягиваемыми
# по требованию полями "deposits" / "withdrawals" / "turnover" (см.
# get_profile_stats ниже). Балансом можно управлять напрямую через
# profile["balance"] += ... — это тот же объект, что лежит в USER_PROFILES,
# изменения сохраняются и в памяти, и в БД.


class _ProfileDict(dict):
    """Профиль пользователя. При любой записи в ключ "balance" синхронно
    сохраняет новое значение в SQLite — этого достаточно, чтобы работали
    все существующие в боте паттерны вида `profile["balance"] += amount`,
    `stats["balance"] -= amount` и т.п., без изменений в main.py/games.py."""

    def __init__(self, user_id: int, balance: float = 0.0):
        super().__init__(balance=balance)
        self._user_id = user_id

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "balance":
            _db_set_balance(self._user_id, value)


USER_PROFILES: dict[int, dict[str, float]] = {}


def _ensure_profile(user_id: int) -> dict[str, float]:
    """Возвращает профиль пользователя, создавая его при первом обращении."""
    profile = USER_PROFILES.get(user_id)
    if profile is None:
        profile = _ProfileDict(user_id, 0.0)
        USER_PROFILES[user_id] = profile
    return profile


# --------------------------------------------------------------------------
# Транзакции (депозиты, выводы, чеки, начисления/списания админом и т.п.)
# --------------------------------------------------------------------------
# USER_TRANSACTIONS[user_id] — список словарей вида:
#   {"timestamp": datetime, "amount": float, "type": str}
# где "type" — один из: "deposit", "withdraw", "check_activation",
# "check_create", "check_refund", "admin_grant", "admin_deduct" и т.п.


class _TxList(list):
    """Список транзакций одного пользователя: append() сразу пишет строку в БД."""

    def __init__(self, user_id: int, items=()):
        super().__init__(items)
        self._user_id = user_id

    def append(self, item):
        super().append(item)
        _db_insert_transaction(self._user_id, item)


class _TxStore(dict):
    """dict[user_id -> _TxList]. Переопределён только setdefault — именно он
    используется по всей кодовой базе как `USER_TRANSACTIONS.setdefault(uid, []).append(...)`,
    и должен при первом обращении создавать персистентный _TxList, а не голый list."""

    def setdefault(self, user_id, default=None):
        if user_id not in self:
            self[user_id] = _TxList(user_id, default or [])
        return self[user_id]


USER_TRANSACTIONS: dict[int, list[dict]] = _TxStore()


# --------------------------------------------------------------------------
# История игровых раундов — нужна для топа игроков (оборот/выигрыши/кол-во
# игр) и для оборота в статистике профиля.
# --------------------------------------------------------------------------
# USER_GAME_ROUNDS[user_id] — список словарей вида:
#   {"timestamp": datetime, "bet": float, "win": float}


class _RoundList(list):
    """Список сыгранных раундов одного пользователя: append() сразу пишет строку в БД."""

    def __init__(self, user_id: int, items=()):
        super().__init__(items)
        self._user_id = user_id

    def append(self, item):
        super().append(item)
        _db_insert_round(self._user_id, item)


class _RoundStore(dict):
    """Аналог _TxStore, но для USER_GAME_ROUNDS."""

    def setdefault(self, user_id, default=None):
        if user_id not in self:
            self[user_id] = _RoundList(user_id, default or [])
        return self[user_id]


USER_GAME_ROUNDS: dict[int, list[dict]] = _RoundStore()


def log_game_round(user_id: int, bet: float, win: float) -> None:
    """Записывает один сыгранный раунд (ставка/выигрыш) — вызывается из
    games.py после каждой игры."""
    USER_GAME_ROUNDS.setdefault(user_id, []).append(
        {"timestamp": datetime.now(timezone.utc), "bet": bet, "win": win}
    )


# --------------------------------------------------------------------------
# Периоды для статистики профиля ("День" / "Неделя" / "Месяц")
# --------------------------------------------------------------------------

PERIOD_DAYS: dict[str, int] = {"day": 1, "week": 7, "month": 30}
PERIOD_LABELS: dict[str, str] = {"day": "День", "week": "Неделя", "month": "Месяц"}


def _sum_transactions(user_id: int, tx_type: str, cutoff: datetime | None) -> float:
    total = 0.0
    for tx in USER_TRANSACTIONS.get(user_id, []):
        if cutoff and tx["timestamp"] < cutoff:
            continue
        if tx["type"] == tx_type:
            total += tx["amount"]
    return total


def _sum_turnover(user_id: int, cutoff: datetime | None) -> float:
    total = 0.0
    for round_ in USER_GAME_ROUNDS.get(user_id, []):
        if cutoff and round_["timestamp"] < cutoff:
            continue
        total += round_["bet"]
    return total


def get_period_stats(user_id: int, period: str) -> dict[str, float]:
    """Депозиты/выводы/оборот пользователя за period ("day"/"week"/"month";
    любой другой ключ, включая отсутствующий, трактуется как «за всё
    время»)."""
    days = PERIOD_DAYS.get(period)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    return {
        "deposits": _sum_transactions(user_id, "deposit", cutoff),
        "withdrawals": _sum_transactions(user_id, "withdraw", cutoff),
        "turnover": _sum_turnover(user_id, cutoff),
    }


def get_profile_stats(user_id: int) -> dict[str, float]:
    """Профиль пользователя: баланс + статистика за всё время.

    Возвращает ту же изменяемую запись, что хранится в USER_PROFILES, — это
    важно: во многих местах бота баланс правится напрямую через
    `get_profile_stats(uid)["balance"] += ...`, и изменения должны
    сохраняться и в общем хранилище в памяти, и в БД.
    """
    profile = _ensure_profile(user_id)
    totals = get_period_stats(user_id, "all")
    profile["deposits"] = totals["deposits"]
    profile["withdrawals"] = totals["withdrawals"]
    profile["turnover"] = totals["turnover"]
    return profile


# --------------------------------------------------------------------------
# Начисление / списание баланса администратором
# --------------------------------------------------------------------------
# adjust_balance всегда принимает положительную сумму — знак определяется
# по `reason`. Сейчас единственная «списывающая» причина — "admin_deduct"
# (используется в /admin панели main.py); все остальные причины (например,
# "admin_grant") увеличивают баланс.

_DEDUCT_REASONS = {"admin_deduct"}


def adjust_balance(user_id: int, amount: float, reason: str) -> float:
    """Начисляет (или, если reason — списывающая причина, списывает)
    `amount` с баланса пользователя и возвращает новый баланс. Также
    записывает транзакцию для истории/статистики."""
    profile = _ensure_profile(user_id)
    signed_amount = -amount if reason in _DEDUCT_REASONS else amount

    profile["balance"] += signed_amount
    USER_TRANSACTIONS.setdefault(user_id, []).append(
        {
            "timestamp": datetime.now(timezone.utc),
            "amount": abs(signed_amount),
            "type": reason,
        }
    )
    return profile["balance"]


# --------------------------------------------------------------------------
# Загрузка БД в память при старте
# --------------------------------------------------------------------------


def _load_all() -> None:
    with _read() as c:
        for row in c.execute("SELECT user_id, balance FROM profiles"):
            USER_PROFILES[row["user_id"]] = _ProfileDict(row["user_id"], row["balance"])

        tx_by_user: dict[int, list[dict]] = {}
        for row in c.execute(
            "SELECT user_id, ts, amount, type FROM transactions ORDER BY id"
        ):
            tx_by_user.setdefault(row["user_id"], []).append(
                {
                    "timestamp": datetime.fromtimestamp(row["ts"], tz=timezone.utc),
                    "amount": row["amount"],
                    "type": row["type"],
                }
            )
        for uid, items in tx_by_user.items():
            USER_TRANSACTIONS[uid] = _TxList(uid, items)

        rounds_by_user: dict[int, list[dict]] = {}
        for row in c.execute("SELECT user_id, ts, bet, win FROM game_rounds ORDER BY id"):
            rounds_by_user.setdefault(row["user_id"], []).append(
                {
                    "timestamp": datetime.fromtimestamp(row["ts"], tz=timezone.utc),
                    "bet": row["bet"],
                    "win": row["win"],
                }
            )
        for uid, items in rounds_by_user.items():
            USER_GAME_ROUNDS[uid] = _RoundList(uid, items)


_db_init()
_load_all()
