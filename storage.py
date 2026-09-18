"""
storage.py — единое in-memory хранилище профилей, баланса, транзакций и
истории игровых раундов.

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
"""

from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------
# Профили пользователей
# --------------------------------------------------------------------------
# USER_PROFILES[user_id] — словарь с полем "balance" (число) и подтягиваемыми
# по требованию полями "deposits" / "withdrawals" / "turnover" (см.
# get_profile_stats ниже). Балансом можно управлять напрямую через
# profile["balance"] += ... — это тот же объект, что лежит в USER_PROFILES,
# изменения сохраняются.

USER_PROFILES: dict[int, dict[str, float]] = {}


def _ensure_profile(user_id: int) -> dict[str, float]:
    """Возвращает профиль пользователя, создавая его при первом обращении."""
    profile = USER_PROFILES.get(user_id)
    if profile is None:
        profile = {"balance": 0.0}
        USER_PROFILES[user_id] = profile
    return profile


# --------------------------------------------------------------------------
# Транзакции (депозиты, выводы, чеки, начисления/списания админом и т.п.)
# --------------------------------------------------------------------------
# USER_TRANSACTIONS[user_id] — список словарей вида:
#   {"timestamp": datetime, "amount": float, "type": str}
# где "type" — один из: "deposit", "withdraw", "check_activation",
# "check_create", "check_refund", "admin_grant", "admin_deduct" и т.п.

USER_TRANSACTIONS: dict[int, list[dict]] = {}


# --------------------------------------------------------------------------
# История игровых раундов — нужна для топа игроков (оборот/выигрыши/кол-во
# игр) и для оборота в статистике профиля.
# --------------------------------------------------------------------------
# USER_GAME_ROUNDS[user_id] — список словарей вида:
#   {"timestamp": datetime, "bet": float, "win": float}

USER_GAME_ROUNDS: dict[int, list[dict]] = {}


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
    сохраняться в общем хранилище.
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
