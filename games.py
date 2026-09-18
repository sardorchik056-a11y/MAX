"""
Раздел «Игры» для Lucky Dice bot.

Один общий экран игры (без отдельных «меню» и «выбор кубиков»): сверху —
подсказка + текущая Ставка/Баланс, дальше — ряд вкладок-игр (эмодзи), ряд
«1 Бросок / 2 Броска / 3 Броска» и сетка исходов. Логика ставок и исходы
(MULTIPLIERS/BET_LABELS/check_bet_1/check_bet_2) — общие для всех игр,
использующих бросок 1–6 (bot.send_dice возвращает value 1..6 и для 🎲, и
для 🎯), поэтому «Кости» и «Дартс» — это одна и та же механика с разным
эмодзи броска.

Полностью реализованы: 🎲 Кости и 🎯 Дартс (1 и 2 броска; «3 броска»
оставлена как заглушка — кнопка есть по образцу референс-скрина, но логики
под неё пока нет). Остальные игры (⚽ Футбол, 🏀 Баскетбол, 🎳 Боулинг) —
отдельные вкладки верхнего ряда, по клику показывают "в разработке" (см.
games_soon). «Слот» из раздела убран.

Когда будете добавлять новую полноценную игру — впишите её в GAMES_INFO с
implemented=True и emoji для bot.send_dice, остальное (клавиатуры, розыгрыш,
проверка исходов) подхватится автоматически.

Новая логика ставки: сумма не запрашивается отдельным сообщением под каждую
игру, а задаётся прямо в чате (например: "0.1$" или "$0.1") и сохраняется
как ОБЩАЯ текущая ставка пользователя для ВСЕХ игр, пока он не пришлёт новое
значение. Клик по исходу сразу разыгрывает раунд на эту сумму.

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
# Множители и подписи ставок — общие для любой игры с броском 1..6
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

# --------------------------------------------------------------------------
# Реестр игр — верхний ряд вкладок-переключателей (эмодзи), как на скрине.
# implemented=True → полноценная игра (своя механика 1/2 броска + исходы).
# implemented=False → вкладка-заглушка ("в разработке").
# --------------------------------------------------------------------------

GAMES_INFO: list[dict] = [
    {"id": "dice", "emoji": "🎲", "title": "Кости", "implemented": True},
    {"id": "darts", "emoji": "🎯", "title": "Дартс", "implemented": True},
    {"id": "football", "emoji": "⚽", "title": "Футбол", "implemented": False},
    {"id": "basketball", "emoji": "🏀", "title": "Баскетбол", "implemented": False},
    {"id": "bowling", "emoji": "🎳", "title": "Боулинг", "implemented": False},
]

GAME_TITLE: dict[str, str] = {g["id"]: g["title"] for g in GAMES_INFO}
GAME_EMOJI: dict[str, str] = {g["id"]: g["emoji"] for g in GAMES_INFO}
IMPLEMENTED_GAMES: set[str] = {g["id"] for g in GAMES_INFO if g["implemented"]}

GAME_STUB_LABELS = {g["id"]: f"{g['emoji']} {g['title']}" for g in GAMES_INFO if not g["implemented"]}

BACK_ICON_EMOJI_ID = "6039539366177541657"  # тот же "Назад", что в других разделах
DICE_ICON_EMOJI_ID = "5260547274957672345"  # 🎲, тот же, что в шапке "Lucky Dice"

# tg-emoji для заголовка экрана «угадать оба числа» — для костей берём тот же
# кастомный эмодзи, что и в шапке бота; для остальных игр — обычный юникод.
GAME_HEADER_MARKUP: dict[str, str] = {
    "dice": f'<tg-emoji emoji-id="{DICE_ICON_EMOJI_ID}">🎲</tg-emoji>',
    "darts": "🎯",
}


# --------------------------------------------------------------------------
# Проверка выигрышных условий (общая для любой игры с броском 1..6)
# --------------------------------------------------------------------------


def check_bet_1(value: int, bet_type: str, number: int | None) -> bool:
    """Проверяет ставку на одном броске."""
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
    """Проверяет ставку на двух бросках."""
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


def games_tabs_row(active_game: str) -> list[InlineKeyboardButton]:
    """Верхний ряд вкладок-переключателей между играми — только эмодзи, без
    названий (как на скрине). Активная игра подсвечена."""
    buttons = []
    for g in GAMES_INFO:
        cb = f"games:tab:{g['id']}" if g["implemented"] else f"games:soon:{g['id']}"
        buttons.append(_btn(g["emoji"], cb, selected=g["id"] == active_game))
    return buttons


def throws_row(game_id: str, mode: str) -> list[InlineKeyboardButton]:
    """Ряд «1 Бросок / 2 Броска / 3 Броска». «3 Броска» — заглушка (в
    разработке), добавлена только для визуального соответствия скрину."""
    return [
        _btn("1 Бросок", f"games:mode:{game_id}:1", selected=mode == "1"),
        _btn("2 Броска", f"games:mode:{game_id}:2", selected=mode == "2"),
        _btn("3 Броска", f"games:soon:{game_id}3"),
    ]


def game_screen_keyboard(game_id: str, mode: str) -> InlineKeyboardMarkup:
    rows = [games_tabs_row(game_id), throws_row(game_id, mode)]

    if mode == "1":
        rows += [
            [
                _btn("Чёт (x2)", f"games:play:{game_id}:1:even"),
                _btn("Нечёт (x2)", f"games:play:{game_id}:1:odd"),
            ],
            [
                _btn("Больше 3 (x2)", f"games:play:{game_id}:1:gt3"),
                _btn("Меньше 4 (x2)", f"games:play:{game_id}:1:lt4"),
            ],
            [
                _btn("1 (x6)", f"games:play:{game_id}:1:num:1"),
                _btn("2 (x6)", f"games:play:{game_id}:1:num:2"),
                _btn("3 (x6)", f"games:play:{game_id}:1:num:3"),
            ],
            [
                _btn("4 (x6)", f"games:play:{game_id}:1:num:4"),
                _btn("5 (x6)", f"games:play:{game_id}:1:num:5"),
                _btn("6 (x6)", f"games:play:{game_id}:1:num:6"),
            ],
        ]
    else:  # mode == "2"
        rows += [
            [
                _btn("Чёт (x4)", f"games:play:{game_id}:2:even"),
                _btn("Нечёт (x4)", f"games:play:{game_id}:2:odd"),
            ],
            [
                _btn("Больше 7 (x4)", f"games:play:{game_id}:2:gt7"),
                _btn("Меньше 7 (x4)", f"games:play:{game_id}:2:lt7"),
            ],
            [
                _btn("Ровно 7 (x6)", f"games:play:{game_id}:2:eq7"),
                _btn("Дубль (x6)", f"games:play:{game_id}:2:double"),
            ],
            [_btn("Угадать оба числа (x36)", f"games:pair:start:{game_id}")],
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


def pair_pick_keyboard(game_id: str, step: int) -> InlineKeyboardMarkup:
    prefix = f"games:pair1:{game_id}:" if step == 1 else f"games:pair2:{game_id}:"
    back_target = f"games:mode:{game_id}:2" if step == 1 else f"games:pair:start:{game_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            games_tabs_row(game_id),
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


def pair_pick_text(game_id: str, step: int, first: int | None) -> str:
    icon = GAME_HEADER_MARKUP.get(game_id, GAME_EMOJI.get(game_id, "🎲"))
    title = GAME_TITLE.get(game_id, "Игра")
    if step == 1:
        return f"{icon} <b>{title} — Угадать оба числа (x36)</b>\n\n<i>Выберите первое число:</i>"
    return (
        f"{icon} <b>{title} — Угадать оба числа (x36)</b>\n\n"
        f"Первое число: <b>{first}</b>\n<i>Выберите второе число:</i>"
    )


def format_round_result_text(win: bool, result_line: str, amount: float, multiplier: float, new_balance: float) -> str:
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
# Отрисовка единого экрана игры
# --------------------------------------------------------------------------


def render_game_screen(user_id: int, game_id: str, mode: str) -> tuple[str, InlineKeyboardMarkup]:
    bet = get_current_bet(user_id)
    balance = get_profile_stats(user_id)["balance"]
    return format_games_header(bet, balance), game_screen_keyboard(game_id, mode)


# --------------------------------------------------------------------------
# Хендлеры — вход в раздел игр (сразу открывается экран «Кости», как на скрине)
# --------------------------------------------------------------------------


@router.message(lambda m: m.text == "Игры")
async def games_section(message: Message, state: FSMContext) -> None:
    remember_user(message.from_user)
    await state.clear()
    await state.update_data(game="dice", mode="1")
    text, kb = render_game_screen(message.from_user.id, "dice", "1")
    sent = await message.answer(text, reply_markup=kb)
    await state.update_data(panel_chat_id=sent.chat.id, panel_message_id=sent.message_id)


@router.callback_query(lambda c: c.data.startswith("games:soon:"))
async def games_soon(callback: CallbackQuery) -> None:
    name = callback.data.split(":", 2)[2]
    if name.endswith("3") and name[:-1] in GAME_TITLE:
        label = f"{GAME_TITLE[name[:-1]]} (3 броска)"
    else:
        label = GAME_STUB_LABELS.get(name, "Эта игра")
    await callback.answer(f"{label} — в разработке, скоро появится!", show_alert=True)


# --------------------------------------------------------------------------
# Хендлеры — переключение игры (вкладка) / режима «Бросков»
# --------------------------------------------------------------------------


@router.callback_query(lambda c: c.data.startswith("games:tab:"))
async def game_tab(callback: CallbackQuery, state: FSMContext) -> None:
    game_id = callback.data.split(":", 2)[2]
    if game_id not in IMPLEMENTED_GAMES:
        await callback.answer("Эта игра — в разработке, скоро появится!", show_alert=True)
        return

    data = await state.get_data()
    mode = data.get("mode", "1") if data.get("game") == game_id else "1"

    text, kb = render_game_screen(callback.from_user.id, game_id, mode)
    await callback.message.edit_text(text, reply_markup=kb)
    await state.update_data(
        game=game_id,
        mode=mode,
        panel_chat_id=callback.message.chat.id,
        panel_message_id=callback.message.message_id,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("games:mode:"))
async def game_mode_switch(callback: CallbackQuery, state: FSMContext) -> None:
    # games : mode : <game_id> : <mode>
    _, _, game_id, mode = callback.data.split(":")
    await state.update_data(game=game_id, mode=mode)
    text, kb = render_game_screen(callback.from_user.id, game_id, mode)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# --------------------------------------------------------------------------
# Хендлеры — «угадать оба числа» (двухшаговый выбор пары)
# --------------------------------------------------------------------------


@router.callback_query(lambda c: c.data.startswith("games:pair:start:"))
async def pair_start(callback: CallbackQuery, state: FSMContext) -> None:
    game_id = callback.data.split(":", 3)[3]
    await state.update_data(game=game_id, mode="2", pair_first=None)
    await callback.message.edit_text(pair_pick_text(game_id, 1, None), reply_markup=pair_pick_keyboard(game_id, step=1))
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("games:pair1:"))
async def pair_first(callback: CallbackQuery, state: FSMContext) -> None:
    # games : pair1 : <game_id> : <n>
    _, _, game_id, n = callback.data.split(":")
    first = int(n)
    await state.update_data(pair_first=first)
    await callback.message.edit_text(pair_pick_text(game_id, 2, first), reply_markup=pair_pick_keyboard(game_id, step=2))
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith("games:pair2:"))
async def pair_second(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    # games : pair2 : <game_id> : <n>
    _, _, game_id, n = callback.data.split(":")
    second = int(n)
    data = await state.get_data()
    first = data.get("pair_first")
    await _play_round(callback, state, bot, game_id=game_id, mode="2", bet_type="pair", number=None, pair=(first, second))


# --------------------------------------------------------------------------
# Хендлеры — клик по исходу → мгновенный розыгрыш на текущую ставку
# --------------------------------------------------------------------------


@router.callback_query(lambda c: c.data.startswith("games:play:"))
async def game_play(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    # games : play : <game_id> : <mode> : <type> [ : <number> ]
    parts = callback.data.split(":")
    game_id, mode, bet_type = parts[2], parts[3], parts[4]
    number = int(parts[5]) if bet_type == "num" else None
    await _play_round(callback, state, bot, game_id=game_id, mode=mode, bet_type=bet_type, number=number, pair=None)


async def _play_round(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    *,
    game_id: str,
    mode: str,
    bet_type: str,
    number: int | None,
    pair: tuple[int, int] | None,
) -> None:
    remember_user(callback.from_user)
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    emoji = GAME_EMOJI.get(game_id, "🎲")

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
        roll = await bot.send_dice(chat_id, emoji=emoji)
        await asyncio.sleep(4)
        value = roll.dice.value
        win = check_bet_1(value, bet_type, number)
        result_line = f"Выпало: {emoji} {value}"
    else:
        roll1 = await bot.send_dice(chat_id, emoji=emoji)
        await asyncio.sleep(2.5)
        roll2 = await bot.send_dice(chat_id, emoji=emoji)
        await asyncio.sleep(4)
        v1, v2 = roll1.dice.value, roll2.dice.value
        win = check_bet_2(v1, v2, bet_type, pair)
        result_line = f"Выпало: {emoji} {v1} и {emoji} {v2} (сумма {v1 + v2})"

    payout = amount * multiplier if win else 0.0
    if win:
        profile["balance"] += payout
    log_game_round(user_id, bet=amount, win=payout)

    await state.update_data(game=game_id, mode=mode)
    result_text = format_round_result_text(win, result_line, amount, multiplier, profile["balance"])
    await edit_panel(bot, state, chat_id, result_text, game_screen_keyboard(game_id, mode))


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
    game_id = data.get("game", "dice")
    mode = data.get("mode", "1")
    text, kb = render_game_screen(user_id, game_id, mode)
    await edit_panel(bot, state, message.chat.id, text, kb)
