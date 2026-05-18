import telebot
import requests
import threading
import time
import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto

BOT_TOKEN       = "8628524678:AAH6AuW7KdTF-_-OiVfVH5i_LJH5NLSDg1I"
CRYPTOBOT_TOKEN = "583673:AAwGj7YtqTJZuSomTia1W08YRNo1udgrQiL"
CRYPTOBOT_API   = "https://pay.crypt.bot/api"

# ══════════════════════════════════════════════════════
#  👑  СПИСОК АДМИНИСТРАТОРОВ  (добавляйте ID сюда)
# ══════════════════════════════════════════════════════
ADMIN_IDS = [8118184388, 8521752725]          # пример: [111111111, 222222222]

PAYOUT_AMOUNT = 5.0
QUEUE_ENABLED = True

EMOJI_RULES   = "5258185631355378853"
EMOJI_BALANCE = "5258204546391351475"
EMOJI_SUBMIT  = "5449407131675558756"
EMOJI_HISTORY = "6030776052345737530"
EMOJI_STATS   = "5258330865674494479"
EMOJI_BACK    = "6039539366177541657"
EMOJI_ADMIN   = "5258185631355378853"
EMOJI_CHECK   = "5282843764451195532"
EMOJI_QUEUE   = "5323442290708985472"
EMOJI_WISS    = "5258043150110301407"

BANNER_FILE_ID = "AgACAgIAAxkBAAMeagQukWF_Zj77_eYNPXcEywJNg0EAAg4Taxs1GSBI3gdnW__fsXUBAAMCAAN5AAM7BA"

# ══════════════════════════════════════════════════════
#  Хранилища данных
# ══════════════════════════════════════════════════════
users_db           = {}   # user_id → dict
queue              = []   # очередь: список user_id
pending            = {}   # user_id → user_msg_id  (QR на проверке)
pending_admin_msgs = {}   # user_id → [(admin_chat_id, admin_msg_id), ...]
withdraw_requests  = {}   # req_id  → dict
withdraw_counter   = [0]
settings = {
    "payout": PAYOUT_AMOUNT,
    "rules": (
        '<b><tg-emoji emoji-id="6030776052345737530">🎟</tg-emoji> Правила сервиса:</b>\n\n'
        "├ <b>1.</b> Номер должен быть зарегистрирован на вас\n"
        "├ <b>2.</b> Номер не должен быть заблокирован\n"
        "├ <b>3.</b> QR-код должен быть чётким и читаемым\n"
        "├ <b>4.</b> Одна заявка в день с одного аккаунта\n"
        "├ <b>5.</b> При нарушении — бан без предупреждения\n"
        "╰ <b>6.</b> Выплата производится после проверки"
    ),
}

user_states       = {}
waiting_for_photo = set()
waiting_for_qr    = set()
admin_states      = {}

bot = telebot.TeleBot(BOT_TOKEN)

# ══════════════════════════════════════════════════════
#  Вспомогательные функции
# ══════════════════════════════════════════════════════
def get_user(user_id):
    if user_id not in users_db:
        users_db[user_id] = {
            "balance":        0.0,
            "numbers_rented": 0,
            "history":        [],
            "banned":         False,
            "username":       "",
            "first_name":     "",
        }
    return users_db[user_id]

def get_status(user):
    return "Активен" if user["numbers_rented"] >= 1 else "Неактивен"

def esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def is_admin(user_id):
    return user_id in ADMIN_IDS

def em(eid, fallback="⭐"):
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'

def notify_all_admins(text="", markup=None, photo=None, caption=None):
    """Отправить сообщение всем администраторам. Возвращает [(chat_id, msg_id), ...]."""
    sent = []
    for admin_id in ADMIN_IDS:
        try:
            if photo:
                m = bot.send_photo(admin_id, photo, caption=caption,
                                   parse_mode="HTML", reply_markup=markup)
            else:
                m = bot.send_message(admin_id, text,
                                     parse_mode="HTML", reply_markup=markup)
            sent.append((admin_id, m.message_id))
        except Exception as e:
            print(f"[notify_admin {admin_id}] {e}")
    return sent

