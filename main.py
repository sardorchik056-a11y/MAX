import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto

# =============================================
#   🔑 ТОКЕН БОТА
# =============================================
BOT_TOKEN = "8918670807:AAHFkCF8kemTCIVlbeLfmRkPUd6gk3wdKVo"

# =============================================
#   👑 ADMIN ID — вставь свой Telegram ID
# =============================================
ADMIN_ID = 8118184388  # <- замени на свой ID

# =============================================
#   💰 НАСТРОЙКИ
# =============================================
PAYOUT_AMOUNT = 5.0          # $ за сдачу номера (меняется в /admin)
QUEUE_ENABLED = True          # включить очередь

# =============================================
#   🎨 КАСТОМНЫЕ ЭМОДЗИ ID
# =============================================
EMOJI_RULES    = "5260399854500191689"
EMOJI_BALANCE  = "5258204546391351475"
EMOJI_SUBMIT   = "5449407131675558756"
EMOJI_HISTORY  = "6030776052345737530"
EMOJI_STATS    = "5258330865674494479"
EMOJI_BACK     = "6039539366177541657"
EMOJI_ADMIN    = "5258185631355378853"
EMOJI_CHECK    = "5282843764451195532"
EMOJI_QUEUE    = "5323442290708985472"

# =============================================
#   🖼️ BANNER FILE_ID
# =============================================
BANNER_FILE_ID = "AgACAgIAAxkBAAMeagQukWF_Zj77_eYNPXcEywJNg0EAAg4Taxs1GSBI3gdnW__fsXUBAAMCAAN5AAM7BA"

# =============================================
#   📊 БАЗА ДАННЫХ (в памяти)
# =============================================
users_db = {}       # {user_id: {...}}
queue    = []       # [user_id, ...]  — очередь
pending  = {}       # {user_id: message_id}  — ожидают проверки
settings = {
    "payout": PAYOUT_AMOUNT,
    "rules": (
        "📋 <b>Правила сервиса:</b>\n\n"
        "├ <b>1.</b> Номер должен быть зарегистрирован на вас\n"
        "├ <b>2.</b> Номер не должен быть заблокирован\n"
        "├ <b>3.</b> QR-код должен быть чётким и читаемым\n"
        "├ <b>4.</b> Одна заявка в день с одного аккаунта\n"
        "├ <b>5.</b> При нарушении — бан без предупреждения\n"
        "╰ <b>6.</b> Выплата производится после проверки"
    )
}

# Состояния пользователей
user_states = {}   # {user_id: state_string}
waiting_for_photo  = set()
waiting_for_qr     = set()   # ждут QR-фото для submit
admin_states       = {}      # {admin_id: {"action": ..., "target": ...}}

bot = telebot.TeleBot(BOT_TOKEN)

# =============================================
#   🔧 УТИЛИТЫ
# =============================================
def get_user(user_id):
    if user_id not in users_db:
        users_db[user_id] = {
            "balance":        0.0,
            "numbers_rented": 0,
            "history":        [],  # [{date, amount, status}]
            "banned":         False,
            "username":       "",
            "first_name":     "",
        }
    return users_db[user_id]

def get_status(user):
    return "✅ Активен" if user["numbers_rented"] >= 1 else "⏳ Новичок"

def esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def is_admin(user_id):
    return user_id == ADMIN_ID

def em(eid, fallback="⭐"):
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'


# =============================================
#   📝 ТЕКСТЫ
# =============================================
def welcome_text(tg_user, user):
    name     = esc(tg_user.first_name or "—")
    username = f"@{esc(tg_user.username)}" if tg_user.username else "—"
    return (
        f"╭─────────────────\n"
        f'├ <b><tg-emoji emoji-id="5260399854500191689">🎟</tg-emoji> {name}\n'
        f'├ <tg-emoji emoji-id="5282843764451195532">🎟</tg-emoji> ID: <code>{tg_user.id}</code>\n'
        f'├ <tg-emoji emoji-id="5323442290708985472">🎟</tg-emoji> : {username}\n'
        f'├\n'
        f'├ <tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> Баланс: ${user["balance"]:.2f}\n'
        f'├ <tg-emoji emoji-id="5449407131675558756">🎟</tg-emoji> Сдано: {user["numbers_rented"]} номеров\n'
        f'├ <tg-emoji emoji-id="5258185631355378853">🎟</tg-emoji> Статус: {get_status(user)}</b>\n'
        f"╰─────────────────"
    )

