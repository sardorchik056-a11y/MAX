"""
payments.py — пополнение баланса через CryptoBot (Crypto Pay API) и xRocket (Pay API)
по схеме «создание счёта»:

    1. Игрок выбирает способ (CryptoBot / xRocket) и сумму в USD.
    2. Бот создаёт счёт через API провайдера (по API-токену) и присылает ссылку на оплату.
    3. Оплату ловит фоновая проверка (polling) — публичный HTTPS-сервер/вебхуки не нужны.
       Есть и кнопка «Проверить оплату» для мгновенной проверки.
    4. При статусе paid баланс пополняется ОДИН раз (защита от двойного зачисления),
       в storage.py пишется транзакция "deposit" — она автоматически попадает в
       «Всего депозитов» профиля, статистику по периодам и условия чеков.

Документация:
    CryptoBot: https://help.send.tg/en/articles/10279948-crypto-pay-api
    xRocket:   https://docs.xrocket.exchange/api/pay/pay-api-overview

Подключение — см. main.py: router, start_watchers(bot), stop_watchers().
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from storage import adjust_balance, get_profile_stats

# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------

# Токены платёжек — вписываются прямо сюда, между кавычками (как BOT_TOKEN в main.py).
# НЕ коммитьте файл с настоящими токенами в git.
#
#   CryptoBot: @CryptoBot -> Crypto Pay -> Create App -> API Token
#              (тестовая сеть: @CryptoTestnetBot)
#   xRocket:   @xRocket -> Pay API -> Create an app -> Settings -> API Version = «Current»
#              -> «API Token» (это Bearer-токен нового Pay API; старый ключ
#              Rocket-Pay-Key от Legacy API сюда НЕ подходит).
#              (тестовая сеть: @xrocket_testnet_bot)
#
# Пустая строка = провайдер отключён (кнопка покажет «временно недоступен»).
CRYPTOBOT_TOKEN = "582363:AALEf7JOugnrQyrkMHzH5UrO7pdOjjYnTQy"   # <- вставьте API Token из @CryptoBot
XROCKET_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhcHBJZCI6IjMwMDgzMiIsImp0aSI6ImFwcDozMDA4MzI6ZTM5MDM0ZmMtMWU2MC00MjdjLWEzNjktOWU2ZDI3YzQ3YWI0IiwiaWF0IjoxNzg5ODAxMjcwfQ.ZD9DA2KUtwes2rDwKEreoRzUuRSqw_0hB9kQWgM_7c0"     # <- вставьте API Token из @xRocket

CRYPTOBOT_TESTNET = False
XROCKET_TESTNET = False

MIN_DEPOSIT_USD = 1.0
MAX_DEPOSIT_USD = 10000.0
QUICK_AMOUNTS = (1, 5, 10, 25, 50, 100)

INVOICE_TTL_SECONDS = 30 * 60          # счёт живёт 30 минут
EXPIRE_GRACE_SECONDS = 10 * 60         # после этого без оплаты считаем счёт закрытым
MAX_PENDING_PER_USER = 3               # сколько неоплаченных счетов может быть одновременно
AMOUNT_INPUT_TTL_SECONDS = 10 * 60     # сколько бот ждёт ввод суммы

CRYPTOBOT_POLL_SECONDS = 6             # пакетная проверка всех счетов CryptoBot
XROCKET_MIN_GAP_SECONDS = 3.2          # лимит xRocket: 20 запросов/мин на эндпоинт -> держим ~18
CHECK_BUTTON_COOLDOWN_SECONDS = 4

DB_PATH = Path(__file__).with_name("deposits.db")

EMOJI_BACK = "6039539366177541657"     # тот же, что в main.py/games.py

# Кастомные эмодзи (кнопки: icon_custom_emoji_id, тексты: <tg-emoji>)
EMOJI_DEPOSIT = "5879814368572478751"      # 🏧 заголовки «Пополнение баланса»
EMOJI_CRYPTOBOT = "5798650400189980129"    # 💵 CryptoBot
EMOJI_XROCKET = "5798534328698805312"      # 🚀 xRocket
EMOJI_PAY = "5836907383292436018"          # 💎 кнопка «Оплатить»
EMOJI_CHECK = "6039859895291877126"        # 💎 кнопка «Проверить оплату»

log = logging.getLogger("payments")


# --------------------------------------------------------------------------
# Ошибки и HTTP
# --------------------------------------------------------------------------


class PaymentError(Exception):
    """Ошибка платёжного провайдера. Текст можно показывать пользователю."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _session


