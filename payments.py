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

Вывод средствв (кнопка «Вывести» в профиле):

    1. Игрок выбирает способ (CryptoBot / xRocket) и сумму, подтверждает вывод.
    2. Баланс списывается (транзакция "withdraw"), затем бот отправляет USDT на
       Telegram-аккаунт игрока через API провайдера (CryptoBot: transfer, xRocket: payouts).
    3. Любая ошибка -> игроку пишем «Обратитесь в поддержку» (+ кнопка «Поддержка»);
       если провайдер ТОЧНО отказал — деньги возвращаются на баланс, если результат
       неизвестен (таймаут/5xx) — НЕ возвращаются, заявка уходит на ручную проверку
       (status = 'review'), админам приходит уведомление.

Документация:
    CryptoBot: https://help.send.tg/en/articles/10279948-crypto-pay-api
    xRocket:   https://docs.xrocket.exchange/api/pay/pay-api-overview

Подключение — см. main.py: router, start_watchers(bot), stop_watchers().
"""

from __future__ import annotations

import asyncio
import html
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

import refs
from ui import edit_any, edit_by_id
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

MIN_DEPOSIT_USD = 0.1
MAX_DEPOSIT_USD = 10000.0
QUICK_AMOUNTS = (2, 5, 10, 25, 50, 100)

# --- Вывод средств ---
MIN_WITHDRAW_USD = 0.1
MAX_WITHDRAW_USD = 10000.0
WITHDRAW_QUICK_AMOUNTS = (5, 10, 25, 50, 100)
WITHDRAW_ASSET = "USDT"                # в чём отправляем (1 USDT ≈ 1 USD)
SUPPORT_HANDLE = "@luckydicesupport"   # тот же, что в SUPPORT_TEXT в main.py
WITHDRAW_POLL_SECONDS = 8              # как часто проверять «висящие» выводы
PENDING_WITHDRAW_ALERT_SECONDS = 60 * 60   # xRocket держит выплату в pending дольше часа -> на ручную проверку
STALE_PROCESSING_SECONDS = 5 * 60      # заявка застряла в processing (например, бот упал) -> на ручную проверку

# Типы транзакций в storage.adjust_balance(user_id, amount, kind):
#   списание при выводе — "withdraw" (её суммирует «Всего выводов» в main.py);
#   возврат при неудачном выводе — "admin_grant" (заведомо начисляет и не попадает в депозиты).
WITHDRAW_DEBIT_KIND = "withdraw"
WITHDRAW_REFUND_KIND = "admin_grant"

# Кому слать уведомления о сбоях выводов. Заполняется из main.py (ADMIN_IDS).
ALERT_ADMIN_IDS: set[int] = set()

INVOICE_TTL_SECONDS = 30 * 60          # счёт живёт 30 минут
EXPIRE_GRACE_SECONDS = 10 * 60         # после этого без оплаты считаем счёт закрытым
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
EMOJI_WITHDRAW = "5890848474563352982"     # 🪙 как «Вывести» в профиле
EMOJI_TREASURY = "5197288647275071607"     # 🛡 заголовок «Баланс казны»
EMOJI_WAIT = "5386367538735104399"         # ⌛ «Запрашиваю баланс казны…»
EMOJI_SUPPORT = "5812150667812280629"      # 🛠 как «Поддержка» в main.py

log = logging.getLogger("payments")


# --------------------------------------------------------------------------
# Ошибки и HTTP
# --------------------------------------------------------------------------


class PaymentError(Exception):
    """Ошибка платёжного провайдера. Текст можно показывать пользователю."""

    def __init__(self, message: str, retry_after: float | None = None, ambiguous: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        # ambiguous=True — неизвестно, выполнил ли провайдер операцию (таймаут, обрыв связи, 5xx).
        # При выводе такую ошибку нельзя «лечить» возвратом денег: перевод мог уйти.
        self.ambiguous = ambiguous


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


@dataclass
class ProviderPayout:
    ref: str
    status: str  # paid | pending | failed


def _tge(emoji_id: str, fallback: str) -> str:
    """Кастомный эмодзи для текста сообщения (parse_mode=HTML)."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


DEPOSIT_ICON = _tge(EMOJI_DEPOSIT, "🏧")
WITHDRAW_ICON = _tge(EMOJI_WITHDRAW, "🪙")


def _normalize_status(raw: str | None) -> str:
    """Приводим статусы провайдеров к: paid / expired / pending."""
    if raw == "paid":
        return "paid"
    if raw in ("expired", "cancelled"):
        return "expired"
    return "pending"