def rules_text():
    return settings["rules"]

def balance_text(user):
    history_lines = ""
    if user["history"]:
        last = user["history"][-5:][::-1]
        for h in last:
            sign = "+" if h["amount"] > 0 else ""
            history_lines += f"├ {h['date']} — <b>{sign}${h['amount']:.2f}</b> ({h['status']})\n"
    else:
        history_lines = "├ История пуста\n"
    return (
        f"╭─────────────────────\n"
        f"├ {em(EMOJI_BALANCE,'💰')} <b>Ваш баланс</b>\n"
        f"├\n"
        f"├ 💵 Доступно: <b>${user['balance']:.2f}</b>\n"
        f"├\n"
        f"├ 📜 <b>Последние операции:</b>\n"
        f"{history_lines}"
        f"╰─────────────────────"
    )

def queue_text(pos):
    return (
        f"╭─────────────────────\n"
        f"├ {em(EMOJI_QUEUE,'🔄')} <b>Вы добавлены в очередь!</b>\n"
        f"├\n"
        f"├ 📍 Ваша позиция: <b>#{pos}</b>\n"
        f"├ ⏳ Ожидайте — мы уведомим вас\n"
        f"├    когда подойдёт ваша очередь\n"
        f"╰─────────────────────"
    )

def submit_price_text():
    amt = settings["payout"]
    return (
        f"╭─────────────────────\n"
        f"├ {em(EMOJI_SUBMIT,'📦')} <b>Сдать номер</b>\n"
        f"├\n"
        f"├ 💵 Выплата за номер: <b>${amt:.2f}</b>\n"
        f"├\n"
        f"├ 📸 Прикрепите QR-код номера\n"
        f"├    и нажмите кнопку ниже\n"
        f"╰─────────────────────"
    )

def history_text(user):
    if not user["history"]:
        body = "├ История операций пуста\n"
    else:
        body = ""
        for h in reversed(user["history"][-10:]):
            sign = "+" if h["amount"] > 0 else ""
            body += f"├ {h['date']} {sign}${h['amount']:.2f} — {h['status']}\n"
    return (
        f"╭─────────────────────\n"
        f"├ {em(EMOJI_HISTORY,'📜')} <b>История операций</b>\n"
        f"├\n"
        f"{body}"
        f"╰─────────────────────"
    )

def statistics_text():
    total_users   = len(users_db)
    total_rented  = sum(u["numbers_rented"] for u in users_db.values())
    total_paid    = sum(u["balance"] for u in users_db.values())
    queue_count   = len(queue)
    pending_count = len(pending)
    return (
        f"╭─────────────────────\n"
        f"├ {em(EMOJI_STATS,'📊')} <b>Статистика</b>\n"
        f"├\n"
        f"├ 👥 Пользователей: <b>{total_users}</b>\n"
        f"├ 📦 Сдано номеров: <b>{total_rented}</b>\n"
        f"├ 💰 Выплачено: <b>${total_paid:.2f}</b>\n"
        f"├ 🔄 В очереди: <b>{queue_count}</b>\n"
        f"├ ⏳ Ожидают проверки: <b>{pending_count}</b>\n"
        f"╰─────────────────────"
    )

# =============================================
#   ⌨️ КЛАВИАТУРЫ
# =============================================
def main_menu():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("Правила",     callback_data="rules",         icon_custom_emoji_id=EMOJI_RULES),
        InlineKeyboardButton("Баланс",      callback_data="balance",       icon_custom_emoji_id=EMOJI_BALANCE),
    )
    markup.row(
        InlineKeyboardButton("Сдать номер", callback_data="submit_number", icon_custom_emoji_id=EMOJI_SUBMIT),
    )
    markup.row(
        InlineKeyboardButton("История",     callback_data="history",       icon_custom_emoji_id=EMOJI_HISTORY),
        InlineKeyboardButton("Статистика",  callback_data="statistics",    icon_custom_emoji_id=EMOJI_STATS),
    )
    return markup

def back_btn(target="back_menu"):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("◀️ Назад", callback_data=target, icon_custom_emoji_id=EMOJI_BACK))
    return markup

def submit_menu():
    """Кнопка 'Прикрепить QR-код'"""
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("📷 Прикрепить QR-код", callback_data="attach_qr"))
    markup.row(InlineKeyboardButton("◀️ Назад", callback_data="back_menu", icon_custom_emoji_id=EMOJI_BACK))
    return markup

