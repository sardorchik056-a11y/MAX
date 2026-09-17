"""
Раздел «Игры» для Lucky Dice bot.

Пока полностью реализована игра «Кости» (1 и 2 кубика). Остальные игры
(Футбол, Баскетбол, Дартс, Боулинг, Слот) выведены как отдельные кнопки
верхнего меню — используют встроенные анимированные эмодзи Telegram
(bot.send_dice), но логика ставок для них ещё не разработана и показывает
"в разработке" (см. games_soon). Когда будете готовы их делать — по структуре
это будет копия блока «Кости» ниже, только с другим набором bet_type/emoji.

NOTE по не указанным явно множителям: для 2 кубиков множители «Ровно 7»
и «Дубль» в задаче не были явно заданы (были даны только 2х/4х/6х/36х для
базовых типов). Выставил обоим справедливые 6х (вероятность выпадения
7 из 36 = 6/36 = 1/6, дубля — тоже 6/36 = 1/6) — при необходимости
поправьте константы в MULTIPLIERS ниже.

Файл подключается в main.py:
    from games import router as games_router
    ...
    dp.include_router(games_router)
"""

from __future__ import annotations

import asyncio
from collections import Counter

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# main.py запускается напрямую (python main.py), поэтому его модуль во время
# исполнения называется "__main__", а не "main" — импортируем оттуда, чтобы
# не создать вторую отдельную копию модуля (со своими USER_PROFILES и т.д.).
# Если у вас другой способ запуска (например, `python -m main`) — сработает
# запасной вариант `from main import ...`.
try:
    from __main__ import _safe_delete, edit_panel, get_profile_stats, log_game_round, remember_user
except ImportError:
    from main import _safe_delete, edit_panel, get_profile_stats, log_game_round, remember_user

router = Router()


# --------------------------------------------------------------------------
# Множители и подписи ставок
# --------------------------------------------------------------------------

# Ключ — (режим "1"/"2", тип ставки)
MULTIPLIERS: dict[tuple[str, str], float] = {
    ("1", "even"): 2,
    ("1", "odd"): 2,
    ("1", "gt3"): 2,
    ("1", "lt4"): 2,
    ("1", "num"): 6,
    ("2", "even"): 4,
    ("2", "odd"): 4,
    ("2", "gt7"): 4,
    ("2", "lt7"): 4,
    ("2", "eq7"): 6,  # не задано явно в ТЗ — справедливые 6х, см. NOTE выше
    ("2", "double"): 6,  # не задано явно в ТЗ — справедливые 6х, см. NOTE выше
    ("2", "pair"): 36,
}

BET_LABELS: dict[tuple[str, str], str] = {
    ("1", "even"): "Чёт",
    ("1", "odd"): "Нечёт",
    ("1", "gt3"): "Больше 3",
    ("1", "lt4"): "Меньше 4",
    ("1", "num"): "Число",
    ("2", "even"): "Чёт (сумма)",
    ("2", "odd"): "Нечёт (сумма)",
    ("2", "gt7"): "Больше 7",
    ("2", "lt7"): "Меньше 7",
    ("2", "eq7"): "Ровно 7",
    ("2", "double"): "Дубль (два одинаковых числа)",
    ("2", "pair"): "Угадать оба числа",
}

GAME_STUB_LABELS = {
    "football": "⚽ Футбол",
    "basketball": "🏀 Баскетбол",
    "darts": "🎯 Дартс",
    "bowling": "🎳 Боулинг",
    "slot": "🎰 Слот",
}

DICE_ICON_EMOJI_ID = "5260547274957672345"  # 🎲, тот же, что в шапке "Lucky Dice"
BACK_ICON_EMOJI_ID = "6039539366177541657"  # тот же "Назад", что в других разделах

# Самый высокий множитель по игре «Кости» — показывается в главном меню игр
# (сейчас это "Угадать оба числа" x36 для 2 костей).
DICE_MAX_MULTIPLIER = max(MULTIPLIERS.values())