def _normalize_payout_status(raw: str | None) -> str:
    """Статусы выплат xRocket -> paid / failed / pending."""
    if raw == "finished":
        return "paid"
    if raw == "failed":
        return "failed"
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
                http_status = resp.status
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as ex:
            raise PaymentError(f"CryptoBot недоступен: {ex}", ambiguous=True) from ex

        if not isinstance(data, dict) or not data.get("ok"):
            error = data.get("error") if isinstance(data, dict) else None
            name = error.get("name") if isinstance(error, dict) else error
            # 5xx или мусор вместо JSON — неизвестно, дошёл ли запрос до исполнения
            ambiguous = http_status >= 500 or not isinstance(data, dict)
            raise PaymentError(f"CryptoBot: {name or 'неизвестная ошибка'}", ambiguous=ambiguous)
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

    async def transfer(self, tg_user_id: int, amount_usd: float, spend_id: str) -> ProviderPayout:
        """Отправка USDT на аккаунт игрока в CryptoBot (метод transfer).

        Нужно один раз включить в @CryptoBot -> Crypto Pay -> My Apps -> ваше приложение ->
        Security -> Transfers -> Enable. Игрок должен хотя бы раз запускать @CryptoBot.
        spend_id — идемпотентность (один spend_id принимается только один раз)."""
        result = await self._call(
            "transfer",
            {
                "user_id": tg_user_id,
                "asset": WITHDRAW_ASSET,
                "amount": f"{amount_usd:.2f}",
                "spend_id": spend_id,
                "comment": "Вывод средств",
            },
        )
        if not isinstance(result, dict) or result.get("status") not in (None, "completed"):
            raise PaymentError("CryptoBot: неожиданный ответ на перевод", ambiguous=True)
        return ProviderPayout(str(result.get("transfer_id") or spend_id), "paid")


    async def get_balances(self) -> list[dict]:
        """Сырые остатки приложения: [{currency_code, available, onhold}, ...]."""
        result = await self._call("getBalance")
        return result if isinstance(result, list) else []

    async def get_exchange_rates_usd(self) -> dict[str, float]:
        """{currency_code: курс_в_USD} — только валюты с target == 'USD'."""
        result = await self._call("getExchangeRates")
        rates: dict[str, float] = {}
        for item in result if isinstance(result, list) else []:
            if not isinstance(item, dict) or item.get("target") != "USD":
                continue
            try:
                rates[str(item["source"])] = float(item["rate"])
            except (TypeError, ValueError, KeyError):
                continue
        return rates


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
        self._pgap_lock = asyncio.Lock()   # то же для GET /payout
        self._last_pget = 0.0

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
            raise PaymentError(f"xRocket недоступен: {ex}", ambiguous=True) from ex

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
            # 5xx — неизвестно, выполнилась ли операция; 4xx — запрос точно отклонён
            raise PaymentError(f"xRocket: {problem or status} {detail}".strip(), ambiguous=status >= 500)
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

    async def transfer(self, tg_user_id: int, amount_usd: float, client_payout_id: str) -> ProviderPayout:
        """Выплата USDT на Telegram-аккаунт игрока (Pay API: POST /api/v1/payouts).
        clientPayoutId — наш id, по нему выплату можно сверить при таймауте.
        Игрок должен хотя бы раз запускать @xRocket."""
        data = await self._call(
            "POST",
            "/api/v1/payouts",
            body={
                "clientPayoutId": client_payout_id,
                "target": str(tg_user_id),
                "targetType": "telegram_user_id",
                "asset": WITHDRAW_ASSET,
                "amount": f"{amount_usd:.2f}",
            },
        )
        if not isinstance(data, dict) or not data.get("payoutId"):
            raise PaymentError("xRocket: неожиданный ответ на выплату", ambiguous=True)
        return ProviderPayout(str(data["payoutId"]), _normalize_payout_status(data.get("status")))

    async def get_payout_status(self, payout_id: str) -> str | None:
        """paid / failed / pending для выплаты по payoutId."""
        async with self._pgap_lock:
            wait = XROCKET_MIN_GAP_SECONDS - (time.monotonic() - self._last_pget)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_pget = time.monotonic()
        data = await self._call("GET", "/api/v1/payout", params={"payoutId": payout_id})
        return _normalize_payout_status(data.get("status")) if isinstance(data, dict) else None


    async def get_balances(self) -> list[dict]:
        """Остатки приложения по валютам: GET /api/v1/balances.
        Возвращает список объектов вида {currency, balance} (или под ключом
        "balances" — обрабатываем оба варианта на случай изменений в API)."""
        data = await self._call("GET", "/api/v1/balances")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = data.get("balances") or data.get("data")
            if isinstance(inner, list):
                return inner
        return []


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS withdrawals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                provider     TEXT    NOT NULL,
                client_id    TEXT    NOT NULL UNIQUE,
                user_id      INTEGER NOT NULL,
                amount       REAL    NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'processing',
                provider_ref TEXT,
                error        TEXT,
                created_at   REAL    NOT NULL,
                updated_at   REAL    NOT NULL
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