def send_qr_btn():
    """Кнопка 'Отправить' после прикрепления QR"""
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("✅ Отправить заявку", callback_data="send_qr"))
    markup.row(InlineKeyboardButton("🔄 Изменить фото",   callback_data="attach_qr"))
    markup.row(InlineKeyboardButton("◀️ Назад",           callback_data="back_menu", icon_custom_emoji_id=EMOJI_BACK))
    return markup

def admin_review_btn(user_id):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✅ Принять",  callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{user_id}"),
    )
    return markup

def admin_panel_menu():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("📊 Статистика",       callback_data="adm_stats"))
    markup.row(
        InlineKeyboardButton("🔍 Проверка юзера",  callback_data="adm_check"),
        InlineKeyboardButton("💰 Выдать баланс",   callback_data="adm_give"),
    )
    markup.row(
        InlineKeyboardButton("➖ Снять баланс",    callback_data="adm_take"),
        InlineKeyboardButton("🔄 Обнулить всех",   callback_data="adm_reset_all"),
    )
    markup.row(InlineKeyboardButton("📢 Рассылка",         callback_data="adm_broadcast"))
    markup.row(InlineKeyboardButton("💵 Изменить выплату", callback_data="adm_payout"))
    return markup


# =============================================
#   📸 /getfileid
# =============================================
@bot.message_handler(commands=["getfileid"])
def cmd_getfileid(message):
    waiting_for_photo.add(message.from_user.id)
    bot.send_message(message.chat.id, "📸 Отправь фото — верну <b>file_id</b>", parse_mode="HTML")


# =============================================
#   👑 /admin
# =============================================
@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        return
    bot.send_message(
        message.chat.id,
        f"╭─────────────────────\n"
        f"├ {em(EMOJI_ADMIN,'👑')} <b>Панель администратора</b>\n"
        f"├\n"
        f"├ 💵 Выплата за номер: <b>${settings['payout']:.2f}</b>\n"
        f"├ 👥 Пользователей: <b>{len(users_db)}</b>\n"
        f"╰─────────────────────",
        parse_mode="HTML",
        reply_markup=admin_panel_menu()
    )


# =============================================
#   🚀 /start
# =============================================
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
        bot.send_photo(message.chat.id, BANNER_FILE_ID, caption=text, parse_mode="HTML", reply_markup=main_menu())
    else:
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=main_menu())


# =============================================
#   📸 ОБРАБОТЧИК ФОТО
# =============================================
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    uid = message.from_user.id

    # /getfileid режим
    if uid in waiting_for_photo:
        waiting_for_photo.discard(uid)
        file_id = message.photo[-1].file_id
        bot.send_message(
            message.chat.id,
            f"✅ <b>file_id</b>:\n\n<code>{file_id}</code>",
            parse_mode="HTML"
        )
        return

    # Ожидаем QR-код для заявки
    if uid in waiting_for_qr:
        waiting_for_qr.discard(uid)
        file_id = message.photo[-1].file_id
        # Сохраняем временно
        get_user(uid)["_pending_qr"] = file_id

        bot.send_photo(
            message.chat.id,
            file_id,
            caption=(
                f"╭─────────────────────\n"
                f"├ 📸 <b>QR-код получен!</b>\n"
                f"├\n"
                f"├ Проверьте фото и нажмите\n"
                f"├ <b>«Отправить заявку»</b>\n"
                f"╰─────────────────────"
            ),
            parse_mode="HTML",
            reply_markup=send_qr_btn()
        )
        return


