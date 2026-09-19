import asyncio
import logging
import random
import string
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------

# Вставьте сюда токен вашего бота, полученный у @BotFather
BOT_TOKEN = "8841055640:AAE65cYHaE9XVEo2fQLwZ5kPxrR1Fncqm5Q"

# ID администраторов бота (Telegram user_id). Узнать свой ID можно, например,
# у @userinfobot. Добавьте сюда ID всех, кому нужен доступ к админ-панели.
ADMIN_IDS: set[int] = {8118184388}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# Юзернейм бота, определяется автоматически при запуске (см. main()) — нужен
# для формирования ссылок активации чеков вида t.me/<bot>?start=check_<code>
BOT_USERNAME: str | None = None


def get_check_link(code: str) -> str:
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}?start=check_{code}"
    return f"t.me/<bot>?start=check_{code}"


IN_DEV_TEXT = "🚧 Этот раздел находится в разработке.\nСкоро здесь появится функционал!"

SUPPORT_TEXT = (
    '<tg-emoji emoji-id="5812150667812280629">🛠</tg-emoji> <b>Поддержка</b>\n\n'
    "<i>Возникли вопросы или проблема с игрой, депозитом или выводом?\n"
    "Напишите нам — ответим как можно быстрее.</i>\n\n"
    "└ Оператор: @luckydicesupport\n"
    "└ Среднее время ответа: ~10 минут"
)

# --------------------------------------------------------------------------
# Хранилище профилей / транзакций / баланса
# --------------------------------------------------------------------------
# ВАЖНО: этот блок раньше был определён прямо здесь, в main.py, а games.py
# обращался к нему через `from main import get_profile_stats` внутри своих
# методов. Из-за этого при запуске `python main.py` баланс в профиле и в
# играх мог расходиться — main.py по пути импортируется ещё раз (уже как
# модуль "main", а не "__main__"), и получается два разных словаря
# USER_PROFILES. Подробности — в шапке storage.py.
#
# Теперь всё, что нужно и main.py, и games.py, лежит в одном месте —
# storage.py, — и импортируется отсюда, чтобы обработчики ниже (профиль,
# админка, чеки) продолжали работать без изменений.

import ui
from ui import edit_any, edit_by_id, send_menu, show_menu_screen
from storage import (
    USER_PROFILES,
    get_profile_stats,
    USER_TRANSACTIONS,
    PERIOD_DAYS,
    PERIOD_LABELS,
    get_period_stats,
    adjust_balance,
    USER_GAME_ROUNDS,
    log_game_round,
)

# Бонусный баланс (см. bonus.py). games.py сам списывает ставки с бонусного кошелька и учитывает
# отыгрыш (bonus.try_spend / settle / refund) — хук на storage.log_game_round больше не нужен.
import bonus as bonus_module

bonus_module.ALERT_ADMIN_IDS = set(ADMIN_IDS)

# Обязательная подписка на канал(ы) (см. subscription.py). Список каналов админ ведёт
# через админ-панель; SubscriptionMiddleware блокирует весь бот для тех, кто подписан
# не на все каналы. Админов модуль не проверяет никогда.
import subscription as subscription_module

subscription_module.ADMIN_IDS = set(ADMIN_IDS)


# --------------------------------------------------------------------------
# Хранилище данных о пользователях (временное, in-memory) — чтобы можно было
# показать ник/имя игрока в топе и других списках.
# --------------------------------------------------------------------------

USER_INFO: dict[int, dict[str, str | None]] = {}


def remember_user(user) -> None:
    """Запоминает имя и юзернейм пользователя (для отображения, например, в топе)."""
    USER_INFO[user.id] = {
        "full_name": user.full_name,
        "username": user.username,
    }


def get_display_name(user_id: int) -> str:
    """Ник (имя) пользователя, а если его нет — юзернейм."""
    info = USER_INFO.get(user_id)
    if not info:
        return f"Игрок {user_id}"

    full_name = info.get("full_name")
    if full_name:
        return full_name

    username = info.get("username")
    if username:
        return f"@{username}"

    return f"Игрок {user_id}"


TOP_PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "all": None}
TOP_PERIOD_LABELS = {"day": "День", "week": "Неделя", "month": "Месяц", "all": "Всё время"}

TOP_CATEGORY_LABELS = {
    "turnover": "Оборот",
    "wins": "Выигрыши",
    "games": "Кол-во игр",
}
# Эмодзи подобраны из уже используемых в боте (профиль/статистика/меню)
TOP_CATEGORY_EMOJI_IDS = {
    "turnover": "5778421276024509124",  # 💰 — как в "Оборот" в профиле/статистике
    "wins": "5902206159095339799",  # 🤑 — денежный выигрыш
    "games": "5260547274957672345",  # 🎲 — как в "Lucky Dice"
}
# Эмодзи периодов — те же, что в статистике
TOP_PERIOD_EMOJI_ID = "5890937706803894250"

# Emoji ID для позиций 1–10 в топе
TOP_POSITION_EMOJI_IDS = [
    "5794164805065514131",
    "5794085322400733645",
    "5794280000383358988",
    "5794241397217304511",
    "5793985348446984682",
    "5794324702402976226",
    "5793942849745591465",
    "5793926687783655907",
    "5793979472931723221",
    "5794375786743995258",
]


def get_top_players(category: str, period: str, limit: int = 10) -> list[tuple[int, float]]:
    """Считает топ игроков по выбранной категории (оборот/выигрыши/кол-во игр) за период."""
    days = TOP_PERIOD_DAYS.get(period)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None

    aggregated: dict[int, float] = {}
    for user_id, rounds in USER_GAME_ROUNDS.items():
        turnover = 0.0
        wins = 0.0
        games = 0

        for round_ in rounds:
            if cutoff and round_["timestamp"] < cutoff:
                continue
            turnover += round_["bet"]
            wins += round_["win"]
            games += 1

        if games == 0:
            continue

        if category == "turnover":
            value = turnover
        elif category == "wins":
            value = wins
        else:  # "games"
            value = float(games)

        aggregated[user_id] = value

    return sorted(aggregated.items(), key=lambda item: item[1], reverse=True)[:limit]


def get_global_stats() -> dict[str, float]:
    """Считает общую статистику по всем пользователям — для админ-панели."""
    total_users = len(USER_INFO) or len(USER_PROFILES)
    total_balance = sum(profile["balance"] for profile in USER_PROFILES.values())

    total_deposits = 0.0
    total_withdrawals = 0.0
    for txs in USER_TRANSACTIONS.values():
        for tx in txs:
            if tx["type"] == "deposit":
                total_deposits += tx["amount"]
            elif tx["type"] == "withdraw":
                total_withdrawals += tx["amount"]

    total_games = 0
    total_bet = 0.0
    total_win = 0.0
    for rounds in USER_GAME_ROUNDS.values():
        for round_ in rounds:
            total_games += 1
            total_bet += round_["bet"]
            total_win += round_["win"]

    return {
        "total_users": total_users,
        "total_balance": total_balance,
        "total_deposits": total_deposits,
        "total_withdrawals": total_withdrawals,
        "total_games": total_games,
        "total_bet": total_bet,
        "total_win": total_win,
        "casino_profit": total_bet - total_win,
    }


