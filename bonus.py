import asyncio
from aiogram import Bot, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple

import bonus as bonus_module  # бонусный баланс (см. bonus.py)

# ========== СОЗДАЁМ РОУТЕР СРАЗУ ПОСЛЕ ИМПОРТОВ ==========
router = Router()

# Единственный актуальный экземпляр BettingGame. Регистрируется автоматически
# в BettingGame.__init__, поэтому НЕ нужно делать `from main import betting_game`
# (это создаёт отдельный, второй экземпляр модуля main при запуске `python main.py`,
# в котором глобальная переменная betting_game так и останется None).
_shared_betting_game = None


def set_betting_game(bg):
    global _shared_betting_game
    _shared_betting_game = bg


def get_betting_game():
    return _shared_betting_game


# Та же схема, что и с _shared_betting_game выше: main.py регистрирует свой
# games_callback здесь один раз при импорте (см. games_module.set_games_callback
# в main.py), вместо того чтобы cancel_bet делал `from main import games_callback`
# при каждом вызове — такой импорт заново подгружал бы main.py как отдельный
# модуль "main" (та же причина, по которой раньше расходился баланс — см.
# шапку storage.py) и плодил бы отдельный, рассинхронизированный экземпляр
# состояния main.py.
_games_callback = None


def set_games_callback(cb):
    global _games_callback
    _games_callback = cb


def get_games_callback():
    return _games_callback


# main.py регистрирует здесь свою remember_user (ник/юзернейм для топа) — по той же
# схеме, что и games_callback выше, без `from main import ...`. Нужна, чтобы игрок,
# который играет по «старой» кнопке (например, после перезапуска бота, когда /start
# он ещё не нажимал), всё равно попадал в топ под своим именем, а не как «Игрок 123».
_remember_user = None


def set_remember_user(fn):
    global _remember_user
    _remember_user = fn

try:
    from database import save_game_result as db_save_game_result, update_balance as db_update_balance
except ImportError:
    async def db_save_game_result(user_id, game_name, score): pass
    async def db_update_balance(user_id, amount): return None

try:
    from referrals import notify_referrer_commission
except ImportError:
    async def notify_referrer_commission(user_id: int, bet_amount: float):
        pass

try:
    from leaders import record_game_result
except ImportError:
    def record_game_result(user_id, name, bet, win, game_name=""):
        pass

logging.basicConfig(level=logging.INFO)

MIN_BET = 0.1
MAX_BET = 10000.0

RATE_LIMIT_SECONDS = 3
user_last_bet_time: Dict[int, datetime] = {}

user_current_bet: Dict[int, float] = {}

# Из какого баланса играет игрок: 'real' (по умолчанию) или 'bonus'.
# 'bonus' включается командой «0.1 бонус», 'real' — командой «0.1$».
# Независимо от режима, если реальный баланс меньше MIN_BET, ставка автоматически идёт с бонусного.
BET_MODE_REAL = 'real'
BET_MODE_BONUS = 'bonus'
user_bet_mode: Dict[int, str] = {}

SET_BET_PATTERN = re.compile(r'^\s*(\d+(?:[.,]\d+)?)\s*\$\s*$')
# «0.1 бонус», «0,5 бонус», «1$ бонус», «2 bonus»
SET_BONUS_BET_PATTERN = re.compile(r'^\s*(\d+(?:[.,]\d+)?)\s*\$?\s*(?:бонус\w*|bonus\w*)\s*$', re.IGNORECASE)
# «/куб чет 0.1 бонус» — бонусная ставка только на этот раз (режим не меняется)
TEXT_BONUS_SUFFIX = re.compile(r'\s(?:бонус\w*|bonus\w*)\s*$', re.IGNORECASE)
BONUS_HOWTO = "Бонусная ставка: 0.1 бонус"

def e(eid: str, fallback: str = "•") -> str:
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


EMOJI_BACK   = "6039539366177541657"
EMOJI_CROSS  = "5906949717859230132"
EMOJI_COIN   = "5285484782791177666"  # 💲
EMOJI_CHET       = "5330320040883411678"
EMOJI_NECHET     = "5391032818111363540"
EMOJI_MORE       = "5449683594425410231"
EMOJI_LESS       = "5447183459602669338"
EMOJI_2MORE      = "5429651785352501917"
EMOJI_2LESS      = "5429518319243775957"
EMOJI_NUMBER     = "5456140674028019486"
EMOJI_GOAL       = "5206607081334906820"
EMOJI_3POINT     = "5397782960512444700"
EMOJI_MISS       = "5210952531676504517"

EMOJI_REPLAY     = "6039859895291877126"
EMOJI_RAISE      = "5890848474563352982"
EMOJI_LOWER      = "5902206159095339799"
EMOJI_CHANGE     = "5811925731785052842"

EMOJI_MAGNIFY       = "5231012545799666522"
EMOJI_BET_LABEL     = "5904462880941545555"  # 🪙 — "Ставка"
EMOJI_BALANCE_LABEL = "5769126056262898415"  # 👛 — "Баланс"
EMOJI_CHOOSE_GAME   = "5864019342873598613"  # 🧠 — "Выберите игру"

# --- ТИПЫ СТАВОК ДЛЯ 1 КУБА ---
DICE_BET_TYPES = {
    'куб_нечет':   {'values': [1, 3, 5], 'multiplier': 2.0},
    'куб_чет':     {'values': [2, 4, 6], 'multiplier': 2.0},
    'куб_мал':     {'values': [1, 2, 3], 'multiplier': 2.0},
    'куб_бол':     {'values': [4, 5, 6], 'multiplier': 2.0},
    'куб_1':       {'values': [1], 'multiplier': 6.0},
    'куб_2':       {'values': [2], 'multiplier': 6.0},
    'куб_3':       {'values': [3], 'multiplier': 6.0},
    'куб_4':       {'values': [4], 'multiplier': 6.0},
    'куб_5':       {'values': [5], 'multiplier': 6.0},
    'куб_6':       {'values': [6], 'multiplier': 6.0},
}

# --- ТИПЫ СТАВОК ДЛЯ 2 КУБОВ ---
DICE_2_BET_TYPES = {
    'куб2_сумма_ровно7':   {'multiplier': 6.0, 'special': 'double_dice_sum_exact'},
    'куб2_сумма_больше7':  {'multiplier': 2.4, 'special': 'double_dice_sum'},
    'куб2_сумма_меньше7':  {'multiplier': 2.4, 'special': 'double_dice_sum'},
    'куб2_обачет':         {'multiplier': 4.0, 'special': 'double_dice_parity'},
    'куб2_обанечет':       {'multiplier': 4.0, 'special': 'double_dice_parity'},
    'куб2_обабольше':      {'multiplier': 4.0, 'special': 'double_dice_range'},
    'куб2_обаменьше':      {'multiplier': 4.0, 'special': 'double_dice_range'},
    'куб2_любойдубль':     {'multiplier': 6.0, 'special': 'double_dice_any_double'},
    'куб2_конкретныйдубль': {'multiplier': 36.0, 'special': 'double_dice_specific_double'},
    'куб2_произведение':   {'multiplier': 4.0, 'special': 'double_dice_product'},
}

# --- ТИПЫ СТАВОК ДЛЯ 3 КУБОВ ---
DICE_3_BET_TYPES = {
    'куб3_3чет':    {'multiplier': 8.0, 'special': 'triple_dice_parity'},
    'куб3_3нечет':  {'multiplier': 8.0, 'special': 'triple_dice_parity'},
    'куб3_больше10': {'multiplier': 8.0, 'special': 'triple_dice_range'},
    'куб3_меньше10': {'multiplier': 8.0, 'special': 'triple_dice_range'},
    'куб3_любойтрипл': {'multiplier': 36.0, 'special': 'triple_dice_any_triple'},
    'куб3_конкретныйтрипл': {'multiplier': 216.0, 'special': 'triple_dice_specific_triple'},
    'куб3_произведение': {'multiplier': 12.7, 'special': 'triple_dice_product'},
}

BASKETBALL_BET_TYPES = {
    # «Застрял» (3) тоже считается промахом: мяч не попал в корзину.
    # Значения 1, 2, 3 = 3 из 5 исходов -> шанс 60% -> честный множитель ~1.66.
    'баскет_промах':    {'values': [1, 2, 3], 'multiplier': 1.66},
    'баскет_отскок':    {'values': [1],    'multiplier': 5.0},
    'баскет_застрял':   {'values': [3],    'multiplier': 5.0},
    'баскет_любойгол':  {'values': [4, 5], 'multiplier': 2.5},
    'баскет_чистыйгол': {'values': [5],    'multiplier': 5.0},
    # --- 2 броска подряд (по аналогии с футбольными дублями) ---
    'баскет_любойдубль':      {'multiplier': 5.0,  'special': 'double_basketball_any_double'},
    'баскет_конкретныйдубль': {'multiplier': 25.0, 'special': 'double_basketball_specific_double'},
    'баскет_двагола':         {'multiplier': 6.25, 'special': 'double_basketball_both_goal'},
    'баскет_двапромаха':      {'multiplier': 2.77, 'special': 'double_basketball_both_miss'},
}

FOOTBALL_BET_TYPES = {
    'футбол_гол':  {'values': [3, 4, 5], 'multiplier': 1.65},
    'футбол_мимо': {'values': [1, 2],    'multiplier': 2.5},
    # --- точные исходы (по одному на каждое из 5 значений эмодзи ⚽) ---
    'футбол_штанга':    {'values': [2], 'multiplier': 5.0},
    'футбол_мимоворот': {'values': [1], 'multiplier': 5.0},
    'футбол_угол':      {'values': [4], 'multiplier': 5.0},
    'футбол_центр':     {'values': [3], 'multiplier': 5.0},
    'футбол_девятка':   {'values': [5], 'multiplier': 5.0},
    # --- дубли (2 мяча подряд, как в кубах, но с более низкими множителями) ---
    'футбол_любойдубль':      {'multiplier': 5.0,  'special': 'double_football_any_double'},
    'футбол_конкретныйдубль': {'multiplier': 23.0, 'special': 'double_football_specific_double'},
}

DART_BET_TYPES = {
    'дартс_белое':   {'values': [3, 5], 'multiplier': 3.0},
    # Центр (6) больше не считается красным сектором — это отдельный исход.
    'дартс_красное': {'values': [2, 4], 'multiplier': 3.0},
    'дартс_мимо':    {'values': [1],    'multiplier': 6.0},
    'дартс_центр':   {'values': [6],    'multiplier': 6.0},
}

# --- ТИПЫ СТАВОК ДЛЯ ДВОЙНОГО ДАРТСА (2 броска подряд, по аналогии с футболом/кубами) ---
DART_2_BET_TYPES = {
    'дартс2_дубльбелое':   {'multiplier': 9.0,  'special': 'double_darts_category', 'category': 'дартс_белое'},
    'дартс2_дублькрасное': {'multiplier': 9.0,  'special': 'double_darts_category', 'category': 'дартс_красное'},
    'дартс2_дубльцентр':   {'multiplier': 36.0, 'special': 'double_darts_category', 'category': 'дартс_центр'},
    'дартс2_дубльмимо':    {'multiplier': 36.0, 'special': 'double_darts_category', 'category': 'дартс_мимо'},
}

BOWLING_BET_TYPES = {
    # --- 1 бросок: против бота ---
    'боулинг_поражение': {'values': [], 'multiplier': 2.0, 'special': 'bowling_vs'},
    'боулинг_победа':    {'values': [], 'multiplier': 2.0, 'special': 'bowling_vs'},
    # --- 1 бросок: точный исход. Значения Telegram-эмодзи 🎳:
    #     1 = промах (мимо всех кегль), 2 = сбита 1 кегля, 3 = 3 кегли,
    #     4 = 4 кегли, 5 = 5 кеглей, 6 = страйк (все 6). 6 исходов по 1/6 -> x6. ---
    'боулинг_промах':    {'values': [1], 'multiplier': 6.0},
    'боулинг_1из6':      {'values': [2], 'multiplier': 6.0},
    'боулинг_3из6':      {'values': [3], 'multiplier': 6.0},
    'боулинг_4из6':      {'values': [4], 'multiplier': 6.0},
    'боулинг_5из6':      {'values': [5], 'multiplier': 6.0},
    'боулинг_страйк':    {'values': [6], 'multiplier': 6.0},
    # --- 2 броска подряд (дубли, по аналогии с кубами) ---
    'боулинг_любойдубль':      {'multiplier': 6.0,  'special': 'double_bowling_any_double'},
    'боулинг_конкретныйдубль': {'multiplier': 36.0, 'special': 'double_bowling_specific_double'},
}

_BET_TYPE_DISPLAY_NAMES = {
    'куб_':     'Кубик',
    'куб2_':    '2 Куба',
    'куб3_':    '3 Куба',
    'баскет_':  'Баскетбол',
    'футбол_':  'Футбол',
    'дартс_':   'Дартс',
    'дартс2_':  'Дартс (дубль)',
    'боулинг_': 'Боулинг',
}

def _get_game_display_name(bet_type: str) -> str:
    for prefix, name in _BET_TYPE_DISPLAY_NAMES.items():
        if bet_type.startswith(prefix):
            return name
    return 'Эмодзи'


