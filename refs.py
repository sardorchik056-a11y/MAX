"""
refs.py — партнёрская (реферальная) программа.

Как это работает:

    1. У каждого игрока есть личная ссылка:  https://t.me/<bot>?start=ref_<user_id>
       Ссылка показывается в разделе «Партнеры» (кнопка в нижней клавиатуре).
    2. Новый игрок запускает бота по этой ссылке — он закрепляется за пригласившим НАВСЕГДА
       (привязка делается один раз, переназначить нельзя).
    3. С КАЖДОГО оплаченного пополнения реферала пригласивший получает REF_PERCENT (2%)
       на свой баланс. Начисление идёт из payments.py в момент зачисления счёта и
       защищено от дублей: один счёт — одно начисление (UNIQUE по deposit_ref).
    4. Обоим сторонам приходят уведомления: о новом реферале и о каждом начислении.

Защита от злоупотреблений:
    • нельзя пригласить самого себя;
    • привязаться можно только один раз и только к тому, кто открывал раздел «Партнеры»;
    • нельзя привязаться, если у игрока уже есть оплаченные пополнения (не новичок);
    • нельзя создать «петлю» (A пригласил B, а B пытается пригласить A).

Подключение — см. инструкцию в конце ответа / комментарии ниже:
    main.py:      import refs as refs_module ; refs_router = refs_module.router ;
                  dp.include_router(refs_router)  (до games_router)
    payments.py:  после adjust_balance(..., "deposit") -> await refs.reward_referrer(...)
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import sqlite3
import time
from contextlib import closing
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from storage import adjust_balance, get_profile_stats

# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------

REF_PERCENT = 2.0            # % от каждого пополнения реферала, который получает пригласивший
REF_PREFIX = "ref_"          # start-параметр: t.me/<bot>?start=ref_<user_id>
REF_LIST_LIMIT = 10          # сколько рефералов показывать в списке

# Тип транзакции в storage.adjust_balance(user_id, amount, kind).
# "admin_grant" — заведомо начисляет и НЕ попадает в «Всего депозитов» (как возврат в payments.py).
# Если в storage.py есть отдельный тип для бонусов (например "referral") — впишите его сюда.
REF_REWARD_KIND = "admin_grant"

SHARE_TEXT = "🎲 Lucky Dice — заходи и испытай удачу! Играем вместе:"

# Кому слать уведомления о сбоях начислений. Заполняется из main.py (ADMIN_IDS).
ALERT_ADMIN_IDS: set[int] = set()

DB_PATH = Path(__file__).with_name("refs.db")

# Кастомные эмодзи (те же, что уже используются в main.py / payments.py)
EMOJI_PARTNERS = "5258362837411045098"    # кнопка «Партнеры»
EMOJI_STATS = "5258330865674494479"       # как «Статистика»
EMOJI_DEPOSITS = "5902206159095339799"    # 🤑 как «Всего депозитов»
EMOJI_MONEY = "5778421276024509124"       # 💰 как «Оборот»
EMOJI_PROFILE = "5316727448644103237"     # как «Профиль»
EMOJI_DONE = "6037175527846975726"        # ✅ как «Чеки»
EMOJI_BACK = "6039539366177541657"        # «Назад»

log = logging.getLogger("refs")


def _tge(emoji_id: str, fallback: str) -> str:
    """Кастомный эмодзи для текста сообщения (parse_mode=HTML)."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


PARTNERS_ICON = _tge(EMOJI_PARTNERS, "🤝")
STATS_ICON = _tge(EMOJI_STATS, "📊")
DEPOSITS_ICON = _tge(EMOJI_DEPOSITS, "🤑")
MONEY_ICON = _tge(EMOJI_MONEY, "💰")
PROFILE_ICON = _tge(EMOJI_PROFILE, "👤")
DONE_ICON = _tge(EMOJI_DONE, "✅")


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------


def _fmt_usd(value: float) -> str:
    return f"${value:,.2f}"


def _pct() -> str:
    return f"{REF_PERCENT:g}%"