# --------------------------------------------------------------------------
# Хранилище чеков (временное, in-memory)
# --------------------------------------------------------------------------
# NOTE: заглушка, как и остальные хранилища. С появлением БД чеки нужно
# будет хранить там же, с теми же полями.

CHECKS: dict[str, dict] = {}

CHECK_RESTRICTION_LABELS = {
    "none": "Без ограничений",
    "turnover_day": "Оборот за день",
    "turnover_week": "Оборот за неделю",
    "deposits_total": "Сумма депозитов",
}


def generate_check_code() -> str:
    """Генерирует уникальный код чека вида LD-XXXXXXXX."""
    while True:
        code = "LD-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        if code not in CHECKS:
            return code


def get_total_deposits(user_id: int) -> float:
    """Сумма всех депозитов пользователя за всё время."""
    return sum(
        tx["amount"] for tx in USER_TRANSACTIONS.get(user_id, []) if tx["type"] == "deposit"
    )


def check_restriction_status(user_id: int, check: dict) -> tuple[bool, str]:
    """Проверяет, выполняет ли пользователь условие активации чека.
    Возвращает (выполнено ли условие, текст с требованием и текущим значением)."""
    restriction_type = check["restriction_type"]
    value = check["restriction_value"]

    if restriction_type == "none":
        return True, ""

    if restriction_type == "turnover_day":
        current = get_period_stats(user_id, "day")["turnover"]
        label = "оборот за день"
    elif restriction_type == "turnover_week":
        current = get_period_stats(user_id, "week")["turnover"]
        label = "оборот за неделю"
    else:  # "deposits_total"
        current = get_total_deposits(user_id)
        label = "сумма депозитов"

    ok = current >= value
    description = f"Нужно: {label} от ${value:,.2f}. У вас: ${current:,.2f}"
    return ok, description


def create_check(
    creator_id: int,
    amount: float,
    max_activations: int,
    restriction_type: str,
    restriction_value: float,
) -> dict:
    """Создаёт новый чек и сохраняет его в хранилище."""
    check = {
        "code": generate_check_code(),
        "creator_id": creator_id,
        "amount": amount,
        "max_activations": max_activations,
        "activations_used": 0,
        "activated_by": set(),
        "created_at": datetime.now(timezone.utc),
        "active": True,
        "restriction_type": restriction_type,
        "restriction_value": restriction_value,
    }
    CHECKS[check["code"]] = check
    return check


def activate_check(user_id: int, code: str) -> tuple[bool, str, float]:
    """Пытается активировать чек. Возвращает (успех, сообщение, зачисленная сумма)."""
    check = CHECKS.get(code.strip().upper())
    if not check or not check["active"]:
        return False, "Чек не найден или уже недействителен.", 0.0

    if check["creator_id"] == user_id:
        return False, "Нельзя активировать собственный чек.", 0.0

    if user_id in check["activated_by"]:
        return False, "Вы уже активировали этот чек.", 0.0

    if check["activations_used"] >= check["max_activations"]:
        check["active"] = False
        return False, "Все активации этого чека уже использованы.", 0.0

    ok, description = check_restriction_status(user_id, check)
    if not ok:
        return False, f"Не выполнено условие активации.\n{description}", 0.0

    amount = check["amount"]
    profile = get_profile_stats(user_id)
    profile["balance"] += amount
    USER_TRANSACTIONS.setdefault(user_id, []).append(
        {"timestamp": datetime.now(timezone.utc), "amount": amount, "type": "check_activation"}
    )

    check["activations_used"] += 1
    check["activated_by"].add(user_id)
    if check["activations_used"] >= check["max_activations"]:
        check["active"] = False

    return True, "Чек успешно активирован!", amount


# --------------------------------------------------------------------------
# Форматирование
# --------------------------------------------------------------------------


def tree_block(lines: list[str]) -> str:
    """Оформляет список строк в виде соединённого блока: ┌ / ├ / └.

    Используется для любых статистик/списков (профиль, топ, статистика и т.д.),
    чтобы не дублировать разметку вручную в каждом хендлере.
    """
    if not lines:
        return ""
    if len(lines) == 1:
        return f"└ {lines[0]}"

    result = [f"┌ {lines[0]}"]
    result += [f"├ {line}" for line in lines[1:-1]]
    result.append(f"└ {lines[-1]}")
    return "\n".join(result)


# --------------------------------------------------------------------------
# Клавиатуры
# --------------------------------------------------------------------------


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Меню",
                    style="primary",
                    icon_custom_emoji_id="5257965174979042426",
                ),
                KeyboardButton(
                    text="Игры",
                    style="primary",
                    icon_custom_emoji_id="5350708744558753862",
                ),
                KeyboardButton(
                    text="Партнеры",
                    style="primary",
                    icon_custom_emoji_id="5258362837411045098",
                ),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите раздел...",
    )


def menu_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Профиль",
                    callback_data="menu:profile",
                    icon_custom_emoji_id="5316727448644103237",
                ),
                InlineKeyboardButton(
                    text="Статистика",
                    callback_data="menu:stats",
                    icon_custom_emoji_id="5258330865674494479",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Топ",
                    callback_data="menu:top",
                    icon_custom_emoji_id="6037083366438737901",
                ),
                InlineKeyboardButton(
                    text="Чеки",
                    callback_data="menu:checks",
                    icon_custom_emoji_id="6037175527846975726",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Поддержка",
                    callback_data="menu:support",
                    icon_custom_emoji_id="5812150667812280629",
                ),
            ],
        ]
    )


def profile_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Пополнить",
                    callback_data="profile:deposit",
                    icon_custom_emoji_id="5879814368572478751",
                ),
                InlineKeyboardButton(
                    text="Вывести",
                    callback_data="profile:withdraw",
                    icon_custom_emoji_id="5890848474563352982",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="menu:back",
                    icon_custom_emoji_id="6039539366177541657",
                ),
            ],
        ]
    )


def stats_period_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="День",
                    callback_data="stats:day",
                    icon_custom_emoji_id="5890937706803894250",
                ),
                InlineKeyboardButton(
                    text="Неделя",
                    callback_data="stats:week",
                    icon_custom_emoji_id="5890937706803894250",
                ),
                InlineKeyboardButton(
                    text="Месяц",
                    callback_data="stats:month",
                    icon_custom_emoji_id="5890937706803894250",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="menu:back",
                    icon_custom_emoji_id="6039539366177541657",
                ),
            ],
        ]
    )


def top_keyboard(category: str, period: str) -> InlineKeyboardMarkup:
    def category_button(cat: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=("• " if cat == category else "") + TOP_CATEGORY_LABELS[cat],
            callback_data=f"top:{cat}:{period}",
            icon_custom_emoji_id=TOP_CATEGORY_EMOJI_IDS[cat],
        )

    def period_button(per: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=("• " if per == period else "") + TOP_PERIOD_LABELS[per],
            callback_data=f"top:{category}:{per}",
            icon_custom_emoji_id=TOP_PERIOD_EMOJI_ID,
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [category_button("turnover"), category_button("wins"), category_button("games")],
            [period_button("day"), period_button("week"), period_button("month"), period_button("all")],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="menu:back",
                    icon_custom_emoji_id="6039539366177541657",
                ),
            ],
        ]
    )