# --- Короткие коды ставок для callback_data (чтобы уложиться в лимит Telegram 64 байта) ---
BET_TYPE_TO_CODE = {
    'куб_нечет': 'd_odd', 'куб_чет': 'd_evn', 'куб_мал': 'd_low', 'куб_бол': 'd_hig',
    'куб_1': 'd_1', 'куб_2': 'd_2', 'куб_3': 'd_3', 'куб_4': 'd_4', 'куб_5': 'd_5', 'куб_6': 'd_6',
    'куб2_сумма_ровно7': 's_eq7', 'куб2_сумма_больше7': 's_gt7', 'куб2_сумма_меньше7': 's_lt7',
    'куб2_обачет': 's_bev', 'куб2_обанечет': 's_bod', 'куб2_любойдубль': 's_dbl',
    'куб2_обабольше': 's_bbig', 'куб2_обаменьше': 's_bsml',
    'куб2_конкретныйдубль': 's_sdbl', 'куб2_произведение': 's_prod',
    'куб3_3чет': 't_ev', 'куб3_3нечет': 't_od', 'куб3_больше10': 't_gt10', 'куб3_меньше10': 't_lt10',
    'куб3_любойтрипл': 't_trp', 'куб3_конкретныйтрипл': 't_strp', 'куб3_произведение': 't_prod',
    'баскет_промах': 'bk_m', 'баскет_отскок': 'bk_o', 'баскет_застрял': 'bk_z',
    'баскет_любойгол': 'bk_g', 'баскет_чистыйгол': 'bk_c',
    'баскет_любойдубль': 'bk_d', 'баскет_конкретныйдубль': 'bk_sd',
    'баскет_двагола': 'bk_2g', 'баскет_двапромаха': 'bk_2m',
    'футбол_гол': 'fb_g', 'футбол_мимо': 'fb_m',
    'футбол_штанга': 'fb_p', 'футбол_мимоворот': 'fb_w', 'футбол_угол': 'fb_a',
    'футбол_центр': 'fb_c', 'футбол_девятка': 'fb_9',
    'футбол_любойдубль': 'fb_d', 'футбол_конкретныйдубль': 'fb_sd',
    'дартс_белое': 'dt_w', 'дартс_красное': 'dt_r', 'дартс_мимо': 'dt_m', 'дартс_центр': 'dt_c',
    'дартс2_дубльбелое': 'dt2_w', 'дартс2_дублькрасное': 'dt2_r',
    'дартс2_дубльцентр': 'dt2_c', 'дартс2_дубльмимо': 'dt2_m',
    'боулинг_поражение': 'bw_l', 'боулинг_победа': 'bw_w', 'боулинг_страйк': 'bw_s',
    'боулинг_промах': 'bw_m', 'боулинг_1из6': 'bw_1', 'боулинг_3из6': 'bw_3',
    'боулинг_4из6': 'bw_4', 'боулинг_5из6': 'bw_5',
    'боулинг_любойдубль': 'bw_d', 'боулинг_конкретныйдубль': 'bw_sd',
}
CODE_TO_BET_TYPE = {v: k for k, v in BET_TYPE_TO_CODE.items()}

_OUTCOME_LABELS = {
    'куб_нечет': 'Нечёт', 'куб_чет': 'Чёт', 'куб_мал': 'Меньше (1-3)', 'куб_бол': 'Больше (4-6)',
    'куб_1': 'Число 1', 'куб_2': 'Число 2', 'куб_3': 'Число 3', 'куб_4': 'Число 4', 'куб_5': 'Число 5', 'куб_6': 'Число 6',
    'куб2_сумма_ровно7': 'Сумма = 7', 'куб2_сумма_больше7': 'Сумма > 7', 'куб2_сумма_меньше7': 'Сумма < 7',
    'куб2_обачет': 'Оба чёт', 'куб2_обанечет': 'Оба нечёт', 'куб2_любойдубль': 'Любой дубль',
    'куб2_обабольше': 'Оба больше (4-6)', 'куб2_обаменьше': 'Оба меньше (1-3)',
    'куб2_произведение': 'Произведение ≥18',
    'куб3_3чет': 'Три чёт', 'куб3_3нечет': 'Три нечёт', 'куб3_больше10': 'Три больше (4-6)', 'куб3_меньше10': 'Три меньше (1-3)',
    'куб3_любойтрипл': 'Любой трипл', 'куб3_произведение': 'Произведение ≥108',
    'баскет_промах': 'Промах', 'баскет_отскок': 'Отскок', 'баскет_застрял': 'Застрял',
    'баскет_любойгол': 'Любой гол', 'баскет_чистыйгол': 'Чистый гол',
    'баскет_любойдубль': 'Любой дубль', 'баскет_двагола': 'Два гола', 'баскет_двапромаха': 'Два промаха',
    'футбол_гол': 'Любой гол', 'футбол_мимо': 'Промах',
    'футбол_штанга': 'Штанга', 'футбол_мимоворот': 'Мимо ворот', 'футбол_угол': 'Гол под углом',
    'футбол_центр': 'Гол в центр', 'футбол_девятка': 'Девятка', 'футбол_любойдубль': 'Любой дубль',
    'дартс_белое': 'Белое', 'дартс_красное': 'Красное', 'дартс_мимо': 'Мимо', 'дартс_центр': 'Центр',
    'дартс2_дубльбелое': 'Дубль белое', 'дартс2_дублькрасное': 'Дубль красное',
    'дартс2_дубльцентр': 'Дубль центр', 'дартс2_дубльмимо': 'Дубль мимо',
    'боулинг_поражение': 'Поражение', 'боулинг_победа': 'Победа', 'боулинг_страйк': 'Страйк',
    'боулинг_промах': 'Промах', 'боулинг_1из6': 'Сбито 1/6', 'боулинг_3из6': 'Сбито 3/6',
    'боулинг_4из6': 'Сбито 4/6', 'боулинг_5из6': 'Сбито 5/6',
    'боулинг_любойдубль': 'Любой дубль',
}

# --- Автоматически строим "число -> название" для футбола из FOOTBALL_BET_TYPES,
#     чтобы кнопки дублей и текст ставки всегда были в одном месте синхронизированы. ---
_FOOTBALL_DOUBLE_TARGET_NAME: Dict[int, str] = {}
for _fb_bt, _fb_cfg in FOOTBALL_BET_TYPES.items():
    _fb_vals = _fb_cfg.get('values')
    if _fb_vals and len(_fb_vals) == 1:
        _FOOTBALL_DOUBLE_TARGET_NAME[_fb_vals[0]] = _OUTCOME_LABELS.get(_fb_bt, _fb_bt)


# --- Аналогично футболу: "число -> название" для конкретных дублей баскетбола,
#     построено автоматически из BASKETBALL_BET_TYPES (только по исходам с одним
#     конкретным значением — 2 и 4 общих названий не имеют, останутся как "2"/"4"). ---
_BASKETBALL_DOUBLE_TARGET_NAME: Dict[int, str] = {}
for _bk_bt, _bk_cfg in BASKETBALL_BET_TYPES.items():
    _bk_vals = _bk_cfg.get('values')
    if _bk_vals and len(_bk_vals) == 1:
        _BASKETBALL_DOUBLE_TARGET_NAME[_bk_vals[0]] = _OUTCOME_LABELS.get(_bk_bt, _bk_bt)


# --- Боулинг: "значение кубика -> название" для конкретных дублей (строится
#     автоматически из BOWLING_BET_TYPES по исходам с одним значением). ---
_BOWLING_DOUBLE_TARGET_NAME: Dict[int, str] = {}
for _bw_bt, _bw_cfg in BOWLING_BET_TYPES.items():
    _bw_vals = _bw_cfg.get('values')
    if _bw_vals and len(_bw_vals) == 1:
        _BOWLING_DOUBLE_TARGET_NAME[_bw_vals[0]] = _OUTCOME_LABELS.get(_bw_bt, _bw_bt)

# Короткие названия для кнопок раздела «2 броска»
_BOWLING_SHORT_NAME: Dict[int, str] = {1: 'Промах', 2: '1/6', 3: '3/6', 4: '4/6', 5: '5/6', 6: 'Страйк'}


def _get_outcome_label(bet_type: str, bet_config: dict) -> str:
    if bet_type == 'куб2_конкретныйдубль':
        t = bet_config.get('target', 0)
        return f'Дубль {t},{t}'
    if bet_type == 'футбол_конкретныйдубль':
        t = bet_config.get('target', 0)
        name = _FOOTBALL_DOUBLE_TARGET_NAME.get(t, str(t))
        return f'Дубль «{name}»'
    if bet_type == 'баскет_конкретныйдубль':
        t = bet_config.get('target', 0)
        name = _BASKETBALL_DOUBLE_TARGET_NAME.get(t, str(t))
        return f'Дубль «{name}»'
    if bet_type == 'боулинг_конкретныйдубль':
        t = bet_config.get('target', 0)
        name = _BOWLING_DOUBLE_TARGET_NAME.get(t, str(t))
        return f'Дубль «{name}»'
    if bet_type == 'куб3_конкретныйтрипл':
        t = bet_config.get('target', 0)
        return f'Трипл {t},{t},{t}'
    if bet_type == 'футбол_конкретныйдубль':
        t = bet_config.get('target', 0)
        return f'Дубль {t},{t}'
    return _OUTCOME_LABELS.get(bet_type, _get_game_display_name(bet_type))


def _bet_emoji_for(bet_type: str) -> str:
    if bet_type.startswith('куб'):
        return "🎲"
    elif bet_type.startswith('баскет_'):
        return "🏀"
    elif bet_type.startswith('футбол_'):
        return "⚽"
    elif bet_type.startswith('дартс_') or bet_type.startswith('дартс2_'):
        return "🎯"
    elif bet_type.startswith('боулинг_'):
        return "🎳"
    return "🎲"


def _menu_key_for(bet_type: str) -> str:
    if bet_type.startswith('куб3_'):
        return 'dice3'
    elif bet_type.startswith('куб2_'):
        return 'dice2'
    elif bet_type.startswith('куб_') or bet_type.startswith('куб'):
        return 'dice1'
    elif bet_type.startswith('баскет_'):
        return 'basketball'
    elif bet_type.startswith('футбол_'):
        return 'football'
    elif bet_type.startswith('дартс_') or bet_type.startswith('дартс2_'):
        return 'darts'
    elif bet_type in ('боулинг_любойдубль', 'боулинг_конкретныйдубль'):
        return 'bowling2'
    elif bet_type.startswith('боулинг_'):
        return 'bowling'
    return 'dice1'


def _build_replay_keyboard(user_id: int, bet_type: str, amount: float, bet_config: dict) -> Optional[InlineKeyboardMarkup]:
    code = BET_TYPE_TO_CODE.get(bet_type)
    if not code:
        return None
    target = bet_config.get('target') if bet_type in ('куб2_конкретныйдубль', 'куб3_конкретныйтрипл', 'футбол_конкретныйдубль', 'баскет_конкретныйдубль', 'боулинг_конкретныйдубль') else None
    target_str = str(target) if target is not None else ''

    def _cb(amt: float) -> str:
        amt = max(MIN_BET, min(MAX_BET, amt))
        return f"replay:{user_id}:{code}:{amt:.2f}:{target_str}"

    double_amt = min(round(amount * 2, 2), MAX_BET)
    half_amt = max(round(amount / 2, 2), MIN_BET)
    menu_key = _menu_key_for(bet_type)

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Повторить ({amount:.2f}$)", callback_data=_cb(amount), icon_custom_emoji_id=EMOJI_REPLAY
        )],
        [
            InlineKeyboardButton(
                text=f"x2 ({double_amt:.2f}$)", callback_data=_cb(double_amt), icon_custom_emoji_id=EMOJI_RAISE
            ),
            InlineKeyboardButton(
                text=f"÷2 ({half_amt:.2f}$)", callback_data=_cb(half_amt), icon_custom_emoji_id=EMOJI_LOWER
            ),
        ],
        [InlineKeyboardButton(
            text="Изменить исход", callback_data=f"backmenu:{user_id}:{menu_key}", icon_custom_emoji_id=EMOJI_CHANGE
        )],
    ])


COMMAND_MAPPING = {
    'фут':        'футбол',
    'fut':        'футбол',
    'foot':       'футбол',
    'футбол':     'футбол',
    'football':   'футбол',
    'баскет':     'баскет',
    'basket':     'баскет',
    'basketball': 'баскет',
    'баскетбол':  'баскет',
    'bask':       'баскет',
    'куб':    'куб',
    'dice':   'куб',
    'кубик':  'куб',
    'cube':   'куб',
    'дартс': 'дартс',
    'dart':  'дартс',
    'darts': 'дартс',
    'дарт':  'дартс',
    'боулинг': 'боулинг',
    'bowling': 'боулинг',
    'боул':    'боулинг',
    'bowl':    'боулинг',
}