# --------------------------------------------------------------------------
# Общий реестр игр — используется и в главном меню («эмодзи + до Nх»),
# и в верхнем ряду вкладок-переключателей на экранах уже выбранной игры
# (по аналогии с переключателем периода в статистике).
# --------------------------------------------------------------------------

GAMES_INFO: list[dict] = [
    {
        "id": "dice",
        "emoji": "🎲",
        "callback": "games:dice:menu",
        "max_multiplier": DICE_MAX_MULTIPLIER,
    },
    {"id": "football", "emoji": "⚽", "callback": "games:soon:football", "max_multiplier": None},
    {"id": "basketball", "emoji": "🏀", "callback": "games:soon:basketball", "max_multiplier": None},
    {"id": "darts", "emoji": "🎯", "callback": "games:soon:darts", "max_multiplier": None},
    {"id": "bowling", "emoji": "🎳", "callback": "games:soon:bowling", "max_multiplier": None},
    {"id": "slot", "emoji": "🎰", "callback": "games:soon:slot", "max_multiplier": None},
]


# --------------------------------------------------------------------------
# Проверка выигрышных условий
# --------------------------------------------------------------------------


def check_bet_1(value: int, bet_type: str, number: int | None) -> bool:
    """Проверяет ставку на одном кубике."""
    if bet_type == "even":
        return value % 2 == 0
    if bet_type == "odd":
        return value % 2 == 1
    if bet_type == "gt3":
        return value > 3
    if bet_type == "lt4":
        return value < 4
    if bet_type == "num":
        return value == number
    return False


def check_bet_2(v1: int, v2: int, bet_type: str, pair: tuple[int, int] | None) -> bool:
    """Проверяет ставку на двух кубиках."""
    total = v1 + v2
    if bet_type == "even":
        return total % 2 == 0
    if bet_type == "odd":
        return total % 2 == 1
    if bet_type == "gt7":
        return total > 7
    if bet_type == "lt7":
        return total < 7
    if bet_type == "eq7":
        return total == 7
    if bet_type == "double":
        return v1 == v2
    if bet_type == "pair":
        assert pair is not None
        return Counter((v1, v2)) == Counter(pair)
    return False


# --------------------------------------------------------------------------
# Состояния FSM
# --------------------------------------------------------------------------


class GameStates(StatesGroup):
    dice_bet_amount = State()


# --------------------------------------------------------------------------
# Клавиатуры
# --------------------------------------------------------------------------


def _game_menu_label(game: dict) -> str:
    """Формат кнопки в главном меню игр: эмодзи + множитель (без названия)."""
    mult = game["max_multiplier"]
    suffix = f"(до {mult:g}x)" if mult is not None else "(скоро)"
    return f"{game['emoji']}{suffix}"