def support_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="menu:back",
                    icon_custom_emoji_id="6039539366177541657",
                ),
            ],
        ]
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Выдать баланс",
                    callback_data="admin:grant",
                    icon_custom_emoji_id="5879814368572478751",  # как «Пополнить» в профиле
                ),
                InlineKeyboardButton(
                    text="Списать баланс",
                    callback_data="admin:deduct",
                    icon_custom_emoji_id="5890848474563352982",  # как «Вывести» в профиле
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Общая статистика",
                    callback_data="admin:stats",
                    icon_custom_emoji_id="5258330865674494479",  # как «Статистика» в меню
                ),
                InlineKeyboardButton(text="Найти пользователя", callback_data="admin:find"),
            ],
            [
                InlineKeyboardButton(text="Рассылка", callback_data="admin:broadcast"),
            ],
            [
                InlineKeyboardButton(text="🔒 Обязательная подписка", callback_data="admin:subs"),
            ],
            [
                InlineKeyboardButton(text="Закрыть", callback_data="admin:close"),
            ],
        ]
    )


def admin_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin:cancel")]]
    )


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="admin:back",
                    icon_custom_emoji_id="6039539366177541657",
                )
            ]
        ]
    )


# --------------------------------------------------------------------------
# Админ-панель — форматирование и состояния FSM
# --------------------------------------------------------------------------


class AdminStates(StatesGroup):
    grant_user_id = State()
    grant_amount = State()
    deduct_user_id = State()
    deduct_amount = State()
    find_user_id = State()
    broadcast_text = State()


def format_global_stats_text() -> str:
    stats = get_global_stats()

    users_block = tree_block(
        [
            f"<b>Пользователей:</b> {stats['total_users']}",
            f"<b>Суммарный баланс:</b> ${stats['total_balance']:,.2f}",
        ]
    )
    money_block = tree_block(
        [
            f"<b>Депозиты (всего):</b> ${stats['total_deposits']:,.2f}",
            f"<b>Выводы (всего):</b> ${stats['total_withdrawals']:,.2f}",
        ]
    )
    games_block = tree_block(
        [
            f"<b>Игр сыграно:</b> {stats['total_games']}",
            f"<b>Оборот по играм:</b> ${stats['total_bet']:,.2f}",
            f"<b>Выплачено выигрышей:</b> ${stats['total_win']:,.2f}",
            f"<b>Прибыль казино:</b> ${stats['casino_profit']:,.2f}",
        ]
    )

    return (
        "⚙️ <b>Общая статистика</b>\n\n"
        f"{users_block}\n\n"
        f"{money_block}\n\n"
        f"{games_block}"
    )


# --------------------------------------------------------------------------
# Раздел «Чеки» — клавиатуры и состояния FSM
# --------------------------------------------------------------------------


class CheckStates(StatesGroup):
    create_count = State()
    create_amount = State()
    create_restriction_value = State()


# Эмодзи для раздела «Чеки»: часть — свои, часть переиспользована из профиля/меню
CHECK_CREATE_EMOJI_ID = "5258108352008823107"  # ➕
CHECK_MINE_EMOJI_ID = "5280962371207077415"  # 💎
CHECK_NONE_RESTRICTION_EMOJI_ID = "5260342697075416641"  # ❌
CHECK_TURNOVER_EMOJI_ID = "5778421276024509124"  # 💰 как «Оборот» в профиле
CHECK_DEPOSITS_EMOJI_ID = "5902206159095339799"  # 🤑 как «Всего депозитов» в профиле
CHECK_SINGLE_TYPE_EMOJI_ID = TOP_POSITION_EMOJI_IDS[0]  # 1️⃣ — тот же, что в топе


def checks_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Создать чек",
                    callback_data="checks:create",
                    icon_custom_emoji_id=CHECK_CREATE_EMOJI_ID,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Мои чеки",
                    callback_data="checks:mine",
                    icon_custom_emoji_id=CHECK_MINE_EMOJI_ID,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="menu:back",
                    icon_custom_emoji_id="6039539366177541657",
                ),
            ],
        ]
    )


def checks_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="checks:cancel")]]
    )


def check_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Одноразовый",
                    callback_data="checks:type:single",
                    icon_custom_emoji_id=CHECK_SINGLE_TYPE_EMOJI_ID,
                ),
                InlineKeyboardButton(
                    text="Многоразовый",
                    callback_data="checks:type:multi",
                    icon_custom_emoji_id="5805331990618053402",
                ),
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="checks:cancel")],
        ]
    )


def check_restriction_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Без ограничений",
                    callback_data="checks:restriction:none",
                    icon_custom_emoji_id=CHECK_NONE_RESTRICTION_EMOJI_ID,
                )
            ],
            [
                InlineKeyboardButton(
                    text="Оборот за день",
                    callback_data="checks:restriction:turnover_day",
                    icon_custom_emoji_id=CHECK_TURNOVER_EMOJI_ID,
                ),
                InlineKeyboardButton(
                    text="Оборот за неделю",
                    callback_data="checks:restriction:turnover_week",
                    icon_custom_emoji_id=CHECK_TURNOVER_EMOJI_ID,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Сумма депозитов",
                    callback_data="checks:restriction:deposits_total",
                    icon_custom_emoji_id=CHECK_DEPOSITS_EMOJI_ID,
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="checks:cancel")],
        ]
    )


def check_created_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Готово",
                    callback_data="checks:back",
                    icon_custom_emoji_id="5774022692642492953",
                )
            ]
        ]
    )


def my_checks_keyboard(codes: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"Деактивировать {code}",
                callback_data=f"checks:deactivate:{code}",
                icon_custom_emoji_id=CHECK_NONE_RESTRICTION_EMOJI_ID,
            )
        ]
        for code in codes
    ]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="checks:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------
# Хендлеры
# --------------------------------------------------------------------------

router = Router()


def bonus_profile_lines(user_id: int) -> list[str]:
    """Строки бонусного баланса для профиля: сумма 💎 и сколько ещё нужно отыграть."""
    bonus = bonus_module.get_summary(user_id)
    lines = [f"{bonus_module.BONUS_ICON} <b>Бонусный баланс:</b> ${bonus['balance']:.2f}"]
    if bonus["balance"] > 0:
        lines.append(
            '<tg-emoji emoji-id="5778421276024509124">💰</tg-emoji> '
            f"<b>Осталось отыграть:</b> ${bonus['remaining']:.2f}"
        )
    return lines


