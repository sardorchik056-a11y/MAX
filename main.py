import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# =============================================
#   🔑 ТОКЕН БОТА
# =============================================
BOT_TOKEN = "8918670807:AAHFkCF8kemTCIVlbeLfmRkPUd6gk3wdKVo"

# =============================================
#   🎨 КАСТОМНЫЕ ЭМОДЗИ ID — вставь свои
# =============================================
EMOJI_PROFILE  = "5260399854500191689"  # <- вставь свой ID
EMOJI_BALANCE  = "5258204546391351475"  # <- вставь свой ID
EMOJI_SUBMIT   = "5449407131675558756"  # <- вставь свой ID
EMOJI_HISTORY  = "6030776052345737530"  # <- вставь свой ID
EMOJI_STATS    = "5258330865674494479"  # <- вставь свой ID
EMOJI_BACK     = "6039539366177541657"  # <- вставь свой ID

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
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# =============================================
#   🎛️ МЕНЮ С КАСТОМНЫМИ ЭМОДЗИ
# =============================================
def main_menu():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("Профиль",     callback_data="profile",       icon_custom_emoji_id=EMOJI_PROFILE),
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

def back_btn():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("Назад", callback_data="back_menu", icon_custom_emoji_id=EMOJI_BACK))
    return markup

def welcome_text(tg_user, user):
    name     = esc(tg_user.first_name or "—")
    username = f"@{esc(tg_user.username)}" if tg_user.username else "—"
    return (
        f"╭─────────────────\n"
        f'├ <b><tg-emoji emoji-id="5260399854500191689">🎟</tg-emoji> <b>{name}</b>\n'
        f'├ <tg-emoji emoji-id="5282843764451195532">🎟</tg-emoji> ID: <code>{tg_user.id}</code>\n'
        f'├ <tg-emoji emoji-id="5323442290708985472">🎟</tg-emoji> : {username}\n'
        f'├'
        f'├ <tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> Баланс: <code>${user['balance']:.2f}</code>\n'
        f'├ <tg-emoji emoji-id="5449407131675558756">🎟</tg-emoji> Сдано: {user['numbers_rented']} номеров\n'
        f'├ <tg-emoji emoji-id="5258185631355378853">🎟</tg-emoji> Статус: {get_status(user)}</b>\n'
        f"╰─────────────────\n"
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