BET_TYPE_MAPPING = {
    '3очка':      'баскет_чистыйгол',
    '3points':    'баскет_чистыйгол',
    '3':          'баскет_чистыйгол',
    'три':        'баскет_чистыйгол',
    'three':      'баскет_чистыйгол',
    'чистыйгол':  'баскет_чистыйгол',
    'чистый':     'баскет_чистыйгол',
    'clean':      'баскет_чистыйгол',
    'застрял':    'баскет_застрял',
    'stuck':      'баскет_застрял',
    'отскок':     'баскет_отскок',
    'rebound':    'баскет_отскок',
    'bounce':     'баскет_отскок',
    'любойгол':   'баскет_любойгол',
    'anygoal':    'баскет_любойгол',
    'промах':     'баскет_промах',
    'нечет':    'куб_нечет',
    'odd':      'куб_нечет',
    'нечетное': 'куб_нечет',
    'нечётное': 'куб_нечет',
    'чет':    'куб_чет',
    'even':   'куб_чет',
    'четное': 'куб_чет',
    'чётное': 'куб_чет',
    'мал':    'куб_мал',
    'small':  'куб_мал',
    'меньше': 'куб_мал',
    'less':   'куб_мал',
    'бол':    'куб_бол',
    'big':    'куб_бол',
    'больше': 'куб_бол',
    'more':   'куб_бол',
    '1': 'куб_1',
    '2': 'куб_2',
    '3': 'куб_3',
    '4': 'куб_4',
    '5': 'куб_5',
    '6': 'куб_6',
    'белое':   'дартс_белое',
    'white':   'дартс_белое',
    'белый':   'дартс_белое',
    'бел':     'дартс_белое',
    'красное': 'дартс_красное',
    'red':     'дартс_красное',
    'красный': 'дартс_красное',
    'крас':    'дартс_красное',
    'центр':  'дартс_центр',
    'center': 'дартс_центр',
    'bull':   'дартс_центр',
    'дубльбелое':   'дартс2_дубльбелое',
    'дубль_белое':  'дартс2_дубльбелое',
    'дубльwhite':   'дартс2_дубльбелое',
    'дублькрасное': 'дартс2_дублькрасное',
    'дубль_красное': 'дартс2_дублькрасное',
    'дубльred':     'дартс2_дублькрасное',
    'дубльцентр':   'дартс2_дубльцентр',
    'дубль_центр':  'дартс2_дубльцентр',
    'дубльbull':    'дартс2_дубльцентр',
    'дубльмимо':    'дартс2_дубльмимо',
    'дубль_мимо':   'дартс2_дубльмимо',
    'дубльmiss':    'дартс2_дубльмимо',
    'победа':    'боулинг_победа',
    'win':       'боулинг_победа',
    'victory':   'боулинг_победа',
    'побед':     'боулинг_победа',
    'поражение': 'боулинг_поражение',
    'lose':      'боулинг_поражение',
    'loss':      'боулинг_поражение',
    'пораж':     'боулинг_поражение',
    'страйк': 'боулинг_страйк',
    'strike': 'боулинг_страйк',
    'стр':    'боулинг_страйк',
}

# Текстовые названия исходов боулинга ("боулинг 1/6 0.5", "боулинг промах 1" и т.п.).
# Проверяются раньше общего BET_TYPE_MAPPING, т.к. там "промах" относится к баскетболу.
BOWLING_TEXT_ALIASES = {
    'промах': 'боулинг_промах', 'мимо': 'боулинг_промах', 'miss': 'боулинг_промах',
    '0/6': 'боулинг_промах', '0из6': 'боулинг_промах',
    '1/6': 'боулинг_1из6', '1из6': 'боулинг_1из6', '1of6': 'боулинг_1из6',
    '3/6': 'боулинг_3из6', '3из6': 'боулинг_3из6', '3of6': 'боулинг_3из6',
    '4/6': 'боулинг_4из6', '4из6': 'боулинг_4из6', '4of6': 'боулинг_4из6',
    '5/6': 'боулинг_5из6', '5из6': 'боулинг_5из6', '5of6': 'боулинг_5из6',
    '6/6': 'боулинг_страйк', '6из6': 'боулинг_страйк',
    'дубль': 'боулинг_любойдубль', 'любойдубль': 'боулинг_любойдубль',
    'double': 'боулинг_любойдубль', 'anydouble': 'боулинг_любойдубль',
}


class BetStates(StatesGroup):
    waiting_for_amount = State()

class BettingGame:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.pending_bets = {}
        self.active_games = {}
        # Из какого баланса списана ставка идущей сейчас игры: user_id -> 'real' | 'bonus'.
        # Выигрыш и возврат ставки идут в тот же баланс. Очищается в end_game().
        self.bet_source: Dict[int, str] = {}
        self.referral_system = None
        set_betting_game(self)

    # Баланс хранится в storage.py (единое хранилище для профиля/статистики/
    # админки и игр). storage.py не импортирует ни main.py, ни games.py, поэтому
    # `from main import ...` здесь больше не нужен — он заново запускал main.py
    # как отдельный модуль "main" и плодил второй словарь USER_PROFILES.

    @property
    def user_balances(self):
        from storage import USER_PROFILES
        return {uid: d.get('balance', 0.0) for uid, d in USER_PROFILES.items()}

    def save_balances(self):
        pass

    def get_balance(self, user_id: int) -> float:
        from storage import get_profile_stats
        return get_profile_stats(user_id)["balance"]

    def add_balance(self, user_id: int, amount: float) -> float:
        from storage import get_profile_stats
        stats = get_profile_stats(user_id)
        stats["balance"] += amount
        return stats["balance"]

    def subtract_balance(self, user_id: int, amount: float) -> bool:
        from storage import get_profile_stats
        stats = get_profile_stats(user_id)
        if stats["balance"] < amount:
            return False
        stats["balance"] -= amount
        return True

    # ---- источник ставки: реальный или бонусный баланс -------------------------------------

    def get_bet_mode(self, user_id: int) -> str:
        return user_bet_mode.get(user_id, BET_MODE_REAL)

    def set_bet_mode(self, user_id: int, mode: str) -> None:
        user_bet_mode[user_id] = mode

    def preview_source(self, user_id: int) -> str:
        """Из какого баланса пойдёт ставка прямо сейчас (для показа в меню)."""
        if self.get_bet_mode(user_id) == BET_MODE_BONUS:
            return BET_MODE_BONUS
        if self.get_balance(user_id) < MIN_BET and bonus_module.get_summary(user_id)["balance"] > 0:
            return BET_MODE_BONUS
        return BET_MODE_REAL

    def take_bet(self, user_id: int, amount: float, force_bonus: bool = False) -> Tuple[Optional[str], str]:
        """Списывает ставку с подходящего баланса.

        Бонусный баланс используется, если: игрок включил режим «бонус» (или force_bonus),
        либо реальный баланс меньше MIN_BET (автоматически).
        Возвращает ('real'|'bonus', '') при успехе или (None, причина) при отказе;
        причина — обычный текст без HTML, годится и для alert, и для сообщения."""
        manual_bonus = force_bonus or self.get_bet_mode(user_id) == BET_MODE_BONUS
        real_balance = self.get_balance(user_id)

        if manual_bonus or real_balance < MIN_BET:
            if bonus_module.try_spend(user_id, amount):
                self.bet_source[user_id] = BET_MODE_BONUS
                return BET_MODE_BONUS, ""
            bonus_balance = bonus_module.get_summary(user_id)["balance"]
            if manual_bonus:
                if bonus_balance <= 0:
                    return None, "Бонусного баланса нет.\nЧтобы играть на реальный баланс, отправьте: 0.1$"
                return None, (
                    f"Недостаточно бонусных средств! Бонусный баланс: {bonus_balance:.2f}$\n"
                    "Чтобы играть на реальный баланс, отправьте: 0.1$"
                )
            # авто-режим не сработал (бонуса нет или не хватает) — падаем в обычную проверку ниже

        if real_balance >= amount and self.subtract_balance(user_id, amount):
            self.bet_source[user_id] = BET_MODE_REAL
            return BET_MODE_REAL, ""

        reason = f"Недостаточно средств! Ваш баланс: {real_balance:.2f}$"
        bonus_balance = bonus_module.get_summary(user_id)["balance"]
        if bonus_balance > 0:
            reason += f"\nБонусный баланс: {bonus_balance:.2f}$"
        return None, reason

    def refund_bet(self, user_id: int, amount: float) -> None:
        """Возвращает ставку туда, откуда она была списана (игра не состоялась)."""
        if self.bet_source.get(user_id) == BET_MODE_BONUS:
            bonus_module.refund(user_id, amount)
        else:
            self.add_balance(user_id, amount)

    def get_bet_config(self, bet_type: str):
        if bet_type.startswith('куб_'):
            return DICE_BET_TYPES.get(bet_type)
        elif bet_type.startswith('куб2_'):
            return DICE_2_BET_TYPES.get(bet_type)
        elif bet_type.startswith('куб3_'):
            return DICE_3_BET_TYPES.get(bet_type)
        elif bet_type.startswith('баскет_'):
            return BASKETBALL_BET_TYPES.get(bet_type)
        elif bet_type.startswith('футбол_'):
            return FOOTBALL_BET_TYPES.get(bet_type)
        elif bet_type.startswith('дартс_'):
            return DART_BET_TYPES.get(bet_type)
        elif bet_type.startswith('дартс2_'):
            return DART_2_BET_TYPES.get(bet_type)
        elif bet_type.startswith('боулинг_'):
            return BOWLING_BET_TYPES.get(bet_type)
        return None

    def set_referral_system(self, referral_system):
        self.referral_system = referral_system

    def get_current_bet(self, user_id: int) -> Optional[float]:
        return user_current_bet.get(user_id)

    def set_current_bet(self, user_id: int, amount: float):
        user_current_bet[user_id] = amount

    def is_user_in_game(self, user_id: int) -> bool:
        return user_id in self.active_games

    def start_game(self, user_id: int):
        self.active_games[user_id] = datetime.now()

    def end_game(self, user_id: int):
        if user_id in self.active_games:
            del self.active_games[user_id]
        self.bet_source.pop(user_id, None)


def check_rate_limit(user_id: int) -> Tuple[bool, float]:
    now = datetime.now()
    if user_id in user_last_bet_time:
        time_passed = (now - user_last_bet_time[user_id]).total_seconds()
        if time_passed < RATE_LIMIT_SECONDS:
            return False, RATE_LIMIT_SECONDS - time_passed
    user_last_bet_time[user_id] = now
    return True, 0.0


def parse_bet_command(text: str) -> Optional[Tuple[str, float]]:
    text = text.strip()
    if text.startswith('/'):
        text = text[1:]
    text = text.lower()
    parts = text.split()
    if len(parts) < 3:
        return None
    game = parts[0]
    bet_type_key = parts[1]
    try:
        amount = float(parts[2])
    except (ValueError, IndexError):
        return None
    if amount < MIN_BET or amount > MAX_BET:
        return None
    game_prefix = COMMAND_MAPPING.get(game)
    if not game_prefix:
        return None
    if game_prefix == 'баскет':
        if bet_type_key in ['гол', 'goal', 'любойгол', 'anygoal']:
            full_bet_type = 'баскет_любойгол'
        elif bet_type_key in ['мимо', 'miss', 'промах']:
            full_bet_type = 'баскет_промах'
        else:
            full_bet_type = BET_TYPE_MAPPING.get(bet_type_key)
    elif game_prefix == 'футбол':
        if bet_type_key in ['гол', 'goal', 'любойгол', 'anygoal']:
            full_bet_type = 'футбол_гол'
        elif bet_type_key in ['мимо', 'miss', 'промах']:
            full_bet_type = 'футбол_мимо'
        else:
            full_bet_type = BET_TYPE_MAPPING.get(bet_type_key)
    elif game_prefix == 'дартс':
        if bet_type_key in ['мимо', 'miss']:
            full_bet_type = 'дартс_мимо'
        else:
            full_bet_type = BET_TYPE_MAPPING.get(bet_type_key)
    elif game_prefix == 'боулинг':
        full_bet_type = BOWLING_TEXT_ALIASES.get(bet_type_key) or BET_TYPE_MAPPING.get(bet_type_key)
    else:
        full_bet_type = BET_TYPE_MAPPING.get(bet_type_key)
    if not full_bet_type:
        return None
    if not full_bet_type.startswith(game_prefix):
        return None
    return (full_bet_type, amount)


def is_set_bet_command(text: str) -> bool:
    if not text:
        return False
    return bool(SET_BET_PATTERN.match(text.strip()))


def is_set_bonus_bet_command(text: str) -> bool:
    if not text:
        return False
    return bool(SET_BONUS_BET_PATTERN.match(text.strip()))


async def handle_set_bet_command(message: Message, betting_game: 'BettingGame'):
    user_id = message.from_user.id
    match = SET_BET_PATTERN.match((message.text or '').strip())
    if not match:
        return

    amount_str = match.group(1).replace(',', '.')
    try:
        amount = float(amount_str)
    except ValueError:
        await message.answer(f"{e(EMOJI_CROSS,'❌')} Введите корректную сумму, например: 0.1$")
        return

    if amount < MIN_BET:
        await message.answer(f"{e(EMOJI_CROSS,'❌')} Минимальная ставка: {MIN_BET}$")
        return
    if amount > MAX_BET:
        await message.answer(f"{e(EMOJI_CROSS,'❌')} Максимальная ставка: {MAX_BET}$")
        return

    betting_game.set_current_bet(user_id, amount)
    betting_game.set_bet_mode(user_id, BET_MODE_REAL)
    hint = ""
    if bonus_module.get_summary(user_id)["active"]:
        hint = f"\n<blockquote><i>{BONUS_HOWTO}</i></blockquote>"
    await message.answer(
        f"<blockquote><b>✅ Ставка установлена: <code>{amount:.2f}</code>$</b></blockquote>\n\n"
        f"<blockquote><i>Действует для Кубика, Футбола, Баскетбола, Дартса и Боулинга.</i></blockquote>"
        f"{hint}",
        parse_mode='HTML'
    )