# ══════════════════════════════════════════════════════
#  CryptoBot
# ══════════════════════════════════════════════════════
def cryptobot_create_check(amount: float, currency: str = "USDT") -> dict | None:
    try:
        resp = requests.post(
            f"{CRYPTOBOT_API}/createCheck",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
            json={"asset": currency, "amount": str(amount)},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return data["result"]
        return None
    except Exception as e:
        print(f"CryptoBot error: {e}")
        return None

# ══════════════════════════════════════════════════════
#  Фоновый поток — автообновление позиции каждые 5 мин
# ══════════════════════════════════════════════════════
def _queue_updater():
    while True:
        time.sleep(300)          # 5 минут
        snapshot = list(queue)   # копия, чтобы не блокировать
        total    = len(snapshot)
        for i, user_id in enumerate(snapshot):
            pos = i + 1
            if pos == 1:
                continue         # первый — пусть сам заходит в «Сдать номер»
            try:
                bot.send_message(
                    user_id,
                    f"╭─────────────────────\n"
                    f'├ {em(EMOJI_QUEUE,"🔄")} <b>Обновление очереди</b>\n'
                    f"├\n"
                    f"├ Ваша позиция: <b>{pos}</b> из <b>{total}</b>\n"
                    f"╰─────────────────────",
                    parse_mode="HTML",
                )
            except Exception:
                pass

threading.Thread(target=_queue_updater, daemon=True).start()

# ══════════════════════════════════════════════════════
#  Тексты
# ══════════════════════════════════════════════════════
def queue_text(pos):
    total = len(queue)
    return (
        f"╭─────────────────────\n"
        f'├ <b>{em(EMOJI_QUEUE,"⏳")} Вы в очереди</b>\n'
        f"├\n"
        f'├ <tg-emoji emoji-id="6030537810509828330">🎟</tg-emoji> '
        f"Ваша позиция: <b>{pos}</b> из <b>{total}</b>\n"
        f"├\n"
        f"├ Позиция обновляется каждые 5 минут\n"
        f"╰─────────────────────"
    )

def welcome_text(tg_user, user):
    name     = esc(tg_user.first_name or "—")
    username = f"@{esc(tg_user.username)}" if tg_user.username else "—"
    return (
        f"╭─────────────────────\n"
        f'├ <b><tg-emoji emoji-id="5260399854500191689">🎟</tg-emoji> {name}</b>\n'
        f'├ <tg-emoji emoji-id="5282843764451195532">🎟</tg-emoji> ID: <code>{tg_user.id}</code>\n'
        f'├ <tg-emoji emoji-id="5323442290708985472">🎟</tg-emoji> : {username}\n'
        f"├\n"
        f'├ <tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> Баланс: <b>${user["balance"]:.2f}</b>\n'
        f'├ <tg-emoji emoji-id="5449407131675558756">🎟</tg-emoji> Сдано: <b>{user["numbers_rented"]}</b> номеров\n'
        f'├ <tg-emoji emoji-id="5258185631355378853">🎟</tg-emoji> Статус: {get_status(user)}\n'
        f"╰─────────────────────"
    )

def rules_text():
    return settings["rules"]

def balance_text(user):
    if user["history"]:
        lines = ""
        for h in reversed(user["history"][-5:]):
            sign  = "+" if h["amount"] > 0 else ""
            lines += f"├ {h['date']} — <b>{sign}${h['amount']:.2f}</b> ({h['status']})\n"
    else:
        lines = "├ История пуста\n"
    return (
        f"╭─────────────────────\n"
        f"├ <b>{em(EMOJI_BALANCE,'💰')} Ваш баланс</b>\n"
        f"├\n"
        f'├ <tg-emoji emoji-id="5904462880941545555">🎟</tg-emoji> '
        f'Доступно: <b>${user["balance"]:.2f}</b>\n'
        f"├\n"
        f'├ <tg-emoji emoji-id="6030776052345737530">🎟</tg-emoji> <b>Последние операции:</b>\n'
        f"{lines}"
        f"╰─────────────────────"
    )

def withdraw_text(user):
    return (
        f"╭─────────────────────\n"
        f"├ <b>{em(EMOJI_BALANCE,'💸')} Вывод средств</b>\n"
        f"├\n"
        f'├ <tg-emoji emoji-id="5904462880941545555">🎟</tg-emoji> '
        f'Доступно: <b>${user["balance"]:.2f}</b>\n'
        f"├\n"
        f'├ <tg-emoji emoji-id="5258108352008823107">🎟</tg-emoji> Минимальная сумма: <b>$1.00</b>\n'
        f'├ <tg-emoji emoji-id="6030776052345737530">🎟</tg-emoji> Выплата через: <b>@CryptoBot</b>\n'
        f"├\n"
        f"├ Введите сумму для вывода\n"
        f"╰─────────────────────"
    )

def withdraw_confirm_text(amount: float, user):
    return (
        f"╭─────────────────────\n"
        f"├ <b>{em(EMOJI_BALANCE,'💸')} Подтверждение вывода</b>\n"
        f"├\n"
        f'├ <tg-emoji emoji-id="5890848474563352982">🎟</tg-emoji> Сумма: <b>${amount:.2f}</b>\n'
        f'├ <tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> '
        f'Останется: <b>${user["balance"] - amount:.2f}</b>\n'
        f'├ <tg-emoji emoji-id="5258108352008823107">🎟</tg-emoji> Способ: <b>@CryptoBot (USDT)</b>\n'
        f"├\n"
        f"├ Подтвердите заявку на вывод\n"
        f"╰─────────────────────"
    )

def withdraw_pending_admin_text(req_id, user_id, amount, first_name, username):
    return (
        f"╭─────────────────────\n"
        f'├ <b><tg-emoji emoji-id="5904462880941545555">🎟</tg-emoji> '
        f"Заявка на вывод #{req_id}</b>\n"
        f"├\n"
        f'├ <tg-emoji emoji-id="5260399854500191689">🎟</tg-emoji> Имя: {first_name}\n'
        f'├ <tg-emoji emoji-id="5323442290708985472">🎟</tg-emoji> Username: {username}\n'
        f'├ <tg-emoji emoji-id="5282843764451195532">🎟</tg-emoji> ID: <code>{user_id}</code>\n'
        f'├ <tg-emoji emoji-id="5890848474563352982">🎟</tg-emoji> Сумма: <b>${amount:.2f} USDT</b>\n'
        f"╰─────────────────────"
    )

def submit_price_text():
    amt = settings["payout"]
    return (
        f"╭─────────────────────\n"
        f"├ <b>{em(EMOJI_SUBMIT,'📦')} Сдать номер</b>\n"
        f"├\n"
        f'├ <tg-emoji emoji-id="5890848474563352982">🎟</tg-emoji> '
        f"Выплата за номер: <b>${amt:.2f}</b>\n"
        f"├\n"
        f'├ <tg-emoji emoji-id="5258108352008823107">🎟</tg-emoji> Прикрепите QR-код номера\n'
        f"├    и нажмите кнопку ниже\n"
        f"╰─────────────────────"
    )

def history_text(user):
    if not user["history"]:
        body = "├ История операций пуста\n"
    else:
        body = ""
        for h in reversed(user["history"][-10:]):
            sign  = "+" if h["amount"] > 0 else ""
            body += f"├ {h['date']} {sign}${h['amount']:.2f} — {h['status']}\n"
    return (
        f"╭─────────────────────\n"
        f'├ <b><tg-emoji emoji-id="6030776052345737530">🎟</tg-emoji> История операций</b>\n'
        f"├\n"
        f"{body}"
        f"╰─────────────────────"
    )

def statistics_text():
    return (
        f"╭─────────────────────\n"
        f"├ <b>{em(EMOJI_STATS,'📊')} Статистика</b>\n"
        f"├\n"
        f'├ <tg-emoji emoji-id="5258513401784573443">🎟</tg-emoji> '
        f"Пользователей: <b>{len(users_db)}</b>\n"
        f'├ <tg-emoji emoji-id="5449407131675558756">🎟</tg-emoji> '
        f"Сдано номеров: <b>{sum(u['numbers_rented'] for u in users_db.values())}</b>\n"
        f'├ <tg-emoji emoji-id="5890848474563352982">🎟</tg-emoji> '
        f"Выплачено: <b>${sum(u['balance'] for u in users_db.values()):.2f}</b>\n"
        f'├ <tg-emoji emoji-id="6030537810509828330">🎟</tg-emoji> '
        f"В очереди: <b>{len(queue)}</b>\n"
        f'├ <tg-emoji emoji-id="6039496266180726678">🎟</tg-emoji> '
        f"На проверке: <b>{len(pending)}</b>\n"
        f"╰─────────────────────"
    )

def admin_top_stats_text():
    """ТОП-20 пользователей по сдаче и по балансу."""
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}

    by_rented  = sorted(users_db.items(),
                        key=lambda x: x[1]["numbers_rented"], reverse=True)[:20]
    by_balance = sorted(users_db.items(),
                        key=lambda x: x[1]["balance"],        reverse=True)[:20]

    def row(i, uid, u, val):
        m    = medal.get(i, f"{i}.")
        name = esc(u.get("first_name") or str(uid))
        un   = f"@{esc(u['username'])}" if u.get("username") else "—"
        return f"├ {m} {name} ({un}) — <b>{val}</b>\n"

    text  = "╭─────────────────────\n"
    text += "├ 🏆 <b>ТОП-20 по сдаче номеров:</b>\n├\n"
    if by_rented:
        for i, (uid, u) in enumerate(by_rented, 1):
            text += row(i, uid, u, f"{u['numbers_rented']} шт.")
    else:
        text += "├ Нет данных\n"

    text += "├\n├ 💰 <b>ТОП-20 по балансу:</b>\n├\n"
    if by_balance:
        for i, (uid, u) in enumerate(by_balance, 1):
            text += row(i, uid, u, f"${u['balance']:.2f}")
    else:
        text += "├ Нет данных\n"

    text += "╰─────────────────────"
    return text

