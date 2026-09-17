import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
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


IN_DEV_TEXT = "🚧 Этот раздел находится в разработке.\nСкоро здесь появится функционал!"

SUPPORT_TEXT = (
    '<tg-emoji emoji-id="5812150667812280629">🛠</tg-emoji> <b>Поддержка</b>\n\n'
    "<i>Возникли вопросы или проблема с игрой, депозитом или выводом?\n"
    "Напишите нам — ответим как можно быстрее.</i>\n\n"
    "└ Оператор: @luckydicesupport\n"
    "└ Среднее время ответа: ~10 минут"
)

# --------------------------------------------------------------------------
# Хранилище профилей (временное, in-memory)
# --------------------------------------------------------------------------
# NOTE: пока нет подключения к БД — статистика хранится в памяти процесса
# и обнулится при перезапуске бота. Когда появится база данных, эту часть
# нужно будет заменить на реальные запросы к ней.

USER_PROFILES: dict[int, dict[str, float]] = {}


def get_profile_stats(user_id: int) -> dict[str, float]:
    """Возвращает статистику пользователя, создавая запись по умолчанию."""
    if user_id not in USER_PROFILES:
        USER_PROFILES[user_id] = {
            "balance": 0.0,
            "deposits": 0.0,
            "withdrawals": 0.0,
            "turnover": 0.0,
        }
    return USER_PROFILES[user_id]


# --------------------------------------------------------------------------
# Хранилище транзакций (временное, in-memory) — для статистики по периодам
# --------------------------------------------------------------------------
# NOTE: как и USER_PROFILES, это заглушка. Когда появится БД, депозиты и
# выводы нужно будет логировать сюда (или сразу в БД с полем timestamp),
# чтобы get_period_stats считал реальные суммы за день/неделю/месяц.

USER_TRANSACTIONS: dict[int, list[dict]] = {}

PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}
PERIOD_LABELS = {"day": "День", "week": "Неделя", "month": "Месяц"}