# =============================================
#   📨 ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ (admin)
# =============================================
@bot.message_handler(content_types=["text"])
def handle_text(message):
    uid = message.from_user.id

    if uid not in admin_states:
        return

    state  = admin_states[uid]
    action = state.get("action")
    text   = message.text.strip()

    # --- Рассылка ---
    if action == "broadcast":
        del admin_states[uid]
        count = 0
        for u_id in list(users_db.keys()):
            try:
                bot.send_message(u_id, f"📢 <b>Сообщение от администратора:</b>\n\n{text}", parse_mode="HTML")
                count += 1
            except Exception:
                pass
        bot.send_message(message.chat.id, f"✅ Рассылка отправлена <b>{count}</b> пользователям.", parse_mode="HTML")

    # --- Проверка юзера ---
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
        bot.send_message(
            message.chat.id,
            f"╭─────────────────────\n"
            f"├ 👤 <b>Пользователь #{target_id}</b>\n"
            f"├\n"
            f"├ 📛 Имя: {esc(u['first_name'])}\n"
            f"├ 🔗 Username: @{esc(u['username']) if u['username'] else '—'}\n"
            f"├ 💰 Баланс: ${u['balance']:.2f}\n"
            f"├ 📦 Сдано: {u['numbers_rented']}\n"
            f"├ 🚫 Бан: {'Да' if u.get('banned') else 'Нет'}\n"
            f"╰─────────────────────",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup().row(
                InlineKeyboardButton("🚫 Забанить" if not u.get("banned") else "✅ Разбанить",
                                     callback_data=f"adm_ban_{target_id}")
            )
        )

    # --- Выдать баланс: шаг 1 — ID ---
    elif action == "give_step1":
        try:
            admin_states[uid] = {"action": "give_step2", "target": int(text)}
            bot.send_message(message.chat.id, "💵 Введите сумму (например: 10):")
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите числовой ID")
            del admin_states[uid]

    # --- Выдать баланс: шаг 2 — сумма ---
    elif action == "give_step2":
        try:
            amount    = float(text)
            target_id = state["target"]
            u         = get_user(target_id)
            u["balance"] += amount
            import datetime
            u["history"].append({"date": datetime.date.today().strftime("%d.%m"), "amount": amount, "status": "Пополнение"})
            del admin_states[uid]
            bot.send_message(message.chat.id, f"✅ Начислено <b>${amount:.2f}</b> пользователю <code>{target_id}</code>", parse_mode="HTML")
            try:
                bot.send_message(target_id, f"💰 На ваш баланс начислено <b>${amount:.2f}</b>!", parse_mode="HTML")
            except Exception:
                pass
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите корректную сумму")
            del admin_states[uid]

    # --- Снять баланс: шаг 1 ---
    elif action == "take_step1":
        try:
            admin_states[uid] = {"action": "take_step2", "target": int(text)}
            bot.send_message(message.chat.id, "💸 Введите сумму для списания:")
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите числовой ID")
            del admin_states[uid]

    # --- Снять баланс: шаг 2 ---
    elif action == "take_step2":
        try:
            amount    = float(text)
            target_id = state["target"]
            u         = get_user(target_id)
            u["balance"] = max(0, u["balance"] - amount)
            import datetime
            u["history"].append({"date": datetime.date.today().strftime("%d.%m"), "amount": -amount, "status": "Списание"})
            del admin_states[uid]
            bot.send_message(message.chat.id, f"✅ Списано <b>${amount:.2f}</b> у пользователя <code>{target_id}</code>", parse_mode="HTML")
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите корректную сумму")
            del admin_states[uid]

    # --- Изменить выплату ---
    elif action == "set_payout":
        try:
            amount = float(text)
            settings["payout"] = amount
            del admin_states[uid]
            bot.send_message(message.chat.id, f"✅ Выплата за номер изменена на <b>${amount:.2f}</b>", parse_mode="HTML")
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите корректную сумму")
            del admin_states[uid]

    # --- Причина отклонения ---
    elif action == "reject_reason":
        target_id = state["target"]
        reason    = text
        del admin_states[uid]
        # Убираем pending
        pending.pop(target_id, None)
        try:
            bot.send_message(
                target_id,
                f"╭─────────────────────\n"
                f"├ ❌ <b>Ваша заявка отклонена</b>\n"
                f"├\n"
                f"├ 📝 Причина: {esc(reason)}\n"
                f"╰─────────────────────",
                parse_mode="HTML"
            )
        except Exception:
            pass
        bot.send_message(message.chat.id, "✅ Заявка отклонена, пользователь уведомлён.")