def format_profile_text(user_id: int, full_name: str, username: str | None) -> str:
    stats = get_profile_stats(user_id)
    username_line = f"@{username}" if username else "не указан"

    info_block = tree_block(
        [
            '<tg-emoji emoji-id="5890925363067886150">✨</tg-emoji> '
            f"<b>ID:</b> <code>{user_id}</code>",
            '<tg-emoji emoji-id="5864019342873598613">🧠</tg-emoji> '
            f"<b>Имя:</b> {full_name}",
            '<tg-emoji emoji-id="6039451237743595514">📎</tg-emoji> '
            f"<b>Юзернейм:</b> {username_line}",
        ]
    )
    stats_block = tree_block(
        [
            '<tg-emoji emoji-id="5769126056262898415">👛</tg-emoji> '
            f"<b>Баланс:</b> ${stats['balance']:.2f}",
            *bonus_profile_lines(user_id),
            '<tg-emoji emoji-id="5902206159095339799">🤑</tg-emoji> '
            f"<b>Всего депозитов:</b> ${stats['deposits']:.2f}",
            '<tg-emoji emoji-id="5890848474563352982">🪙</tg-emoji> '
            f"<b>Всего выводов:</b> ${stats['withdrawals']:.2f}",
            '<tg-emoji emoji-id="5778421276024509124">💰</tg-emoji> '
            f"<b>Оборот:</b> ${stats['turnover']:.2f}",
        ]
    )

    return (
        '<tg-emoji emoji-id="5316727448644103237">👤</tg-emoji> <b>Профиль</b>\n\n'
        f"{info_block}\n\n"
        f"{stats_block}"
    )


def format_stats_text(user_id: int, period: str) -> str:
    stats = get_period_stats(user_id, period)
    label = PERIOD_LABELS.get(period, "День")

    stats_block = tree_block(
        [
            '<tg-emoji emoji-id="5902206159095339799">🤑</tg-emoji> '
            f"<b>Депозиты:</b> ${stats['deposits']:.2f}",
            '<tg-emoji emoji-id="5890848474563352982">🪙</tg-emoji> '
            f"<b>Выводы:</b> ${stats['withdrawals']:.2f}",
            '<tg-emoji emoji-id="5778421276024509124">💰</tg-emoji> '
            f"<b>Оборот:</b> ${stats['turnover']:.2f}",
        ]
    )

    return (
        '<tg-emoji emoji-id="5890937706803894250">📅</tg-emoji> '
        f"<b>Статистика — {label}</b>\n\n"
        f"{stats_block}"
    )


def format_top_text(category: str, period: str) -> str:
    top_players = get_top_players(category, period)
    category_label = TOP_CATEGORY_LABELS.get(category, "Оборот")
    period_label = TOP_PERIOD_LABELS.get(period, "День")

    header = (
        '<tg-emoji emoji-id="6037083366438737901">🏆</tg-emoji> '
        f"<b>Топ — {category_label} — {period_label}</b>"
    )

    if not top_players:
        return f"{header}\n\n└ Нет данных за этот период."

    lines = []
    for index, (user_id, value) in enumerate(top_players):
        if index < len(TOP_POSITION_EMOJI_IDS):
            position = f'<tg-emoji emoji-id="{TOP_POSITION_EMOJI_IDS[index]}">{index + 1}️⃣</tg-emoji>'
        else:
            position = f"{index + 1}."

        name = get_display_name(user_id)
        value_text = f"{int(value)}" if category == "games" else f"${value:,.2f}"
        lines.append(f"{position} <b>{name}</b> — {value_text}")

    return f"{header}\n\n" + "\n".join(lines)


def format_checks_menu_text() -> str:
    return (
        '<tg-emoji emoji-id="6037175527846975726">🎫</tg-emoji> <b>Чеки</b>\n\n'
        "<i>Создавайте одноразовые и многоразовые чеки, чтобы делиться балансом "
        "с другими игроками — активация происходит по ссылке, в один тап.</i>"
    )


def format_check_card(check: dict) -> str:
    status = "🟢 Активен" if check["active"] else "🔴 Использован / выключен"
    restriction_label = CHECK_RESTRICTION_LABELS.get(check["restriction_type"], "Без ограничений")
    restriction_line = (
        f"{restriction_label} — от ${check['restriction_value']:,.2f}"
        if check["restriction_type"] != "none"
        else restriction_label
    )

    lines = [
        f"<b>Код:</b> <code>{check['code']}</code>",
        f"<b>Сумма за активацию:</b> ${check['amount']:,.2f}",
        f"<b>Активации:</b> {check['activations_used']}/{check['max_activations']}",
        f"<b>Условие:</b> {restriction_line}",
        f"<b>Статус:</b> {status}",
    ]
    card = tree_block(lines)

    if check["active"]:
        card += f"\n🔗 {get_check_link(check['code'])}"

    return card


def format_my_checks_text(user_id: int) -> str:
    my_checks = sorted(
        (c for c in CHECKS.values() if c["creator_id"] == user_id and c["active"]),
        key=lambda c: c["created_at"],
        reverse=True,
    )

    header = '<tg-emoji emoji-id="6037175527846975726">📄</tg-emoji> <b>Мои чеки</b>'
    if not my_checks:
        return f"{header}\n\n└ У вас нет активных чеков."

    blocks = [format_check_card(c) for c in my_checks[:10]]
    return f"{header}\n\n" + "\n\n".join(blocks)


def format_check_created_text(check: dict) -> str:
    restriction_label = CHECK_RESTRICTION_LABELS.get(check["restriction_type"], "Без ограничений")
    restriction_line = (
        f"{restriction_label} — от ${check['restriction_value']:,.2f}"
        if check["restriction_type"] != "none"
        else restriction_label
    )
    type_label = (
        "Одноразовый"
        if check["max_activations"] == 1
        else f"Многоразовый ({check['max_activations']} активаций)"
    )

    body = tree_block(
        [
            f"<b>Тип:</b> {type_label}",
            f"<b>Сумма за активацию:</b> ${check['amount']:,.2f}",
            f"<b>Условие активации:</b> {restriction_line}",
        ]
    )

    return (
        '<tg-emoji emoji-id="6037175527846975726">✅</tg-emoji> <b>Чек создан!</b>\n\n'
        f"{body}\n\n"
        "<i>Ссылка активации — перешлите её тому, кому хотите передать баланс. "
        "Достаточно перейти по ней и нажать «Старт»:</i>\n"
        f"{get_check_link(check['code'])}\n\n"
        f"<i>Код чека:</i> <code>{check['code']}</code>"
    )