def get_period_stats(user_id: int, period: str) -> dict[str, float]:
    """Суммирует депозиты/выводы/оборот пользователя за период."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=PERIOD_DAYS.get(period, 1))
    stats = {"deposits": 0.0, "withdrawals": 0.0, "turnover": 0.0}

    for tx in USER_TRANSACTIONS.get(user_id, []):
        if tx["timestamp"] < cutoff:
            continue
        if tx["type"] not in ("deposit", "withdraw"):
            continue
        amount = tx["amount"]
        stats["turnover"] += amount
        if tx["type"] == "deposit":
            stats["deposits"] += amount
        elif tx["type"] == "withdraw":
            stats["withdrawals"] += amount

    return stats


def adjust_balance(user_id: int, amount: float, tx_type: str) -> float:
    """Начисляет ('admin_grant') или списывает ('admin_deduct') баланс пользователю
    (админское действие) и логирует операцию отдельным типом транзакции, чтобы она
    не искажала личную статистику пользователя по депозитам/выводам."""
    stats = get_profile_stats(user_id)
    stats["balance"] += amount if tx_type == "admin_grant" else -amount

    USER_TRANSACTIONS.setdefault(user_id, []).append(
        {
            "timestamp": datetime.now(timezone.utc),
            "amount": amount,
            "type": tx_type,
        }
    )
    return stats["balance"]


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


# --------------------------------------------------------------------------
# Хранилище игровых раундов (временное, in-memory) — для топа игроков по
# обороту, выигрышам и количеству игр.
# --------------------------------------------------------------------------
# NOTE: заглушка, как и USER_TRANSACTIONS. Когда появится БД и игровой
# движок, каждый сыгранный раунд нужно будет логировать сюда (или сразу в
# БД с полем timestamp), чтобы get_top_players считал реальные значения.

USER_GAME_ROUNDS: dict[int, list[dict]] = {}


def log_game_round(user_id: int, bet: float, win: float) -> None:
    """Логирует сыгранный раунд игры (ставка/выигрыш) для последующего подсчёта топа."""
    USER_GAME_ROUNDS.setdefault(user_id, []).append(
        {
            "timestamp": datetime.now(timezone.utc),
            "bet": bet,
            "win": win,
        }
    )


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
                InlineKeyboardButton(text="💰 Выдать баланс", callback_data="admin:grant"),
                InlineKeyboardButton(text="➖ Списать баланс", callback_data="admin:deduct"),
            ],
            [
                InlineKeyboardButton(text="📊 Общая статистика", callback_data="admin:stats"),
                InlineKeyboardButton(text="🔎 Найти пользователя", callback_data="admin:find"),
            ],
            [
                InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast"),
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
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="admin:back")]]
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
# Хендлеры
# --------------------------------------------------------------------------

router = Router()


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


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    remember_user(message.from_user)
    await message.answer(
        f"Привет, {message.from_user.full_name}! 👋\n\n"
        "Выберите раздел из меню ниже.",
        reply_markup=main_reply_keyboard(),
    )


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
    await message.answer(text, reply_markup=menu_inline_keyboard())


@router.message(F.text == "Игры")
async def games_section(message: Message) -> None:
    remember_user(message.from_user)
    await message.answer(IN_DEV_TEXT)


@router.message(F.text == "Партнеры")
async def partners_section(message: Message) -> None:
    remember_user(message.from_user)
    await message.answer(IN_DEV_TEXT)


@router.callback_query(F.data == "menu:profile")
async def profile_section(callback: CallbackQuery) -> None:
    user = callback.from_user
    remember_user(user)
    text = format_profile_text(user.id, user.full_name, user.username)

    await callback.message.edit_text(text, reply_markup=profile_inline_keyboard())
    await callback.answer()


@router.callback_query(F.data == "profile:deposit")
async def profile_deposit(callback: CallbackQuery) -> None:
    await callback.answer(IN_DEV_TEXT, show_alert=True)


@router.callback_query(F.data == "profile:withdraw")
async def profile_withdraw(callback: CallbackQuery) -> None:
    await callback.answer(IN_DEV_TEXT, show_alert=True)


@router.callback_query(F.data == "menu:stats")
async def stats_section(callback: CallbackQuery) -> None:
    remember_user(callback.from_user)
    text = format_stats_text(callback.from_user.id, "day")
    await callback.message.edit_text(text, reply_markup=stats_period_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("stats:"))
async def stats_period_switch(callback: CallbackQuery) -> None:
    remember_user(callback.from_user)
    period = callback.data.split(":", 1)[1]
    text = format_stats_text(callback.from_user.id, period)
    await callback.message.edit_text(text, reply_markup=stats_period_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:top")
async def top_section(callback: CallbackQuery) -> None:
    remember_user(callback.from_user)
    text = format_top_text("turnover", "day")
    await callback.message.edit_text(text, reply_markup=top_keyboard("turnover", "day"))
    await callback.answer()


@router.callback_query(F.data.startswith("top:"))
async def top_switch(callback: CallbackQuery) -> None:
    remember_user(callback.from_user)
    _, category, period = callback.data.split(":", 2)
    text = format_top_text(category, period)
    await callback.message.edit_text(text, reply_markup=top_keyboard(category, period))
    await callback.answer()


@router.callback_query(F.data == "menu:support")
async def support_section(callback: CallbackQuery) -> None:
    remember_user(callback.from_user)
    await callback.message.edit_text(SUPPORT_TEXT, reply_markup=support_inline_keyboard())
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
    await callback.message.edit_text(text, reply_markup=menu_inline_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("menu:"))
async def menu_callback(callback: CallbackQuery) -> None:
    await callback.answer("В разработке 🚧", show_alert=True)


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
    await callback.message.edit_text(
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
    await callback.message.edit_text(
        "⚙️ <b>Админ-панель</b>\n\nВыберите действие:",
        reply_markup=admin_panel_keyboard(),
    )
    await callback.answer("Отменено")


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return
    await callback.message.edit_text(format_global_stats_text(), reply_markup=admin_back_keyboard())
    await callback.answer()


# --- Выдача баланса ---


@router.callback_query(F.data == "admin:grant")
async def admin_grant_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён.", show_alert=True)
        return
    await state.set_state(AdminStates.grant_user_id)
    await callback.message.edit_text(
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
    await callback.message.edit_text(
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
    await callback.message.edit_text(
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
    await callback.message.edit_text(
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
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