def games_menu_keyboard() -> InlineKeyboardMarkup:
    game_buttons = [
        InlineKeyboardButton(text=_game_menu_label(g), callback_data=g["callback"])
        for g in GAMES_INFO
    ]
    # По 2 кнопки в ряд, как раньше.
    rows = [game_buttons[i : i + 2] for i in range(0, len(game_buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton(
                text="Назад",
                callback_data="menu:back",
                icon_custom_emoji_id=BACK_ICON_EMOJI_ID,
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def games_tabs_row() -> list[InlineKeyboardButton]:
    """Верхний ряд вкладок-переключателей между играми — только эмодзи,
    без названий (по аналогии с переключателем периода в статистике)."""
    return [
        InlineKeyboardButton(text=g["emoji"], callback_data=g["callback"])
        for g in GAMES_INFO
    ]


def dice_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            games_tabs_row(),
            [
                InlineKeyboardButton(text="1 кость", callback_data="games:dice:mode:1"),
                InlineKeyboardButton(text="2 кости", callback_data="games:dice:mode:2"),
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="games:menu",
                    icon_custom_emoji_id=BACK_ICON_EMOJI_ID,
                ),
            ],
        ]
    )


def dice_bet_keyboard_1() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            games_tabs_row(),
            [
                InlineKeyboardButton(text="Чёт (x2)", callback_data="games:dice:bet:1:even"),
                InlineKeyboardButton(text="Нечёт (x2)", callback_data="games:dice:bet:1:odd"),
            ],
            [
                InlineKeyboardButton(text="Больше 3 (x2)", callback_data="games:dice:bet:1:gt3"),
                InlineKeyboardButton(text="Меньше 4 (x2)", callback_data="games:dice:bet:1:lt4"),
            ],
            [
                InlineKeyboardButton(text="1 (x6)", callback_data="games:dice:bet:1:num:1"),
                InlineKeyboardButton(text="2 (x6)", callback_data="games:dice:bet:1:num:2"),
                InlineKeyboardButton(text="3 (x6)", callback_data="games:dice:bet:1:num:3"),
            ],
            [
                InlineKeyboardButton(text="4 (x6)", callback_data="games:dice:bet:1:num:4"),
                InlineKeyboardButton(text="5 (x6)", callback_data="games:dice:bet:1:num:5"),
                InlineKeyboardButton(text="6 (x6)", callback_data="games:dice:bet:1:num:6"),
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="games:dice:menu",
                    icon_custom_emoji_id=BACK_ICON_EMOJI_ID,
                ),
            ],
        ]
    )


def dice_bet_keyboard_2() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            games_tabs_row(),
            [
                InlineKeyboardButton(text="Чёт (x4)", callback_data="games:dice:bet:2:even"),
                InlineKeyboardButton(text="Нечёт (x4)", callback_data="games:dice:bet:2:odd"),
            ],
            [
                InlineKeyboardButton(text="Больше 7 (x4)", callback_data="games:dice:bet:2:gt7"),
                InlineKeyboardButton(text="Меньше 7 (x4)", callback_data="games:dice:bet:2:lt7"),
            ],
            [
                InlineKeyboardButton(text="Ровно 7 (x6)", callback_data="games:dice:bet:2:eq7"),
                InlineKeyboardButton(text="Дубль (x6)", callback_data="games:dice:bet:2:double"),
            ],
            [
                InlineKeyboardButton(
                    text="Угадать оба числа (x36)", callback_data="games:dice:bet:2:pair"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="games:dice:menu",
                    icon_custom_emoji_id=BACK_ICON_EMOJI_ID,
                ),
            ],
        ]
    )


def dice_pair_pick_keyboard(step: int) -> InlineKeyboardMarkup:
    prefix = "games:dice:pair1:" if step == 1 else "games:dice:pair2:"
    back_target = "games:dice:mode:2" if step == 1 else "games:dice:bet:2:pair"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            games_tabs_row(),
            [InlineKeyboardButton(text=str(n), callback_data=f"{prefix}{n}") for n in (1, 2, 3)],
            [InlineKeyboardButton(text=str(n), callback_data=f"{prefix}{n}") for n in (4, 5, 6)],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data=back_target,
                    icon_custom_emoji_id=BACK_ICON_EMOJI_ID,
                ),
            ],
        ]
    )


def dice_bet_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="games:dice:menu")]]
    )


# --------------------------------------------------------------------------
# Тексты
# --------------------------------------------------------------------------


def format_games_menu_text() -> str:
    return (
        f'<tg-emoji emoji-id="{DICE_ICON_EMOJI_ID}">🎲</tg-emoji> <b>Игры</b>\n\n'
        "<i>Выберите игру:</i>"
    )


def format_dice_mode_text() -> str:
    return (
        f'<tg-emoji emoji-id="{DICE_ICON_EMOJI_ID}">🎲</tg-emoji> <b>Кости</b>\n\n'
        "<i>Сколько кубиков бросаем?</i>"
    )


def format_dice_bet_menu_text(mode: str) -> str:
    title = "1 кость" if mode == "1" else "2 кости"
    return (
        f'<tg-emoji emoji-id="{DICE_ICON_EMOJI_ID}">🎲</tg-emoji> <b>Кости — {title}</b>\n\n'
        "<i>Выберите тип ставки:</i>"
    )