# ══════════════════════════════════════════════════════
#  Клавиатуры
# ══════════════════════════════════════════════════════
def main_menu():
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("Правила",     callback_data="rules",
                             icon_custom_emoji_id=EMOJI_RULES),
        InlineKeyboardButton("Баланс",      callback_data="balance",
                             icon_custom_emoji_id=EMOJI_BALANCE),
    )
    m.row(InlineKeyboardButton("Сдать номер", callback_data="submit_number",
                               icon_custom_emoji_id=EMOJI_SUBMIT))
    m.row(
        InlineKeyboardButton("История",    callback_data="history",
                             icon_custom_emoji_id=EMOJI_HISTORY),
        InlineKeyboardButton("Статистика", callback_data="statistics",
                             icon_custom_emoji_id=EMOJI_STATS),
    )
    return m

def back_btn(target="back_menu"):
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("Назад", callback_data=target,
                               icon_custom_emoji_id=EMOJI_BACK))
    return m

def submit_menu():
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("Прикрепить QR-код", callback_data="attach_qr"))
    m.row(InlineKeyboardButton("Назад", callback_data="back_menu",
                               icon_custom_emoji_id=EMOJI_BACK))
    return m

def send_qr_btn():
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("✅ Отправить заявку", callback_data="send_qr"))
    m.row(InlineKeyboardButton("Изменить QR-код",    callback_data="attach_qr"))
    m.row(InlineKeyboardButton("Назад", callback_data="back_menu",
                               icon_custom_emoji_id=EMOJI_BACK))
    return m

def pending_menu():
    """Меню когда заявка уже на проверке — можно отменить."""
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("❌ Отменить заявку", callback_data="cancel_application"))
    m.row(InlineKeyboardButton("Назад", callback_data="back_menu",
                               icon_custom_emoji_id=EMOJI_BACK))
    return m

def balance_menu():
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("Вывести", callback_data="withdraw",
                               icon_custom_emoji_id=EMOJI_WISS))
    m.row(InlineKeyboardButton("Назад",   callback_data="back_menu",
                               icon_custom_emoji_id=EMOJI_BACK))
    return m

def withdraw_confirm_btn(amount: float):
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("✅ Подтвердить",
                             callback_data=f"withdraw_confirm_{amount:.2f}"),
        InlineKeyboardButton("❌ Отмена", callback_data="balance"),
    )
    return m

def admin_withdraw_btn(req_id: int):
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("✅ Принять",   callback_data=f"wd_take_{req_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"wd_reject_{req_id}"),
    )
    return m

def admin_review_btn(user_id):
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("✅ Принять",   callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{user_id}"),
    )
    return m

def admin_panel_menu():
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("📊 Статистика",         callback_data="adm_stats"))
    m.row(InlineKeyboardButton("🏆 Топ пользователей",  callback_data="adm_top_stats"))
    m.row(
        InlineKeyboardButton("🔍 Проверка юзера",  callback_data="adm_check"),
        InlineKeyboardButton("💰 Выдать баланс",   callback_data="adm_give"),
    )
    m.row(
        InlineKeyboardButton("➖ Снять баланс",    callback_data="adm_take"),
        InlineKeyboardButton("🔄 Обнулить всех",   callback_data="adm_reset_all"),
    )
    m.row(InlineKeyboardButton("📢 Рассылка",          callback_data="adm_broadcast"))
    m.row(InlineKeyboardButton("💵 Изменить выплату",  callback_data="adm_payout"))
    return m

# ══════════════════════════════════════════════════════
#  Вспомогательная функция: завершение обработки QR
#  После approve/reject пользователь добавляется в
#  конец очереди — он может подать новый QR только
#  дождавшись своей очереди.
# ══════════════════════════════════════════════════════
def _finish_qr_review(target_id):
    """Убрать из pending и поставить в конец очереди."""
    pending.pop(target_id, None)
    pending_admin_msgs.pop(target_id, None)
    if QUEUE_ENABLED and target_id not in queue:
        queue.append(target_id)