@dataclass
class ProviderInvoice:
    invoice_id: str
    pay_url: str


def _tge(emoji_id: str, fallback: str) -> str:
    """Кастомный эмодзи для текста сообщения (parse_mode=HTML)."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


DEPOSIT_ICON = _tge(EMOJI_DEPOSIT, "🏧")


def _normalize_status(raw: str | None) -> str:
    """Приводим статусы провайдеров к: paid / expired / pending."""
    if raw == "paid":
        return "paid"
    if raw in ("expired", "cancelled"):
        return "expired"
    return "pending"


# --------------------------------------------------------------------------
# CryptoBot — Crypto Pay API
# --------------------------------------------------------------------------


class CryptoBotClient:
    key = "cryptobot"
    title = "CryptoBot"
    emoji_id = EMOJI_CRYPTOBOT
    emoji_char = "💵"

    def __init__(self, token: str, testnet: bool = False, base_url: str | None = None):
        self.token = token
        host = "testnet-pay.crypt.bot" if testnet else "pay.crypt.bot"
        self.base_url = (base_url or f"https://{host}/api").rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        session = await _get_session()
        try:
            async with session.post(
                f"{self.base_url}/{method}",
                json=params or {},
                headers={"Crypto-Pay-API-Token": self.token},
            ) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as ex:
            raise PaymentError(f"CryptoBot недоступен: {ex}") from ex

        if not isinstance(data, dict) or not data.get("ok"):
            error = data.get("error") if isinstance(data, dict) else None
            name = error.get("name") if isinstance(error, dict) else error
            raise PaymentError(f"CryptoBot: {name or 'неизвестная ошибка'}")
        return data["result"]

    async def create_invoice(self, amount_usd: float, client_id: str, description: str) -> ProviderInvoice:
        # Счёт в фиате (USD): игрок сам выбирает, чем платить (USDT, TON, BTC, ...),
        # а мы зачисляем ровно указанную сумму в долларах.
        result = await self._call(
            "createInvoice",
            {
                "currency_type": "fiat",
                "fiat": "USD",
                "amount": f"{amount_usd:.2f}",
                "description": description,
                "payload": client_id,
                "expires_in": INVOICE_TTL_SECONDS,
            },
        )
        pay_url = result.get("bot_invoice_url") or result.get("pay_url")
        if not pay_url or "invoice_id" not in result:
            raise PaymentError("CryptoBot вернул счёт без ссылки на оплату")
        return ProviderInvoice(str(result["invoice_id"]), pay_url)

    async def get_statuses(self, invoice_ids: list[str]) -> dict[str, str]:
        """{invoice_id: paid|expired|pending} для переданных счетов (одним запросом)."""
        if not invoice_ids:
            return {}
        result = await self._call(
            "getInvoices", {"invoice_ids": ",".join(invoice_ids), "count": min(len(invoice_ids), 1000)}
        )
        items = result.get("items", []) if isinstance(result, dict) else result
        return {str(item["invoice_id"]): _normalize_status(item.get("status")) for item in items}

    async def get_status(self, invoice_id: str) -> str | None:
        return (await self.get_statuses([invoice_id])).get(invoice_id)


# --------------------------------------------------------------------------
# xRocket — Pay API (Bearer-токен)
# --------------------------------------------------------------------------


class XRocketClient:
    key = "xrocket"
    title = "xRocket"
    emoji_id = EMOJI_XROCKET
    emoji_char = "🚀"

    def __init__(self, token: str, testnet: bool = False, base_url: str | None = None):
        self.token = token
        default = "https://pay.api.testnet.xrocket.exchange" if testnet else "https://pay.api.xrocket.exchange"
        self.base_url = (base_url or default).rstrip("/")
        # лимит xRocket — 20 запросов/мин на эндпоинт: все GET /invoice идут через этот «пропуск»
        self._gap_lock = asyncio.Lock()
        self._last_get = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def _call(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> Any:
        session = await _get_session()
        try:
            async with session.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            ) as resp:
                text = await resp.text()
                status = resp.status
                retry_after = resp.headers.get("Retry-After")
        except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
            raise PaymentError(f"xRocket недоступен: {ex}") from ex

        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None

        if status >= 400:
            # ошибки Pay API — RFC 9457: ветвимся по `type`, а не по `detail`
            problem = data.get("type", "") if isinstance(data, dict) else ""
            detail = data.get("detail", "") if isinstance(data, dict) else ""
            if status == 401:
                raise PaymentError("xRocket: токен недействителен (нужен Bearer-токен Pay API)")
            if status == 429:
                raise PaymentError("xRocket: слишком много запросов", retry_after=float(retry_after or 10))
            raise PaymentError(f"xRocket: {problem or status} {detail}".strip())
        return data

    async def create_invoice(self, amount_usd: float, client_id: str, description: str) -> ProviderInvoice:
        # Цена в USDT (1 USDT ≈ 1 USD). clientInvoiceId — наш id, защищает от дублей при ретрае.
        data = await self._call(
            "POST",
            "/api/v1/invoices",
            body={
                "priceCurrency": "USDT",
                "priceAmount": f"{amount_usd:.2f}",
                "clientInvoiceId": client_id,
                "description": description,
                "expiresIn": INVOICE_TTL_SECONDS * 1000,  # миллисекунды
            },
        )
        if not isinstance(data, dict) or not data.get("id"):
            raise PaymentError("xRocket вернул счёт без id")
        invoice_id = str(data["id"])
        pay_url = (data.get("links") or {}).get("telegramBotLink")
        if not pay_url:
            log.warning("xRocket не вернул telegramBotLink для %s — использую запасную ссылку", invoice_id)
            pay_url = f"https://t.me/xRocket?start={invoice_id}"
        return ProviderInvoice(invoice_id, pay_url)

    async def get_status(self, invoice_id: str) -> str | None:
        async with self._gap_lock:
            wait = XROCKET_MIN_GAP_SECONDS - (time.monotonic() - self._last_get)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_get = time.monotonic()
        data = await self._call("GET", "/api/v1/invoice", params={"invoiceId": invoice_id})
        return _normalize_status(data.get("status")) if isinstance(data, dict) else None


cryptobot = CryptoBotClient(CRYPTOBOT_TOKEN, CRYPTOBOT_TESTNET)
xrocket = XRocketClient(XROCKET_TOKEN, XROCKET_TESTNET)
PROVIDERS: dict[str, CryptoBotClient | XRocketClient] = {cryptobot.key: cryptobot, xrocket.key: xrocket}


def _provider_label(provider: CryptoBotClient | XRocketClient) -> str:
    """«💵 CryptoBot» для текста сообщения (с кастомным эмодзи)."""
    return f"{_tge(provider.emoji_id, provider.emoji_char)} {provider.title}"


# --------------------------------------------------------------------------
# База счетов (SQLite). Хранит счета, чтобы неоплаченные переживали перезапуск бота
# и один счёт не мог быть зачислен дважды.
# --------------------------------------------------------------------------


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _db_init() -> None:
    with closing(_conn()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deposits (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                provider    TEXT    NOT NULL,
                invoice_id  TEXT    NOT NULL,
                client_id   TEXT    NOT NULL,
                user_id     INTEGER NOT NULL,
                amount      REAL    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending',
                chat_id     INTEGER,
                message_id  INTEGER,
                created_at  REAL    NOT NULL,
                paid_at     REAL,
                last_check  REAL    NOT NULL DEFAULT 0,
                UNIQUE (provider, invoice_id)
            )
            """
        )