def format_bet_amount_prompt(mode: str, bet_type: str, number: int | None, pair: tuple[int, int] | None) -> str:
    label = BET_LABELS[(mode, bet_type)]
    if number is not None:
        label += f" {number}"
    if pair is not None:
        label += f" ({pair[0]} и {pair[1]})"
    multiplier = MULTIPLIERS[(mode, bet_type)]
    return (
        f"<i>Ставка «{label}» (x{multiplier}).\n"
        "Введите сумму ставки:</i>"
    )


def format_dice_result_text(win: bool, result_line: str, amount: float, multiplier: float, new_balance: float) -> str:
    if win:
        payout = amount * multiplier
        return (
            "🟢 <b>Победа!</b>\n\n"
            f"{result_line}\n"
            f"Ставка ${amount:,.2f} × {multiplier:g} = <b>${payout:,.2f}</b>\n"
            f"Баланс: ${new_balance:,.2f}"
        )
    return (
        "🔴 <b>Мимо</b>\n\n"
        f"{result_line}\n"
        f"Ставка ${amount:,.2f} сгорела.\n"
        f"Баланс: ${new_balance:,.2f}"
    )


# --------------------------------------------------------------------------
# Хендлеры — верхнее меню игр
# --------------------------------------------------------------------------


@router.message(F.text == "Игры")
async def games_section(message: Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await state.clear()
    sent = await message.answer(format_games_menu_text(), reply_markup=games_menu_keyboard())
    await state.update_data(panel_chat_id=sent.chat.id, panel_message_id=sent.message_id)


@router.callback_query(F.data == "games:menu")
async def games_menu_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(format_games_menu_text(), reply_markup=games_menu_keyboard())
    await state.update_data(
        panel_chat_id=callback.message.chat.id,
        panel_message_id=callback.message.message_id,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("games:soon:"))
async def games_soon(callback: CallbackQuery) -> None:
    name = callback.data.split(":", 2)[2]
    label = GAME_STUB_LABELS.get(name, "Эта игра")
    await callback.answer(f"{label} — в разработке, скоро появится!", show_alert=True)


# --------------------------------------------------------------------------
# Хендлеры — Кости: выбор режима
# --------------------------------------------------------------------------


@router.callback_query(F.data == "games:dice:menu")
async def dice_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(format_dice_mode_text(), reply_markup=dice_mode_keyboard())
    await state.update_data(
        panel_chat_id=callback.message.chat.id,
        panel_message_id=callback.message.message_id,
    )
    await callback.answer()


@router.callback_query(F.data == "games:dice:mode:1")
async def dice_mode_1(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(mode="1")
    await callback.message.edit_text(format_dice_bet_menu_text("1"), reply_markup=dice_bet_keyboard_1())
    await callback.answer()


@router.callback_query(F.data == "games:dice:mode:2")
async def dice_mode_2(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(mode="2")
    await callback.message.edit_text(format_dice_bet_menu_text("2"), reply_markup=dice_bet_keyboard_2())
    await callback.answer()


# --------------------------------------------------------------------------
# Хендлеры — Кости: выбор типа ставки на 2 кубика «угадать оба числа»
# --------------------------------------------------------------------------


@router.callback_query(F.data == "games:dice:bet:2:pair")
async def dice_pair_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(mode="2", bet_type="pair", number=None, pair=None, pair_first=None)
    await callback.message.edit_text(
        f'<tg-emoji emoji-id="{DICE_ICON_EMOJI_ID}">🎲</tg-emoji> '
        "<b>Угадать оба числа (x36)</b>\n\n<i>Выберите первое число:</i>",
        reply_markup=dice_pair_pick_keyboard(step=1),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("games:dice:pair1:"))
async def dice_pair_first(callback: CallbackQuery, state: FSMContext) -> None:
    first = int(callback.data.rsplit(":", 1)[1])
    await state.update_data(pair_first=first)
    await callback.message.edit_text(
        f'<tg-emoji emoji-id="{DICE_ICON_EMOJI_ID}">🎲</tg-emoji> '
        f"<b>Угадать оба числа (x36)</b>\n\nПервое число: <b>{first}</b>\n<i>Выберите второе число:</i>",
        reply_markup=dice_pair_pick_keyboard(step=2),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("games:dice:pair2:"))
async def dice_pair_second(callback: CallbackQuery, state: FSMContext) -> None:
    second = int(callback.data.rsplit(":", 1)[1])
    data = await state.get_data()
    first = data.get("pair_first")
    pair = (first, second)

    await state.update_data(pair=pair)
    await state.set_state(GameStates.dice_bet_amount)
    await callback.message.edit_text(
        format_bet_amount_prompt("2", "pair", None, pair),
        reply_markup=dice_bet_cancel_keyboard(),
    )
    await callback.answer()


# --------------------------------------------------------------------------
# Хендлеры — Кости: остальные типы ставок (общий обработчик)
# --------------------------------------------------------------------------


@router.callback_query(F.data.startswith("games:dice:bet:"))
async def dice_bet_selected(callback: CallbackQuery, state: FSMContext) -> None:
    # games : dice : bet : <mode> : <type> [ : <number> ]
    parts = callback.data.split(":")
    mode, bet_type = parts[3], parts[4]
    number = int(parts[5]) if bet_type == "num" else None

    await state.update_data(mode=mode, bet_type=bet_type, number=number, pair=None)
    await state.set_state(GameStates.dice_bet_amount)
    await callback.message.edit_text(
        format_bet_amount_prompt(mode, bet_type, number, None),
        reply_markup=dice_bet_cancel_keyboard(),
    )
    await callback.answer()


# --------------------------------------------------------------------------
# Хендлеры — Кости: ввод суммы ставки и розыгрыш
# --------------------------------------------------------------------------


@router.message(GameStates.dice_bet_amount)
async def dice_bet_amount_input(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await edit_panel(
            bot, state, message.chat.id,
            "Некорректная сумма. Введите положительное число:",
            dice_bet_cancel_keyboard(),
        )
        await _safe_delete(message)
        return

    data = await state.get_data()
    mode = data["mode"]
    bet_type = data["bet_type"]
    number = data.get("number")
    pair = tuple(data["pair"]) if data.get("pair") else None

    user_id = message.from_user.id
    profile = get_profile_stats(user_id)
    if profile["balance"] < amount:
        await edit_panel(
            bot, state, message.chat.id,
            f"❌ Недостаточно средств на балансе. У вас: ${profile['balance']:,.2f}",
            dice_bet_cancel_keyboard(),
        )
        await _safe_delete(message)
        return

    multiplier = MULTIPLIERS[(mode, bet_type)]
    profile["balance"] -= amount
    await _safe_delete(message)

    if mode == "1":
        roll = await message.answer_dice(emoji="🎲")
        await asyncio.sleep(4)
        value = roll.dice.value
        win = check_bet_1(value, bet_type, number)
        result_line = f"Выпало: 🎲 {value}"
    else:
        roll1 = await message.answer_dice(emoji="🎲")
        await asyncio.sleep(2.5)
        roll2 = await message.answer_dice(emoji="🎲")
        await asyncio.sleep(4)
        v1, v2 = roll1.dice.value, roll2.dice.value
        win = check_bet_2(v1, v2, bet_type, pair)
        result_line = f"Выпало: 🎲 {v1} и 🎲 {v2} (сумма {v1 + v2})"

    payout = amount * multiplier if win else 0.0
    if win:
        profile["balance"] += payout
    log_game_round(user_id, bet=amount, win=payout)

    await state.clear()
    await edit_panel(
        bot, state, message.chat.id,
        format_dice_result_text(win, result_line, amount, multiplier, profile["balance"]),
        dice_mode_keyboard(),
    )