# ══════════════════════════════════════════════════════
#  Обработка вывода средств
# ══════════════════════════════════════════════════════
def _process_withdraw_take(req_id: int, chat_id: int, msg_id: int | None = None):
    req = withdraw_requests.get(req_id)
    if not req:
        bot.send_message(chat_id, f"❌ Заявка #{req_id} не найдена.")
        return
    if req["status"] != "pending":
        bot.send_message(chat_id, f"⚠️ Заявка #{req_id} уже обработана.")
        return

    amount  = req["amount"]
    user_id = req["user_id"]
    check   = cryptobot_create_check(amount)
    if check is None:
        bot.send_message(chat_id,
                         f"❌ Ошибка создания чека CryptoBot для заявки #{req_id}.")
        return

    req["status"]    = "done"
    check_link       = check.get("bot_check_url") or check.get("check_url") or "—"
    req["check_url"] = check_link

    u = users_db.get(user_id)
    if u:
        for h in reversed(u["history"]):
            if h["status"] == "Вывод (ожидание)" and h["amount"] == -amount:
                h["status"] = "Вывод выплачен"
                break

    try:
        bot.send_message(
            user_id,
            f"╭─────────────────────\n"
            f'├ <b><tg-emoji emoji-id="6041720006973067267">🎟</tg-emoji> Вывод одобрен!</b>\n'
            f"├\n"
            f'├ <tg-emoji emoji-id="5904462880941545555">🎟</tg-emoji> '
            f"Сумма: <b>${amount:.2f} USDT</b>\n"
            f'├ <tg-emoji emoji-id="6030776052345737530">🎟</tg-emoji> Чек: <b>@CryptoBot</b>\n'
            f"├\n"
            f"├ Нажмите кнопку ниже, чтобы получить\n"
            f"├ ваши средства через @CryptoBot\n"
            f"╰─────────────────────",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup().row(
                InlineKeyboardButton("Получить средства", url=check_link)
            ),
        )
    except Exception:
        pass

    ok_text = (
        f"╭─────────────────────\n"
        f"├ ✅ <b>Заявка #{req_id} выплачена!</b>\n"
        f"├\n"
        f'├ 💸 Чек на <b>${amount:.2f} USDT</b>\n'
        f'├ 🔗 {check_link}\n'
        f"╰─────────────────────"
    )
    if msg_id:
        try:
            bot.edit_message_text(ok_text, chat_id, msg_id, parse_mode="HTML")
            return
        except Exception:
            pass
    bot.send_message(chat_id, ok_text, parse_mode="HTML")


def _process_withdraw_reject(req_id: int, chat_id: int, msg_id: int | None = None):
    req = withdraw_requests.get(req_id)
    if not req:
        bot.send_message(chat_id, f"❌ Заявка #{req_id} не найдена.")
        return
    if req["status"] != "pending":
        bot.send_message(chat_id, f"⚠️ Заявка #{req_id} уже обработана.")
        return

    amount  = req["amount"]
    user_id = req["user_id"]
    req["status"] = "rejected"

    u = get_user(user_id)
    u["balance"] += amount
    for h in reversed(u["history"]):
        if h["status"] == "Вывод (ожидание)" and h["amount"] == -amount:
            h["status"] = "Вывод отклонён"
            break

    try:
        bot.send_message(
            user_id,
            f"╭─────────────────────\n"
            f'├ <b><tg-emoji emoji-id="6030776052345737530">🎟</tg-emoji> Вывод отклонён</b>\n'
            f"├\n"
            f'├ <tg-emoji emoji-id="6039539366177541657">🎟</tg-emoji> '
            f"Возвращено: <b>${amount:.2f}</b>\n"
            f'├ <tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> '
            f'Ваш баланс: <b>${u["balance"]:.2f}</b>\n'
            f"├\n"
            f"├ Обратитесь в поддержку за деталями\n"
            f"╰─────────────────────",
            parse_mode="HTML",
        )
    except Exception:
        pass

    rej_text = (
        f"╭─────────────────────\n"
        f"├ ❌ <b>Заявка #{req_id} отклонена.</b>\n"
        f"╰─────────────────────"
    )
    if msg_id:
        try:
            bot.edit_message_text(rej_text, chat_id, msg_id, parse_mode="HTML")
            return
        except Exception:
            pass
    bot.send_message(chat_id, rej_text, parse_mode="HTML")

# ══════════════════════════════════════════════════════
#  Команды
# ══════════════════════════════════════════════════════
@bot.message_handler(commands=["getfileid"])
def cmd_getfileid(message):
    waiting_for_photo.add(message.from_user.id)
    bot.send_message(message.chat.id,
                     "Отправь фото — верну <b>file_id</b>", parse_mode="HTML")


