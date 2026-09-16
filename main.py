import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------

# Вставьте сюда токен вашего бота, полученный у @BotFather
BOT_TOKEN = "8841055640:AAE65cYHaE9XVEo2fQLwZ5kPxrR1Fncqm5Q"

IN_DEV_TEXT = "🚧 Этот раздел находится в разработке.\nСкоро здесь появится функционал!"

# --------------------------------------------------------------------------
# Хранилище профилей (временное, in-memory)
# --------------------------------------------------------------------------
# NOTE: пока нет подключения к БД — статистика хранится в памяти процесса
# и обнулится при перезапуске бота. Когда появится база данных, эту часть
# нужно будет заменить на реальные запросы к ней.

USER_PROFILES: dict[int, dict[str, float]] = {}


def get_profile_stats(user_id: int) -> dict[str, float]:
    """Возвращает статистику пользователя, создавая запись по умолчанию."""
    if user_id not in USER_PROFILES:
        USER_PROFILES[user_id] = {
            "balance": 0.0,
            "deposits": 0.0,
            "withdrawals": 0.0,
            "turnover": 0.0,
        }
    return USER_PROFILES[user_id]

# --------------------------------------------------------------------------
# Форматирование
# --------------------------------------------------------------------------


def tree_block(lines: list[str]) -> str:
    """Оформляет список строк в виде соединённого блока: ┌ / ├ / └.

    Используется для любых статистик/списков (профиль, топ, статистика и т.д.),
    чтобы не дублировать разметку вручную в каждом хендлере.
    """
    if not lines:
        return ""
    if len(lines) == 1:
        return f"└ {lines[0]}"

    result = [f"┌ {lines[0]}"]
    result += [f"├ {line}" for line in lines[1:-1]]
    result.append(f"└ {lines[-1]}")
    return "\n".join(result)


# --------------------------------------------------------------------------
# Клавиатуры
# --------------------------------------------------------------------------


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Меню",
                    style="primary",
                    icon_custom_emoji_id="5257965174979042426",
                ),
                KeyboardButton(
                    text="Игры",
                    style="primary",
                    icon_custom_emoji_id="5350708744558753862",
                ),
                KeyboardButton(
                    text="Партнеры",
                    style="primary",
                    icon_custom_emoji_id="5258362837411045098",
                ),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите раздел...",
    )


def menu_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Профиль",
                    callback_data="menu:profile",
                    icon_custom_emoji_id="5316727448644103237",
                ),
                InlineKeyboardButton(
                    text="Статистика",
                    callback_data="menu:stats",
                    icon_custom_emoji_id="5258330865674494479",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Топ",
                    callback_data="menu:top",
                    icon_custom_emoji_id="6037083366438737901",
                ),
                InlineKeyboardButton(
                    text="Чеки",
                    callback_data="menu:checks",
                    icon_custom_emoji_id="6037175527846975726",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Поддержка",
                    callback_data="menu:support",
                    icon_custom_emoji_id="5812150667812280629",
                ),
            ],
        ]
    )


def profile_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data="menu:profile",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="menu:back",
                ),
            ],
        ]
    )


# --------------------------------------------------------------------------
# Хендлеры
# --------------------------------------------------------------------------

router = Router()


def format_profile_text(user_id: int, full_name: str, username: str | None) -> str:
    stats = get_profile_stats(user_id)
    username_line = f"@{username}" if username else "не указан"

    info_block = tree_block(
        [
            f"🆔 <b>ID:</b> <code>{user_id}</code>",
            f"📛 <b>Имя:</b> {full_name}",
            f"🔗 <b>Юзернейм:</b> {username_line}",
        ]
    )
    stats_block = tree_block(
        [
            f"💰 <b>Баланс:</b> {stats['balance']:.2f} ₽",
            f"📥 <b>Всего депозитов:</b> {stats['deposits']:.2f} ₽",
            f"📤 <b>Всего выводов:</b> {stats['withdrawals']:.2f} ₽",
            f"📊 <b>Оборот:</b> {stats['turnover']:.2f} ₽",
        ]
    )

    return (
        '<tg-emoji emoji-id="5316727448644103237">👤</tg-emoji> <b>Профиль</b>\n\n'
        f"{info_block}\n\n"
        f"{stats_block}"
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        f"Привет, {message.from_user.full_name}! 👋\n\n"
        "Выберите раздел из меню ниже.",
        reply_markup=main_reply_keyboard(),
    )


@router.message(F.text == "Меню")
async def show_menu(message: Message) -> None:
    text = (
        '<tg-emoji emoji-id="5260547274957672345">🎲</tg-emoji> '
        "<b>Lucky Dice</b> — испытай удачу!\n\n"
        "<i>Здесь заводят игры, зарабатывают на партнёрке "
        "и следят за своим прогрессом.</i>\n\n"
        '<tg-emoji emoji-id="5886676966102274844">👆</tg-emoji> '
        "Выберите раздел ниже:"
    )
    await message.answer(text, reply_markup=menu_inline_keyboard())


@router.message(F.text == "Игры")
async def games_section(message: Message) -> None:
    await message.answer(IN_DEV_TEXT)


@router.message(F.text == "Партнеры")
async def partners_section(message: Message) -> None:
    await message.answer(IN_DEV_TEXT)


@router.callback_query(F.data == "menu:profile")
async def profile_section(callback: CallbackQuery) -> None:
    user = callback.from_user
    text = format_profile_text(user.id, user.full_name, user.username)

    await callback.message.edit_text(text, reply_markup=profile_inline_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:back")
async def back_to_menu(callback: CallbackQuery) -> None:
    text = (
        '<tg-emoji emoji-id="5260547274957672345">🎲</tg-emoji> '
        "<b>Lucky Dice</b> — испытай удачу!\n\n"
        "<i>Здесь заводят игры, зарабатывают на партнёрке "
        "и следят за своим прогрессом.</i>\n\n"
        '<tg-emoji emoji-id="5886676966102274844">👆</tg-emoji> '
        "Выберите раздел ниже:"
    )
    await callback.message.edit_text(text, reply_markup=menu_inline_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("menu:"))
async def menu_callback(callback: CallbackQuery) -> None:
    await callback.answer("В разработке 🚧", show_alert=True)


# --------------------------------------------------------------------------
# Запуск бота
# --------------------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
