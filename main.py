import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = "8918670807:AAHFkCF8kemTCIVlbeLfmRkPUd6gk3wdKVo"

bot = telebot.TeleBot(BOT_TOKEN)

users_db = {}

def get_user(user_id):
    if user_id not in users_db:
        users_db[user_id] = {
            "balance": 0.0,
            "numbers_rented": 0,
        }
    return users_db[user_id]

def get_status(user):
    return "✅ Активен" if user["numbers_rented"] >= 1 else "❌ Неактивен"

def esc(text):
    """Экранирует спецсимволы HTML"""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def main_menu():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("👤 Профиль",     callback_data="profile"),
        InlineKeyboardButton("💰 Баланс",      callback_data="balance"),
    )
    markup.row(
        InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"),
    )
    markup.row(
        InlineKeyboardButton("📜 История",     callback_data="history"),
        InlineKeyboardButton("📊 Статистика",  callback_data="statistics"),
    )
    return markup

def back_btn():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("◀️ Назад", callback_data="back_menu"))
    return markup

def welcome_text(tg_user, user):
    name     = esc(tg_user.first_name or "—")
    username = f"@{esc(tg_user.username)}" if tg_user.username else "—"
    status   = get_status(user)
    return (
        f"🏠 <b>Аренда MAX</b>\n\n"
        f"👤 Имя:      <b>{name}</b>\n"
        f"🆔 ID:       <code>{tg_user.id}</code>\n"
        f"📎 Username: {username}\n"
        f"💵 Баланс:   <code>${user['balance']:.2f}</code>\n"
        f"📱 Сдано:    {user['numbers_rented']} номеров\n"
        f"🔖 Статус:   {status}\n\n"
        f"Выберите раздел 👇"
    )


@bot.message_handler(commands=["start", "menu"])
def start(message):
    user = get_user(message.from_user.id)
    bot.send_message(
        message.chat.id,
        welcome_text(message.from_user, user),
        parse_mode="HTML",
        reply_markup=main_menu()
    )


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    msg_id  = call.message.message_id

    if call.data == "back_menu":
        user = get_user(call.from_user.id)
        bot.edit_message_text(
            welcome_text(call.from_user, user),
            chat_id, msg_id,
            parse_mode="HTML",
            reply_markup=main_menu()
        )

    elif call.data in ("profile", "balance", "submit_number", "history", "statistics"):
        bot.edit_message_text(
            "🔧 В разработке",
            chat_id, msg_id,
            reply_markup=back_btn()
        )


if __name__ == "__main__":
    print("✅ Бот Аренда MAX запущен...")
    bot.infinity_polling()