# =============================================
#   🔘 CALLBACK HANDLER
# =============================================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    bot.answer_callback_query(call.id)
    uid     = call.from_user.id
    chat_id = call.message.chat.id
    msg_id  = call.message.message_id
    data    = call.data

    user = get_user(uid)

    # ---- УТИЛИТА редактирования ----
    def edit(text, markup=None):
        try:
            if call.message.photo:
                bot.edit_message_caption(caption=text, chat_id=chat_id, message_id=msg_id,
                                         parse_mode="HTML", reply_markup=markup)
            else:
                bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass

    # =========================================================
    #   ГЛАВНОЕ МЕНЮ
    # =========================================================
    if data == "back_menu":
        text = welcome_text(call.from_user, user)
        try:
            if BANNER_FILE_ID:
                bot.edit_message_media(
                    InputMediaPhoto(BANNER_FILE_ID, caption=text, parse_mode="HTML"),
                    chat_id, msg_id, reply_markup=main_menu()
                )
            else:
                edit(text, main_menu())
        except Exception:
            edit(text, main_menu())

    # =========================================================
    #   ПРАВИЛА
    # =========================================================
    elif data == "rules":
        edit(rules_text(), back_btn())

    # =========================================================
    #   БАЛАНС
    # =========================================================
    elif data == "balance":
        edit(balance_text(user), back_btn())

    # =========================================================
    #   ИСТОРИЯ
    # =========================================================
    elif data == "history":
        edit(history_text(user), back_btn())

    # =========================================================
    #   СТАТИСТИКА
    # =========================================================
    elif data == "statistics":
        edit(statistics_text(), back_btn())

    # =========================================================
    #   СДАТЬ НОМЕР
    # =========================================================
    elif data == "submit_number":
        if user.get("banned"):
            bot.answer_callback_query(call.id, "🚫 Вы заблокированы!", show_alert=True)
            return

        # Проверяем: есть ли уже pending заявка
        if uid in pending:
            edit(
                f"╭─────────────────────\n"
                f"├ ⏳ <b>Заявка уже на проверке</b>\n"
                f"├\n"
                f"├ Дождитесь решения администратора\n"
                f"╰─────────────────────",
                back_btn()
            )
            return

        # Проверяем очередь
        if QUEUE_ENABLED and len(queue) > 0 and uid not in queue:
            # Есть очередь — добавляем
            queue.append(uid)
            pos = queue.index(uid) + 1
            edit(queue_text(pos), back_btn())
            return

        if uid in queue:
            pos = queue.index(uid) + 1
            edit(queue_text(pos), back_btn())
            return

        # Очереди нет — показываем цену и кнопку QR
        edit(submit_price_text(), submit_menu())

    # =========================================================
    #   ПРИКРЕПИТЬ QR — переходим в режим ожидания фото
    # =========================================================
    elif data == "attach_qr":
        waiting_for_qr.add(uid)
        edit(
            f"╭─────────────────────\n"
            f"├ 📸 <b>Отправьте фото QR-кода</b>\n"
            f"├\n"
            f"├ Просто прикрепите изображение\n"
            f"├ к этому чату\n"
            f"╰─────────────────────",
            back_btn()
        )

    # =========================================================
    #   ОТПРАВИТЬ ЗАЯВКУ АДМИНУ
    # =========================================================
    elif data == "send_qr":
        qr_file_id = user.get("_pending_qr")
        if not qr_file_id:
            bot.answer_callback_query(call.id, "❌ Сначала прикрепите QR-код!", show_alert=True)
            return

        # Убираем временный QR
        del user["_pending_qr"]
        pending[uid] = msg_id

        import datetime
        name     = esc(call.from_user.first_name or "—")
        username = f"@{esc(call.from_user.username)}" if call.from_user.username else "—"
        admin_caption = (
            f"╭─────────────────────\n"
            f"├ 📦 <b>Новая заявка на сдачу номера</b>\n"
            f"├\n"
            f"├ 👤 Имя: {name}\n"
            f"├ 🔗 Username: {username}\n"
            f"├ 🆔 ID: <code>{uid}</code>\n"
            f"├ 📅 Дата: {datetime.date.today().strftime('%d.%m.%Y')}\n"
            f"├ 💰 Выплата: <b>${settings['payout']:.2f}</b>\n"
            f"╰─────────────────────"
        )
        try:
            bot.send_photo(
                ADMIN_ID,
                qr_file_id,
                caption=admin_caption,
                parse_mode="HTML",
                reply_markup=admin_review_btn(uid)
            )
        except Exception as e:
            print(f"Ошибка отправки админу: {e}")

        edit(
            f"╭─────────────────────\n"
            f"├ ✅ <b>Заявка отправлена!</b>\n"
            f"├\n"
            f"├ ⏳ Ожидайте решения администратора\n"
            f"├ Мы уведомим вас о результате\n"
            f"╰─────────────────────",
            back_btn()
        )

    # =========================================================
    #   ADMIN: ПРИНЯТЬ ЗАЯВКУ
    # =========================================================
    elif data.startswith("approve_"):
        if not is_admin(uid):
            return
        target_id = int(data.split("_")[1])
        u = get_user(target_id)
        u["balance"]        += settings["payout"]
        u["numbers_rented"] += 1
        import datetime
        u["history"].append({
            "date":   datetime.date.today().strftime("%d.%m"),
            "amount": settings["payout"],
            "status": "Одобрено"
        })
        pending.pop(target_id, None)
        # Убираем из очереди если был
        if target_id in queue:
            queue.remove(target_id)

        # Уведомляем пользователя
        try:
            bot.send_message(
                target_id,
                f"╭─────────────────────\n"
                f"├ ✅ <b>Заявка принята!</b>\n"
                f"├\n"
                f"├ 💰 Начислено: <b>${settings['payout']:.2f}</b>\n"
                f"├ 💵 Ваш баланс: <b>${u['balance']:.2f}</b>\n"
                f"╰─────────────────────",
                parse_mode="HTML"
            )
        except Exception:
            pass

        # Редактируем сообщение у админа
        try:
            bot.edit_message_caption(
                caption=call.message.caption + f"\n\n✅ <b>ПРИНЯТО</b> — начислено ${settings['payout']:.2f}",
                chat_id=chat_id, message_id=msg_id, parse_mode="HTML"
            )
        except Exception:
            pass

    # =========================================================
    #   ADMIN: ОТКЛОНИТЬ ЗАЯВКУ
    # =========================================================
    elif data.startswith("reject_"):
        if not is_admin(uid):
            return
        target_id = int(data.split("_")[1])
        admin_states[uid] = {"action": "reject_reason", "target": target_id}

        try:
            bot.edit_message_caption(
                caption=call.message.caption + "\n\n❌ <b>Отклоняется...</b>\nВведите причину отказа:",
                chat_id=chat_id, message_id=msg_id, parse_mode="HTML"
            )
        except Exception:
            bot.send_message(chat_id, "✏️ Введите причину отказа:")

    # =========================================================
    #   ADMIN PANEL CALLBACKS
    # =========================================================
    elif data == "adm_stats":
        if not is_admin(uid):
            return
        total_users   = len(users_db)
        total_rented  = sum(u["numbers_rented"] for u in users_db.values())
        total_balance = sum(u["balance"] for u in users_db.values())
        bot.send_message(
            chat_id,
            f"╭─────────────────────\n"
            f"├ 📊 <b>Статистика бота</b>\n"
            f"├\n"
            f"├ 👥 Всего пользователей: <b>{total_users}</b>\n"
            f"├ 📦 Всего сдано: <b>{total_rented}</b>\n"
            f"├ 💰 На балансах: <b>${total_balance:.2f}</b>\n"
            f"├ 🔄 В очереди: <b>{len(queue)}</b>\n"
            f"├ ⏳ На проверке: <b>{len(pending)}</b>\n"
            f"├ 💵 Выплата: <b>${settings['payout']:.2f}</b>\n"
            f"╰─────────────────────",
            parse_mode="HTML"
        )

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
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("✅ Да, обнулить", callback_data="adm_reset_confirm"),
            InlineKeyboardButton("❌ Отмена",        callback_data="adm_cancel"),
        )
        bot.send_message(chat_id, "⚠️ <b>Обнулить баланс ВСЕХ пользователей?</b>", parse_mode="HTML", reply_markup=markup)

    elif data == "adm_reset_confirm":
        if not is_admin(uid):
            return
        import datetime
        for u in users_db.values():
            u["balance"] = 0.0
            u["history"].append({"date": datetime.date.today().strftime("%d.%m"), "amount": 0, "status": "Обнуление"})
        try:
            bot.edit_message_text("✅ Балансы всех пользователей обнулены.", chat_id, msg_id)
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
        bot.send_message(chat_id, f"💵 Текущая выплата: <b>${settings['payout']:.2f}</b>\n\nВведите новую сумму:", parse_mode="HTML")

    elif data.startswith("adm_ban_"):
        if not is_admin(uid):
            return
        target_id = int(data.split("_")[2])
        u = get_user(target_id)
        u["banned"] = not u.get("banned", False)
        status = "🚫 Заблокирован" if u["banned"] else "✅ Разблокирован"
        bot.send_message(chat_id, f"{status}: <code>{target_id}</code>", parse_mode="HTML")
        try:
            bot.send_message(target_id, "🚫 Вы заблокированы администратором." if u["banned"] else "✅ Ваш аккаунт разблокирован.")
        except Exception:
            pass


if __name__ == "__main__":
    print("✅ Бот Аренда MAX запущен...")
    print(f"   💵 Выплата: ${settings['payout']:.2f}")
    print(f"   👑 Admin ID: {ADMIN_ID}")
    bot.infinity_polling()
