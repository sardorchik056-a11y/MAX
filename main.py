import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# =============================================
#   🔑 ТОКЕН БОТА
# =============================================
BOT_TOKEN = "8918670807:AAHFkCF8kemTCIVlbeLfmRkPUd6gk3wdKVo"

# =============================================
#   🎨 КАСТОМНЫЕ ЭМОДЗИ ID — вставь свои
# =============================================
EMOJI_PROFILE  = "5260399854500191689"
EMOJI_BALANCE  = "5258204546391351475"
EMOJI_SUBMIT   = "5449407131675558756"
EMOJI_HISTORY  = "6030776052345737530"
EMOJI_STATS    = "5258330865674494479"
EMOJI_BACK     = "6039539366177541657"

# =============================================
#   🖼️ BANNER FILE_ID — вставь после /getfileid
# =============================================
BANNER_FILE_ID = "AgACAgIAAxkBAAMeagQukWF_Zj77_eYNPXcEywJNg0EAAg4Taxs1GSBI3gdnW__fsXUBAAMCAAN5AAM7BA"  # <- сюда вставишь полученный file_id

bot = telebot.TeleBot(BOT_TOKEN)

# Храним ID пользователей, которые вызвали /getfileid
waiting_for_photo = set()

users_db = {}

def get_user(user_id):
    if user_id not in users_db:
        users_db[user_id] = {
            "balance": 0.0,
            "numbers_rented": 0,
        }
    return users_db[user_id]

def get_status(user):
    return "Активен" if user["numbers_rented"] >= 1 else "Неактивен"

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
        f'├ <b><tg-emoji emoji-id="5260399854500191689">🎟</tg-emoji> {name}\n'
        f'├ <tg-emoji emoji-id="5282843764451195532">🎟</tg-emoji> ID: <code>{tg_user.id}</code>\n'
        f'├ <tg-emoji emoji-id="5323442290708985472">🎟</tg-emoji> : {username}\n'
        f'├\n'
        f'├ <tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> Баланс: ${user["balance"]:.2f}\n'
        f'├ <tg-emoji emoji-id="5449407131675558756">🎟</tg-emoji> Сдано: {user["numbers_rented"]} номеров\n'
        f'├ <tg-emoji emoji-id="5258185631355378853">🎟</tg-emoji> Статус: {get_status(user)}</b>\n'
        f"╰─────────────────"
    )


# =============================================
#   📸 /getfileid — получить file_id баннера
# =============================================
@bot.message_handler(commands=["getfileid"])
def cmd_getfileid(message):
    waiting_for_photo.add(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "📸 Теперь отправь фото — я верну его <b>file_id</b>",
        parse_mode="HTML"
    )


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    user_id = message.from_user.id

    if user_id in waiting_for_photo:
        waiting_for_photo.discard(user_id)
        file_id = message.photo[-1].file_id  # берём самое высокое качество
        bot.send_message(
            message.chat.id,
            f"✅ Твой <b>file_id</b>:\n\n<code>{file_id}</code>\n\n"
            f"Скопируй и вставь в код в переменную <code>BANNER_FILE_ID</code>",
            parse_mode="HTML"
        )
    # если не в режиме ожидания — просто игнорируем фото


# =============================================
#   🚀 /start
# =============================================
@bot.message_handler(commands=["start", "menu"])
def start(message):
    user = get_user(message.from_user.id)
    text = welcome_text(message.from_user, user)

    if BANNER_FILE_ID:
        bot.send_photo(
            message.chat.id,
            BANNER_FILE_ID,
            caption=text,
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    else:
        bot.send_message(
            message.chat.id,
            text,
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
        text = welcome_text(call.from_user, user)

        if BANNER_FILE_ID:
            # Если сообщение было с фото — редактируем медиа
            try:
                from telebot.types import InputMediaPhoto
                bot.edit_message_media(
                    InputMediaPhoto(BANNER_FILE_ID, caption=text, parse_mode="HTML"),
                    chat_id, msg_id,
                    reply_markup=main_menu()
                )
            except Exception:
                bot.edit_message_caption(
                    caption=text,
                    chat_id=chat_id,
                    message_id=msg_id,
                    parse_mode="HTML",
                    reply_markup=main_menu()
                )
        else:
            bot.edit_message_text(
                text,
                chat_id, msg_id,
                parse_mode="HTML",
                reply_markup=main_menu()
            )

    elif call.data in ("profile", "balance", "submit_number", "history", "statistics"):
        bot.edit_message_text(
            "🔧 В разработке",
            chat_id, msg_id,
            reply_markup=back_btn()
        ) if not BANNER_FILE_ID else bot.edit_message_caption(
            caption="🔧 В разработке",
            chat_id=chat_id,
            message_id=msg_id,
            reply_markup=back_btn()
        )


if __name__ == "__main__":
    print("✅ Бот Аренда MAX запущен...")
    bot.infinity_polling()