async def send_start_welcome(bot: Bot, chat_id: int, user, payload: str | None) -> None:
    """Обрабатывает deep-link payload (если он был) и отправляет приветствие с меню.

    Вызывается из /start напрямую (когда подписка не нужна или уже выполнена), а
    также из subscription.py — после того, как пользователь подтвердил подписку
    кнопкой «Я подписался» (см. subscription_module.set_on_verified ниже)."""
    payload = payload or ""
    result_line = ""
    if payload.startswith("check_"):
        code = payload[len("check_") :]
        ok, msg, amount = activate_check(user.id, code)
        if ok:
            result_line = (
                '<tg-emoji emoji-id="6037175527846975726">✅</tg-emoji> '
                f"{msg}\nНа баланс зачислено: <b>${amount:,.2f}</b>\n\n"
            )
        else:
            result_line = f"❌ {msg}\n\n"
    elif payload.startswith("bcheck_"):
        ok, msg, amount = await asyncio.to_thread(
            bonus_module.activate_check, user.id, payload[len("bcheck_") :]
        )
        result_line = bonus_module.activation_text(amount) if ok else f"❌ {msg}\n\n"
    elif payload.startswith("ref_"):
        if await refs_module.bind_from_payload(bot, user, payload):
            result_line = "🤝 Вы присоединились по приглашению партнёра!\n\n"

    await bot.send_message(
        chat_id,
        f"{result_line}Привет, {user.full_name}! 👋\n\n"
        "Выберите раздел из меню ниже.",
        reply_markup=main_reply_keyboard(),
    )


# Подключаем shared-функцию выше как колбэк для subscription.py: она вызывает её
# после успешной проверки подписки (кнопка «Я подписался»), чтобы новый пользователь
# всё-таки получил приветствие/меню и, если был deep-link, — начисление по нему.
subscription_module.set_on_verified(send_start_welcome)


@router.message(CommandStart(deep_link=True))
async def cmd_start_deep_link(message: Message, command: CommandObject, state: FSMContext) -> None:
    remember_user(message.from_user)
    await state.clear()

    payload = command.args or ""

    missing = (
        []
        if is_admin(message.from_user.id)
        else await subscription_module.get_missing_channels(message.bot, message.from_user.id)
    )
    if missing:
        # Деплинк (например, чек) применится после подтверждения подписки — см. subscription.py.
        await state.update_data(pending_start_payload=payload)
        text, markup = subscription_module.format_required_screen(missing)
        await message.answer(text, reply_markup=markup)
        return

    await send_start_welcome(message.bot, message.chat.id, message.from_user, payload)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await state.clear()

    missing = (
        []
        if is_admin(message.from_user.id)
        else await subscription_module.get_missing_channels(message.bot, message.from_user.id)
    )
    if missing:
        text, markup = subscription_module.format_required_screen(missing)
        await message.answer(text, reply_markup=markup)
        return

    await send_start_welcome(message.bot, message.chat.id, message.from_user, None)


@router.message(F.text == "Меню")
async def show_menu(message: Message) -> None:
    remember_user(message.from_user)
    text = (
        '<tg-emoji emoji-id="5260547274957672345">🎲</tg-emoji> '
        "<b>Lucky Dice</b> — испытай удачу!\n\n"
        "<i>Здесь заводят игры, зарабатывают на партнёрке "
        "и следят за своим прогрессом.</i>\n\n"
        '<tg-emoji emoji-id="5886676966102274844">👆</tg-emoji> '
        "Выберите раздел ниже:"
    )
    await send_menu(message, text, menu_inline_keyboard())


@router.message(F.text == "Партнеры")
async def partners_section(message: Message) -> None:
    remember_user(message.from_user)
    await refs_module.show_partners(message)


@router.callback_query(F.data == "menu:profile")
async def profile_section(callback: CallbackQuery) -> None:
    user = callback.from_user
    remember_user(user)
    text = format_profile_text(user.id, user.full_name, user.username)

    await edit_any(callback.message, text, reply_markup=profile_inline_keyboard())
    await callback.answer()


@router.callback_query(F.data == "profile:deposit")
async def profile_deposit(callback: CallbackQuery, state: FSMContext) -> None:
    remember_user(callback.from_user)
    await payments_module.show_deposit_methods(callback, state)


@router.callback_query(F.data == "profile:withdraw")
async def profile_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    remember_user(callback.from_user)
    await payments_module.show_withdraw_methods(callback, state)


@router.callback_query(F.data == "menu:stats")
async def stats_section(callback: CallbackQuery) -> None:
    remember_user(callback.from_user)
    text = format_stats_text(callback.from_user.id, "day")
    await edit_any(callback.message, text, reply_markup=stats_period_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("stats:"))
