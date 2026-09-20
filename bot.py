import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.types import (
    InputRichMessage,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    RichBlockTableCell,
)

BOT_TOKEN = "8841055640:AAE65cYHaE9XVEo2fQLwZ5kPxrR1Fncqm5Q"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def build_card() -> InputRichMessage:
    # Заголовок карточки
    heading = InputRichBlockSectionHeading(text="Candy Cane #11319", size=1)

    # Таблица: каждая строка — список ячеек
    rows = [
        [
            RichBlockTableCell(align="left", valign="middle", text="🎨 Модель", is_header=True),
            RichBlockTableCell(align="right", valign="middle", text="Golden Jelly • 2.6%"),
        ],
        [
            RichBlockTableCell(align="left", valign="middle", text="✏️ Узор", is_header=True),
            RichBlockTableCell(align="right", valign="middle", text="Anubis • 0.2%"),
        ],
        [
            RichBlockTableCell(align="left", valign="middle", text="🖼 Фон", is_header=True),
            RichBlockTableCell(align="right", valign="middle", text="Khaki Green • 1.5%"),
        ],
    ]
    table = InputRichBlockTable(cells=rows, is_bordered=True, is_striped=False)

    return InputRichMessage(blocks=[heading, table])


def build_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Посмотреть подарок ↗", url="https://t.me/nft/CandyCane-11319")],
            [InlineKeyboardButton(text="🛍 Купить за $6.81", callback_data="buy_11319")],
            [InlineKeyboardButton(text="← Назад", callback_data="back")],
        ]
    )


@dp.message(CommandStart())
async def start(message: Message):
    await bot.send_rich_message(
        chat_id=message.chat.id,
        rich_message=build_card(),
        reply_markup=build_keyboard(),
    )


@dp.callback_query(F.data == "buy_11319")
async def on_buy(call: CallbackQuery):
    await call.answer("Покупка оформляется…")
    # тут — логика оплаты / инвойса


@dp.callback_query(F.data == "back")
async def on_back(call: CallbackQuery):
    await call.message.delete()
    await call.answer()


async def main():
    # удаляем webhook (если был установлен) и накопившиеся апдейты,
    # иначе getUpdates/polling будет конфликтовать с ним
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