def _db_insert(provider: str, invoice_id: str, client_id: str, user_id: int, amount: float) -> int:
    with closing(_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO deposits (provider, invoice_id, client_id, user_id, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (provider, invoice_id, client_id, user_id, amount, time.time()),
        )
        return int(cur.lastrowid)


def _db_get(dep_id: int) -> sqlite3.Row | None:
    with closing(_conn()) as conn:
        return conn.execute("SELECT * FROM deposits WHERE id = ?", (dep_id,)).fetchone()


def _db_pending(provider: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM deposits WHERE status = 'pending'"
    args: tuple = ()
    if provider:
        query += " AND provider = ?"
        args = (provider,)
    with closing(_conn()) as conn:
        return conn.execute(query + " ORDER BY last_check ASC, id ASC", args).fetchall()


def _db_count_pending(user_id: int) -> int:
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM deposits WHERE user_id = ? AND status = 'pending' AND created_at > ?",
            (user_id, time.time() - INVOICE_TTL_SECONDS - EXPIRE_GRACE_SECONDS),
        ).fetchone()
        return int(row[0])


def _db_set_message(dep_id: int, chat_id: int, message_id: int) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("UPDATE deposits SET chat_id = ?, message_id = ? WHERE id = ?", (chat_id, message_id, dep_id))


def _db_touch(dep_id: int) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("UPDATE deposits SET last_check = ? WHERE id = ?", (time.time(), dep_id))


def _db_claim_paid(dep_id: int) -> bool:
    """Атомарно переводит pending -> paid. True получит только ОДИН вызывающий —
    так кнопка «Проверить» и фоновая проверка не смогут зачислить счёт дважды."""
    with closing(_conn()) as conn, conn:
        cur = conn.execute(
            "UPDATE deposits SET status = 'paid', paid_at = ? WHERE id = ? AND status = 'pending'",
            (time.time(), dep_id),
        )
        return cur.rowcount == 1


def _db_mark_expired(dep_id: int) -> bool:
    with closing(_conn()) as conn, conn:
        cur = conn.execute("UPDATE deposits SET status = 'expired' WHERE id = ? AND status = 'pending'", (dep_id,))
        return cur.rowcount == 1


async def _run(fn, *args):
    """SQLite — синхронный, поэтому выносим в поток, чтобы не блокировать event loop."""
    return await asyncio.to_thread(fn, *args)


# --------------------------------------------------------------------------
# Зачисление
# --------------------------------------------------------------------------


def _fmt_usd(value: float) -> str:
    return f"${value:,.2f}"


async def _edit_invoice_message(bot: Bot, dep: sqlite3.Row, text: str) -> None:
    if not dep["chat_id"] or not dep["message_id"]:
        return
    try:
        await bot.edit_message_text(
            text,
            chat_id=dep["chat_id"],
            message_id=dep["message_id"],
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu:profile", icon_custom_emoji_id=EMOJI_BACK)]]
            ),
        )
    except Exception:
        pass  # сообщение могли удалить / оно не изменилось — не критично