async def stats_period_switch(callback: CallbackQuery) -> None:
    remember_user(callback.from_user)
    period = callback.data.split(":", 1)[1]
    text = format_stats_text(callback.from_user.id, period)
    await edit_any(callback.message, text, reply_markup=stats_period_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:top")
async def top_section(callback: CallbackQuery) -> None:
    remember_user(callback.from_user)
    text = format_top_text("turnover", "day")
    await edit_any(callback.message, text, reply_markup=top_keyboard("turnover", "day"))
    await callback.answer()


@router.callback_query(F.data.startswith("top:"))
async def top_switch(callback: CallbackQuery) -> None:
    remember_user(callback.from_user)
    _, category, period = callback.data.split(":", 2)
    text = format_top_text(category, period)
    await edit_any(callback.message, text, reply_markup=top_keyboard(category, period))
    await callback.answer()


@router.callback_query(F.data == "menu:support")
async def support_section(callback: CallbackQuery) -> None:
    remember_user(callback.from_user)
    await edit_any(callback.message, SUPPORT_TEXT, reply_markup=support_inline_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:back")
async def back_to_menu(callback: CallbackQuery) -> None:
    remember_user(callback.from_user)
    text = (
        '<tg-emoji emoji-id="5260547274957672345">🎲</tg-emoji> '
        "<b>Lucky Dice</b> — испытай удачу!\n\n"
        "<i>Здесь заводят игры, зарабатывают на партнёрке "
        "и следят за своим прогрессом.</i>\n\n"
        '<tg-emoji emoji-id="5886676966102274844">👆</tg-emoji> '
        "Выберите раздел ниже:"
    )
    await show_menu_screen(callback.message, text, menu_inline_keyboard())
    await callback.answer()


# --------------------------------------------------------------------------
# Раздел «Чеки»
# --------------------------------------------------------------------------


@router.callback_query(F.data == "menu:checks")
async def checks_section(callback: CallbackQuery, state: FSMContext) -> None:
    remember_user(callback.from_user)
    await state.clear()
    await edit_any(callback.message, format_checks_menu_text(), reply_markup=checks_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "checks:back")
async def checks_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_any(callback.message, format_checks_menu_text(), reply_markup=checks_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "checks:cancel")
async def checks_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_any(callback.message, format_checks_menu_text(), reply_markup=checks_menu_keyboard())
    await callback.answer("Отменено")


# --- Вспомогательное: редактирование "панели" вместо новых сообщений ---


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def edit_panel(
    bot: Bot,
    state: FSMContext,
    fallback_chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Редактирует исходное сообщение-панель вместо отправки нового сообщения.
    Если панель по какой-то причине недоступна — отправляет новое и запоминает его."""
    data = await state.get_data()
    panel_chat_id = data.get("panel_chat_id", fallback_chat_id)
    panel_message_id = data.get("panel_message_id")

    if panel_message_id:
        try:
            await edit_by_id(bot, panel_chat_id, panel_message_id, text, reply_markup)
            return
        except Exception:
            pass

    sent = await bot.send_message(fallback_chat_id, text, reply_markup=reply_markup)
    await state.update_data(panel_chat_id=sent.chat.id, panel_message_id=sent.message_id)


# Импортируется здесь (а не в начале файла), потому что games.py на этапе
# своего импорта уже обращается к remember_user/get_profile_stats/log_game_round/
# edit_panel/_safe_delete — они должны быть определены к этому моменту.
import games as games_module

games_router = games_module.router

# Пополнение баланса через CryptoBot / xRocket (см. payments.py). Импорт безопасен для
# циклов: payments.py зависит только от storage.py, а не от main.py.
import payments as payments_module

payments_router = payments_module.router
# Кому payments.py шлёт уведомления о сбоях выводов
payments_module.ALERT_ADMIN_IDS = set(ADMIN_IDS)

# Партнёрская программа (см. refs.py): 2% пригласившему с каждого пополнения реферала.
# refs.py зависит только от storage.py (и лениво от payments.py), поэтому циклов нет.
import refs as refs_module

refs_router = refs_module.router
refs_module.ALERT_ADMIN_IDS = set(ADMIN_IDS)


@router.callback_query(F.data == "games")
async def games_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Открывает селектор игр (Кубик/Футбол/Баскетбол/Дартс/Боулинг). Ссылается
    отсюда сама games.py (кнопки «Назад» и отмена ставки), поэтому функция
    называется именно games_callback и определена в main.py."""
    remember_user(callback.from_user)
    betting_game = games_module.get_betting_game()
    if betting_game is None:
        await callback.answer("❌ Бот перезапускается, попробуйте ещё раз чуть позже", show_alert=True)
        return
    await games_module.show_games_selector(callback, betting_game)


@router.message(F.text == "Игры")
async def games_section(message: Message) -> None:
    remember_user(message.from_user)
    betting_game = games_module.get_betting_game()
    if betting_game is None:
        await message.answer(IN_DEV_TEXT)
        return
    text = games_module.build_games_selector_text(betting_game, message.from_user.id)
    markup = games_module.build_games_selector_keyboard()
    await message.answer(text, reply_markup=markup)  # noqa: E402


# games.py раньше сам делал `from main import games_callback` внутри cancel_bet
# (см. games.py), что заново импортировало main.py как отдельный модуль (та же
# причина расхождения баланса — см. комментарий у storage.py). Вместо этого
# регистрируем функцию здесь, как уже сделано для betting_game через
# set_betting_game/get_betting_game.
games_module.set_games_callback(games_callback)
games_module.set_remember_user(remember_user)


# --- Создание чека ---


@router.callback_query(F.data == "checks:create")
async def checks_create_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(
        panel_chat_id=callback.message.chat.id,
        panel_message_id=callback.message.message_id,
    )
    await edit_any(callback.message, 
        '<tg-emoji emoji-id="6037175527846975726">➕</tg-emoji> <b>Создание чека</b>\n\n'
        "Выберите тип чека:",
        reply_markup=check_type_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("checks:type:"))
async def checks_create_type(callback: CallbackQuery, state: FSMContext) -> None:
    check_type = callback.data.split(":", 2)[2]

    if check_type == "single":
        await state.update_data(max_activations=1)
        await state.set_state(CheckStates.create_amount)
        await edit_any(callback.message, 
            '<tg-emoji emoji-id="6039614175917903752">✏</tg-emoji> <i>Введите сумму, которая будет зачисляться за активацию (например, 5):</i>',
            reply_markup=checks_cancel_keyboard(),
        )
    else:
        await state.set_state(CheckStates.create_count)
        await edit_any(callback.message, 
            '<tg-emoji emoji-id="6039614175917903752">✏</tg-emoji> <i>Введите количество активаций (например, 10):</i>',
            reply_markup=checks_cancel_keyboard(),
        )
    await callback.answer()


@router.message(CheckStates.create_count)
async def checks_create_count(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await edit_panel(
            bot, state, message.chat.id,
            '<tg-emoji emoji-id="6039614175917903752">✏</tg-emoji> <i>Некорректное число. Введите количество активаций (целое число больше 0):</i>',
            checks_cancel_keyboard(),
        )
        await _safe_delete(message)
        return

    await state.update_data(max_activations=int(raw))
    await state.set_state(CheckStates.create_amount)
    await edit_panel(
        bot, state, message.chat.id,
        '<tg-emoji emoji-id="6039614175917903752">✏</tg-emoji> <i>Введите сумму, которая будет зачисляться за КАЖДУЮ активацию (например, 5):</i>',
        checks_cancel_keyboard(),
    )
    await _safe_delete(message)


@router.message(CheckStates.create_amount)
async def checks_create_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    try:
        amount = float((message.text or "").strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await edit_panel(
            bot, state, message.chat.id,
            '<tg-emoji emoji-id="6039614175917903752">✏</tg-emoji> <i>Некорректная сумма. Введите положительное число:</i>',
            checks_cancel_keyboard(),
        )
        await _safe_delete(message)
        return

    data = await state.get_data()
    max_activations = data["max_activations"]
    total_cost = amount * max_activations

    profile = get_profile_stats(message.from_user.id)
    if profile["balance"] < total_cost:
        await edit_panel(
            bot, state, message.chat.id,
            "❌ Недостаточно средств на балансе.\n"
            f"Нужно: ${total_cost:,.2f}, у вас: ${profile['balance']:,.2f}",
            checks_menu_keyboard(),
        )
        await state.clear()
        await _safe_delete(message)
        return

    await state.update_data(amount=amount)
    await edit_panel(
        bot, state, message.chat.id,
        "Выберите условие активации чека:",
        check_restriction_keyboard(),
    )
    await _safe_delete(message)


async def _finalize_check_creation(
    bot: Bot,
    state: FSMContext,
    user_id: int,
    fallback_chat_id: int,
    restriction_type: str,
    restriction_value: float,
) -> None:
    data = await state.get_data()
    amount = data["amount"]
    max_activations = data["max_activations"]
    total_cost = amount * max_activations

    profile = get_profile_stats(user_id)
    if profile["balance"] < total_cost:
        await state.clear()
        await edit_panel(
            bot, state, fallback_chat_id,
            "❌ Недостаточно средств на балансе.\n"
            f"Нужно: ${total_cost:,.2f}, у вас: ${profile['balance']:,.2f}",
            checks_menu_keyboard(),
        )
        return

    profile["balance"] -= total_cost
    USER_TRANSACTIONS.setdefault(user_id, []).append(
        {"timestamp": datetime.now(timezone.utc), "amount": total_cost, "type": "check_create"}
    )

    check = create_check(user_id, amount, max_activations, restriction_type, restriction_value)
    await state.clear()

    await edit_panel(
        bot, state, fallback_chat_id,
        format_check_created_text(check),
        check_created_keyboard(),
    )


@router.callback_query(F.data.startswith("checks:restriction:"))
async def checks_create_restriction(callback: CallbackQuery, state: FSMContext) -> None:
    restriction_type = callback.data.split(":", 2)[2]

    if restriction_type == "none":
        await _finalize_check_creation(
            callback.bot, state, callback.from_user.id, callback.message.chat.id, restriction_type, 0.0
        )
        await callback.answer()
        return

    await state.update_data(restriction_type=restriction_type)
    await state.set_state(CheckStates.create_restriction_value)
    label = CHECK_RESTRICTION_LABELS.get(restriction_type, "")
    await edit_any(callback.message, 
        f"Введите минимальное значение для условия «{label}» (например, 50):",
        reply_markup=checks_cancel_keyboard(),
    )
    await callback.answer()


@router.message(CheckStates.create_restriction_value)
async def checks_create_restriction_value(message: Message, state: FSMContext, bot: Bot) -> None:
    try:
        value = float((message.text or "").strip().replace(",", "."))
        if value <= 0:
            raise ValueError
    except ValueError:
        await edit_panel(
            bot, state, message.chat.id,
            "Некорректное значение. Введите положительное число:",
            checks_cancel_keyboard(),
        )
        await _safe_delete(message)
        return

    data = await state.get_data()
    await _finalize_check_creation(
        bot, state, message.from_user.id, message.chat.id, data["restriction_type"], value
    )
    await _safe_delete(message)


# --- Мои чеки ---


@router.callback_query(F.data == "checks:mine")
async def checks_mine(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    my_codes = sorted(
        (c["code"] for c in CHECKS.values() if c["creator_id"] == user_id and c["active"]),
        key=lambda code: CHECKS[code]["created_at"],
        reverse=True,
    )[:10]

    await edit_any(callback.message, 
        format_my_checks_text(user_id),
        reply_markup=my_checks_keyboard(my_codes) if my_codes else checks_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("checks:deactivate:"))
async def checks_deactivate(callback: CallbackQuery) -> None:
    code = callback.data.split(":", 2)[2]
    check = CHECKS.get(code)

    if not check or check["creator_id"] != callback.from_user.id:
        await callback.answer("Чек не найден.", show_alert=True)
        return

    if not check["active"]:
        await callback.answer("Чек уже неактивен.", show_alert=True)
        return

    remaining = check["max_activations"] - check["activations_used"]
    refund = remaining * check["amount"]
    if refund > 0:
        profile = get_profile_stats(callback.from_user.id)
        profile["balance"] += refund
        USER_TRANSACTIONS.setdefault(callback.from_user.id, []).append(
            {"timestamp": datetime.now(timezone.utc), "amount": refund, "type": "check_refund"}
        )

    check["active"] = False

    my_codes = sorted(
        (c["code"] for c in CHECKS.values() if c["creator_id"] == callback.from_user.id and c["active"]),
        key=lambda c: CHECKS[c]["created_at"],
        reverse=True,
    )[:10]

    await edit_any(callback.message, 
        format_my_checks_text(callback.from_user.id),
        reply_markup=my_checks_keyboard(my_codes) if my_codes else checks_menu_keyboard(),
    )
    await callback.answer(f"Чек деактивирован, возвращено ${refund:,.2f}" if refund else "Чек деактивирован")


@router.callback_query(F.data.startswith("menu:"))
async def menu_callback(callback: CallbackQuery) -> None:
    await callback.answer("В разработке 🚧", show_alert=True)


# --------------------------------------------------------------------------
# Бонусные чеки (/addcheck — только админы)
# --------------------------------------------------------------------------


@router.message(Command("addcheck"))
async def cmd_addcheck(message: Message, command: CommandObject) -> None:
    """/addcheck <сумма> [активаций] — создаёт чек на бонусный баланс (💎).
    После бонусных ставок на ×3 от суммы на реальный баланс переходит начальная сумма (см. bonus.py)."""
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Доступ запрещён.")
        return

    usage = (
        f"{bonus_module.BONUS_ICON} <b>Бонусный чек</b>\n\n"
        "Формат: <code>/addcheck &lt;сумма&gt; [активаций]</code>\n"
        "Пример: <code>/addcheck 0.5 100</code> — бонус $0.50, 100 активаций.\n\n"
        f"<i>Играть можно с бонусного баланса. После бонусных ставок на ×{bonus_module.WAGER_MULT:g} от суммы "
        "на реальный баланс переходит только начальная сумма бонуса.</i>"
    )
    parts = (command.args or "").replace(",", ".").split()
    try:
        amount = round(float(parts[0]), 2)
        activations = int(parts[1]) if len(parts) > 1 else 1
    except (IndexError, ValueError):
        await message.answer(usage)
        return
    if not (0.01 <= amount <= bonus_module.MAX_CHECK_AMOUNT) or not (
        1 <= activations <= bonus_module.MAX_CHECK_ACTIVATIONS
    ):
        await message.answer(
            f"❌ Сумма — от $0.01 до ${bonus_module.MAX_CHECK_AMOUNT:,.0f}, "
            f"активаций — от 1 до {bonus_module.MAX_CHECK_ACTIVATIONS:,}.\n\n{usage}"
        )
        return

    code = await asyncio.to_thread(bonus_module.create_check, message.from_user.id, amount, activations)
    link = await bonus_module.check_link(message.bot, code)
    await message.answer(
        f"{bonus_module.BONUS_ICON} <b>Бонусный чек создан</b>\n\n"
        f"┌ Бонус за активацию: <b>${amount:,.2f}</b>\n"
        f"├ Активаций: <b>{activations}</b>\n"
        f"├ Отыгрыш: <b>×{bonus_module.WAGER_MULT:g}</b> (ставок на ${amount * bonus_module.WAGER_MULT:,.2f})\n"
        f"└ Код: <code>{code}</code>\n\n"
        f"🔗 {link}"
    )


# --------------------------------------------------------------------------
# Картинка главного меню (/img — только админы)
# --------------------------------------------------------------------------


@router.message(Command("img"))
async def cmd_img(message: Message, command: CommandObject) -> None:
    """/img в ответ на фото — меню будет отправляться вместе с этим фото.
    /img off — убрать картинку. Экраны меню редактируются через edit_any (edit_text / edit_caption)."""
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Доступ запрещён.")
        return

    if (command.args or "").strip().lower() in ("off", "reset", "del", "delete"):
        ui.set_menu_image(None)
        await message.answer("🗑 Картинка меню удалена — меню снова текстовое.")
        return

    reply = message.reply_to_message
    if reply is None or not reply.photo:
        await message.answer(
            "🖼 <b>Картинка меню</b>\n\n"
            "Ответьте командой <code>/img</code> на изображение (отправленное как фото, "
            "не файлом) — меню будет отправляться вместе с ним.\n"
            "Убрать картинку: <code>/img off</code>"
        )
        return

    ui.set_menu_image(reply.photo[-1].file_id)  # последнее фото в списке — максимального размера
    await message.answer("✅ Картинка меню обновлена. Так теперь выглядит меню:")
    await show_menu(message)


# --------------------------------------------------------------------------
# Админ-панель
# --------------------------------------------------------------------------


@router.message(Command("admin"))
async def admin_entry(message: Message, state: FSMContext) -> None:
    await state.clear()
    remember_user(message.from_user)

    if not is_admin(message.from_user.id):
        await message.answer("🚫 Доступ запрещён.")
        return

    await message.answer(
        "⚙️ <b>Админ-панель</b>\n\nВыберите действие:",
        reply_markup=admin_panel_keyboard(),
    )


@router.callback_query(F.data == "admin:close")
async def admin_close(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return
    await state.clear()
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == "admin:back")
async def admin_back(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return
    await state.clear()
    await edit_any(callback.message, 
        "⚙️ <b>Админ-панель</b>\n\nВыберите действие:",
        reply_markup=admin_panel_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:cancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return
    await state.clear()
    await edit_any(callback.message, 
        "⚙️ <b>Админ-панель</b>\n\nВыберите действие:",
        reply_markup=admin_panel_keyboard(),
    )
    await callback.answer("Отменено")


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return
    await edit_any(callback.message, format_global_stats_text(), reply_markup=admin_back_keyboard())
    await callback.answer()


# --- Выдача баланса ---


@router.callback_query(F.data == "admin:grant")
async def admin_grant_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return
    await state.set_state(AdminStates.grant_user_id)
    await edit_any(callback.message, 
        "💰 <b>Выдача баланса</b>\n\nВведите ID пользователя, которому начислить баланс:",
        reply_markup=admin_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.grant_user_id)
async def admin_grant_user_id(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("Некорректный ID. Введите числовой ID пользователя:")
        return

    await state.update_data(target_user_id=int(raw))
    await state.set_state(AdminStates.grant_amount)
    await message.answer(
        "Введите сумму для начисления (например, 25.5):",
        reply_markup=admin_cancel_keyboard(),
    )


@router.message(AdminStates.grant_amount)
async def admin_grant_amount(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return

    try:
        amount = float((message.text or "").strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Некорректная сумма. Введите положительное число:")
        return

    data = await state.get_data()
    target_user_id = data["target_user_id"]
    new_balance = adjust_balance(target_user_id, amount, "admin_grant")
    await state.clear()

    name = get_display_name(target_user_id)
    await message.answer(
        f"✅ Начислено ${amount:,.2f} пользователю {name} (ID <code>{target_user_id}</code>).\n"
        f"Новый баланс: ${new_balance:,.2f}",
        reply_markup=admin_panel_keyboard(),
    )


# --- Списание баланса ---


@router.callback_query(F.data == "admin:deduct")
async def admin_deduct_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return
    await state.set_state(AdminStates.deduct_user_id)
    await edit_any(callback.message, 
        "➖ <b>Списание баланса</b>\n\nВведите ID пользователя, у которого списать баланс:",
        reply_markup=admin_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.deduct_user_id)
async def admin_deduct_user_id(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("Некорректный ID. Введите числовой ID пользователя:")
        return

    await state.update_data(target_user_id=int(raw))
    await state.set_state(AdminStates.deduct_amount)
    await message.answer(
        "Введите сумму для списания (например, 10):",
        reply_markup=admin_cancel_keyboard(),
    )


@router.message(AdminStates.deduct_amount)
async def admin_deduct_amount(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return

    try:
        amount = float((message.text or "").strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Некорректная сумма. Введите положительное число:")
        return

    data = await state.get_data()
    target_user_id = data["target_user_id"]
    new_balance = adjust_balance(target_user_id, amount, "admin_deduct")
    await state.clear()

    name = get_display_name(target_user_id)
    await message.answer(
        f"✅ Списано ${amount:,.2f} у пользователя {name} (ID <code>{target_user_id}</code>).\n"
        f"Новый баланс: ${new_balance:,.2f}",
        reply_markup=admin_panel_keyboard(),
    )


# --- Поиск пользователя ---


@router.callback_query(F.data == "admin:find")
async def admin_find_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return
    await state.set_state(AdminStates.find_user_id)
    await edit_any(callback.message, 
        "🔎 <b>Поиск пользователя</b>\n\nВведите ID пользователя:",
        reply_markup=admin_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.find_user_id)
async def admin_find_user_id(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("Некорректный ID. Введите числовой ID пользователя:")
        return

    target_user_id = int(raw)
    await state.clear()

    info = USER_INFO.get(target_user_id)
    if not info:
        await message.answer(
            f"Пользователь с ID <code>{target_user_id}</code> не найден "
            "(ещё не запускал бота).",
            reply_markup=admin_panel_keyboard(),
        )
        return

    text = format_profile_text(target_user_id, info.get("full_name") or "—", info.get("username"))
    await message.answer(text, reply_markup=admin_panel_keyboard())


# --- Рассылка ---


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return
    await state.set_state(AdminStates.broadcast_text)
    await edit_any(callback.message, 
        "📢 <b>Рассылка</b>\n\nОтправьте текст сообщения для рассылки всем пользователям:",
        reply_markup=admin_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.broadcast_text)
async def admin_broadcast_send(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        return

    await state.clear()
    text = message.html_text if message.text else None
    if not text:
        await message.answer("Пустое сообщение, рассылка отменена.", reply_markup=admin_panel_keyboard())
        return

    sent, failed = 0, 0
    for user_id in list(USER_INFO.keys()):
        try:
            await bot.send_message(user_id, text)
            sent += 1
        except Exception:
            failed += 1

    await message.answer(
        f"📢 Рассылка завершена.\nОтправлено: {sent}\nНе доставлено: {failed}",
        reply_markup=admin_panel_keyboard(),
    )


# --------------------------------------------------------------------------
# Запуск бота
# --------------------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    dp = Dispatcher()
    dp.include_router(router)
    # ВАЖНО: subscription_module.router и payments_router — до games_router.
    # У games_router есть «ловец» любого текста (games_text_router), не привязанный
    # к конкретному FSM-состоянию, и он перехватил бы: (а) сумму пополнения,
    # введённую в чат, и (б) @username/ссылку на канал, которую админ присылает
    # в состоянии SubsStates.add_channel при добавлении обязательного канала.
    dp.include_router(subscription_module.router)
    dp.include_router(payments_router)
    dp.include_router(refs_router)
    dp.include_router(games_router)

    # Глобальный гейт обязательной подписки — outer-миддлварь, отрабатывает раньше
    # ЛЮБОГО хендлера во ВСЕХ роутерах выше (games/payments/refs/чеки/профиль и т.д.).
    # Пока список каналов пуст — не делает вообще ничего (см. subscription.py).
    dp.message.outer_middleware(subscription_module.SubscriptionMiddleware())
    dp.callback_query.outer_middleware(subscription_module.SubscriptionMiddleware())

    # Создаёт единственный экземпляр BettingGame и регистрирует его как
    # общий для games.py (через set_betting_game внутри __init__), чтобы все
    # хендлеры раздела «Игры» могли получить его через get_betting_game().
    games_module.BettingGame(bot)
    bonus_module.set_bot(bot)  # уведомления «бонус отыгран»

    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username

    # Фоновая проверка оплаты счетов CryptoBot / xRocket
    payments_module.start_watchers(bot)

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await payments_module.stop_watchers()


if __name__ == "__main__":
    asyncio.run(main())