def calc_reward(amount: float) -> float:
    """Вознаграждение пригласившему с суммы пополнения (округление до центов)."""
    reward = Decimal(str(amount)) * Decimal(str(REF_PERCENT)) / Decimal(100)
    return float(reward.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_ref_payload(payload: str | None) -> int | None:
    """'ref_123456' -> 123456; всё остальное -> None."""
    match = re.fullmatch(rf"{REF_PREFIX}(\d{{1,15}})", payload or "")
    return int(match.group(1)) if match else None


def _display_name(user: User) -> str:
    return user.full_name or (f"@{user.username}" if user.username else f"ID {user.id}")


async def _ref_link(bot: Bot, user_id: int) -> str:
    me = await bot.me()  # aiogram кэширует результат
    return f"https://t.me/{me.username}?start={REF_PREFIX}{user_id}"


# --------------------------------------------------------------------------
# База (SQLite)
# --------------------------------------------------------------------------


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _db_init() -> None:
    with closing(_conn()) as conn, conn:
        # игроки, которым уже показана реферальная ссылка (только к ним можно привязаться)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS partners (
                user_id    INTEGER PRIMARY KEY,
                created_at REAL NOT NULL
            )
            """
        )
        # кто кого пригласил (один игрок — один пригласивший, навсегда)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                user_id     INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                name        TEXT,
                created_at  REAL    NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals (referrer_id)")
        # начисления. status: pending -> paid | failed
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ref_earnings (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id    INTEGER NOT NULL,
                referral_id    INTEGER NOT NULL,
                deposit_ref    TEXT    NOT NULL UNIQUE,
                deposit_amount REAL    NOT NULL,
                reward         REAL    NOT NULL,
                status         TEXT    NOT NULL DEFAULT 'pending',
                created_at     REAL    NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_earnings_referrer ON ref_earnings (referrer_id, status)")


_db_init()  # CREATE IF NOT EXISTS — безопасно вызывать при каждом импорте


async def _run(fn, *args):
    """SQLite — синхронный, поэтому выносим в поток, чтобы не блокировать event loop."""
    return await asyncio.to_thread(fn, *args)


def _db_register_partner(user_id: int) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO partners (user_id, created_at) VALUES (?, ?)", (user_id, time.time()))


def _db_bind(user_id: int, referrer_id: int, name: str) -> str:
    """Атомарная привязка. Возвращает: ok | already | no_referrer | cycle."""
    with closing(_conn()) as conn, conn:
        if conn.execute("SELECT 1 FROM referrals WHERE user_id = ?", (user_id,)).fetchone():
            return "already"
        if not conn.execute("SELECT 1 FROM partners WHERE user_id = ?", (referrer_id,)).fetchone():
            return "no_referrer"
        if conn.execute(
            "SELECT 1 FROM referrals WHERE user_id = ? AND referrer_id = ?", (referrer_id, user_id)
        ).fetchone():
            return "cycle"
        conn.execute(
            "INSERT INTO referrals (user_id, referrer_id, name, created_at) VALUES (?, ?, ?, ?)",
            (user_id, referrer_id, name, time.time()),
        )
        return "ok"


def _db_claim_reward(
    referral_id: int, deposit_amount: float, reward: float, deposit_ref: str
) -> tuple[int, int, str] | None:
    """Резервирует начисление. Возвращает (earning_id, referrer_id, имя реферала)
    либо None — если у игрока нет пригласившего или этот счёт уже обработан."""
    with closing(_conn()) as conn, conn:
        ref = conn.execute("SELECT referrer_id, name FROM referrals WHERE user_id = ?", (referral_id,)).fetchone()
        if ref is None:
            return None
        try:
            cur = conn.execute(
                "INSERT INTO ref_earnings (referrer_id, referral_id, deposit_ref, deposit_amount, reward, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ref["referrer_id"], referral_id, deposit_ref, deposit_amount, reward, time.time()),
            )
        except sqlite3.IntegrityError:
            return None  # этот депозит уже давал начисление
        return int(cur.lastrowid), int(ref["referrer_id"]), ref["name"] or f"ID {referral_id}"


def _db_set_earning_status(earning_id: int, status: str) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("UPDATE ref_earnings SET status = ? WHERE id = ?", (status, earning_id))


def _db_stats(user_id: int) -> dict:
    with closing(_conn()) as conn:
        invited = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)).fetchone()[0]
        row = conn.execute(
            "SELECT COUNT(DISTINCT referral_id), COALESCE(SUM(deposit_amount), 0), COALESCE(SUM(reward), 0) "
            "FROM ref_earnings WHERE referrer_id = ? AND status = 'paid'",
            (user_id,),
        ).fetchone()
    return {"invited": int(invited), "active": int(row[0]), "volume": float(row[1]), "earned": float(row[2])}


def _db_referrals(user_id: int, limit: int) -> list[sqlite3.Row]:
    with closing(_conn()) as conn:
        return conn.execute(
            "SELECT r.user_id, r.name, r.created_at, "
            "       COALESCE(SUM(e.deposit_amount), 0) AS deposits, COALESCE(SUM(e.reward), 0) AS reward "
            "FROM referrals r "
            "LEFT JOIN ref_earnings e ON e.referral_id = r.user_id AND e.referrer_id = r.referrer_id "
            "                        AND e.status = 'paid' "
            "WHERE r.referrer_id = ? GROUP BY r.user_id ORDER BY r.created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


# --------------------------------------------------------------------------
# Привязка реферала (вызывается из main.py при /start ref_...)
# --------------------------------------------------------------------------


async def _has_deposits(user_id: int) -> bool:
    """Есть ли у игрока оплаченные пополнения (тогда он не новичок)."""
    from payments import _db_total_deposited  # локальный импорт — payments.py сам импортирует refs

    return await _run(_db_total_deposited, user_id) > 0


async def bind_from_payload(bot: Bot, user: User, payload: str | None) -> bool:
    """Обрабатывает start-параметр вида ref_<id>. True — игрок успешно закреплён за пригласившим."""
    referrer_id = parse_ref_payload(payload)
    if referrer_id is None or referrer_id == user.id:
        return False

    try:
        if await _has_deposits(user.id):
            return False
        result = await _run(_db_bind, user.id, referrer_id, _display_name(user))
    except Exception:
        log.exception("[ref] не удалось привязать user=%s к referrer=%s", user.id, referrer_id)
        return False

    if result != "ok":
        log.info("[ref] привязка отклонена: user=%s referrer=%s причина=%s", user.id, referrer_id, result)
        return False

    log.info("[ref] user=%s приглашён referrer=%s", user.id, referrer_id)
    try:
        await bot.send_message(
            referrer_id,
            f"{PARTNERS_ICON} <b>Новый реферал</b>\n\n"
            f"└ По вашей ссылке присоединился: <b>{html.escape(_display_name(user))}</b>\n\n"
            f"<i>Вы будете получать {_pct()} с каждого его пополнения.</i>",
        )
    except Exception as ex:
        log.warning("[ref] не удалось уведомить referrer=%s: %s", referrer_id, ex)
    return True


# --------------------------------------------------------------------------
# Начисление вознаграждения (вызывается из payments.py)
# --------------------------------------------------------------------------


async def _alert_admins(bot: Bot, text: str) -> None:
    for admin_id in ALERT_ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"<b>[Партнёрка]</b> {text}")
        except Exception as ex:
            log.warning("[ref] не удалось уведомить админа %s: %s", admin_id, ex)


async def reward_referrer(bot: Bot, user_id: int, deposit_amount: float, deposit_ref: str) -> float:
    """Начисляет пригласившему REF_PERCENT от пополнения. Возвращает начисленную сумму (0 — не начислено).

    deposit_ref — уникальный id пополнения (например "cryptobot:12345"): по нему начисление
    гарантированно происходит один раз, даже если функцию вызовут повторно.
    Никогда не бросает исключений — сбой партнёрки не должен ломать зачисление депозита.
    """
    try:
        reward = calc_reward(deposit_amount)
        if reward < 0.01:
            return 0.0

        claim = await _run(_db_claim_reward, user_id, deposit_amount, reward, deposit_ref)
        if claim is None:
            return 0.0
        earning_id, referrer_id, name = claim

        try:
            new_balance = adjust_balance(referrer_id, reward, REF_REWARD_KIND)
        except Exception as ex:
            await _run(_db_set_earning_status, earning_id, "failed")
            log.critical(
                "[ref] НЕ УДАЛОСЬ начислить %.2f USD referrer=%s (реферал %s, депозит %s)",
                reward, referrer_id, user_id, deposit_ref, exc_info=True,
            )
            await _alert_admins(
                bot,
                f"начисление #{earning_id} не выполнено: referrer=<code>{referrer_id}</code> "
                f"${reward:.2f} за депозит <code>{html.escape(deposit_ref)}</code> — "
                f"<code>{html.escape(repr(ex))}</code>. Нужен ручной разбор!",
            )
            return 0.0

        await _run(_db_set_earning_status, earning_id, "paid")
        log.info(
            "[ref] referrer=%s +%.2f USD (%.1f%% от %.2f, реферал %s) -> баланс %.2f",
            referrer_id, reward, REF_PERCENT, deposit_amount, user_id, new_balance,
        )

        try:
            await bot.send_message(
                referrer_id,
                f"{PARTNERS_ICON} <b>Реферальное начисление</b>\n\n"
                f"┌ Реферал: <b>{html.escape(name)}</b>\n"
                f"├ Пополнение: <b>{_fmt_usd(deposit_amount)}</b>\n"
                f"├ Ваша доля ({_pct()}): <b>+{_fmt_usd(reward)}</b>\n"
                f"└ Баланс: <b>{_fmt_usd(get_profile_stats(referrer_id)['balance'])}</b>",
            )
        except Exception as ex:
            log.warning("[ref] не удалось уведомить referrer=%s: %s", referrer_id, ex)
        return reward
    except Exception:
        log.exception("[ref] сбой начисления: user=%s депозит=%s", user_id, deposit_ref)
        return 0.0


# --------------------------------------------------------------------------
# Экраны (aiogram)
# --------------------------------------------------------------------------

router = Router()


def _back_button(callback_data: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="Назад", callback_data=callback_data, icon_custom_emoji_id=EMOJI_BACK)]


def _home_text(stats: dict, link: str) -> str:
    return (
        f"{PARTNERS_ICON} <b>Партнёрская программа</b>\n\n"
        f"<i>Приглашайте друзей и получайте <b>{_pct()}</b> с каждого их пополнения — "
        "всегда, а не только с первого.</i>\n\n"
        f"┌ Приглашено: <b>{stats['invited']}</b>\n"
        f"├ Пополнили баланс: <b>{stats['active']}</b>\n"
        f"├ Пополнения рефералов: <b>{_fmt_usd(stats['volume'])}</b>\n"
        f"└ Ваш заработок: <b>{_fmt_usd(stats['earned'])}</b>\n\n"
        f"{DONE_ICON} <b>Как это работает</b>\n"
        "├ 1. Отправьте другу свою ссылку\n"
        "├ 2. Друг запускает бота и пополняет баланс\n"
        f"└ 3. Вы мгновенно получаете {_pct()} на свой баланс\n\n"
        "<b>Ваша ссылка</b> <i>(нажмите, чтобы скопировать)</i>:\n"
        f"<code>{link}</code>"
    )


def _home_keyboard(link: str) -> InlineKeyboardMarkup:
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(SHARE_TEXT, safe='')}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Поделиться", url=share_url, icon_custom_emoji_id=EMOJI_PARTNERS)],
            [
                InlineKeyboardButton(
                    text="Скопировать ссылку", copy_text=CopyTextButton(text=link), icon_custom_emoji_id=EMOJI_DONE
                ),
            ],
            [InlineKeyboardButton(text="Мои рефералы", callback_data="ref:list", icon_custom_emoji_id=EMOJI_PROFILE)],
        ]
    )


async def _home_view(bot: Bot, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    link = await _ref_link(bot, user_id)
    stats = await _run(_db_stats, user_id)
    return _home_text(stats, link), _home_keyboard(link)


async def show_partners(message: Message) -> None:
    """Раздел «Партнеры» (вызывается из main.py по кнопке нижней клавиатуры)."""
    user_id = message.from_user.id
    await _run(_db_register_partner, user_id)
    text, kb = await _home_view(message.bot, user_id)
    await message.answer(text, reply_markup=kb)


async def _safe_edit(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass  # «message is not modified» / сообщение удалено — не критично


@router.callback_query(F.data == "ref:home")
async def ref_home(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    await _run(_db_register_partner, user_id)
    text, kb = await _home_view(callback.bot, user_id)
    await _safe_edit(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "ref:list")
async def ref_list(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    stats = await _run(_db_stats, user_id)
    rows = await _run(_db_referrals, user_id, REF_LIST_LIMIT)

    if not rows:
        body = "<i>Пока никого нет. Отправьте свою ссылку друзьям — и здесь появятся ваши рефералы.</i>"
    else:
        lines = []
        for i, row in enumerate(rows, 1):
            name = html.escape(row["name"] or f"ID {row['user_id']}")
            lines.append(
                f"<b>{i}. {name}</b>\n"
                f"├ Пополнил: <b>{_fmt_usd(row['deposits'])}</b>\n"
                f"└ Вам принёс: <b>{_fmt_usd(row['reward'])}</b>"
            )
        body = "\n\n".join(lines)
        hidden = stats["invited"] - len(rows)
        if hidden > 0:
            body += f"\n\n<i>…и ещё {hidden}</i>"

    text = (
        f"{PROFILE_ICON} <b>Мои рефералы</b>\n\n"
        f"┌ Всего приглашено: <b>{stats['invited']}</b>\n"
        f"└ Заработано: <b>{_fmt_usd(stats['earned'])}</b>\n\n"
        f"{body}"
    )
    await _safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=[_back_button("ref:home")]))
    await callback.answer()