async def _settle_paid(bot: Bot, dep: sqlite3.Row) -> bool:
    """Зачисляет оплаченный счёт. Возвращает True, если зачислили сейчас (а не раньше)."""
    if not await _run(_db_claim_paid, dep["id"]):
        return False

    # Единый способ пополнения баланса + запись транзакции "deposit" (storage.py):
    # именно она попадает в «Всего депозитов», статистику и условия чеков.
    new_balance = adjust_balance(dep["user_id"], dep["amount"], "deposit")
    log.info(
        "[deposit] user=%s provider=%s invoice=%s +%.2f USD -> balance %.2f",
        dep["user_id"], dep["provider"], dep["invoice_id"], dep["amount"], new_balance,
    )

    text = (
        f"{DEPOSIT_ICON} <b>Пополнение зачислено</b>\n\n"
        f"┌ Способ: <b>{_provider_label(PROVIDERS[dep['provider']])}</b>\n"
        f"├ Сумма: <b>{_fmt_usd(dep['amount'])}</b>\n"
        f"└ Баланс: <b>{_fmt_usd(get_profile_stats(dep['user_id'])['balance'])}</b>"
    )
    await _edit_invoice_message(bot, dep, text)
    try:
        await bot.send_message(dep["user_id"], text)
    except Exception as ex:
        log.warning("[deposit] не удалось уведомить user=%s: %s", dep["user_id"], ex)
    return True