# Статусы вывода: processing -> paid | pending -> paid | failed | review
#   processing — идёт (баланс списан, запрос провайдеру отправляется)
#   pending    — провайдер принял выплату, но ещё не завершил (xRocket)
#   paid       — деньги отправлены
#   failed     — провайдер точно отказал, деньги возвращены на баланс
#   review     — результат неизвестен (таймаут/5xx/сбой) — разбирается вручную, автоматически НЕ возвращаем


def _wd_insert(provider: str, client_id: str, user_id: int, amount: float) -> int:
    now = time.time()
    with closing(_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO withdrawals (provider, client_id, user_id, amount, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (provider, client_id, user_id, amount, now, now),
        )
        return int(cur.lastrowid)


def _wd_move(wd_id: int, from_statuses: tuple[str, ...], to_status: str,
             ref: str | None = None, error: str | None = None) -> bool:
    """Атомарный переход статуса. True получит только один вызывающий —
    так бот и фоновая проверка не смогут, например, дважды вернуть деньги."""
    marks = ",".join("?" * len(from_statuses))
    with closing(_conn()) as conn, conn:
        cur = conn.execute(
            "UPDATE withdrawals SET status = ?, provider_ref = COALESCE(?, provider_ref), "
            f"error = COALESCE(?, error), updated_at = ? WHERE id = ? AND status IN ({marks})",
            (to_status, ref, error, time.time(), wd_id, *from_statuses),
        )
        return cur.rowcount == 1


def _wd_active_count(user_id: int) -> int:
    with closing(_conn()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM withdrawals WHERE user_id = ? AND "
            "(status = 'pending' OR (status = 'processing' AND created_at > ?))",
            (user_id, time.time() - STALE_PROCESSING_SECONDS),
        ).fetchone()
        return int(row[0])


def _wd_list(status: str, provider: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM withdrawals WHERE status = ?"
    args: tuple = (status,)
    if provider:
        query += " AND provider = ?"
        args = (status, provider)
    with closing(_conn()) as conn:
        return conn.execute(query + " ORDER BY id ASC", args).fetchall()


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
        await edit_by_id(
            bot,
            dep["chat_id"],
            dep["message_id"],
            text,
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

    # Партнёрка: пригласивший получает % с этого пополнения (один счёт — одно начисление).
    # reward_referrer сам ловит любые ошибки и не может сломать зачисление депозита.
    await refs.reward_referrer(bot, dep["user_id"], dep["amount"], f"{dep['provider']}:{dep['invoice_id']}")

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
# Вывод средств
# --------------------------------------------------------------------------


@dataclass
class WithdrawResult:
    status: str          # paid | pending | failed | review | rejected
    text: str            # rejected — обычный текст для alert; остальные — HTML для сообщения
    wd_id: int | None = None


def _rejected(text: str) -> WithdrawResult:
    return WithdrawResult("rejected", text)


def _support_hint(wd_id: int | None) -> str:
    ref = f" Номер заявки: <b>#{wd_id}</b>." if wd_id else ""
    return f"Обратитесь в поддержку: <b>{SUPPORT_HANDLE}</b>.{ref}"


def _error_text(title: str, wd_id: int | None, note: str = "") -> str:
    note_part = f"{note}\n\n" if note else ""
    return f"{WITHDRAW_ICON} <b>{title}</b>\n\n{note_part}{_support_hint(wd_id)}"


def _paid_text(provider: CryptoBotClient | XRocketClient, user_id: int, amount: float) -> str:
    return (
        f"{WITHDRAW_ICON} <b>Вывод выполнен</b>\n\n"
        f"┌ Способ: <b>{_provider_label(provider)}</b>\n"
        f"├ Сумма: <b>{_fmt_usd(amount)}</b>\n"
        f"└ Баланс: <b>{_fmt_usd(get_profile_stats(user_id)['balance'])}</b>\n\n"
        f"<i>Средства отправлены на ваш аккаунт в {provider.title}.</i>"
    )


async def _alert_admins(bot: Bot, text: str) -> None:
    """Сообщаем админам о сбое вывода (текст — уже безопасный HTML)."""
    for admin_id in ALERT_ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"<b>[Вывод]</b> {text}")
        except Exception as ex:
            log.warning("[withdraw] не удалось уведомить админа %s: %s", admin_id, ex)


def _debit_balance(user_id: int, amount: float) -> bool:
    """Списывает баланс и проверяет, что он ДЕЙСТВИТЕЛЬНО уменьшился на amount.
    Защита от неверного знака/типа транзакции в storage.py: если списание не сработало,
    платёж провайдеру не отправляется."""
    before = get_profile_stats(user_id)["balance"]
    after = adjust_balance(user_id, amount, WITHDRAW_DEBIT_KIND)
    if abs(after - (before - amount)) <= 0.005:
        return True
    log.critical(
        "[withdraw] списание не сработало: user=%s было=%.2f стало=%.2f сумма=%.2f kind=%s",
        user_id, before, after, amount, WITHDRAW_DEBIT_KIND,
    )
    if after > before + 0.005:  # storage приплюсовал вместо списания — откатываем
        try:
            adjust_balance(user_id, amount, "admin_deduct")
        except Exception:
            log.exception("[withdraw] не удалось откатить ошибочное начисление user=%s", user_id)
    return False


def _refund_balance(user_id: int, amount: float) -> bool:
    try:
        adjust_balance(user_id, amount, WITHDRAW_REFUND_KIND)
        return True
    except Exception:
        log.critical("[withdraw] НЕ УДАЛОСЬ вернуть %.2f USD пользователю %s", amount, user_id, exc_info=True)
        return False


async def _fail_and_refund(bot: Bot, wd_id: int, user_id: int, amount: float, reason: str) -> WithdrawResult:
    """Провайдер ТОЧНО отказал: переводим заявку в failed и возвращаем деньги (один раз)."""
    moved = await _run(_wd_move, wd_id, ("processing", "pending"), "failed", None, reason)
    refunded = True
    if moved:
        refunded = _refund_balance(user_id, amount)
        if not refunded:
            await _run(_wd_move, wd_id, ("failed",), "review")
    log.error("[withdraw] #%s user=%s %.2f USD не выполнен: %s (возврат: %s)", wd_id, user_id, amount, reason, refunded)
    await _alert_admins(
        bot,
        f"#{wd_id} user=<code>{user_id}</code> ${amount:.2f} — ошибка: <code>{html.escape(reason)}</code>. "
        + ("Деньги возвращены на баланс." if refunded else "<b>Деньги НЕ возвращены — нужен ручной разбор!</b>"),
    )
    if refunded:
        return WithdrawResult(
            "failed", _error_text("Не удалось выполнить вывод", wd_id, "Средства возвращены на баланс."), wd_id
        )
    return WithdrawResult(
        "review",
        _error_text("Не удалось выполнить вывод", wd_id, "Заявка передана на проверку — мы разберёмся вручную."),
        wd_id,
    )


_wd_locks: dict[int, asyncio.Lock] = {}


async def _execute_withdrawal(bot: Bot, user_id: int, provider_key: str, amount: float) -> WithdrawResult:
    """Весь вывод целиком. Порядок важен: заявка в БД -> списание баланса -> запрос провайдеру.
    Любое исключение здесь превращается в результат с текстом «обратитесь в поддержку»."""
    provider = PROVIDERS.get(provider_key)
    if provider is None:
        return _rejected("Неизвестный способ вывода")
    if not provider.configured:
        return _rejected(f"{provider.title} временно недоступен")
    if not MIN_WITHDRAW_USD <= amount <= MAX_WITHDRAW_USD:
        return _rejected(f"Сумма вывода должна быть от {MIN_WITHDRAW_USD:g}$ до {MAX_WITHDRAW_USD:g}$")

    lock = _wd_locks.setdefault(user_id, asyncio.Lock())
    if lock.locked():
        return _rejected("Предыдущий вывод ещё обрабатывается")

    async with lock:
        wd_id: int | None = None
        debited = False
        try:
            if await _run(_wd_active_count, user_id):
                return _rejected("У вас уже есть вывод в обработке. Дождитесь его завершения.")
            balance = get_profile_stats(user_id)["balance"]
            if balance + 0.005 < amount:
                return _rejected(f"Недостаточно средств. Доступно: {_fmt_usd(balance)}")

            client_id = f"wd_{uuid.uuid4().hex}"   # он же spend_id / clientPayoutId (идемпотентность)
            wd_id = await _run(_wd_insert, provider_key, client_id, user_id, amount)

            debited = _debit_balance(user_id, amount)
            if not debited:
                await _run(_wd_move, wd_id, ("processing",), "failed", None, "debit_mismatch")
                await _alert_admins(
                    bot,
                    f"#{wd_id} user=<code>{user_id}</code> — списание баланса не сработало "
                    f"(тип «{WITHDRAW_DEBIT_KIND}» в storage.py). Платёж НЕ отправлялся.",
                )
                return WithdrawResult("failed", _error_text("Не удалось выполнить вывод", wd_id), wd_id)

            try:
                payout = await provider.transfer(user_id, amount, client_id)
            except PaymentError as ex:
                if ex.ambiguous:
                    # перевод мог уйти — деньги НЕ возвращаем, разбираем вручную
                    await _run(_wd_move, wd_id, ("processing",), "review", None, str(ex))
                    log.error("[withdraw] #%s user=%s результат неизвестен: %s", wd_id, user_id, ex)
                    await _alert_admins(
                        bot,
                        f"#{wd_id} user=<code>{user_id}</code> ${amount:.2f} через {provider.title} — "
                        f"результат неизвестен (<code>{html.escape(str(ex))}</code>). Баланс списан, "
                        f"деньги не возвращались. Сверьте выплату в {provider.title} по id "
                        f"<code>{client_id}</code>.",
                    )
                    return WithdrawResult(
                        "review",
                        _error_text(
                            "Не удалось подтвердить вывод", wd_id,
                            "Статус выплаты уточняется — проверим вручную.",
                        ),
                        wd_id,
                    )
                return await _fail_and_refund(bot, wd_id, user_id, amount, str(ex))

            if payout.status == "failed":
                return await _fail_and_refund(bot, wd_id, user_id, amount, "provider status: failed")
            if payout.status == "paid":
                await _run(_wd_move, wd_id, ("processing",), "paid", payout.ref)
                log.info("[withdraw] #%s user=%s provider=%s -%.2f USD", wd_id, user_id, provider_key, amount)
                return WithdrawResult("paid", _paid_text(provider, user_id, amount), wd_id)

            await _run(_wd_move, wd_id, ("processing",), "pending", payout.ref)
            return WithdrawResult(
                "pending",
                f"{WITHDRAW_ICON} <b>Вывод обрабатывается</b>\n\n"
                f"┌ Способ: <b>{_provider_label(provider)}</b>\n"
                f"└ Сумма: <b>{_fmt_usd(amount)}</b>\n\n"
                "<i>Как только выплата завершится, пришлём уведомление. "
                f"Если этого не произошло в течение часа — обратитесь в поддержку {SUPPORT_HANDLE}.</i>",
                wd_id,
            )
        except Exception as ex:
            log.exception("[withdraw] неожиданная ошибка user=%s provider=%s amount=%s", user_id, provider_key, amount)
            if wd_id is not None:
                try:
                    # если баланс уже списан — результат неизвестен (review), иначе деньги не тронуты
                    await _run(_wd_move, wd_id, ("processing",), "review" if debited else "failed", None, f"internal: {ex!r}")
                except Exception:
                    log.exception("[withdraw] не удалось обновить заявку #%s", wd_id)
            await _alert_admins(
                bot,
                f"#{wd_id or '—'} user=<code>{user_id}</code> ${amount:.2f} — внутренняя ошибка: "
                f"<code>{html.escape(repr(ex))}</code>. Баланс списан: {'да' if debited else 'нет'}.",
            )
            return WithdrawResult("review" if debited else "failed", _error_text("Не удалось выполнить вывод", wd_id), wd_id)


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


async def _watch_withdrawals(bot: Bot) -> None:
    """Доводит до конца выводы, которые не завершились сразу:
    xRocket pending -> paid/failed; застрявшие processing/долгие pending -> review + уведомление админам."""
    while True:
        try:
            now = time.time()
            for row in await _run(_wd_list, "processing"):
                if now - row["created_at"] > STALE_PROCESSING_SECONDS:
                    if await _run(_wd_move, row["id"], ("processing",), "review", None, "stale processing"):
                        log.error("[withdraw] #%s застряла в processing", row["id"])
                        await _alert_admins(
                            bot,
                            f"#{row['id']} user=<code>{row['user_id']}</code> ${row['amount']:.2f} — заявка "
                            f"застряла (бот перезапускался?). Баланс мог быть списан, платёж мог не уйти — "
                            f"проверьте вручную (id <code>{row['client_id']}</code>).",
                        )

            if xrocket.configured:
                for row in await _run(_wd_list, "pending", "xrocket"):
                    status = await xrocket.get_payout_status(row["provider_ref"])
                    if status == "paid":
                        if await _run(_wd_move, row["id"], ("pending",), "paid"):
                            await _notify_user(bot, row["user_id"], _paid_text(xrocket, row["user_id"], row["amount"]), "paid")
                    elif status == "failed":
                        result = await _fail_and_refund(bot, row["id"], row["user_id"], row["amount"], "provider status: failed")
                        await _notify_user(bot, row["user_id"], result.text, result.status)
                    elif now - row["created_at"] > PENDING_WITHDRAW_ALERT_SECONDS:
                        if await _run(_wd_move, row["id"], ("pending",), "review", None, "pending too long"):
                            await _alert_admins(
                                bot,
                                f"#{row['id']} user=<code>{row['user_id']}</code> ${row['amount']:.2f} — выплата xRocket "
                                f"висит в pending больше часа (payoutId <code>{row['provider_ref']}</code>).",
                            )
        except asyncio.CancelledError:
            raise
        except PaymentError as ex:
            log.warning("[watch withdrawals] %s", ex)
            await asyncio.sleep(ex.retry_after if ex.retry_after is not None else 5)
        except Exception as ex:
            log.warning("[watch withdrawals] %s", ex)
        await asyncio.sleep(WITHDRAW_POLL_SECONDS)


def start_watchers(bot: Bot) -> None:
    """Создаёт БД и запускает фоновые проверки оплаты. Вызывать из main() один раз."""
    _db_init()
    if not (cryptobot.configured or xrocket.configured):
        log.warning("[payments] ни CRYPTOBOT_TOKEN, ни XROCKET_TOKEN не заданы — пополнение недоступно")
    _tasks.append(asyncio.create_task(_watch_cryptobot(bot), name="watch-cryptobot"))
    _tasks.append(asyncio.create_task(_watch_xrocket(bot), name="watch-xrocket"))
    _tasks.append(asyncio.create_task(_watch_withdrawals(bot), name="watch-withdrawals"))


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


# --------------------------------------------------------------------------
# Казна / резерв — команды «казна», «kazna», «резерв», «reserve» (со слешем
# и без) показывают админу ЖИВОЙ остаток на провайдерах: CryptoBot — через
# getBalance + getExchangeRates, xRocket — через GET /app/info. Курс xRocket
# оценивается по курсам CryptoBot (совпадающие коды активов: USDT, TON, TRX...),
# USDT всегда считается 1:1 к доллару.
# --------------------------------------------------------------------------

TREASURY_TRIGGERS = ("казна", "kazna", "резерв", "reserve")
_TREASURY_RE = re.compile(
    r"^/?(?:" + "|".join(re.escape(t) for t in TREASURY_TRIGGERS) + r")(?:@\w+)?\s*$",
    re.IGNORECASE,
)


def _fmt_amount(value: float) -> str:
    """«1,676.75», «2,553» (без лишних нулей), «0.00003142» — компактно, но точно."""
    text = f"{value:,.8f}".rstrip("0").rstrip(".")
    return text or "0"


def _tree_lines(rows: list[str]) -> list[str]:
    """Оформляет список строк веткой ┌ ├ └, как в остальных сводках бота."""
    if not rows:
        return []
    if len(rows) == 1:
        return [f"└ {rows[0]}"]
    return [f"┌ {rows[0]}", *(f"├ {r}" for r in rows[1:-1]), f"└ {rows[-1]}"]


async def _cryptobot_snapshot(rates_usd: dict[str, float]) -> tuple[list[tuple[str, float]], float | None, str | None]:
    """(остатки по активам, итог в $ или None, текст ошибки или None)."""
    try:
        balances = await cryptobot.get_balances()
    except PaymentError as ex:
        return [], None, str(ex)

    items: list[tuple[str, float]] = []
    total, have_total = 0.0, False
    for row in balances:
        if not isinstance(row, dict):
            continue
        code = str(row.get("currency_code") or "").upper()
        try:
            amount = float(row.get("available") or 0)
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        items.append((code, amount))
        rate = rates_usd.get(code) or (1.0 if code == "USDT" else None)
        if rate is not None:
            total += amount * rate
            have_total = True
    items.sort(key=lambda x: -x[1])
    return items, (total if have_total else None), None


async def _xrocket_snapshot(rates_usd: dict[str, float]) -> tuple[list[tuple[str, float]], float | None, str | None]:
    try:
        balances = await xrocket.get_balances()
    except PaymentError as ex:
        return [], None, str(ex)

    items: list[tuple[str, float]] = []
    total, have_total = 0.0, False
    for row in balances:
        if not isinstance(row, dict):
            continue
        code = str(row.get("currency") or row.get("currency_code") or "").upper()
        try:
            amount = float(row.get("balance") if row.get("balance") is not None else row.get("available") or 0)
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        items.append((code, amount))
        rate = rates_usd.get(code) or (1.0 if code == "USDT" else None)
        if rate is not None:
            total += amount * rate
            have_total = True
    items.sort(key=lambda x: -x[1])
    return items, (total if have_total else None), None


def _treasury_section(provider: CryptoBotClient | XRocketClient, items: list[tuple[str, float]],
                       total_usd: float | None, error: str | None) -> str:
    label = _provider_label(provider)
    if not provider.configured:
        return f"{label}\n└ <i>провайдер не настроен</i>"
    if error:
        return f"{label}\n└ ⚠️ <i>не удалось получить баланс: {html.escape(error)}</i>"
    if not items:
        return f"{label} — <b>{_fmt_usd(0)}</b>\n└ <i>баланс пуст</i>"

    head = label + (f" — <b>{_fmt_usd(total_usd)}</b>" if total_usd is not None else " — <i>оценить в $ не удалось</i>")
    rows = [f"{code}: <b>{_fmt_amount(amount)}</b>" for code, amount in items]
    return "\n".join([head, *_tree_lines(rows)])


async def _treasury_text() -> str:
    rates_usd: dict[str, float] = {}
    if cryptobot.configured:
        try:
            rates_usd = await cryptobot.get_exchange_rates_usd()
        except PaymentError:
            rates_usd = {}

    cb_items, cb_total, cb_err = await _cryptobot_snapshot(rates_usd) if cryptobot.configured else ([], None, None)
    xr_items, xr_total, xr_err = await _xrocket_snapshot(rates_usd) if xrocket.configured else ([], None, None)

    parts = [
        f"{_tge(EMOJI_TREASURY, '🏦')} <b>Баланс казны</b>",
        "",
        _treasury_section(cryptobot, cb_items, cb_total, cb_err),
        "",
        _treasury_section(xrocket, xr_items, xr_total, xr_err),
    ]

    known_totals = [t for t in (cb_total, xr_total) if t is not None]
    if known_totals:
        parts += ["", f"💰 <b>Итого:</b> {_fmt_usd(sum(known_totals))}"]
        if len(known_totals) < sum(1 for p in (cryptobot, xrocket) if p.configured):
            parts.append("<i>Часть остатков не оценена в $ — сумма может быть неполной.</i>")

    return "\n".join(parts)


@router.message(F.text.regexp(_TREASURY_RE))
async def treasury_command(message: Message) -> None:
    # Баланс казны — не для игроков: тихо игнорируем не-админов (без ответа),
    # чтобы не палить сам факт существования команды.
    if message.from_user.id not in ALERT_ADMIN_IDS:
        return
    status = await message.answer(f"<i>{_tge(EMOJI_WAIT, '⌛')} Запрашиваю баланс казны…</i>")
    try:
        text = await _treasury_text()
    except Exception:
        log.exception("Не удалось получить баланс казны")
        text = f"{_tge(EMOJI_TREASURY, '🏦')} <b>Баланс казны</b>\n\n⚠️ Не удалось получить данные, попробуйте ещё раз позже."
    try:
        await status.edit_text(text)
    except Exception:
        await message.answer(text)


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
    await edit_any(callback.message, 
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
    await edit_any(callback.message, 
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
            await edit_any(panel, text, kb)
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


# --------------------------------------------------------------------------
# Хендлеры вывода (aiogram)
# --------------------------------------------------------------------------


class WithdrawStates(StatesGroup):
    waiting_amount = State()


def _balance(user_id: int) -> float:
    return float(get_profile_stats(user_id)["balance"])


def _max_cents(balance: float) -> int:
    """Максимум, который можно вывести сейчас (в центах, округляем вниз)."""
    return int(min(balance, MAX_WITHDRAW_USD) * 100 + 1e-6)


def _amount_error(user_id: int, amount: float) -> str | None:
    if not MIN_WITHDRAW_USD <= amount <= MAX_WITHDRAW_USD:
        return f"Сумма должна быть от {MIN_WITHDRAW_USD:g}$ до {MAX_WITHDRAW_USD:g}$."
    balance = _balance(user_id)
    if amount > balance + 0.005:
        return f"Недостаточно средств. Доступно: {_fmt_usd(balance)}"
    return None


def _wd_methods_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=p.title, callback_data=f"wd:m:{p.key}", icon_custom_emoji_id=p.emoji_id)]
        for p in (cryptobot, xrocket)
    ]
    rows.append(_back_button("menu:profile"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _wd_amount_keyboard(provider_key: str, balance: float) -> InlineKeyboardMarkup:
    max_cents = _max_cents(balance)
    buttons = [
        InlineKeyboardButton(text=f"{a}$", callback_data=f"wd:a:{provider_key}:{a * 100}")
        for a in WITHDRAW_QUICK_AMOUNTS
        if a * 100 <= max_cents and a >= MIN_WITHDRAW_USD
    ]
    if max_cents >= int(MIN_WITHDRAW_USD * 100):
        buttons.append(InlineKeyboardButton(text="Весь баланс", callback_data=f"wd:a:{provider_key}:{max_cents}"))
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append(_back_button("profile:withdraw"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _wd_confirm_text(provider: CryptoBotClient | XRocketClient, amount: float) -> str:
    return (
        f"{WITHDRAW_ICON} <b>Подтверждение вывода</b>\n\n"
        f"┌ Способ: <b>{_provider_label(provider)}</b>\n"
        f"├ Сумма: <b>{_fmt_usd(amount)}</b>\n"
        f"└ Получатель: <b>ваш аккаунт Telegram</b>\n\n"
        f"<i>Сумма спишется с баланса и придёт в {provider.title} в USDT. "
        "Вы должны хотя бы раз запускать этого бота.</i>"
    )


def _wd_confirm_keyboard(provider_key: str, amount: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить вывод", callback_data=f"wd:ok:{provider_key}:{int(round(amount * 100))}")],
            _back_button(f"wd:m:{provider_key}"),
        ]
    )


def _result_keyboard(status: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if status in ("failed", "review"):
        rows.append(
            [InlineKeyboardButton(text="Поддержка", callback_data="menu:support", icon_custom_emoji_id=EMOJI_SUPPORT)]
        )
    rows.append(_back_button("menu:profile"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _notify_user(bot: Bot, user_id: int, text: str, status: str) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=_result_keyboard(status))
    except Exception as ex:
        log.warning("[withdraw] не удалось уведомить user=%s: %s", user_id, ex)


async def show_withdraw_methods(callback: CallbackQuery, state: FSMContext) -> None:
    """Экран выбора способа вывода (вызывается из main.py по кнопке «Вывести»)."""
    await state.clear()
    balance = _balance(callback.from_user.id)
    if balance + 1e-9 < MIN_WITHDRAW_USD:
        await callback.answer(
            f"Минимальная сумма вывода — {MIN_WITHDRAW_USD:g}$. Ваш баланс: {_fmt_usd(balance)}", show_alert=True
        )
        return
    await edit_any(callback.message, 
        f"{WITHDRAW_ICON} <b>Вывод средств</b>\n\n"
        f"└ Доступно: <b>{_fmt_usd(balance)}</b>\n\n"
        "<i>Выберите способ вывода. Средства придут в USDT на ваш аккаунт Telegram "
        "в выбранном сервисе.</i>",
        reply_markup=_wd_methods_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("wd:m:"))
async def withdraw_method_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    provider_key = callback.data.split(":")[2]
    provider = PROVIDERS.get(provider_key)
    if provider is None:
        await callback.answer("Неизвестный способ", show_alert=True)
        return
    if not provider.configured:
        await callback.answer(f"{provider.title} временно недоступен", show_alert=True)
        return

    balance = _balance(callback.from_user.id)
    if balance + 1e-9 < MIN_WITHDRAW_USD:
        await callback.answer(
            f"Минимальная сумма вывода — {MIN_WITHDRAW_USD:g}$. Ваш баланс: {_fmt_usd(balance)}", show_alert=True
        )
        return

    await state.set_state(WithdrawStates.waiting_amount)
    await state.update_data(wd_provider=provider_key, wd_ts=time.time())
    await edit_any(callback.message, 
        f"{WITHDRAW_ICON} <b>Вывод — {_provider_label(provider)}</b>\n\n"
        f"┌ Доступно: <b>{_fmt_usd(balance)}</b>\n"
        f"└ Лимиты: <b>от {MIN_WITHDRAW_USD:g}$ до {MAX_WITHDRAW_USD:g}$</b>\n\n"
        "<i>Выберите сумму или отправьте её в чат числом.</i>",
        reply_markup=_wd_amount_keyboard(provider_key, balance),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("wd:a:"))
async def withdraw_amount_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, _, provider_key, cents_str = callback.data.split(":")
        amount = int(cents_str) / 100
    except ValueError:
        await callback.answer("Недопустимая сумма", show_alert=True)
        return
    provider = PROVIDERS.get(provider_key)
    if provider is None:
        await callback.answer("Неизвестный способ", show_alert=True)
        return
    error = _amount_error(callback.from_user.id, amount)
    if error:
        await callback.answer(error, show_alert=True)
        return

    await state.clear()
    await edit_any(callback.message, 
        _wd_confirm_text(provider, amount), reply_markup=_wd_confirm_keyboard(provider_key, amount)
    )
    await callback.answer()


@router.message(WithdrawStates.waiting_amount, F.text)
async def withdraw_amount_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    provider_key = data.get("wd_provider")
    text = (message.text or "").strip().replace(",", ".")

    # Не число или ввод устарел — игрок ушёл из вывода: отдаём сообщение следующим обработчикам.
    stale = time.time() - data.get("wd_ts", 0) > AMOUNT_INPUT_TTL_SECONDS
    if provider_key not in PROVIDERS or stale or not re.fullmatch(r"\d+(\.\d+)?", text):
        await state.clear()
        raise SkipHandler

    amount = round(float(text), 2)
    error = _amount_error(message.from_user.id, amount)
    if error:
        await message.answer(error)  # сумма неверная — даём ввести заново
        return

    await state.clear()
    await message.answer(
        _wd_confirm_text(PROVIDERS[provider_key], amount), reply_markup=_wd_confirm_keyboard(provider_key, amount)
    )


@router.callback_query(F.data.startswith("wd:ok:"))
async def withdraw_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        _, _, provider_key, cents_str = callback.data.split(":")
        amount = int(cents_str) / 100
    except ValueError:
        await callback.answer("Недопустимая сумма", show_alert=True)
        return

    user_id = callback.from_user.id
    try:
        result = await _execute_withdrawal(callback.bot, user_id, provider_key, amount)
    except Exception:
        # _execute_withdrawal сам ловит всё, это — страховка от совсем неожиданного
        log.exception("[withdraw] сбой обработчика user=%s", user_id)
        result = WithdrawResult("review", _error_text("Не удалось выполнить вывод", None))

    if result.status == "rejected":
        await callback.answer(result.text, show_alert=True)
        return

    await callback.answer()
    try:
        await edit_any(callback.message, result.text, reply_markup=_result_keyboard(result.status))
    except Exception:
        await _notify_user(callback.bot, user_id, result.text, result.status)