async def handle_set_bonus_bet_command(message: Message, betting_game: 'BettingGame'):
    """«0.1 бонус» — устанавливает ставку и переключает игру на бонусный баланс."""
    user_id = message.from_user.id
    match = SET_BONUS_BET_PATTERN.match((message.text or '').strip())
    if not match:
        return

    try:
        amount = float(match.group(1).replace(',', '.'))
    except ValueError:
        await message.answer(f"{e(EMOJI_CROSS,'❌')} Введите корректную сумму, например: 0.1 бонус")
        return

    if amount < MIN_BET:
        await message.answer(f"{e(EMOJI_CROSS,'❌')} Минимальная ставка: {MIN_BET}$")
        return
    if amount > MAX_BET:
        await message.answer(f"{e(EMOJI_CROSS,'❌')} Максимальная ставка: {MAX_BET}$")
        return

    summary = bonus_module.get_summary(user_id)
    if not summary["active"]:
        await message.answer(
            f"<blockquote><b>{e(EMOJI_CROSS,'❌')} У вас нет бонусного баланса.</b></blockquote>\n\n"
            f"<blockquote><i>Для игры на реальный баланс отправьте, например: 0.1$</i></blockquote>",
            parse_mode='HTML'
        )
        return

    betting_game.set_current_bet(user_id, amount)
    betting_game.set_bet_mode(user_id, BET_MODE_BONUS)
    warning = ""
    if summary["balance"] < amount - 1e-9:
        warning = (
            f"\n<blockquote><b>{e(EMOJI_CROSS,'❌')} Бонусного баланса "
            f"(<code>{summary['balance']:.2f}</code>$) не хватает на такую ставку.</b></blockquote>"
        )
    await message.answer(
        f"<blockquote><b>✅ Бонусная ставка установлена: <code>{amount:.2f}</code>{bonus_module.BONUS_ICON}</b></blockquote>\n\n"
        f"<blockquote><i>Игры идут с бонусного баланса (<code>{summary['balance']:.2f}</code>$). "
        f"Вернуться к реальному балансу: отправьте, например, 0.1$</i></blockquote>"
        f"{warning}",
        parse_mode='HTML'
    )


def is_bet_command(text: str) -> bool:
    if not text:
        return False
    text = text.strip().lower()
    if text.startswith('/'):
        text = text[1:]
    parts = text.split()
    if len(parts) < 3:
        return False
    game = parts[0]
    return game in COMMAND_MAPPING