async def _apply_status(bot: Bot, dep: sqlite3.Row, status: str | None) -> str:
    """Применяет статус от провайдера к счёту. Возвращает итоговый: paid/expired/pending."""
    if status == "paid":
        await _settle_paid(bot, dep)
        return "paid"

    too_old = time.time() - dep["created_at"] > INVOICE_TTL_SECONDS + EXPIRE_GRACE_SECONDS
    if status == "expired" or too_old:
        if await _run(_db_mark_expired, dep["id"]):
            await _edit_invoice_message(bot, dep, f"{DEPOSIT_ICON} <b>Счёт истёк</b>\n\nСоздайте новый счёт для пополнения.")
        return "expired"
    return "pending"


async def check_deposit(bot: Bot, dep_id: int) -> str:
    """Разовая проверка одного счёта у провайдера (кнопка «Проверить оплату»)."""
    dep = await _run(_db_get, dep_id)
    if dep is None:
        return "missing"
    if dep["status"] != "pending":
        return dep["status"]
    provider = PROVIDERS[dep["provider"]]
    status = await provider.get_status(dep["invoice_id"])
    await _run(_db_touch, dep_id)
    return await _apply_status(bot, dep, status)


# --------------------------------------------------------------------------
# Фоновые проверки
# --------------------------------------------------------------------------

_tasks: list[asyncio.Task] = []
_xr_wake = asyncio.Event()  # будит проверку xRocket, когда создан новый счёт


async def _watch_cryptobot(bot: Bot) -> None:
    while True:
        try:
            rows = await _run(_db_pending, "cryptobot")
            if rows and cryptobot.configured:
                statuses = await cryptobot.get_statuses([r["invoice_id"] for r in rows])
                for row in rows:
                    await _apply_status(bot, row, statuses.get(row["invoice_id"]))
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.warning("[watch cryptobot] %s", ex)
        await asyncio.sleep(CRYPTOBOT_POLL_SECONDS)


async def _watch_xrocket(bot: Bot) -> None:
    while True:
        try:
            rows = await _run(_db_pending, "xrocket")
            if not rows or not xrocket.configured:
                try:
                    await asyncio.wait_for(_xr_wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                _xr_wake.clear()
                continue
            row = rows[0]  # самый давно проверявшийся; темп задаёт XROCKET_MIN_GAP_SECONDS
            status = await xrocket.get_status(row["invoice_id"])
            await _run(_db_touch, row["id"])
            await _apply_status(bot, row, status)
        except asyncio.CancelledError:
            raise
        except PaymentError as ex:
            log.warning("[watch xrocket] %s", ex)
            await asyncio.sleep(ex.retry_after if ex.retry_after is not None else 5)
        except Exception as ex:
            log.warning("[watch xrocket] %s", ex)
            await asyncio.sleep(5)


def start_watchers(bot: Bot) -> None:
    """Создаёт БД и запускает фоновые проверки оплаты. Вызывать из main() один раз."""
    _db_init()
    if not (cryptobot.configured or xrocket.configured):
        log.warning("[payments] ни CRYPTOBOT_TOKEN, ни XROCKET_TOKEN не заданы — пополнение недоступно")
    _tasks.append(asyncio.create_task(_watch_cryptobot(bot), name="watch-cryptobot"))
    _tasks.append(asyncio.create_task(_watch_xrocket(bot), name="watch-xrocket"))


async def stop_watchers() -> None:
    for task in _tasks:
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    if _session is not None and not _session.closed:
        await _session.close()


# --------------------------------------------------------------------------
# Хендлеры (aiogram)
# --------------------------------------------------------------------------

router = Router()


class DepositStates(StatesGroup):
    waiting_amount = State()


_last_check_press: dict[int, float] = {}


def _back_button(callback_data: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="Назад", callback_data=callback_data, icon_custom_emoji_id=EMOJI_BACK)]


def _methods_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=cryptobot.title,
                    callback_data=f"dep:m:{cryptobot.key}",
                    icon_custom_emoji_id=cryptobot.emoji_id,
                )
            ],
            [
                InlineKeyboardButton(
                    text=xrocket.title,
                    callback_data=f"dep:m:{xrocket.key}",
                    icon_custom_emoji_id=xrocket.emoji_id,
                )
            ],
            _back_button("menu:profile"),
        ]
    )