@bot.message_handler(commands=["take"])
def cmd_take(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        pl = [f"#{r} — ${withdraw_requests[r]['amount']:.2f}"
              for r in withdraw_requests
              if withdraw_requests[r]["status"] == "pending"]
        if not pl:
            bot.send_message(message.chat.id, "📭 Нет ожидающих заявок на вывод.")
        else:
            bot.send_message(
                message.chat.id,
                "╭─────────────────────\n"
                "├ ⏳ <b>Ожидающие заявки:</b>\n├\n"
                + "\n".join(f"├ {l}" for l in pl)
                + "\n╰─────────────────────\n\nИспользуй: <code>/take [номер]</code>",
                parse_mode="HTML",
            )
        return
    try:
        req_id = int(parts[1])
    except ValueError:
        bot.send_message(message.chat.id,
                         "❌ Укажите числовой номер: <code>/take 4</code>",
                         parse_mode="HTML")
        return
    _process_withdraw_take(req_id, message.chat.id)


@bot.message_handler(commands=["reject"])
def cmd_reject(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.send_message(message.chat.id,
                         "❌ Укажите номер: <code>/reject 4</code>",
                         parse_mode="HTML")
        return
    try:
        req_id = int(parts[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ Укажите числовой номер.", parse_mode="HTML")
        return
    _process_withdraw_reject(req_id, message.chat.id)


@bot.message_handler(commands=["takeall"])
def cmd_takeall(message):
    if not is_admin(message.from_user.id):
        return
    ids = [r for r in withdraw_requests
           if withdraw_requests[r]["status"] == "pending"]
    if not ids:
        bot.send_message(message.chat.id, "📭 Нет ожидающих заявок.")
        return
    bot.send_message(message.chat.id, f"⏳ Обрабатываю {len(ids)} заявок...")
    done = failed = 0
    for req_id in ids:
        if cryptobot_create_check(withdraw_requests[req_id]["amount"]):
            _process_withdraw_take(req_id, message.chat.id)
            done += 1
        else:
            failed += 1
    bot.send_message(message.chat.id,
                     f"✅ Принято: <b>{done}</b>  |  ❌ Ошибок: <b>{failed}</b>",
                     parse_mode="HTML")


@bot.message_handler(commands=["rejectall"])
def cmd_rejectall(message):
    if not is_admin(message.from_user.id):
        return
    ids = [r for r in withdraw_requests
           if withdraw_requests[r]["status"] == "pending"]
    if not ids:
        bot.send_message(message.chat.id, "📭 Нет ожидающих заявок.")
        return
    for req_id in ids:
        _process_withdraw_reject(req_id, message.chat.id)
    bot.send_message(message.chat.id,
                     f"❌ Отклонено: <b>{len(ids)}</b>", parse_mode="HTML")


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        return
    bot.send_message(
        message.chat.id,
        f"╭─────────────────────\n"
        f"├ <b>{em(EMOJI_ADMIN,'👑')} Панель администратора</b>\n"
        f"├\n"
        f'├ <tg-emoji emoji-id="5904462880941545555">🎟</tg-emoji> '
        f'Выплата за номер: <b>${settings["payout"]:.2f}</b>\n'
        f'├ <tg-emoji emoji-id="5258513401784573443">🎟</tg-emoji> '
        f'Пользователей: <b>{len(users_db)}</b>\n'
        f'├ <tg-emoji emoji-id="5258185631355378853">🎟</tg-emoji> '
        f'Администраторов: <b>{len(ADMIN_IDS)}</b>\n'
        f"╰─────────────────────",
        parse_mode="HTML",
        reply_markup=admin_panel_menu(),
    )


@bot.message_handler(commands=["start", "menu"])
def start(message):
    uid  = message.from_user.id
    user = get_user(uid)
    user["username"]   = message.from_user.username or ""
    user["first_name"] = message.from_user.first_name or ""
    if user.get("banned"):
        bot.send_message(message.chat.id, "🚫 Вы заблокированы.")
        return
    text = welcome_text(message.from_user, user)
    if BANNER_FILE_ID:
        bot.send_photo(message.chat.id, BANNER_FILE_ID,
                       caption=text, parse_mode="HTML", reply_markup=main_menu())
    else:
        bot.send_message(message.chat.id, text,
                         parse_mode="HTML", reply_markup=main_menu())

# ══════════════════════════════════════════════════════
#  Обработчики медиа
# ══════════════════════════════════════════════════════
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    uid = message.from_user.id

    if uid in waiting_for_photo:
        waiting_for_photo.discard(uid)
        file_id = message.photo[-1].file_id
        bot.send_message(message.chat.id,
                         f"✅ <b>file_id</b>:\n\n<code>{file_id}</code>",
                         parse_mode="HTML")
        return

    if uid in waiting_for_qr:
        waiting_for_qr.discard(uid)
        file_id = message.photo[-1].file_id
        get_user(uid)["_pending_qr"] = file_id
        bot.send_photo(
            message.chat.id,
            file_id,
            caption=(
                f"╭─────────────────────\n"
                f'├ <b><tg-emoji emoji-id="6039496266180726678">🎟</tg-emoji> '
                f"QR-код получен!</b>\n"
                f"├\n"
                f"├ Проверьте фото и нажмите\n"
                f"├ <b>«Отправить заявку»</b>\n"
                f"╰─────────────────────"
            ),
            parse_mode="HTML",
            reply_markup=send_qr_btn(),
        )

# ══════════════════════════════════════════════════════
#  Обработчик текста (состояния пользователя + админа)
# ══════════════════════════════════════════════════════
@bot.message_handler(content_types=["text"])
def handle_text(message):
    uid = message.from_user.id

    # ── Ввод суммы вывода ──
    if user_states.get(uid) == "waiting_withdraw_amount":
        del user_states[uid]
        raw = message.text.strip().replace(",", ".")
        try:
            amount = float(raw)
        except ValueError:
            bot.send_message(
                message.chat.id,
                "╭─────────────────────\n"
                "├ ❌ <b>Некорректная сумма</b>\n"
                "├\n"
                "├ Введите число, например: <code>5.00</code>\n"
                "╰─────────────────────",
                parse_mode="HTML",
                reply_markup=back_btn("balance"),
            )
            return
        u = get_user(uid)
        if amount < 1.0:
            bot.send_message(
                message.chat.id,
                "╭─────────────────────\n"
                "├ ❌ <b>Минимальная сумма вывода — $1.00</b>\n"
                "╰─────────────────────",
                parse_mode="HTML",
                reply_markup=back_btn("balance"),
            )
            return
        if amount > u["balance"]:
            bot.send_message(
                message.chat.id,
                f"╭─────────────────────\n"
                f"├ ❌ <b>Недостаточно средств</b>\n"
                f"├\n"
                f'├ Доступно: <b>${u["balance"]:.2f}</b>\n'
                f"╰─────────────────────",
                parse_mode="HTML",
                reply_markup=back_btn("balance"),
            )
            return
        bot.send_message(
            message.chat.id,
            withdraw_confirm_text(amount, u),
            parse_mode="HTML",
            reply_markup=withdraw_confirm_btn(amount),
        )
        return

    if uid not in admin_states:
        return

    state  = admin_states[uid]
    action = state.get("action")
    text   = message.text.strip()

    # ── Рассылка ──
    if action == "broadcast":
        del admin_states[uid]
        count = 0
        for u_id in list(users_db.keys()):
            try:
                bot.send_message(u_id,
                                 f"<b>Сообщение от администратора:</b>\n\n{text}",
                                 parse_mode="HTML")
                count += 1
            except Exception:
                pass
        bot.send_message(message.chat.id,
                         f"✅ Рассылка отправлена <b>{count}</b> пользователям.",
                         parse_mode="HTML")

    # ── Проверка пользователя ──
    elif action == "check_user":
        del admin_states[uid]
        try:
            target_id = int(text)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите числовой ID")
            return
        u = users_db.get(target_id)
        if not u:
            bot.send_message(message.chat.id, "❌ Пользователь не найден")
            return
        in_q  = target_id in queue
        q_pos = f"Да (позиция {queue.index(target_id)+1})" if in_q else "Нет"
        bot.send_message(
            message.chat.id,
            f"╭─────────────────────\n"
            f"├ 👤 <b>Пользователь {target_id}</b>\n"
            f"├\n"
            f"├ 📛 Имя: {esc(u['first_name'])}\n"
            f"├ 🔗 Username: {'@'+esc(u['username']) if u['username'] else '—'}\n"
            f"├ 💰 Баланс: <b>${u['balance']:.2f}</b>\n"
            f"├ 📦 Сдано: <b>{u['numbers_rented']}</b>\n"
            f"├ 🔄 В очереди: {q_pos}\n"
            f"├ ⏳ На проверке: {'Да' if target_id in pending else 'Нет'}\n"
            f"├ 🚫 Бан: {'Да' if u.get('banned') else 'Нет'}\n"
            f"╰─────────────────────",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup().row(
                InlineKeyboardButton(
                    "🚫 Забанить" if not u.get("banned") else "✅ Разбанить",
                    callback_data=f"adm_ban_{target_id}",
                )
            ),
        )

    elif action == "give_step1":
        try:
            admin_states[uid] = {"action": "give_step2", "target": int(text)}
            bot.send_message(message.chat.id, "💵 Введите сумму (например: 10):")
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите числовой ID")
            del admin_states[uid]

    elif action == "give_step2":
        try:
            amount    = float(text)
            target_id = state["target"]
            u         = get_user(target_id)
            u["balance"] += amount
            u["history"].append({
                "date":   datetime.date.today().strftime("%d.%m"),
                "amount": amount, "status": "Пополнение",
            })
            del admin_states[uid]
            bot.send_message(
                message.chat.id,
                f"✅ Начислено <b>${amount:.2f}</b> пользователю <code>{target_id}</code>",
                parse_mode="HTML",
            )
            try:
                bot.send_message(
                    target_id,
                    f"💰 На ваш баланс начислено <b>${amount:.2f}</b>!",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите корректную сумму")
            del admin_states[uid]

    elif action == "take_step1":
        try:
            admin_states[uid] = {"action": "take_step2", "target": int(text)}
            bot.send_message(message.chat.id, "💸 Введите сумму для списания:")
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите числовой ID")
            del admin_states[uid]

    elif action == "take_step2":
        try:
            amount    = float(text)
            target_id = state["target"]
            u         = get_user(target_id)
            u["balance"] = max(0, u["balance"] - amount)
            u["history"].append({
                "date":   datetime.date.today().strftime("%d.%m"),
                "amount": -amount, "status": "Списание",
            })
            del admin_states[uid]
            bot.send_message(
                message.chat.id,
                f"✅ Списано <b>${amount:.2f}</b> у пользователя <code>{target_id}</code>",
                parse_mode="HTML",
            )
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите корректную сумму")
            del admin_states[uid]

    elif action == "set_payout":
        try:
            amount           = float(text)
            settings["payout"] = amount
            del admin_states[uid]
            bot.send_message(
                message.chat.id,
                f"✅ Выплата за номер изменена на <b>${amount:.2f}</b>",
                parse_mode="HTML",
            )
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите корректную сумму")
            del admin_states[uid]

    elif action == "reject_reason":
        target_id = state["target"]
        reason    = text
        del admin_states[uid]

        # Пометить сообщения у всех администраторов
        for (achat, amsg) in pending_admin_msgs.get(target_id, []):
            try:
                bot.edit_message_caption(
                    caption=f"❌ <b>ОТКЛОНЕНО</b>\n📝 Причина: {esc(reason)}",
                    chat_id=achat, message_id=amsg, parse_mode="HTML",
                )
            except Exception:
                pass

        _finish_qr_review(target_id)

        try:
            bot.send_message(
                target_id,
                f"╭─────────────────────\n"
                f"├ ❌ <b>Ваша заявка отклонена</b>\n"
                f"├\n"
                f"├ 📝 Причина: {esc(reason)}\n"
                f"├\n"
                f"├ Вы добавлены обратно в очередь.\n"
                f"╰─────────────────────",
                parse_mode="HTML",
            )
        except Exception:
            pass
        bot.send_message(message.chat.id,
                         "✅ Заявка отклонена, пользователь уведомлён.")

# ══════════════════════════════════════════════════════
#  Обработчик callback-кнопок
# ══════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    bot.answer_callback_query(call.id)
    uid     = call.from_user.id
    chat_id = call.message.chat.id
    msg_id  = call.message.message_id
    data    = call.data
    user    = get_user(uid)

    def edit(text, markup=None):
        try:
            if call.message.photo:
                bot.edit_message_caption(caption=text, chat_id=chat_id,
                                         message_id=msg_id,
                                         parse_mode="HTML", reply_markup=markup)
            else:
                bot.edit_message_text(text, chat_id, msg_id,
                                      parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            print(f"[edit] {e}")
            try:
                bot.send_message(chat_id, text,
                                 parse_mode="HTML", reply_markup=markup)
            except Exception as e2:
                print(f"[edit fallback] {e2}")

    # ── Назад в меню ──
    if data == "back_menu":
        user_states.pop(uid, None)
        text = welcome_text(call.from_user, user)
        try:
            if BANNER_FILE_ID:
                bot.edit_message_media(
                    InputMediaPhoto(BANNER_FILE_ID, caption=text, parse_mode="HTML"),
                    chat_id, msg_id, reply_markup=main_menu(),
                )
            else:
                edit(text, main_menu())
        except Exception:
            edit(text, main_menu())

    elif data == "rules":
        edit(rules_text(), back_btn())

    elif data == "balance":
        user_states.pop(uid, None)
        edit(balance_text(user), balance_menu())

    elif data == "history":
        edit(history_text(user), back_btn())

    elif data == "statistics":
        edit(statistics_text(), back_btn())

    # ── Сдать номер ──
    elif data == "submit_number":
        if user.get("banned"):
            bot.answer_callback_query(call.id, "🚫 Вы заблокированы!",
                                      show_alert=True)
            return

        # Заявка уже на проверке → показать кнопку отмены
        if uid in pending:
            edit(
                f"╭─────────────────────\n"
                f"├ ⏳ <b>Заявка на проверке</b>\n"
                f"├\n"
                f"├ Ваш QR-код отправлен администратору.\n"
                f"├ Дождитесь решения или отмените заявку.\n"
                f"╰─────────────────────",
                pending_menu(),
            )
            return

        # Очередь: пользователь не первый
        if QUEUE_ENABLED:
            if uid not in queue:
                queue.append(uid)
            pos = queue.index(uid) + 1
            if pos > 1:
                edit(queue_text(pos), back_btn())
                return
            # Позиция 1 → переходим к отправке

        edit(submit_price_text(), submit_menu())

    # ── Отмена заявки пользователем ──
    elif data == "cancel_application":
        if uid not in pending:
            bot.answer_callback_query(call.id, "❌ Нет активной заявки!",
                                      show_alert=True)
            return

        # Пометить сообщения у всех администраторов
        for (achat, amsg) in pending_admin_msgs.get(uid, []):
            try:
                bot.edit_message_caption(
                    caption="🚫 <b>ЗАЯВКА ОТМЕНЕНА ПОЛЬЗОВАТЕЛЕМ</b>\n"
                            f"ID: <code>{uid}</code>",
                    chat_id=achat, message_id=amsg, parse_mode="HTML",
                )
            except Exception:
                pass

        _finish_qr_review(uid)

        edit(
            f"╭─────────────────────\n"
            f"├ ✅ <b>Заявка отменена</b>\n"
            f"├\n"
            f"├ Вы добавлены в конец очереди.\n"
            f"├ Когда подойдёт очередь — сможете\n"
            f"├ отправить новый QR-код.\n"
            f"╰─────────────────────",
            back_btn(),
        )

    # ── Прикрепить QR-код ──
    elif data == "attach_qr":
        waiting_for_qr.add(uid)
        edit(
            f"╭─────────────────────\n"
            f'├ <b><tg-emoji emoji-id="5258108352008823107">🎟</tg-emoji> '
            f"Отправьте фото QR-кода</b>\n"
            f"├\n"
            f"├ Просто прикрепите изображение\n"
            f"├ к этому чату\n"
            f"╰─────────────────────",
            back_btn(),
        )

    # ── Отправить QR на проверку ──
    elif data == "send_qr":
        qr_file_id = user.get("_pending_qr")
        if not qr_file_id:
            bot.answer_callback_query(call.id, "❌ Сначала прикрепите QR-код!",
                                      show_alert=True)
            return

        del user["_pending_qr"]

        # Убираем пользователя из очереди — он «использовал» свой ход
        if uid in queue:
            queue.remove(uid)

        pending[uid] = msg_id

        name     = esc(call.from_user.first_name or "—")
        username = f"@{esc(call.from_user.username)}" if call.from_user.username else "—"
        admin_cap = (
            f"╭─────────────────────\n"
            f'├ <b><tg-emoji emoji-id="5258108352008823107">🎟</tg-emoji> '
            f"Новая заявка на сдачу номера</b>\n"
            f"├\n"
            f'├ <tg-emoji emoji-id="5260399854500191689">🎟</tg-emoji> Имя: {name}\n'
            f'├ <tg-emoji emoji-id="5323442290708985472">🎟</tg-emoji> Username: {username}\n'
            f'├ <tg-emoji emoji-id="5282843764451195532">🎟</tg-emoji> ID: <code>{uid}</code>\n'
            f'├ <tg-emoji emoji-id="5440621591387980068">🎟</tg-emoji> '
            f'Дата: {datetime.date.today().strftime("%d.%m.%Y")}\n'
            f'├ <tg-emoji emoji-id="5890848474563352982">🎟</tg-emoji> '
            f'Выплата: <b>${settings["payout"]:.2f}</b>\n'
            f"╰─────────────────────"
        )

        # Рассылаем всем администраторам и запоминаем msg_id
        sent = notify_all_admins(
            photo=qr_file_id,
            caption=admin_cap,
            markup=admin_review_btn(uid),
        )
        pending_admin_msgs[uid] = sent

        edit(
            f"╭─────────────────────\n"
            f'├ <b><tg-emoji emoji-id="5258043150110301407">🎟</tg-emoji> '
            f"Заявка отправлена!</b>\n"
            f"├\n"
            f'├ <tg-emoji emoji-id="5440621591387980068">🎟</tg-emoji> '
            f"Ожидайте решения администратора\n"
            f"├ Мы уведомим вас о результате\n"
            f"╰─────────────────────",
            back_btn(),
        )

    # ── Вывод ──
    elif data == "withdraw":
        if user.get("banned"):
            bot.answer_callback_query(call.id, "🚫 Вы заблокированы!",
                                      show_alert=True)
            return
        if user["balance"] < 1.0:
            bot.answer_callback_query(call.id,
                                      "❌ Недостаточно средств! Минимум $1.00",
                                      show_alert=True)
            return
        user_states[uid] = "waiting_withdraw_amount"
        try:
            if call.message.photo:
                bot.edit_message_caption(
                    caption=withdraw_text(user),
                    chat_id=chat_id, message_id=msg_id,
                    parse_mode="HTML", reply_markup=back_btn("balance"),
                )
            else:
                bot.edit_message_text(
                    withdraw_text(user), chat_id, msg_id,
                    parse_mode="HTML", reply_markup=back_btn("balance"),
                )
        except Exception as e:
            print(f"[withdraw edit] {e}")
            bot.send_message(chat_id, withdraw_text(user),
                             parse_mode="HTML", reply_markup=back_btn("balance"))

    elif data.startswith("withdraw_confirm_"):
        try:
            amount = float(data.split("withdraw_confirm_")[1])
        except Exception:
            return
        if user["balance"] < amount:
            bot.answer_callback_query(call.id, "❌ Недостаточно средств!",
                                      show_alert=True)
            return
        user["balance"] -= amount
        user["history"].append({
            "date":   datetime.date.today().strftime("%d.%m"),
            "amount": -amount,
            "status": "Вывод (ожидание)",
        })
        withdraw_counter[0] += 1
        req_id     = withdraw_counter[0]
        first_name = esc(call.from_user.first_name or "—")
        username   = (f"@{esc(call.from_user.username)}"
                      if call.from_user.username else "—")
        withdraw_requests[req_id] = {
            "user_id":    uid,
            "amount":     amount,
            "status":     "pending",
            "first_name": first_name,
            "username":   username,
        }
        edit(
            f"╭─────────────────────\n"
            f'├ <b><tg-emoji emoji-id="5258043150110301407">🎟</tg-emoji> '
            f"Заявка отправлена!</b>\n"
            f"├\n"
            f'├ <tg-emoji emoji-id="5890848474563352982">🎟</tg-emoji> '
            f"Сумма: <b>${amount:.2f} USDT</b>\n"
            f'├ <tg-emoji emoji-id="6030537810509828330">🎟</tg-emoji> '
            f"Номер заявки: <b>#{req_id}</b>\n"
            f"├\n"
            f"├ Ожидайте — администратор обработает\n"
            f"├ заявку и пришлёт чек CryptoBot\n"
            f"╰─────────────────────",
            back_btn(),
        )
        notify_all_admins(
            withdraw_pending_admin_text(req_id, uid, amount, first_name, username),
            markup=admin_withdraw_btn(req_id),
        )

    elif data.startswith("wd_take_"):
        if not is_admin(uid):
            return
        try:
            req_id = int(data.split("wd_take_")[1])
        except Exception:
            return
        _process_withdraw_take(req_id, chat_id, msg_id)

    elif data.startswith("wd_reject_"):
        if not is_admin(uid):
            return
        try:
            req_id = int(data.split("wd_reject_")[1])
        except Exception:
            return
        _process_withdraw_reject(req_id, chat_id, msg_id)

    # ── Одобрить QR ──
    elif data.startswith("approve_"):
        if not is_admin(uid):
            return
        target_id = int(data.split("_")[1])
        u = get_user(target_id)
        u["balance"]        += settings["payout"]
        u["numbers_rented"] += 1
        u["history"].append({
            "date":   datetime.date.today().strftime("%d.%m"),
            "amount": settings["payout"],
            "status": "Одобрено",
        })

        _finish_qr_review(target_id)   # → в конец очереди

        try:
            bot.send_message(
                target_id,
                f"╭─────────────────────\n"
                f'├ <b><tg-emoji emoji-id="5258215846450305872">🎟</tg-emoji> '
                f"Заявка принята!</b>\n"
                f"├\n"
                f'├ <tg-emoji emoji-id="5890848474563352982">🎟</tg-emoji> '
                f'Начислено: <b>${settings["payout"]:.2f}</b>\n'
                f'├ <tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> '
                f'Ваш баланс: <b>${u["balance"]:.2f}</b>\n'
                f"├\n"
                f"├ Вы добавлены в конец очереди.\n"
                f"╰─────────────────────",
                parse_mode="HTML",
            )
        except Exception:
            pass
        try:
            bot.edit_message_caption(
                caption=call.message.caption
                        + f"\n\n✅ <b>ПРИНЯТО</b> — начислено ${settings['payout']:.2f}",
                chat_id=chat_id, message_id=msg_id, parse_mode="HTML",
            )
        except Exception:
            pass

    # ── Отклонить QR (запрос причины) ──
    elif data.startswith("reject_"):
        if not is_admin(uid):
            return
        target_id             = int(data.split("_")[1])
        admin_states[uid]     = {"action": "reject_reason", "target": target_id}
        try:
            bot.edit_message_caption(
                caption=call.message.caption
                        + "\n\n❌ <b>Отклоняется...</b>\nВведите причину отказа:",
                chat_id=chat_id, message_id=msg_id, parse_mode="HTML",
            )
        except Exception:
            bot.send_message(chat_id, "✏️ Введите причину отказа:")

    # ── Статистика (общая) ──
    elif data == "adm_stats":
        if not is_admin(uid):
            return
        bot.send_message(
            chat_id,
            f"╭─────────────────────\n"
            f"├ 📊 <b>Статистика бота</b>\n"
            f"├\n"
            f"├ 👥 Пользователей: <b>{len(users_db)}</b>\n"
            f"├ 📦 Всего сдано: <b>{sum(u['numbers_rented'] for u in users_db.values())}</b>\n"
            f"├ 💰 На балансах: <b>${sum(u['balance'] for u in users_db.values()):.2f}</b>\n"
            f"├ 🔄 В очереди: <b>{len(queue)}</b>\n"
            f"├ ⏳ На проверке: <b>{len(pending)}</b>\n"
            f"├ 💵 Выплата: <b>${settings['payout']:.2f}</b>\n"
            f"├ 👑 Администраторов: <b>{len(ADMIN_IDS)}</b>\n"
            f"╰─────────────────────",
            parse_mode="HTML",
        )

    # ── Топ-20 пользователей ──
    elif data == "adm_top_stats":
        if not is_admin(uid):
            return
        bot.send_message(chat_id, admin_top_stats_text(), parse_mode="HTML")

    elif data == "adm_check":
        if not is_admin(uid):
            return
        admin_states[uid] = {"action": "check_user"}
        bot.send_message(chat_id, "🔍 Введите ID пользователя:")

    elif data == "adm_give":
        if not is_admin(uid):
            return
        admin_states[uid] = {"action": "give_step1"}
        bot.send_message(chat_id, "💰 Введите ID пользователя для начисления:")

    elif data == "adm_take":
        if not is_admin(uid):
            return
        admin_states[uid] = {"action": "take_step1"}
        bot.send_message(chat_id, "💸 Введите ID пользователя для списания:")

    elif data == "adm_reset_all":
        if not is_admin(uid):
            return
        mk = InlineKeyboardMarkup()
        mk.row(
            InlineKeyboardButton("✅ Да, обнулить", callback_data="adm_reset_confirm"),
            InlineKeyboardButton("❌ Отмена",        callback_data="adm_cancel"),
        )
        bot.send_message(chat_id, "⚠️ <b>Обнулить баланс ВСЕХ пользователей?</b>",
                         parse_mode="HTML", reply_markup=mk)

    elif data == "adm_reset_confirm":
        if not is_admin(uid):
            return
        for u in users_db.values():
            u["balance"] = 0.0
            u["history"].append({
                "date":   datetime.date.today().strftime("%d.%m"),
                "amount": 0,
                "status": "Обнуление",
            })
        try:
            bot.edit_message_text("✅ Балансы всех пользователей обнулены.",
                                  chat_id, msg_id)
        except Exception:
            bot.send_message(chat_id, "✅ Балансы всех пользователей обнулены.")

    elif data == "adm_cancel":
        if not is_admin(uid):
            return
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    elif data == "adm_broadcast":
        if not is_admin(uid):
            return
        admin_states[uid] = {"action": "broadcast"}
        bot.send_message(chat_id, "📢 Введите текст рассылки:")

    elif data == "adm_payout":
        if not is_admin(uid):
            return
        admin_states[uid] = {"action": "set_payout"}
        bot.send_message(
            chat_id,
            f"💵 Текущая выплата: <b>${settings['payout']:.2f}</b>\n\nВведите новую сумму:",
            parse_mode="HTML",
        )

    elif data.startswith("adm_ban_"):
        if not is_admin(uid):
            return
        target_id      = int(data.split("_")[2])
        u              = get_user(target_id)
        u["banned"]    = not u.get("banned", False)
        status         = "🚫 Заблокирован" if u["banned"] else "✅ Разблокирован"
        bot.send_message(chat_id,
                         f"{status}: <code>{target_id}</code>",
                         parse_mode="HTML")
        try:
            bot.send_message(
                target_id,
                "🚫 Вы заблокированы администратором."
                if u["banned"]
                else "✅ Ваш аккаунт разблокирован.",
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    print("✅ Бот Аренда MAX запущен...")
    print(f"   💵 Выплата: ${settings['payout']:.2f}")
    print(f"   👑 Admin IDs: {ADMIN_IDS}")
    bot.infinity_polling()
