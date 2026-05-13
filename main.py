import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# =============================================
#   🔑 НАСТРОЙКИ — вставь свой токен
# =============================================
BOT_TOKEN = "8918670807:AAHFkCF8kemTCIVlbeLfmRkPUd6gk3wdKVo"

bot = telebot.TeleBot(BOT_TOKEN)

# =============================================
#   📦 ТЕСТОВАЯ БД
# =============================================
users_db = {}

def get_user(user_id, first_name="Пользователь"):
    if user_id not in users_db:
        users_db[user_id] = {
            "name": first_name,
            "balance": 0.0,
            "rating": 5.0,
            "numbers_rented": 0,
            "joined": "сегодня",
            "status": "✅ Активен",
            "history": [],
        }
    return users_db[user_id]


# =============================================
#   🎛️ ГЛАВНОЕ МЕНЮ
#   [ 👤 Профиль ]  [ 💰 Баланс ]
#   [    📱 Сдать номер         ]
#   [ 📜 История ]  [ 📊 Статистика ]
# =============================================
def main_menu():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("👤 Профиль",    callback_data="profile"),
        InlineKeyboardButton("💰 Баланс",     callback_data="balance"),
    )
    markup.row(
        InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"),
    )
    markup.row(
        InlineKeyboardButton("📜 История",    callback_data="history"),
        InlineKeyboardButton("📊 Статистика", callback_data="statistics"),
    )
    return markup

def back_btn():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("◀️ Назад в меню", callback_data="back_menu"))
    return markup

def welcome_text(user, name):
    return (
        f"👋 Привет, *{name}*! Добро пожаловать в *Аренда MAX*\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Баланс:*        `{user['balance']:.2f} сум`\n"
        f"⭐ *Рейтинг:*       {user['rating']} / 5.0\n"
        f"📱 *Номеров сдано:* {user['numbers_rented']}\n"
        f"🔖 *Статус:*        {user['status']}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"Выберите нужный раздел 👇"
    )

def dev_text(section_name, emoji):
    return (
        f"{emoji} *{section_name}*\n\n"
        f"🔧 *Раздел в разработке*\n\n"
        f"Мы активно работаем над этой функцией.\n"
        f"Следите за обновлениями! 🚀\n\n"
        f"По вопросам: @max_support"
    )


# =============================================
#   /start
# =============================================
@bot.message_handler(commands=["start"])
def start(message):
    user = get_user(message.from_user.id, message.from_user.first_name)
    name = message.from_user.first_name or "Пользователь"
    bot.send_message(
        message.chat.id,
        welcome_text(user, name),
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

# =============================================
#   /menu
# =============================================
@bot.message_handler(commands=["menu"])
def menu_cmd(message):
    user = get_user(message.from_user.id, message.from_user.first_name)
    name = message.from_user.first_name or "Пользователь"
    bot.send_message(
        message.chat.id,
        welcome_text(user, name),
        parse_mode="Markdown",
        reply_markup=main_menu()
    )


# =============================================
#   🔁 ОБРАБОТЧИК ВСЕХ CALLBACK
# =============================================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    msg_id  = call.message.message_id

    # ── Назад в меню ──────────────────────────
    if call.data == "back_menu":
        user = get_user(call.from_user.id, call.from_user.first_name)
        name = call.from_user.first_name or "Пользователь"
        bot.edit_message_text(
            welcome_text(user, name),
            chat_id, msg_id,
            parse_mode="Markdown",
            reply_markup=main_menu()
        )

    # ── 👤 Профиль ────────────────────────────
    elif call.data == "profile":
        bot.edit_message_text(
            dev_text("ПРОФИЛЬ", "👤"),
            chat_id, msg_id,
            parse_mode="Markdown",
            reply_markup=back_btn()
        )

    # ── 💰 Баланс ─────────────────────────────
    elif call.data == "balance":
        bot.edit_message_text(
            dev_text("БАЛАНС", "💰"),
            chat_id, msg_id,
            parse_mode="Markdown",
            reply_markup=back_btn()
        )

    # ── 📱 Сдать номер ────────────────────────
    elif call.data == "submit_number":
        bot.edit_message_text(
            dev_text("СДАТЬ НОМЕР", "📱"),
            chat_id, msg_id,
            parse_mode="Markdown",
            reply_markup=back_btn()
        )

    # ── 📜 История ────────────────────────────
    elif call.data == "history":
        bot.edit_message_text(
            dev_text("ИСТОРИЯ ОПЕРАЦИЙ", "📜"),
            chat_id, msg_id,
            parse_mode="Markdown",
            reply_markup=back_btn()
        )

    # ── 📊 Статистика ─────────────────────────
    elif call.data == "statistics":
        bot.edit_message_text(
            dev_text("СТАТИСТИКА", "📊"),
            chat_id, msg_id,
            parse_mode="Markdown",
            reply_markup=back_btn()
        )


# =============================================
#   🚀 ЗАПУСК
# =============================================
if __name__ == "__main__":
    print("✅ Бот Аренда MAX запущен...")
    bot.infinity_polling()