def _amount_keyboard(provider_key: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=f"{a}$", callback_data=f"dep:a:{provider_key}:{a}") for a in QUICK_AMOUNTS
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append(_back_button("profile:deposit"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_deposit_methods(callback: CallbackQuery, state: FSMContext) -> None:
    """Экран выбора способа пополнения (вызывается из main.py по кнопке «Пополнить»)."""
    await state.clear()
    await callback.message.edit_text(
        f"{DEPOSIT_ICON} <b>Пополнение баланса</b>\n\n"
        "<i>Выберите способ оплаты. Счёт создаётся автоматически, "
        "баланс пополнится сразу после оплаты.</i>",
        reply_markup=_methods_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dep:m:"))
async def deposit_method_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    provider_key = callback.data.split(":")[2]
    provider = PROVIDERS.get(provider_key)
    if provider is None:
        await callback.answer("Неизвестный способ", show_alert=True)
        return
    if not provider.configured:
        await callback.answer(f"{provider.title} временно недоступен", show_alert=True)
        return

    await state.set_state(DepositStates.waiting_amount)
    await state.update_data(dep_provider=provider_key, dep_ts=time.time())
    await callback.message.edit_text(
        f"{DEPOSIT_ICON} <b>Пополнение — {_provider_label(provider)}</b>\n\n"
        f"<i>Выберите сумму или отправьте её в чат числом (от {MIN_DEPOSIT_USD:g}$ до {MAX_DEPOSIT_USD:g}$).</i>",
        reply_markup=_amount_keyboard(provider_key),
    )
    await callback.answer()


async def _create_deposit(bot: Bot, user_id: int, provider_key: str, amount: float) -> tuple[int, str, str]:
    """Создаёт счёт у провайдера и запись в БД. Возвращает (deposit_id, pay_url, provider_title)."""
    provider = PROVIDERS[provider_key]
    client_id = f"dep_{uuid.uuid4().hex}"
    invoice = await provider.create_invoice(amount, client_id, "Пополнение баланса")
    try:
        dep_id = await _run(_db_insert, provider_key, invoice.invoice_id, client_id, user_id, amount)
    except Exception:
        # счёт уже создан у провайдера, а записать не вышло — оставляем след для ручной сверки
        log.error("[deposit] НЕ ЗАПИСАН счёт: provider=%s invoice=%s user=%s amount=%s",
                  provider_key, invoice.invoice_id, user_id, amount)
        raise
    return dep_id, invoice.pay_url, provider.title


def _invoice_text(provider_key: str, amount: float) -> str:
    minutes = INVOICE_TTL_SECONDS // 60
    return (
        f"{DEPOSIT_ICON} <b>Счёт на пополнение создан</b>\n\n"
        f"┌ Способ: <b>{_provider_label(PROVIDERS[provider_key])}</b>\n"
        f"├ Сумма: <b>{_fmt_usd(amount)}</b>\n"
        f"└ Действует: <b>{minutes} мин</b>\n\n"
        "<i>Оплатите счёт по кнопке ниже — баланс пополнится автоматически. "
        "Если этого не произошло, нажмите «Проверить оплату».</i>"
    )


def _invoice_keyboard(dep_id: int, pay_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", url=pay_url, icon_custom_emoji_id=EMOJI_PAY)],
            [
                InlineKeyboardButton(
                    text="Проверить оплату",
                    callback_data=f"dep:c:{dep_id}",
                    icon_custom_emoji_id=EMOJI_CHECK,
                )
            ],
            _back_button("menu:profile"),
        ]
    )


async def _start_deposit(
    bot: Bot, user_id: int, provider_key: str, amount: float, *, panel: Message | None, chat_id: int
) -> str | None:
    """Общая часть для кнопок суммы и ручного ввода. Возвращает текст ошибки или None."""
    provider = PROVIDERS[provider_key]
    if not provider.configured:
        return f"{provider.title} временно недоступен"
    if await _run(_db_count_pending, user_id) >= MAX_PENDING_PER_USER:
        return "У вас уже есть неоплаченные счета. Оплатите их или дождитесь истечения."
    try:
        dep_id, pay_url, _title = await _create_deposit(bot, user_id, provider_key, amount)
    except PaymentError as ex:
        log.warning("[deposit] не удалось создать счёт: %s", ex)
        return f"Не удалось создать счёт: {ex}"
    except Exception:
        log.exception("[deposit] ошибка при создании счёта")
        return "Не удалось создать счёт, попробуйте позже."

    if provider_key == xrocket.key:
        _xr_wake.set()
    text, kb = _invoice_text(provider_key, amount), _invoice_keyboard(dep_id, pay_url)
    if panel is not None:
        try:
            await panel.edit_text(text, reply_markup=kb)
            await _run(_db_set_message, dep_id, panel.chat.id, panel.message_id)
            return None
        except Exception:
            pass
    sent = await bot.send_message(chat_id, text, reply_markup=kb)
    await _run(_db_set_message, dep_id, sent.chat.id, sent.message_id)
    return None


@router.callback_query(F.data.startswith("dep:a:"))
async def deposit_quick_amount(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, provider_key, amount_str = callback.data.split(":")
    if provider_key not in PROVIDERS:
        await callback.answer("Неизвестный способ", show_alert=True)
        return
    amount = float(amount_str)
    if not MIN_DEPOSIT_USD <= amount <= MAX_DEPOSIT_USD:
        await callback.answer("Недопустимая сумма", show_alert=True)
        return

    await callback.answer("Создаю счёт…")
    error = await _start_deposit(
        callback.bot, callback.from_user.id, provider_key, amount,
        panel=callback.message, chat_id=callback.message.chat.id,
    )
    if error:
        await callback.message.answer(error)
    else:
        await state.clear()


@router.message(DepositStates.waiting_amount, F.text)
async def deposit_amount_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    provider_key = data.get("dep_provider")
    text = (message.text or "").strip().replace(",", ".")

    # Не число (например «0.1$» или «куб чет 1») или ввод устарел — игрок ушёл из пополнения:
    # выходим из режима и отдаём сообщение следующим обработчикам (играм и т.д.).
    stale = time.time() - data.get("dep_ts", 0) > AMOUNT_INPUT_TTL_SECONDS
    if provider_key not in PROVIDERS or stale or not re.fullmatch(r"\d+(\.\d+)?", text):
        await state.clear()
        raise SkipHandler

    amount = round(float(text), 2)
    if not MIN_DEPOSIT_USD <= amount <= MAX_DEPOSIT_USD:
        await message.answer(f"Сумма должна быть от {MIN_DEPOSIT_USD:g}$ до {MAX_DEPOSIT_USD:g}$.")
        return

    error = await _start_deposit(
        message.bot, message.from_user.id, provider_key, amount, panel=None, chat_id=message.chat.id
    )
    if error:
        await message.answer(error)
    else:
        await state.clear()


@router.callback_query(F.data.startswith("dep:c:"))
async def deposit_check_button(callback: CallbackQuery) -> None:
    dep_id = int(callback.data.split(":")[2])
    dep = await _run(_db_get, dep_id)
    if dep is None or dep["user_id"] != callback.from_user.id:
        await callback.answer("Счёт не найден", show_alert=True)
        return
    if dep["status"] == "paid":
        await callback.answer("Этот счёт уже оплачен и зачислен", show_alert=True)
        return
    if dep["status"] == "expired":
        await callback.answer("Счёт истёк, создайте новый", show_alert=True)
        return

    now = time.monotonic()
    if now - _last_check_press.get(callback.from_user.id, 0) < CHECK_BUTTON_COOLDOWN_SECONDS:
        await callback.answer("Подождите пару секунд…")
        return
    _last_check_press[callback.from_user.id] = now

    try:
        status = await check_deposit(callback.bot, dep_id)
    except PaymentError as ex:
        await callback.answer(f"Не удалось проверить: {ex}", show_alert=True)
        return

    if status == "paid":
        await callback.answer("Оплата получена, баланс пополнен")
    elif status == "expired":
        await callback.answer("Счёт истёк, создайте новый", show_alert=True)
    else:
        await callback.answer("Оплата пока не найдена. Оплатите счёт и нажмите снова.", show_alert=True)