async def _safe_reply(target_message: Message, text: str, parse_mode: str = 'HTML', reply_markup=None):
    try:
        await target_message.reply(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logging.warning(f"[safe_reply] Не удалось отправить результат игры: {e}")


async def _delayed_safe_reply(target_message: Message, text: str, delay: float = 3.0, parse_mode: str = 'HTML', reply_markup=None):
    await asyncio.sleep(delay)
    await _safe_reply(target_message, text, parse_mode=parse_mode, reply_markup=reply_markup)


# Внутренняя комиссия проекта, удерживаемая с выигрышей (не упоминается в текстах пользователю).
WIN_COMMISSION_RATE = 0.05


def _apply_game_result(
    user_id: int,
    nickname: str,
    amount: float,
    is_win: bool,
    bet_config: dict,
    betting_game: 'BettingGame',
    bet_type: str = '',
) -> float:
    game_name = _get_game_display_name(bet_type)
    from storage import log_game_round  # для топа игроков (оборот/выигрыши/кол-во игр)

    if betting_game.bet_source.get(user_id) == BET_MODE_BONUS:
        # Бонусный раунд: выигрыш падает на БОНУСНЫЙ баланс, ставка идёт в отыгрыш. В топ и оборот
        # такие раунды не попадают (record_game_result / log_game_round не вызываем).
        winnings = round(amount * bet_config['multiplier'] * (1 - WIN_COMMISSION_RATE), 2) if is_win else 0.0
        event = bonus_module.settle(user_id, amount, winnings)
        bonus_module.notify_event(user_id, event)
        logging.info(
            f"[game] user={user_id} game={game_name} BONUS {'WIN' if is_win else 'LOSE'} "
            f"bet={amount} net={winnings:.2f}"
            + (f" -> wallet {event['kind']}" if event else "")
        )
        return winnings

    if is_win:
        gross_winnings = amount * bet_config['multiplier']
        winnings = round(gross_winnings * (1 - WIN_COMMISSION_RATE), 2)
        betting_game.add_balance(user_id, winnings)
        record_game_result(user_id, nickname, amount, winnings, game_name)
        log_game_round(user_id, amount, winnings)
        logging.info(
            f"[game] user={user_id} game={game_name} WIN bet={amount} "
            f"gross={gross_winnings:.2f} commission={WIN_COMMISSION_RATE*100:.0f}% net={winnings:.2f}"
        )
        return winnings
    else:
        record_game_result(user_id, nickname, amount, 0.0, game_name)
        log_game_round(user_id, amount, 0.0)
        logging.info(f"[game] user={user_id} game={game_name} LOSE bet={amount}")
        return 0.0


def _round_is_bonus(user_id: int) -> bool:
    """Идёт ли сейчас раунд игрока на бонусном балансе (источник запоминается в take_bet)."""
    bg = get_betting_game()
    return bool(bg and bg.bet_source.get(user_id) == BET_MODE_BONUS)


def _coin_icon(user_id: int) -> str:
    return bonus_module.BONUS_ICON if _round_is_bonus(user_id) else e(EMOJI_COIN, '💲')


def _build_win_text(nickname: str, user_id: int, amount: float, outcome_label: str, winnings: float) -> str:
    coin = _coin_icon(user_id)
    target = "бонусный баланс" if _round_is_bonus(user_id) else "баланс"
    return (
        f"<b>Игрок {nickname} (ID: <code>{user_id}</code>) выигрывает"
        f"<tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji></b>\n\n"
        f"<blockquote>Ставка: <code>{amount:.2f}</code>{coin} на «<b>{outcome_label}</b>»</blockquote>\n"
        f"<blockquote><code>{winnings:.2f}</code>"
        f"{coin} "
        f"Успешно зачислены на {target}!</blockquote>\n"
        f"<blockquote><tg-emoji emoji-id=\"5461151367559141950\">🎉</tg-emoji>"
        f"Поздравляем!</blockquote>"
    )


def _build_lose_text(nickname: str, user_id: int, amount: float, outcome_label: str) -> str:
    coin = _coin_icon(user_id)
    return (
        f"<b>Игрок {nickname} (ID: <code>{user_id}</code>) проигрывает"
        f"<tg-emoji emoji-id=\"5422858869372104873\">❌</tg-emoji></b>\n\n"
        f"<blockquote>Ставка: <code>{amount:.2f}</code>{coin} на «<b>{outcome_label}</b>» — не сыграла.</blockquote>\n"
        f"<blockquote><b><i>Это не повод сдаваться! "
        f"Пробуй снова и снова до победного!</i></b></blockquote>\n"
        f"<blockquote><tg-emoji emoji-id=\"5305699699204837855\">🎉</tg-emoji>"
        f"Желаем удачи!</blockquote>"
    )


async def play_single_dice_game(
    chat_id: int,
    user_id: int,
    nickname: str,
    amount: float,
    bet_type: str,
    bet_config: dict,
    betting_game: BettingGame,
    bet_msg: Message = None,
):
    emoji = _bet_emoji_for(bet_type)

    send_kwargs = {'chat_id': chat_id, 'emoji': emoji}
    if bet_msg:
        send_kwargs['reply_to_message_id'] = bet_msg.message_id

    dice_message = await betting_game.bot.send_dice(**send_kwargs)
    dice_value = dice_message.dice.value

    is_win = dice_value in bet_config.get('values', [])
    # ВРЕМЕННЫЙ диагностический лог — помогает сверить реальное значение,
    # которое присылает Telegram для 🏀/⚽/🎯 и т.п., с тем, что мы ожидаем
    # в values у bet_type. Telegram официально не документирует, какая именно
    # анимация соответствует каждому конкретному числу (кроме того, что 4-5
    # у 🏀/⚽ — это гол, а 1-3 — мимо), поэтому при подозрении на
    # неправильный маппинг ("выпал отскок, а не засчиталось") нужно смотреть
    # именно сюда: что реально было в dice_value в момент броска.
    logging.info(
        f"[dice_debug] emoji={emoji} bet_type={bet_type} dice_value={dice_value} "
        f"expected_values={bet_config.get('values', [])} is_win={is_win}"
    )
    winnings = _apply_game_result(
        user_id, nickname, amount, is_win, bet_config, betting_game, bet_type=bet_type
    )

    outcome_label = _get_outcome_label(bet_type, bet_config)
    text = (
        _build_win_text(nickname, user_id, amount, outcome_label, winnings)
        if is_win else _build_lose_text(nickname, user_id, amount, outcome_label)
    )
    keyboard = _build_replay_keyboard(user_id, bet_type, amount, bet_config)
    asyncio.create_task(_delayed_safe_reply(dice_message, text, delay=3.0, reply_markup=keyboard))


async def play_double_dice_game(
    chat_id: int,
    user_id: int,
    nickname: str,
    amount: float,
    bet_type: str,
    bet_config: dict,
    betting_game: BettingGame,
    bet_msg: Message = None,
):
    send_kwargs = {'chat_id': chat_id, 'emoji': '🎲'}
    if bet_msg:
        send_kwargs['reply_to_message_id'] = bet_msg.message_id

    dice1 = await betting_game.bot.send_dice(**send_kwargs)
    await asyncio.sleep(2)

    dice2_kwargs = {'chat_id': chat_id, 'emoji': '🎲'}
    if bet_msg:
        dice2_kwargs['reply_to_message_id'] = bet_msg.message_id
    dice2 = await betting_game.bot.send_dice(**dice2_kwargs)

    dice1_value = dice1.dice.value
    dice2_value = dice2.dice.value
    total = dice1_value + dice2_value
    product = dice1_value * dice2_value
    both_even = dice1_value % 2 == 0 and dice2_value % 2 == 0
    both_odd = dice1_value % 2 == 1 and dice2_value % 2 == 1
    both_big = dice1_value >= 4 and dice2_value >= 4
    both_small = dice1_value <= 3 and dice2_value <= 3
    is_double = dice1_value == dice2_value

    if bet_type == 'куб2_сумма_ровно7':
        is_win = total == 7
    elif bet_type == 'куб2_сумма_больше7':
        is_win = total > 7
    elif bet_type == 'куб2_сумма_меньше7':
        is_win = total < 7
    elif bet_type == 'куб2_обачет':
        is_win = both_even
    elif bet_type == 'куб2_обанечет':
        is_win = both_odd
    elif bet_type == 'куб2_обабольше':
        is_win = both_big
    elif bet_type == 'куб2_обаменьше':
        is_win = both_small
    elif bet_type == 'куб2_любойдубль':
        is_win = is_double
    elif bet_type == 'куб2_конкретныйдубль':
        # значение передаётся в данных ставки
        target = bet_config.get('target', 0)
        is_win = is_double and dice1_value == target
    elif bet_type == 'куб2_произведение':
        is_win = product >= 18
    else:
        is_win = False

    winnings = _apply_game_result(
        user_id, nickname, amount, is_win, bet_config, betting_game, bet_type=bet_type
    )

    outcome_label = _get_outcome_label(bet_type, bet_config)
    text = (
        _build_win_text(nickname, user_id, amount, outcome_label, winnings)
        if is_win else _build_lose_text(nickname, user_id, amount, outcome_label)
    )
    keyboard = _build_replay_keyboard(user_id, bet_type, amount, bet_config)
    asyncio.create_task(_delayed_safe_reply(dice2, text, delay=3.0, reply_markup=keyboard))


async def play_double_football_game(
    chat_id: int,
    user_id: int,
    nickname: str,
    amount: float,
    bet_type: str,
    bet_config: dict,
    betting_game: BettingGame,
    bet_msg: Message = None,
):
    send_kwargs = {'chat_id': chat_id, 'emoji': '⚽'}
    if bet_msg:
        send_kwargs['reply_to_message_id'] = bet_msg.message_id

    ball1 = await betting_game.bot.send_dice(**send_kwargs)
    await asyncio.sleep(2)

    ball2_kwargs = {'chat_id': chat_id, 'emoji': '⚽'}
    if bet_msg:
        ball2_kwargs['reply_to_message_id'] = bet_msg.message_id
    ball2 = await betting_game.bot.send_dice(**ball2_kwargs)

    ball1_value = ball1.dice.value
    ball2_value = ball2.dice.value
    is_double = ball1_value == ball2_value

    if bet_type == 'футбол_любойдубль':
        is_win = is_double
    elif bet_type == 'футбол_конкретныйдубль':
        target = bet_config.get('target', 0)
        is_win = is_double and ball1_value == target
    else:
        is_win = False

    winnings = _apply_game_result(
        user_id, nickname, amount, is_win, bet_config, betting_game, bet_type=bet_type
    )

    outcome_label = _get_outcome_label(bet_type, bet_config)
    text = (
        _build_win_text(nickname, user_id, amount, outcome_label, winnings)
        if is_win else _build_lose_text(nickname, user_id, amount, outcome_label)
    )
    keyboard = _build_replay_keyboard(user_id, bet_type, amount, bet_config)
    asyncio.create_task(_delayed_safe_reply(ball2, text, delay=3.0, reply_markup=keyboard))


async def play_double_basketball_game(
    chat_id: int,
    user_id: int,
    nickname: str,
    amount: float,
    bet_type: str,
    bet_config: dict,
    betting_game: BettingGame,
    bet_msg: Message = None,
):
    """Два броска мяча в кольцо подряд: дубли (конкретный/любой), а также
    'оба гола' и 'оба промаха' — по аналогии с двойным футболом, но с
    категориями гол/промах, специфичными для баскетбола."""
    send_kwargs = {'chat_id': chat_id, 'emoji': '🏀'}
    if bet_msg:
        send_kwargs['reply_to_message_id'] = bet_msg.message_id

    ball1 = await betting_game.bot.send_dice(**send_kwargs)
    await asyncio.sleep(2)

    ball2_kwargs = {'chat_id': chat_id, 'emoji': '🏀'}
    if bet_msg:
        ball2_kwargs['reply_to_message_id'] = bet_msg.message_id
    ball2 = await betting_game.bot.send_dice(**ball2_kwargs)

    ball1_value = ball1.dice.value
    ball2_value = ball2.dice.value
    is_double = ball1_value == ball2_value

    goal_values = BASKETBALL_BET_TYPES['баскет_любойгол']['values']
    miss_values = BASKETBALL_BET_TYPES['баскет_промах']['values']

    if bet_type == 'баскет_любойдубль':
        is_win = is_double
    elif bet_type == 'баскет_конкретныйдубль':
        target = bet_config.get('target', 0)
        is_win = is_double and ball1_value == target
    elif bet_type == 'баскет_двагола':
        is_win = ball1_value in goal_values and ball2_value in goal_values
    elif bet_type == 'баскет_двапромаха':
        is_win = ball1_value in miss_values and ball2_value in miss_values
    else:
        is_win = False

    winnings = _apply_game_result(
        user_id, nickname, amount, is_win, bet_config, betting_game, bet_type=bet_type
    )

    outcome_label = _get_outcome_label(bet_type, bet_config)
    text = (
        _build_win_text(nickname, user_id, amount, outcome_label, winnings)
        if is_win else _build_lose_text(nickname, user_id, amount, outcome_label)
    )
    keyboard = _build_replay_keyboard(user_id, bet_type, amount, bet_config)
    asyncio.create_task(_delayed_safe_reply(ball2, text, delay=3.0, reply_markup=keyboard))


async def play_double_bowling_game(
    chat_id: int,
    user_id: int,
    nickname: str,
    amount: float,
    bet_type: str,
    bet_config: dict,
    betting_game: BettingGame,
    bet_msg: Message = None,
):
    """Два броска шара подряд. Дубль — оба броска дали один и тот же исход
    (одинаковое значение 🎳): промах/1/3/4/5 кеглей/страйк.
    'Любой дубль' — любое совпадение (x6), 'конкретный' — совпадение на выбранном исходе (x36)."""
    send_kwargs = {'chat_id': chat_id, 'emoji': '🎳'}
    if bet_msg:
        send_kwargs['reply_to_message_id'] = bet_msg.message_id

    roll1 = await betting_game.bot.send_dice(**send_kwargs)
    await asyncio.sleep(2)

    roll2_kwargs = {'chat_id': chat_id, 'emoji': '🎳'}
    if bet_msg:
        roll2_kwargs['reply_to_message_id'] = bet_msg.message_id
    roll2 = await betting_game.bot.send_dice(**roll2_kwargs)

    roll1_value = roll1.dice.value
    roll2_value = roll2.dice.value
    is_double = roll1_value == roll2_value

    if bet_type == 'боулинг_любойдубль':
        is_win = is_double
    elif bet_type == 'боулинг_конкретныйдубль':
        target = bet_config.get('target', 0)
        is_win = is_double and roll1_value == target
    else:
        is_win = False

    logging.info(
        f"[dice_debug] emoji=🎳 bet_type={bet_type} values=({roll1_value}, {roll2_value}) "
        f"target={bet_config.get('target')} is_win={is_win}"
    )

    winnings = _apply_game_result(
        user_id, nickname, amount, is_win, bet_config, betting_game, bet_type=bet_type
    )

    outcome_label = _get_outcome_label(bet_type, bet_config)
    text = (
        _build_win_text(nickname, user_id, amount, outcome_label, winnings)
        if is_win else _build_lose_text(nickname, user_id, amount, outcome_label)
    )
    keyboard = _build_replay_keyboard(user_id, bet_type, amount, bet_config)
    asyncio.create_task(_delayed_safe_reply(roll2, text, delay=3.0, reply_markup=keyboard))


async def play_double_darts_game(
    chat_id: int,
    user_id: int,
    nickname: str,
    amount: float,
    bet_type: str,
    bet_config: dict,
    betting_game: BettingGame,
    bet_msg: Message = None,
):
    """Два броска дротика подряд: выигрыш, если ОБА попадания попали в одну
    и ту же категорию (белое/красное/центр/мимо), по аналогии с дублями в кубах и футболе."""
    send_kwargs = {'chat_id': chat_id, 'emoji': '🎯'}
    if bet_msg:
        send_kwargs['reply_to_message_id'] = bet_msg.message_id

    dart1 = await betting_game.bot.send_dice(**send_kwargs)
    await asyncio.sleep(2)

    dart2_kwargs = {'chat_id': chat_id, 'emoji': '🎯'}
    if bet_msg:
        dart2_kwargs['reply_to_message_id'] = bet_msg.message_id
    dart2 = await betting_game.bot.send_dice(**dart2_kwargs)

    dart1_value = dart1.dice.value
    dart2_value = dart2.dice.value

    category = bet_config.get('category', '')
    category_values = DART_BET_TYPES.get(category, {}).get('values', [])
    is_win = dart1_value in category_values and dart2_value in category_values

    winnings = _apply_game_result(
        user_id, nickname, amount, is_win, bet_config, betting_game, bet_type=bet_type
    )

    outcome_label = _get_outcome_label(bet_type, bet_config)
    text = (
        _build_win_text(nickname, user_id, amount, outcome_label, winnings)
        if is_win else _build_lose_text(nickname, user_id, amount, outcome_label)
    )
    keyboard = _build_replay_keyboard(user_id, bet_type, amount, bet_config)
    asyncio.create_task(_delayed_safe_reply(dart2, text, delay=3.0, reply_markup=keyboard))


async def play_triple_dice_game(
    chat_id: int,
    user_id: int,
    nickname: str,
    amount: float,
    bet_type: str,
    bet_config: dict,
    betting_game: BettingGame,
    bet_msg: Message = None,
):
    send_kwargs = {'chat_id': chat_id, 'emoji': '🎲'}
    if bet_msg:
        send_kwargs['reply_to_message_id'] = bet_msg.message_id

    dice1 = await betting_game.bot.send_dice(**send_kwargs)
    await asyncio.sleep(2)

    dice2_kwargs = {'chat_id': chat_id, 'emoji': '🎲'}
    if bet_msg:
        dice2_kwargs['reply_to_message_id'] = bet_msg.message_id
    dice2 = await betting_game.bot.send_dice(**dice2_kwargs)
    await asyncio.sleep(2)

    dice3_kwargs = {'chat_id': chat_id, 'emoji': '🎲'}
    if bet_msg:
        dice3_kwargs['reply_to_message_id'] = bet_msg.message_id
    dice3 = await betting_game.bot.send_dice(**dice3_kwargs)

    dice1_value = dice1.dice.value
    dice2_value = dice2.dice.value
    dice3_value = dice3.dice.value
    total = dice1_value + dice2_value + dice3_value
    product = dice1_value * dice2_value * dice3_value
    all_even = dice1_value % 2 == 0 and dice2_value % 2 == 0 and dice3_value % 2 == 0
    all_odd = dice1_value % 2 == 1 and dice2_value % 2 == 1 and dice3_value % 2 == 1
    all_big = dice1_value > 3 and dice2_value > 3 and dice3_value > 3
    all_small = dice1_value < 4 and dice2_value < 4 and dice3_value < 4
    is_triple = dice1_value == dice2_value == dice3_value

    if bet_type == 'куб3_3чет':
        is_win = all_even
    elif bet_type == 'куб3_3нечет':
        is_win = all_odd
    elif bet_type == 'куб3_больше10':
        is_win = all_big
    elif bet_type == 'куб3_меньше10':
        is_win = all_small
    elif bet_type == 'куб3_любойтрипл':
        is_win = is_triple
    elif bet_type == 'куб3_конкретныйтрипл':
        target = bet_config.get('target', 0)
        is_win = is_triple and dice1_value == target
    elif bet_type == 'куб3_произведение':
        is_win = product >= 108
    else:
        is_win = False

    winnings = _apply_game_result(
        user_id, nickname, amount, is_win, bet_config, betting_game, bet_type=bet_type
    )

    outcome_label = _get_outcome_label(bet_type, bet_config)
    text = (
        _build_win_text(nickname, user_id, amount, outcome_label, winnings)
        if is_win else _build_lose_text(nickname, user_id, amount, outcome_label)
    )
    keyboard = _build_replay_keyboard(user_id, bet_type, amount, bet_config)
    asyncio.create_task(_delayed_safe_reply(dice3, text, delay=3.0, reply_markup=keyboard))


async def play_bowling_vs_game(
    chat_id: int,
    user_id: int,
    nickname: str,
    amount: float,
    bet_type: str,
    bet_config: dict,
    betting_game: BettingGame,
    bet_msg: Message = None,
):
    send_kwargs = {'chat_id': chat_id, 'emoji': '🎳'}
    if bet_msg:
        send_kwargs['reply_to_message_id'] = bet_msg.message_id

    player_roll = await betting_game.bot.send_dice(**send_kwargs)
    await asyncio.sleep(2)

    bot_kwargs = {'chat_id': chat_id, 'emoji': '🎳'}
    if bet_msg:
        bot_kwargs['reply_to_message_id'] = bet_msg.message_id
    bot_roll = await betting_game.bot.send_dice(**bot_kwargs)
    await asyncio.sleep(3)

    player_value = player_roll.dice.value
    bot_value    = bot_roll.dice.value

    while player_value == bot_value:
        asyncio.create_task(
            _safe_reply(bot_roll, "<tg-emoji emoji-id=\"5402186569006210455\">🎉</tg-emoji>Ничья! Переброс...")
        )
        await asyncio.sleep(1)

        player_roll = await betting_game.bot.send_dice(**send_kwargs)
        await asyncio.sleep(2)
        bot_roll    = await betting_game.bot.send_dice(**bot_kwargs)
        await asyncio.sleep(3)

        player_value = player_roll.dice.value
        bot_value    = bot_roll.dice.value

    if bet_type == 'боулинг_победа':
        is_win = player_value > bot_value
    elif bet_type == 'боулинг_поражение':
        is_win = player_value < bot_value
    else:
        is_win = False

    winnings = _apply_game_result(
        user_id, nickname, amount, is_win, bet_config, betting_game, bet_type=bet_type
    )

    outcome_label = _get_outcome_label(bet_type, bet_config)
    keyboard = _build_replay_keyboard(user_id, bet_type, amount, bet_config)
    if is_win:
        asyncio.create_task(_safe_reply(
            bot_roll, _build_win_text(nickname, user_id, amount, outcome_label, winnings), reply_markup=keyboard
        ))
    else:
        asyncio.create_task(_safe_reply(
            bot_roll, _build_lose_text(nickname, user_id, amount, outcome_label), reply_markup=keyboard
        ))


async def _run_game(
    chat_id: int,
    user_id: int,
    nickname: str,
    amount: float,
    bet_type: str,
    bet_config: dict,
    betting_game: BettingGame,
    callback: CallbackQuery = None,
):
    """Удаляет старое меню (если пришли из callback), отправляет сообщение о ставке
    и запускает соответствующую игру, кубик(и) которой отвечают на это сообщение."""
    if callback is not None:
        try:
            await callback.message.delete()
        except Exception as ex:
            logging.warning(f"[_run_game] Не удалось удалить старое сообщение: {ex}")

    outcome_label = _get_outcome_label(bet_type, bet_config)
    mult = bet_config.get('multiplier', 0)
    emoji = _bet_emoji_for(bet_type)

    bet_text = (
        f"{emoji} <b>{nickname}</b> ставит <code>{amount:.2f}</code>{_coin_icon(user_id)} "
        f"(x{_fmt_mult(mult)}) на «<b>{outcome_label}</b>»"
    )
    bet_msg = await betting_game.bot.send_message(chat_id, bet_text, parse_mode='HTML')

    if bet_type.startswith('куб3_'):
        await play_triple_dice_game(chat_id, user_id, nickname, amount, bet_type, bet_config, betting_game, bet_msg)
    elif bet_type.startswith('куб2_'):
        await play_double_dice_game(chat_id, user_id, nickname, amount, bet_type, bet_config, betting_game, bet_msg)
    elif bet_type in ('футбол_любойдубль', 'футбол_конкретныйдубль'):
        await play_double_football_game(chat_id, user_id, nickname, amount, bet_type, bet_config, betting_game, bet_msg)
    elif bet_type in ('баскет_любойдубль', 'баскет_конкретныйдубль', 'баскет_двагола', 'баскет_двапромаха'):
        await play_double_basketball_game(chat_id, user_id, nickname, amount, bet_type, bet_config, betting_game, bet_msg)
    elif bet_type.startswith('дартс2_'):
        await play_double_darts_game(chat_id, user_id, nickname, amount, bet_type, bet_config, betting_game, bet_msg)
    elif bet_type in ('боулинг_любойдубль', 'боулинг_конкретныйдубль'):
        await play_double_bowling_game(chat_id, user_id, nickname, amount, bet_type, bet_config, betting_game, bet_msg)
    elif bet_type.startswith('боулинг_') and bet_config.get('special') == 'bowling_vs':
        await play_bowling_vs_game(chat_id, user_id, nickname, amount, bet_type, bet_config, betting_game, bet_msg)
    else:
        await play_single_dice_game(chat_id, user_id, nickname, amount, bet_type, bet_config, betting_game, bet_msg)


async def _execute_and_settle(
    chat_id: int,
    user_id: int,
    nickname: str,
    amount: float,
    bet_type: str,
    bet_config: dict,
    betting_game: BettingGame,
    callback: CallbackQuery = None,
    notify_target=None,
):
    """Запускает игру и в случае ошибки возвращает средства, уведомляя пользователя."""
    try:
        await _run_game(chat_id, user_id, nickname, amount, bet_type, bet_config, betting_game, callback=callback)
    except Exception as ex:
        logging.error(f"Ошибка при отправке кубика (до броска): {ex}")
        betting_game.refund_bet(user_id, amount)
        try:
            if notify_target is not None:
                await notify_target.answer("❌ Не удалось начать игру. Средства возвращены.")
        except Exception:
            pass
    finally:
        betting_game.end_game(user_id)


def _build_nickname(user) -> str:
    # вызывается при каждой ставке -> запоминаем имя игрока для топа
    if _remember_user is not None:
        try:
            _remember_user(user)
        except Exception as ex:
            logging.warning(f"[remember_user] не удалось запомнить пользователя: {ex}")
    nickname = user.first_name or ""
    if user.last_name:
        nickname += f" {user.last_name}"
    return nickname.strip() or user.username or "Игрок"


@router.callback_query(F.data.startswith("replay:"))
async def handle_replay_bet(callback: CallbackQuery, state: FSMContext):
    betting_game = get_betting_game()
    if betting_game is None:
        await callback.answer("❌ Бот перезапускается, попробуйте ещё раз чуть позже", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 5:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    _, uid_str, code, amount_str, target_str = parts[:5]

    if str(callback.from_user.id) != uid_str:
        await callback.answer("🚫 Это не ваша кнопка!", show_alert=True)
        return

    user_id = callback.from_user.id

    allowed, wait_time = check_rate_limit(user_id)
    if not allowed:
        await callback.answer(f"⏳ Подождите {wait_time:.1f} сек", show_alert=True)
        return

    if betting_game.is_user_in_game(user_id):
        await callback.answer("⏳ Дождитесь окончания игры!", show_alert=True)
        return

    bet_type = CODE_TO_BET_TYPE.get(code)
    if not bet_type:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    bet_config = betting_game.get_bet_config(bet_type)
    if not bet_config:
        await callback.answer("❌ Ошибка конфигурации ставки", show_alert=True)
        return

    if target_str:
        try:
            # копия, чтобы не менять общий конфиг ставки (им могут пользоваться другие игроки)
            bet_config = dict(bet_config)
            bet_config['target'] = int(target_str)
        except ValueError:
            pass

    try:
        amount = float(amount_str)
    except ValueError:
        await callback.answer("❌ Ошибка суммы", show_alert=True)
        return

    if amount < MIN_BET or amount > MAX_BET:
        await callback.answer("❌ Некорректная сумма ставки", show_alert=True)
        return

    source, reason = betting_game.take_bet(user_id, amount)
    if source is None:
        await callback.answer(f"❌ {reason}", show_alert=True)
        return

    if source == BET_MODE_REAL:  # с бонусных ставок реферальная комиссия не платится
        asyncio.create_task(notify_referrer_commission(user_id, amount))

    nickname = _build_nickname(callback.from_user)
    betting_game.start_game(user_id)
    await callback.answer()

    await _execute_and_settle(
        callback.message.chat.id, user_id, nickname, amount, bet_type, bet_config, betting_game,
        callback=callback, notify_target=callback.message,
    )


_MENU_KEY_DICE_TABS = {'dice1': '1куб', 'dice2': '2куба', 'dice3': '3куба'}


@router.callback_query(F.data.startswith("backmenu:"))
async def handle_back_to_menu(callback: CallbackQuery, state: FSMContext):
    betting_game = get_betting_game()

    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    _, uid_str, menu_key = parts[:3]

    if str(callback.from_user.id) != uid_str:
        await callback.answer("🚫 Это не ваша кнопка!", show_alert=True)
        return

    await state.clear()

    if menu_key in _MENU_KEY_DICE_TABS:
        active = _MENU_KEY_DICE_TABS[menu_key]
        user_id = callback.from_user.id
        text = build_dice_hub_text(active, betting_game, user_id)
        markup = build_dice_hub_keyboard(active)
        await safe_edit_message(callback, text, reply_markup=markup, parse_mode='HTML')
        await callback.answer()
        return

    handler = {
        'basketball': show_basketball_menu,
        'football':   show_football_menu,
        'darts':      show_darts_menu,
        'bowling':    show_bowling_menu,
        'bowling2':   show_bowling2_menu,
    }.get(menu_key)

    if handler:
        await handler(callback, betting_game)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)


async def handle_text_bet_command(message: Message, betting_game: BettingGame):
    user_id = message.from_user.id

    allowed, wait_time = check_rate_limit(user_id)
    if not allowed:
        await message.answer(
            f"⏳ Подождите {wait_time:.1f} сек перед следующей ставкой",
            parse_mode='HTML'
        )
        return

    if betting_game.is_user_in_game(user_id):
        await message.answer("⏳ Дождитесь окончания текущей игры!")
        return

    parsed = parse_bet_command(message.text)
    if not parsed:
        await message.answer(
            "<blockquote>❌<b>Неверный формат команды!</b>\n\n"
            "Используйте /help для уточнения!</blockquote>",
            parse_mode='HTML'
        )
        return

    bet_type, amount = parsed

    bet_config = betting_game.get_bet_config(bet_type)
    if not bet_config:
        await message.answer("❌ Ошибка конфигурации ставки")
        return

    # «/куб чет 0.1 бонус» — эта ставка на бонусный баланс, режим при этом не меняется
    force_bonus = bool(TEXT_BONUS_SUFFIX.search(message.text or ''))
    source, reason = betting_game.take_bet(user_id, amount, force_bonus=force_bonus)
    if source is None:
        await message.answer(
            f"<blockquote><b>{e(EMOJI_CROSS,'❌')} {reason}</b></blockquote>\n\n",
            parse_mode='HTML'
        )
        return

    if source == BET_MODE_REAL:  # с бонусных ставок реферальная комиссия не платится
        asyncio.create_task(notify_referrer_commission(user_id, amount))

    nickname = _build_nickname(message.from_user)

    betting_game.start_game(user_id)

    await _execute_and_settle(
        message.chat.id, user_id, nickname, amount, bet_type, bet_config, betting_game,
        callback=None, notify_target=message,
    )


async def safe_edit_message(callback: CallbackQuery, text: str, reply_markup=None, parse_mode=None):
    try:
        await callback.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logging.error(f"Error editing message: {e}")
        try:
            await callback.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Единый hub игр
# ─────────────────────────────────────────────────────────────────────────────

GAME_TAB_ORDER = ['dice', 'football', 'basketball', 'darts', 'bowling']

GAME_TAB_EMOJI = {
    'dice':       '🎲',
    'football':   '⚽',
    'basketball': '🏀',
    'darts':      '🎯',
    'bowling':    '🎳',
}

GAME_TAB_TITLE = {
    'dice':       'Кубик',
    'football':   'Футбол',
    'basketball': 'Баскетбол',
    'darts':      'Дартс',
    'bowling':    'Боулинг',
}


def _max_multiplier(bet_types: dict) -> float:
    return max(cfg['multiplier'] for cfg in bet_types.values())


def _fmt_mult(x: float) -> str:
    if x == int(x):
        return str(int(x))
    return f"{x:g}"


GAME_MAX_MULTIPLIER = {
    'dice':       _max_multiplier({**DICE_BET_TYPES, **DICE_2_BET_TYPES, **DICE_3_BET_TYPES}),
    'football':   _max_multiplier(FOOTBALL_BET_TYPES),
    'basketball': _max_multiplier(BASKETBALL_BET_TYPES),
    'darts':      _max_multiplier({**DART_BET_TYPES, **DART_2_BET_TYPES}),
    'bowling':    _max_multiplier(BOWLING_BET_TYPES),
}


def build_games_selector_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for key in GAME_TAB_ORDER:
        emoji = GAME_TAB_EMOJI[key]
        mult  = _fmt_mult(GAME_MAX_MULTIPLIER[key])
        row.append(InlineKeyboardButton(text=f"{emoji} (до {mult}х)", callback_data=f"game_{key}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([
        InlineKeyboardButton(text="Авторские", callback_data="custom_games_menu", icon_custom_emoji_id=EMOJI_MAGNIFY)
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _bet_balance_block(betting_game: 'BettingGame', user_id: int) -> str:
    current_bet = betting_game.get_current_bet(user_id)
    bet_display = f"{current_bet:.2f}" if current_bet else "0"
    balance = betting_game.get_balance(user_id)
    bonus = bonus_module.get_summary(user_id)
    on_bonus = betting_game.preview_source(user_id) == BET_MODE_BONUS
    bet_icon = f"{bonus_module.BONUS_ICON} (бонус)" if on_bonus else e(EMOJI_COIN, '💲')
    lines = [
        f"{e(EMOJI_BET_LABEL,'🪙')} Ставка: <code>{bet_display}</code>{bet_icon}",
        f"{e(EMOJI_BALANCE_LABEL,'👛')} Баланс: <code>{balance:.2f}</code>{e(EMOJI_COIN,'💲')}",
    ]
    if bonus["active"]:
        lines.append(
            f"{bonus_module.BONUS_ICON} Бонус: <code>{bonus['balance']:.2f}</code> "
            f"· осталось отыграть: <code>{bonus['remaining']:.2f}</code>"
        )
    return "<blockquote>" + "\n".join(lines) + "</blockquote>\n\n"


def build_games_selector_text(betting_game: 'BettingGame', user_id: int) -> str:
    return (
        f"<blockquote><b>{e(EMOJI_CHOOSE_GAME,'🧠')} Выберите игру, на которую хотите сделать ставку!</b></blockquote>\n\n"
        f"{_bet_balance_block(betting_game, user_id)}"
    )


async def show_games_selector(callback: CallbackQuery, betting_game: 'BettingGame'):
    user_id = callback.from_user.id
    text = build_games_selector_text(betting_game, user_id)
    markup = build_games_selector_keyboard()
    await safe_edit_message(callback, text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


# --- ТАБЫ ДЛЯ КУБИКА ---
DICE_TAB_ORDER = ['1куб', '2куба', '3куба']

DICE_TAB_EMOJI = {
    '1куб': '🎲',
    '2куба': '🎲🎲',
    '3куба': '🎲🎲🎲',
}

DICE_TAB_LABEL = {
    '1куб': '1 бросок',
    '2куба': '2 броска',
    '3куба': '3 броска',
}

DICE_TAB_TITLE = {
    '1куб': '1 Куб',
    '2куба': '2 Куба',
    '3куба': '3 Куба',
}


def _dice_tabs_row(active: str) -> list:
    row = []
    for key in DICE_TAB_ORDER:
        label = DICE_TAB_LABEL[key]
        text = f"· {label} ·" if key == active else label
        row.append(InlineKeyboardButton(text=text, callback_data=f"dtabs_{key}"))
    return row


def _dice_outcome_rows(active: str) -> list:
    if active == '1куб':
        # Всё сразу плоским списком, без под-меню (как в 2 куба / 3 куба)
        return [
            [
                InlineKeyboardButton(text="Нечет (x2)", callback_data="bet_dice_куб_нечет"),
                InlineKeyboardButton(text="Чет (x2)", callback_data="bet_dice_куб_чет")
            ],
            [
                InlineKeyboardButton(text="Меньше (x2)", callback_data="bet_dice_куб_мал"),
                InlineKeyboardButton(text="Больше (x2)", callback_data="bet_dice_куб_бол")
            ],
            [
                InlineKeyboardButton(text="1 (x6)", callback_data="bet_dice_куб_1"),
                InlineKeyboardButton(text="2 (x6)", callback_data="bet_dice_куб_2"),
                InlineKeyboardButton(text="3 (x6)", callback_data="bet_dice_куб_3")
            ],
            [
                InlineKeyboardButton(text="4 (x6)", callback_data="bet_dice_куб_4"),
                InlineKeyboardButton(text="5 (x6)", callback_data="bet_dice_куб_5"),
                InlineKeyboardButton(text="6 (x6)", callback_data="bet_dice_куб_6")
            ],
        ]
    elif active == '2куба':
        # Всё сразу плоским списком, без под-меню
        return [
            [
                InlineKeyboardButton(text="Оба чёт (x4.0)", callback_data="bet_dice2_куб2_обачет"),
                InlineKeyboardButton(text="Оба нечёт (x4.0)", callback_data="bet_dice2_куб2_обанечет")
            ],
            [
                InlineKeyboardButton(text="Оба больше (x4.0)", callback_data="bet_dice2_куб2_обабольше"),
                InlineKeyboardButton(text="Оба меньше (x4.0)", callback_data="bet_dice2_куб2_обаменьше")
            ],
            [
                InlineKeyboardButton(text="Сумма < 7 (x2.4)", callback_data="bet_dice2_куб2_сумма_меньше7"),
                InlineKeyboardButton(text="Сумма > 7 (x2.4)", callback_data="bet_dice2_куб2_сумма_больше7")
            ],
            [
                InlineKeyboardButton(text="Сумма = 7 (x6.0)", callback_data="bet_dice2_куб2_сумма_ровно7")
            ],
            [
                InlineKeyboardButton(text="1,1 (x36)", callback_data="bet_dice2_куб2_конкретныйдубль_1"),
                InlineKeyboardButton(text="2,2 (x36)", callback_data="bet_dice2_куб2_конкретныйдубль_2"),
                InlineKeyboardButton(text="3,3 (x36)", callback_data="bet_dice2_куб2_конкретныйдубль_3")
            ],
            [
                InlineKeyboardButton(text="4,4 (x36)", callback_data="bet_dice2_куб2_конкретныйдубль_4"),
                InlineKeyboardButton(text="5,5 (x36)", callback_data="bet_dice2_куб2_конкретныйдубль_5"),
                InlineKeyboardButton(text="6,6 (x36)", callback_data="bet_dice2_куб2_конкретныйдубль_6")
            ],
            [
                InlineKeyboardButton(text="Любой дубль (x6.0)", callback_data="bet_dice2_куб2_любойдубль"),
                InlineKeyboardButton(text="Произведение ≥18 (x4.0)", callback_data="bet_dice2_куб2_произведение")
            ],
        ]
    elif active == '3куба':
        # Всё сразу плоским списком, без под-меню
        return [
            [
                InlineKeyboardButton(text="Три чёт (x8.0)", callback_data="bet_dice3_куб3_3чет"),
                InlineKeyboardButton(text="Три нечёт (x8.0)", callback_data="bet_dice3_куб3_3нечет")
            ],
            [
                InlineKeyboardButton(text="Три меньше (x8.0)", callback_data="bet_dice3_куб3_меньше10"),
                InlineKeyboardButton(text="Три больше (x8.0)", callback_data="bet_dice3_куб3_больше10")
            ],
            [
                InlineKeyboardButton(text="1,1,1 (x216)", callback_data="bet_dice3_куб3_конкретныйтрипл_1"),
                InlineKeyboardButton(text="2,2,2 (x216)", callback_data="bet_dice3_куб3_конкретныйтрипл_2"),
                InlineKeyboardButton(text="3,3,3 (x216)", callback_data="bet_dice3_куб3_конкретныйтрипл_3")
            ],
            [
                InlineKeyboardButton(text="4,4,4 (x216)", callback_data="bet_dice3_куб3_конкретныйтрипл_4"),
                InlineKeyboardButton(text="5,5,5 (x216)", callback_data="bet_dice3_куб3_конкретныйтрипл_5"),
                InlineKeyboardButton(text="6,6,6 (x216)", callback_data="bet_dice3_куб3_конкретныйтрипл_6")
            ],
            [
                InlineKeyboardButton(text="Любой трипл (x36.0)", callback_data="bet_dice3_куб3_любойтрипл"),
                InlineKeyboardButton(text="Произведение ≥108 (x12.7)", callback_data="bet_dice3_куб3_произведение")
            ],
        ]
    return []


def build_dice_hub_keyboard(active: str) -> InlineKeyboardMarkup:
    if active not in DICE_TAB_ORDER:
        active = '1куб'
    rows = [_tabs_row('dice'), _dice_tabs_row(active)]
    rows.extend(_dice_outcome_rows(active))
    rows.append([
        InlineKeyboardButton(text="Назад", callback_data="games", icon_custom_emoji_id=EMOJI_BACK)
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_dice_hub_text(active: str, betting_game: 'BettingGame' = None, user_id: int = 0) -> str:
    if active not in DICE_TAB_ORDER:
        active = '1куб'
    emoji = DICE_TAB_EMOJI[active]
    title = DICE_TAB_TITLE[active]
    
    header = _bet_balance_block(betting_game, user_id) if betting_game and user_id else ""
    
    return (
        f"<blockquote><b>{emoji} {title}</b></blockquote>\n\n"
        f"{header}"
        f"<blockquote><b><i>Выберите исход:</i></b></blockquote>\n\n"
    )


async def show_dice_menu(callback: CallbackQuery, betting_game: 'BettingGame' = None):
    user_id = callback.from_user.id
    active = '1куб'
    text = build_dice_hub_text(active, betting_game, user_id)
    markup = build_dice_hub_keyboard(active)
    await safe_edit_message(callback, text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@router.callback_query(F.data.startswith("dtabs_"))
async def dice_tab_switch(callback: CallbackQuery, state: FSMContext):
    betting_game = get_betting_game()
    user_id = callback.from_user.id
    active = callback.data.split("_", 1)[1]
    if active not in DICE_TAB_ORDER:
        active = '1куб'
    text = build_dice_hub_text(active, betting_game, user_id)
    markup = build_dice_hub_keyboard(active)
    await safe_edit_message(callback, text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


async def show_exact_number_menu(callback: CallbackQuery, betting_game: 'BettingGame' = None):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 (x6)", callback_data="bet_dice_куб_1"),
            InlineKeyboardButton(text="2 (x6)", callback_data="bet_dice_куб_2"),
            InlineKeyboardButton(text="3 (x6)", callback_data="bet_dice_куб_3")
        ],
        [
            InlineKeyboardButton(text="4 (x6)", callback_data="bet_dice_куб_4"),
            InlineKeyboardButton(text="5 (x6)", callback_data="bet_dice_куб_5"),
            InlineKeyboardButton(text="6 (x6)", callback_data="bet_dice_куб_6")
        ],
        [
            InlineKeyboardButton(text="Назад", callback_data="game_dice", icon_custom_emoji_id=EMOJI_BACK)
        ]
    ])
    header = _bet_balance_block(betting_game, callback.from_user.id) if betting_game else ""
    await safe_edit_message(callback,
        f"<blockquote><b>{e(EMOJI_NUMBER,'🔢')} Точное число</b></blockquote>\n\n"
        f"{header}"
        f"<blockquote><b><i>Выберите число:</i></b></blockquote>",
        reply_markup=markup, parse_mode='HTML'
    )
    await callback.answer()


# --- ОСТАЛЬНЫЕ МЕНЮ ---
def _tabs_row(active: str) -> list:
    row = []
    for key in GAME_TAB_ORDER:
        emoji = GAME_TAB_EMOJI[key]
        text = f"· {emoji} ·" if key == active else emoji
        row.append(InlineKeyboardButton(text=text, callback_data=f"gtab_{key}"))
    return row


def _build_basketball_menu_content(betting_game: 'BettingGame' = None, user_id: int = 0):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        _tabs_row('basketball'),
        [
            InlineKeyboardButton(text="Любой гол (x2.5)", callback_data="bet_basketball_баскет_любойгол"),
            InlineKeyboardButton(text="Чистый гол (x5)", callback_data="bet_basketball_баскет_чистыйгол")
        ],
        [
            InlineKeyboardButton(text="Промах (x1.66)", callback_data="bet_basketball_баскет_промах")
        ],
        [
            InlineKeyboardButton(text="Отскок (x5)", callback_data="bet_basketball_баскет_отскок"),
            InlineKeyboardButton(text="Застрял (x5)", callback_data="bet_basketball_баскет_застрял")
        ],
        [
            InlineKeyboardButton(text=f"2× {_BASKETBALL_DOUBLE_TARGET_NAME.get(1, '1')} (x25)", callback_data="bet_basketball_баскет_конкретныйдубль_1"),
            InlineKeyboardButton(text=f"2× {_BASKETBALL_DOUBLE_TARGET_NAME.get(3, '3')} (x25)", callback_data="bet_basketball_баскет_конкретныйдубль_3"),
            InlineKeyboardButton(text=f"2× {_BASKETBALL_DOUBLE_TARGET_NAME.get(5, '5')} (x25)", callback_data="bet_basketball_баскет_конкретныйдубль_5")
        ],
        [
            InlineKeyboardButton(text="Любой дубль (x5)", callback_data="bet_basketball_баскет_любойдубль")
        ],
        [
            InlineKeyboardButton(text="Два гола (x6.25)", callback_data="bet_basketball_баскет_двагола"),
            InlineKeyboardButton(text="Два промаха (x2.77)", callback_data="bet_basketball_баскет_двапромаха")
        ],
        [
            InlineKeyboardButton(text="Назад", callback_data="games", icon_custom_emoji_id=EMOJI_BACK)
        ]
    ])
    header = _bet_balance_block(betting_game, user_id) if betting_game else ""
    text = (
        f"<blockquote><b>🏀 Баскетбол</b></blockquote>\n\n"
        f"{header}"
        f"<blockquote><b><i>Выберите исход:</i></b></blockquote>\n\n"
    )
    return text, markup


async def show_basketball_menu(callback: CallbackQuery, betting_game: 'BettingGame' = None):
    text, markup = _build_basketball_menu_content(betting_game, callback.from_user.id)
    await safe_edit_message(callback, text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


def _build_football_menu_content(betting_game: 'BettingGame' = None, user_id: int = 0):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        _tabs_row('football'),
        [
            InlineKeyboardButton(text="Любой гол (x1.65)", callback_data="bet_football_футбол_гол"),
            InlineKeyboardButton(text="Промах (x2.5)", callback_data="bet_football_футбол_мимо")
        ],
        [
            InlineKeyboardButton(text="Штанга (x5)", callback_data="bet_football_футбол_штанга"),
            InlineKeyboardButton(text="Мимо ворот (x5)", callback_data="bet_football_футбол_мимоворот")
        ],
        [
            InlineKeyboardButton(text="Гол под углом (x5)", callback_data="bet_football_футбол_угол"),
            InlineKeyboardButton(text="Гол в центр (x5)", callback_data="bet_football_футбол_центр")
        ],
        [
            InlineKeyboardButton(text="Девятка (x5)", callback_data="bet_football_футбол_девятка")
        ],
        [
            InlineKeyboardButton(text=f"2× {_FOOTBALL_DOUBLE_TARGET_NAME.get(1, '1')} (x23)", callback_data="bet_football_футбол_конкретныйдубль_1"),
            InlineKeyboardButton(text=f"2× {_FOOTBALL_DOUBLE_TARGET_NAME.get(2, '2')} (x23)", callback_data="bet_football_футбол_конкретныйдубль_2"),
            InlineKeyboardButton(text=f"2× {_FOOTBALL_DOUBLE_TARGET_NAME.get(3, '3')} (x23)", callback_data="bet_football_футбол_конкретныйдубль_3")
        ],
        [
            InlineKeyboardButton(text=f"2× {_FOOTBALL_DOUBLE_TARGET_NAME.get(4, '4')} (x23)", callback_data="bet_football_футбол_конкретныйдубль_4"),
            InlineKeyboardButton(text=f"2× {_FOOTBALL_DOUBLE_TARGET_NAME.get(5, '5')} (x23)", callback_data="bet_football_футбол_конкретныйдубль_5")
        ],
        [
            InlineKeyboardButton(text="Любой дубль (x5)", callback_data="bet_football_футбол_любойдубль")
        ],
        [
            InlineKeyboardButton(text="Назад", callback_data="games", icon_custom_emoji_id=EMOJI_BACK)
        ]
    ])
    header = _bet_balance_block(betting_game, user_id) if betting_game else ""
    text = (
        f"<blockquote><b>⚽ Футбол</b></blockquote>\n\n"
        f"{header}"
        f"<blockquote><b><i>Выберите исход:</i></b></blockquote>\n\n"
    )
    return text, markup


async def show_football_menu(callback: CallbackQuery, betting_game: 'BettingGame' = None):
    text, markup = _build_football_menu_content(betting_game, callback.from_user.id)
    await safe_edit_message(callback, text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


def _build_darts_menu_content(betting_game: 'BettingGame' = None, user_id: int = 0):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        _tabs_row('darts'),
        [InlineKeyboardButton(text="Мимо (x6)", callback_data="bet_darts_дартс_мимо"),
         InlineKeyboardButton(text="Красное (x3)", callback_data="bet_darts_дартс_красное")],
        [InlineKeyboardButton(text="Белое (x3)", callback_data="bet_darts_дартс_белое")],
        [InlineKeyboardButton(text="Центр (x6)", callback_data="bet_darts_дартс_центр"),
         InlineKeyboardButton(text="Дубль мимо (x36)", callback_data="bet_darts_дартс2_дубльмимо")],
        [InlineKeyboardButton(text="Дубль красное (x9)", callback_data="bet_darts_дартс2_дублькрасное")],
        [InlineKeyboardButton(text="Дубль белое (x9)", callback_data="bet_darts_дартс2_дубльбелое"),
         InlineKeyboardButton(text="Дубль центр (x36)", callback_data="bet_darts_дартс2_дубльцентр")],
        [
            InlineKeyboardButton(text="Назад", callback_data="games", icon_custom_emoji_id=EMOJI_BACK)
        ]
    ])
    header = _bet_balance_block(betting_game, user_id) if betting_game else ""
    text = (
        f"<blockquote><b>🎯 Дартс</b></blockquote>\n\n"
        f"{header}"
        f"<blockquote><b><i>Выберите исход:</i></b></blockquote>\n\n"
    )
    return text, markup


async def show_darts_menu(callback: CallbackQuery, betting_game: 'BettingGame' = None):
    text, markup = _build_darts_menu_content(betting_game, callback.from_user.id)
    await safe_edit_message(callback, text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


# --- РАЗДЕЛЫ ДЛЯ БОУЛИНГА (как в кубике: «1 бросок» / «2 броска» наверху) ---
BOWLING_TAB_ORDER = ['1бросок', '2броска']

BOWLING_TAB_EMOJI = {
    '1бросок': '🎳',
    '2броска': '🎳🎳',
}

BOWLING_TAB_LABEL = {
    '1бросок': '1 бросок',
    '2броска': '2 броска',
}

BOWLING_TAB_TITLE = {
    '1бросок': 'Боулинг · 1 бросок',
    '2броска': 'Боулинг · 2 броска',
}


def _bowling_tabs_row(active: str) -> list:
    row = []
    for key in BOWLING_TAB_ORDER:
        label = BOWLING_TAB_LABEL[key]
        text = f"· {label} ·" if key == active else label
        row.append(InlineKeyboardButton(text=text, callback_data=f"btabs_{key}"))
    return row


def _bowling_outcome_rows(active: str) -> list:
    def btn(text: str, bet_type: str, target: Optional[int] = None) -> InlineKeyboardButton:
        # множитель берём из BOWLING_BET_TYPES, чтобы кнопки не расходились с конфигом
        mult = _fmt_mult(BOWLING_BET_TYPES[bet_type]['multiplier'])
        cb = f"bet_bowling_{bet_type}" + (f"_{target}" if target is not None else "")
        return InlineKeyboardButton(text=f"{text} (x{mult})", callback_data=cb)

    if active == '2броска':
        return [
            [btn("Любой дубль", 'боулинг_любойдубль')],
            [btn(f"2× {_BOWLING_SHORT_NAME[v]}", 'боулинг_конкретныйдубль', v) for v in (1, 2, 3)],
            [btn(f"2× {_BOWLING_SHORT_NAME[v]}", 'боулинг_конкретныйдубль', v) for v in (4, 5, 6)],
        ]
    # '1бросок'
    return [
        [btn("Победа", 'боулинг_победа'), btn("Поражение", 'боулинг_поражение')],
        [btn("Промах", 'боулинг_промах'), btn("Страйк", 'боулинг_страйк')],
        [btn("Сбито 1/6", 'боулинг_1из6'), btn("Сбито 3/6", 'боулинг_3из6')],
        [btn("Сбито 4/6", 'боулинг_4из6'), btn("Сбито 5/6", 'боулинг_5из6')],
    ]


def _build_bowling_menu_content(betting_game: 'BettingGame' = None, user_id: int = 0, active: str = '1бросок'):
    if active not in BOWLING_TAB_ORDER:
        active = '1бросок'
    rows = [_tabs_row('bowling'), _bowling_tabs_row(active)]
    rows.extend(_bowling_outcome_rows(active))
    rows.append([
        InlineKeyboardButton(text="Назад", callback_data="games", icon_custom_emoji_id=EMOJI_BACK)
    ])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    header = _bet_balance_block(betting_game, user_id) if betting_game else ""
    text = (
        f"<blockquote><b>{BOWLING_TAB_EMOJI[active]} {BOWLING_TAB_TITLE[active]}</b></blockquote>\n\n"
        f"{header}"
        f"<blockquote><b><i>Выберите исход:</i></b></blockquote>\n\n"
    )
    return text, markup


async def show_bowling_menu(callback: CallbackQuery, betting_game: 'BettingGame' = None, active: str = '1бросок'):
    text, markup = _build_bowling_menu_content(betting_game, callback.from_user.id, active)
    await safe_edit_message(callback, text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


async def show_bowling2_menu(callback: CallbackQuery, betting_game: 'BettingGame' = None):
    """Возврат сразу в раздел «2 броска» (кнопка «Изменить исход» после дубля)."""
    await show_bowling_menu(callback, betting_game, active='2броска')


@router.callback_query(F.data.startswith("btabs_"))
async def bowling_tab_switch(callback: CallbackQuery, state: FSMContext):
    betting_game = get_betting_game()
    active = callback.data.split("_", 1)[1]
    if active not in BOWLING_TAB_ORDER:
        active = '1бросок'
    await show_bowling_menu(callback, betting_game, active)


# ─────────────────────────────────────────────────────────────────────────────
#  Одиночные текстовые команды вида "куб", "дартс", "фут" и т.п. —
#  открывают соответствующий раздел игры (без исхода и суммы).
# ─────────────────────────────────────────────────────────────────────────────

GAME_MENU_COMMAND_TO_KEY = {
    'куб': 'dice', 'dice': 'dice', 'кубик': 'dice', 'cube': 'dice',
    'фут': 'football', 'fut': 'football', 'foot': 'football',
    'футбол': 'football', 'football': 'football',
    'баскет': 'basketball', 'basket': 'basketball', 'basketball': 'basketball',
    'баскетбол': 'basketball', 'bask': 'basketball',
    'дартс': 'darts', 'dart': 'darts', 'darts': 'darts', 'дарт': 'darts',
    'боулинг': 'bowling', 'bowling': 'bowling', 'боул': 'bowling', 'bowl': 'bowling',
}


def _normalize_menu_command(text: str) -> str:
    t = (text or '').strip().lower()
    if t.startswith('/'):
        t = t[1:]
    return t


def is_game_menu_command(text: str) -> bool:
    if not text:
        return False
    return _normalize_menu_command(text) in GAME_MENU_COMMAND_TO_KEY


async def handle_game_menu_command(message: Message, betting_game: 'BettingGame'):
    menu_key = GAME_MENU_COMMAND_TO_KEY.get(_normalize_menu_command(message.text))
    if not menu_key:
        return

    user_id = message.from_user.id

    if menu_key == 'dice':
        text = build_dice_hub_text('1куб', betting_game, user_id)
        markup = build_dice_hub_keyboard('1куб')
    elif menu_key == 'football':
        text, markup = _build_football_menu_content(betting_game, user_id)
    elif menu_key == 'basketball':
        text, markup = _build_basketball_menu_content(betting_game, user_id)
    elif menu_key == 'darts':
        text, markup = _build_darts_menu_content(betting_game, user_id)
    elif menu_key == 'bowling':
        text, markup = _build_bowling_menu_content(betting_game, user_id)
    else:
        return

    await message.answer(text, reply_markup=markup, parse_mode='HTML')


async def request_amount(callback: CallbackQuery, state: FSMContext, betting_game: BettingGame):
    # Определяем тип ставки из callback_data
    data = callback.data
    bet_type = None
    
    # Обработка конкретных дублей и триплов
    if data.startswith("bet_dice2_куб2_конкретныйдубль_"):
        target = int(data.split("_")[-1])
        bet_type = "куб2_конкретныйдубль"
        # Сохраняем целевое число в конфиге ставки
        bet_config = betting_game.get_bet_config(bet_type)
        if bet_config:
            bet_config['target'] = target
    elif data.startswith("bet_dice3_куб3_конкретныйтрипл_"):
        target = int(data.split("_")[-1])
        bet_type = "куб3_конкретныйтрипл"
        bet_config = betting_game.get_bet_config(bet_type)
        if bet_config:
            bet_config['target'] = target
    elif data.startswith("bet_football_футбол_конкретныйдубль_"):
        target = int(data.split("_")[-1])
        bet_type = "футбол_конкретныйдубль"
        bet_config = betting_game.get_bet_config(bet_type)
        if bet_config:
            bet_config['target'] = target
    elif data.startswith("bet_basketball_баскет_конкретныйдубль_"):
        target = int(data.split("_")[-1])
        bet_type = "баскет_конкретныйдубль"
        bet_config = betting_game.get_bet_config(bet_type)
        if bet_config:
            bet_config['target'] = target
    elif data.startswith("bet_bowling_боулинг_конкретныйдубль_"):
        target = int(data.split("_")[-1])
        bet_type = "боулинг_конкретныйдубль"
        bet_config = betting_game.get_bet_config(bet_type)
        if bet_config:
            bet_config['target'] = target
    else:
        # Обычные ставки
        parts = data.split('_')
        if len(parts) >= 3:
            bet_type = '_'.join(parts[2:])
    
    if not bet_type:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    user_id = callback.from_user.id

    allowed, wait_time = check_rate_limit(user_id)
    if not allowed:
        await callback.answer(f"⏳ Подождите {wait_time:.1f} сек", show_alert=True)
        return

    if betting_game.is_user_in_game(user_id):
        await callback.answer("⏳ Дождитесь окончания игры!", show_alert=True)
        return

    bet_config = betting_game.get_bet_config(bet_type)
    if not bet_config:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    amount = betting_game.get_current_bet(user_id)
    if amount is None:
        await callback.answer(
            "❌ Ставка не установлена!\n"
            "Отправьте сумму в чат:\n"
            "• реальная ставка: 0.1$\n"
            "• бонусная ставка: 0.1 бонус",
            show_alert=True
        )
        return

    source, reason = betting_game.take_bet(user_id, amount)
    if source is None:
        await callback.answer(f"❌ {reason}", show_alert=True)
        return

    if source == BET_MODE_REAL:  # с бонусных ставок реферальная комиссия не платится
        asyncio.create_task(notify_referrer_commission(user_id, amount))

    nickname = _build_nickname(callback.from_user)
    notify_target = callback.message

    betting_game.start_game(user_id)
    await callback.answer()

    await _execute_and_settle(
        callback.message.chat.id, user_id, nickname, amount, bet_type, bet_config, betting_game,
        callback=callback, notify_target=notify_target,
    )


async def process_bet_amount(message: Message, state: FSMContext, betting_game: BettingGame):
    user_id = message.from_user.id

    if user_id not in betting_game.pending_bets:
        await state.clear()
        return

    if betting_game.is_user_in_game(user_id):
        await message.answer("⏳ Дождитесь окончания текущей игры!")
        return

    try:
        amount = float(message.text)

        if amount < MIN_BET:
            await message.answer(f"{e(EMOJI_CROSS,'❌')} Минимальная ставка: {MIN_BET}")
            return

        if amount > MAX_BET:
            await message.answer(f"{e(EMOJI_CROSS,'❌')} Максимальная ставка: {MAX_BET}")
            return

        bet_type   = betting_game.pending_bets[user_id]
        bet_config = betting_game.get_bet_config(bet_type)
        if not bet_config:
            await message.answer("❌ Ошибка конфигурации ставки")
            if user_id in betting_game.pending_bets:
                del betting_game.pending_bets[user_id]
            await state.clear()
            return

        source, reason = betting_game.take_bet(user_id, amount)
        if source is None:
            await message.answer(
                f"<blockquote><b>{e(EMOJI_CROSS,'❌')} {reason}</b></blockquote>\n\n",
                parse_mode='HTML'
            )
            if user_id in betting_game.pending_bets:
                del betting_game.pending_bets[user_id]
            await state.clear()
            return

        if source == BET_MODE_REAL:  # с бонусных ставок реферальная комиссия не платится
            asyncio.create_task(notify_referrer_commission(user_id, amount))

        nickname = _build_nickname(message.from_user)

        betting_game.start_game(user_id)

        if user_id in betting_game.pending_bets:
            del betting_game.pending_bets[user_id]
        await state.clear()

        await _execute_and_settle(
            message.chat.id, user_id, nickname, amount, bet_type, bet_config, betting_game,
            callback=None, notify_target=message,
        )

    except ValueError:
        await message.answer("❌ Введите корректное число")
    except Exception as e:
        logging.error(f"Error: {e}")
        await message.answer("❌ Произошла ошибка")
        if user_id in betting_game.pending_bets:
            del betting_game.pending_bets[user_id]
        await state.clear()


async def cancel_bet(callback: CallbackQuery, state: FSMContext, betting_game: BettingGame):
    user_id = callback.from_user.id
    if user_id in betting_game.pending_bets:
        del betting_game.pending_bets[user_id]
    await state.clear()

    games_callback = get_games_callback()
    if games_callback is not None:
        await games_callback(callback, state)


# --------------------------------------------------------------------------
# Недостающие связи раздела «Игры» с общим меню (main.py)
# --------------------------------------------------------------------------
# Ниже регистрируются обработчики для всех callback_data и текстовых команд,
# на которые ссылаются клавиатуры выше (кнопки категорий игр, кнопки ставок,
# разделы "в разработке"), но которые раньше не имели своего @router-хендлера.

_GAME_CATEGORY_HANDLERS = {
    'football': show_football_menu,
    'basketball': show_basketball_menu,
    'darts': show_darts_menu,
    'bowling': show_bowling_menu,
}

_BET_CALLBACK_PREFIXES = (
    "bet_dice3_", "bet_dice2_", "bet_dice_",
    "bet_basketball_", "bet_football_", "bet_darts_", "bet_bowling_",
)

_IN_DEV_GAME_CALLBACKS = {"custom_games_menu"}

_IN_DEV_GAME_TEXT = "🚧 Этот раздел находится в разработке.\nСкоро здесь появится функционал!"


@router.callback_query(F.data.startswith(("game_", "gtab_")))
async def game_category_callback(callback: CallbackQuery) -> None:
    """Открывает меню конкретной категории игр (Кубик/Футбол/Баскетбол/Дартс/Боулинг).
    Ловит как кнопки выбора игры из общего селектора (game_...), так и верхний
    ряд переключения игр внутри каждого меню (gtab_...)."""
    betting_game = get_betting_game()
    key = callback.data.split("_", 1)[1]

    if key == "dice":
        await show_dice_menu(callback, betting_game)
        return

    handler = _GAME_CATEGORY_HANDLERS.get(key)
    if handler:
        await handler(callback, betting_game)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith(_BET_CALLBACK_PREFIXES))
async def bet_button_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Обрабатывает нажатие любой кнопки ставки (Кубик/Футбол/Баскетбол/Дартс/Боулинг)."""
    betting_game = get_betting_game()
    if betting_game is None:
        await callback.answer("❌ Бот перезапускается, попробуйте ещё раз чуть позже", show_alert=True)
        return
    await request_amount(callback, state, betting_game)


@router.callback_query(F.data.in_(_IN_DEV_GAME_CALLBACKS))
async def games_in_dev_callback(callback: CallbackQuery) -> None:
    """Заглушка для ещё не реализованных разделов (Авторские)."""
    await callback.answer(_IN_DEV_GAME_TEXT, show_alert=True)


@router.message(F.text)
async def games_text_router(message: Message, state: FSMContext) -> None:
    """Ловит текстовые команды раздела игр (установка ставки '0.1$', команды вида
    '/куб чет 1', и текстовые команды меню игр), не перехваченные хендлерами main.py.
    Регистрируется последним и не реагирует, если ни один формат не подошёл —
    тогда сообщение просто остаётся без ответа от этого роутера."""
    betting_game = get_betting_game()
    if betting_game is None:
        return

    text = message.text or ""

    if is_set_bet_command(text):
        await handle_set_bet_command(message, betting_game)
        return

    if is_set_bonus_bet_command(text):
        await handle_set_bonus_bet_command(message, betting_game)
        return

    if is_game_menu_command(text):
        await handle_game_menu_command(message, betting_game)
        return

    if is_bet_command(text):
        await handle_text_bet_command(message, betting_game)
