"""
Раздел «Игры» для Lucky Dice bot.

Полностью переработан под референс-скрин:
- Один общий экран игры (не отдельные «меню» и «выбор кубиков»): сверху —
  подсказка + текущая Ставка/Баланс, дальше — ряд вкладок-игр (эмодзи),
  ряд «1 Бросок / 2 Броска / 3 Броска» и сетка исходов. Исходы и их
  множители — НАШИ, из MULTIPLIERS ниже (менять их не стали, только
  перерисовали интерфейс).
- Новая логика ставки: она больше не запрашивается отдельным сообщением
  под каждую игру. Сумма задаётся прямо в чате (например: "0.1$" или
  "$0.1") и сохраняется как ОБЩАЯ текущая ставка пользователя для ВСЕХ игр,
  пока он не пришлёт новое значение. Клик по исходу сразу разыгрывает раунд
  на эту сумму — шаг «введите сумму ставки» убран.

Пока полностью реализована игра «Кости» (1 и 2 кубика, «3 броска» оставлена
как заглушка — кнопка есть по образцу скрина, но логики под неё ещё нет).
Остальные игры (Футбол, Баскетбол, Дартс, Боулинг, Слот) — отдельные вкладки
верхнего ряда, по клику показывают "в разработке" (см. games_soon). Когда
будете готовы их делать — по структуре это будет копия блока «Кости» ниже,
только с другим набором bet_type/emoji и своей функцией отрисовки экрана,
подключённой к тому же game_id в GAMES_INFO.

Файл подключается в main.py:
    from games import router as games_router
    ...
    dp.include_router(games_router)
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter

from aiogram import Bot, Router
from aiogram.fsm.context import FSMContext
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
# Общая (для всех игр) текущая ставка пользователя
# --------------------------------------------------------------------------
# NOTE: как и USER_PROFILES в main.py — пока просто in-memory словарь.
# Задаётся сообщением в чате вида "0.1$" / "$0.1" (см. set_global_bet ниже)
# и используется КАЖДОЙ игрой, пока пользователь не пришлёт новое значение.

GLOBAL_BETS: dict[int, float] = {}


def get_current_bet(user_id: int) -> float:
    return GLOBAL_BETS.get(user_id, 0.0)


# Сообщение-ставка: "0.1$", "$0.1", "$ 0.1", "0,1$" и т.п. Специально требуем
# знак "$" в сообщении (а не голое число), чтобы бот не путал ставку со
# случайным числом, которое пользователь написал в чате по другому поводу.
BET_MESSAGE_RE = re.compile(
    r"^\$\s*(\d+(?:[.,]\d+)?)$|^(\d+(?:[.,]\d+)?)\s*\$$"
)


async def _is_bet_message(message: Message) -> bool:
    return bool(BET_MESSAGE_RE.fullmatch((message.text or "").strip()))


# --------------------------------------------------------------------------
# Множители и подписи ставок (не менялись — только интерфейс вокруг них)
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
    ("2", "eq7"): 6,  # не задано явно в ТЗ — справедливые 6х, см. NOTE в истории файла
    ("2", "double"): 6,  # не задано явно в ТЗ — справедливые 6х, см. NOTE в истории файла
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

# --------------------------------------------------------------------------
# Реестр игр — верхний ряд вкладок-переключателей (эмодзи), как на скрине.
# --------------------------------------------------------------------------

GAMES_INFO: list[dict] = [
    {"id": "dice", "emoji": "🎲", "callback": "games:dice:menu"},
    {"id": "football", "emoji": "⚽", "callback": "games:soon:football"},
    {"id": "basketball", "emoji": "🏀", "callback": "games:soon:basketball"},
    {"id": "darts", "emoji": "🎯", "callback": "games:soon:darts"},
    {"id": "bowling", "emoji": "🎳", "callback": "games:soon:bowling"},
    {"id": "slot", "emoji": "🎰", "callback": "games:soon:slot"},
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
# Клавиатуры
# --------------------------------------------------------------------------


def _btn(text: str, callback_data: str, selected: bool = False) -> InlineKeyboardButton:
    """Кнопка инлайн-клавиатуры; выделенная (текущий режим) подсвечивается
    через style="primary" — как уже используется в проекте для главных кнопок
    reply-клавиатуры (main_reply_keyboard в main.py)."""
    kwargs: dict = {"text": text, "callback_data": callback_data}
    if selected:
        kwargs["style"] = "primary"
    return InlineKeyboardButton(**kwargs)


def games_tabs_row() -> list[InlineKeyboardButton]:
    """Верхний ряд вкладок-переключателей между играми — только эмодзи,
    без названий (как на скрине)."""
    return [InlineKeyboardButton(text=g["emoji"], callback_data=g["callback"]) for g in GAMES_INFO]


def dice_throws_row(mode: str) -> list[InlineKeyboardButton]:
    """Ряд «1 Бросок / 2 Броска / 3 Броска». «3 Броска» — заглушка (в
    разработке), добавлена только для визуального соответствия скрину."""
    return [
        _btn("1 Бросок", "games:dice:mode:1", selected=mode == "1"),
        _btn("2 Броска", "games:dice:mode:2", selected=mode == "2"),
        _btn("3 Броска", "games:soon:dice3"),
    ]


def dice_screen_keyboard(mode: str) -> InlineKeyboardMarkup:
    rows = [games_tabs_row(), dice_throws_row(mode)]

    if mode == "1":
        rows += [
            [_btn("Чёт (x2)", "games:dice:play:1:even"), _btn("Нечёт (x2)", "games:dice:play:1:odd")],
            [_btn("Больше 3 (x2)", "games:dice:play:1:gt3"), _btn("Меньше 4 (x2)", "games:dice:play:1:lt4")],
            [
                _btn("1 (x6)", "games:dice:play:1:num:1"),
                _btn("2 (x6)", "games:dice:play:1:num:2"),
                _btn("3 (x6)", "games:dice:play:1:num:3"),
            ],
            [
                _btn("4 (x6)", "games:dice:play:1:num:4"),
                _btn("5 (x6)", "games:dice:play:1:num:5"),
                _btn("6 (x6)", "games:dice:play:1:num:6"),
            ],
        ]
    else:  # mode == "2"
        rows += [
            [_btn("Чёт (x4)", "games:dice:play:2:even"), _btn("Нечёт (x4)", "games:dice:play:2:odd")],
            [_btn("Больше 7 (x4)", "games:dice:play:2:gt7"), _btn("Меньше 7 (x4)", "games:dice:play:2:lt7")],
            [_btn("Ровно 7 (x6)", "games:dice:play:2:eq7"), _btn("Дубль (x6)", "games:dice:play:2:double")],
            [_btn("Угадать оба числа (x36)", "games:dice:pair:start")],
        ]

    rows.append(
        [
            InlineKeyboardButton(
                text="Назад",
                callback_data="menu:back",
                icon_custom_emoji_id=BACK_ICON_EMOJI_ID,
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dice_pair_pick_keyboard(step: int) -> InlineKeyboardMarkup:
    prefix = "games:dice:pair1:" if step == 1 else "games:dice:pair2:"
    back_target = "games:dice:mode:2" if step == 1 else "games:dice:pair:start"
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


# --------------------------------------------------------------------------
# Тексты
# --------------------------------------------------------------------------


def format_games_header(bet: float, balance: float) -> str:
    """Шапка экрана игры — как на скрине: подсказка + текущая ставка/баланс.
    Ставка задаётся не тут, а сообщением в чате (см. set_global_bet)."""
    return (
        "🖱 <b>Выберите исход игры, на который хотите сделать ставку!</b>\n\n"
        f"➕ Ставка: <b>${bet:,.2f}</b>\n"
        f"💳 Баланс: <b>${balance:,.2f}</b>"
    )


def pair_pick_text(step: int, first: int | None) -> str:
    if step == 1:
        return (
            f'<tg-emoji emoji-id="{DICE_ICON_EMOJI_ID}">🎲</tg-emoji> '
            "<b>Угадать оба числа (x36)</b>\n\n<i>Выберите первое число:</i>"
        )
    return (
        f'<tg-emoji emoji-id="{DICE_ICON_EMOJI_ID}">🎲</tg-emoji> '
        f"<b>Угадать оба числа (x36)</b>\n\nПервое число: <b>{first}</b>\n<i>Выберите второе число:</i>"
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
# Отрисовка единого экрана «Кости»
# --------------------------------------------------------------------------


def render_dice_screen(user_id: int, mode: str) -> tuple[str, InlineKeyboardMarkup]:
    bet = get_current_bet(user_id)
    balance = get_profile_stats(user_id)["balance"]
    return format_games_header(bet, balance), dice_screen_keyboard(mode)


# --------------------------------------------------------------------------
# Хендлеры — вход в раздел игр (сразу открывается экран «Кости», как на скрине)
# --------------------------------------------------------------------------


@router.message(lambda m: m.text == "Игры")
async def games_section(message: Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await state.clear()
    await state.update_data(mode="1")
    text, kb = render_dice_screen(message.from_user.id, "1")
    sent = await message.answer(text, reply_markup=kb)
    await state.update_data(panel_chat_id=sent.chat.id, panel_message_id=sent.message_id)


@router.callback_query(lambda c: c.data.startswith("games:soon:"))
async def games_soon(callback: CallbackQuery) -> None:
    name = callback.data.split(":", 2)[2]
    label = GAME_STUB_LABELS.get(name, "3 броска" if name == "dice3" else "Эта игра")
    await callback.answer(f"{label} — в разработке, скоро появится!", show_alert=True)


# --------------------------------------------------------------------------
# Хендлеры — Кости: вкладка / переключение режима «Бросков»
# --------------------------------------------------------------------------


@router.callback_query(lambda c: c.data == "games:dice:menu")
async def dice_tab(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    mode = data.get("mode", "1")
    text, kb = render_dice_screen(callback.from_user.id, mode)
    await callback.message.edit_text(text, reply_markup=kb)
    await state.update_data(
        mode=mode,
        panel_chat_id=callback.message.chat.id,
        panel_message_id=callback.message.message_id,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data in ("games:dice:mode:1", "games:dice:mode:2"))
async def dice_mode_switch(callback: CallbackQuery, state: FSMContext) -> None:
    mode = callback.data.rsplit(":", 1)[1]
    await state.update_data(mode=mode)
    text, kb = render_dice_screen(callback.from_user.id, mode)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# --------------------------------------------------------------------------
# Хендлеры — Кости: «угадать оба числа» (двухшаговый выбор пары)
# --------------------------------------------------------------------------


@router.callback_query(lambda c: c.data == "games:dice:pair:start")
async def dice_pair_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(mode="2", pair_first=None)
    await callback.message.edit_text(pair_pick_text(1, None), reply_markup=dice_pair_pick_keyboard(step=1))
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("games:dice:pair1:"))
async def dice_pair_first(callback: CallbackQuery, state: FSMContext) -> None:
    first = int(callback.data.rsplit(":", 1)[1])
    await state.update_data(pair_first=first)
    await callback.message.edit_text(pair_pick_text(2, first), reply_markup=dice_pair_pick_keyboard(step=2))
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("games:dice:pair2:"))
async def dice_pair_second(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    second = int(callback.data.rsplit(":", 1)[1])
    data = await state.get_data()
    first = data.get("pair_first")
    await _play_dice(callback, state, bot, mode="2", bet_type="pair", number=None, pair=(first, second))


# --------------------------------------------------------------------------
# Хендлеры — Кости: клик по исходу → мгновенный розыгрыш на текущую ставку
# --------------------------------------------------------------------------


@router.callback_query(lambda c: c.data.startswith("games:dice:play:"))
async def dice_play(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    # games : dice : play : <mode> : <type> [ : <number> ]
    parts = callback.data.split(":")
    mode, bet_type = parts[3], parts[4]
    number = int(parts[5]) if bet_type == "num" else None
    await _play_dice(callback, state, bot, mode=mode, bet_type=bet_type, number=number, pair=None)


async def _play_dice(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    *,
    mode: str,
    bet_type: str,
    number: int | None,
    pair: tuple[int, int] | None,
) -> None:
    remember_user(callback.from_user)
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    amount = get_current_bet(user_id)
    if amount <= 0:
        await callback.answer(
            'Сначала задайте ставку в чате, например: "0.1$"',
            show_alert=True,
        )
        return

    profile = get_profile_stats(user_id)
    if profile["balance"] < amount:
        await callback.answer(f"❌ Недостаточно средств. Баланс: ${profile['balance']:,.2f}", show_alert=True)
        return

    await callback.answer()

    multiplier = MULTIPLIERS[(mode, bet_type)]
    profile["balance"] -= amount

    if mode == "1":
        roll = await bot.send_dice(chat_id, emoji="🎲")
        await asyncio.sleep(4)
        value = roll.dice.value
        win = check_bet_1(value, bet_type, number)
        result_line = f"Выпало: 🎲 {value}"
    else:
        roll1 = await bot.send_dice(chat_id, emoji="🎲")
        await asyncio.sleep(2.5)
        roll2 = await bot.send_dice(chat_id, emoji="🎲")
        await asyncio.sleep(4)
        v1, v2 = roll1.dice.value, roll2.dice.value
        win = check_bet_2(v1, v2, bet_type, pair)
        result_line = f"Выпало: 🎲 {v1} и 🎲 {v2} (сумма {v1 + v2})"

    payout = amount * multiplier if win else 0.0
    if win:
        profile["balance"] += payout
    log_game_round(user_id, bet=amount, win=payout)

    await state.update_data(mode=mode)
    result_text = format_dice_result_text(win, result_line, amount, multiplier, profile["balance"])
    await edit_panel(bot, state, chat_id, result_text, dice_screen_keyboard(mode))


# --------------------------------------------------------------------------
# Хендлер — установка общей ставки сообщением в чате ("0.1$" / "$0.1")
# --------------------------------------------------------------------------


@router.message(_is_bet_message)
async def set_global_bet(message: Message, state: FSMContext, bot: Bot) -> None:
    remember_user(message.from_user)

    match = BET_MESSAGE_RE.fullmatch((message.text or "").strip())
    raw = (match.group(1) or match.group(2)) if match else None
    await _safe_delete(message)
    if raw is None:
        return

    try:
        amount = float(raw.replace(",", "."))
    except ValueError:
        return
    if amount <= 0:
        return

    user_id = message.from_user.id
    GLOBAL_BETS[user_id] = amount

    # Обновляем текущую панель (если пользователь на экране игры — увидит
    # новую ставку сразу же, как на скрине; если панели ещё нет — edit_panel
    # сам создаст новую).
    data = await state.get_data()
    mode = data.get("mode", "1")
    text, kb = render_dice_screen(user_id, mode)
    await edit_panel(bot, state, message.chat.id, text, kb)
