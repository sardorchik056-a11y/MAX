"""
ui.py — картинка главного меню и «умное» редактирование сообщений.

Зачем нужен:
    Если меню отправлено как ФОТО с подписью, то обычный message.edit_text() на нём падает
    («there is no text in the message to edit») — у фото редактируется подпись (edit_caption).
    Поэтому все экраны бота редактируются через edit_any(): он сам выбирает между
    edit_text и edit_caption, так что кнопки «Профиль», «Статистика», «Назад» и т.д.
    одинаково работают и на текстовом меню, и на меню с картинкой.

Что внутри:
    • get_menu_image / set_menu_image — file_id картинки меню (хранится в menu_image.json,
      переживает перезапуск бота). Устанавливается админской командой /img в main.py.
    • send_menu        — отправить меню (с картинкой, если она задана).
    • show_menu_screen — вернуться в меню (кнопка «Назад»): редактирует подпись, а если тип
                         сообщения не совпадает (текст ↔ фото) — заменяет его новым.
    • edit_any         — отредактировать сообщение-экран (текст или подпись).
    • edit_by_id       — то же, но по chat_id/message_id (для фоновых обновлений).

Лимит подписи к фото в Telegram — 1024 символа. Если текст экрана длиннее (например, длинный
топ или список чеков), сообщение-фото заменяется обычным текстовым сообщением.
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

log = logging.getLogger("ui")

CAPTION_LIMIT = 1024
_IMAGE_FILE = Path(__file__).with_name("menu_image.json")


# --------------------------------------------------------------------------
# Картинка меню
# --------------------------------------------------------------------------


def _load_image() -> str | None:
    try:
        data = json.loads(_IMAGE_FILE.read_text(encoding="utf-8"))
        return data.get("file_id") or None
    except FileNotFoundError:
        return None
    except Exception:
        log.warning("[ui] не удалось прочитать %s", _IMAGE_FILE.name, exc_info=True)
        return None


_menu_image: str | None = _load_image()


def get_menu_image() -> str | None:
    """file_id текущей картинки меню (None — меню без картинки)."""
    return _menu_image


def set_menu_image(file_id: str | None) -> None:
    """Сохраняет картинку меню (None — убрать). Пишет в menu_image.json."""
    global _menu_image
    _menu_image = file_id or None
    tmp = _IMAGE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"file_id": _menu_image}), encoding="utf-8")
    tmp.replace(_IMAGE_FILE)


# --------------------------------------------------------------------------
# Редактирование
# --------------------------------------------------------------------------


def _caption_too_long(text: str) -> bool:
    """Оценка длины подписи после разбора HTML (теги не считаются, эмодзи — 2 единицы UTF-16)."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", text))
    return len(plain.encode("utf-16-le")) // 2 > CAPTION_LIMIT


def _is_text_message(message: Message) -> bool:
    return getattr(message, "text", None) is not None


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def _replace_with_text(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None) -> None:
    """Фото не может вместить этот текст — шлём обычное сообщение и убираем старое."""
    await message.answer(text, reply_markup=reply_markup)
    await _safe_delete(message)


async def edit_any(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует экран: текстовое сообщение — edit_text, сообщение с картинкой — edit_caption."""
    try:
        if _is_text_message(message):
            await message.edit_text(text, reply_markup=reply_markup)
        elif _caption_too_long(text):
            await _replace_with_text(message, text, reply_markup)
        else:
            try:
                await message.edit_caption(caption=text, reply_markup=reply_markup)
            except TelegramBadRequest as ex:
                if "too long" not in str(ex).lower():
                    raise
                await _replace_with_text(message, text, reply_markup)
    except TelegramBadRequest as ex:
        if "message is not modified" in str(ex).lower():
            return  # ничего не изменилось (повторное нажатие) — не ошибка
        raise


async def edit_by_id(
    bot: Bot, chat_id: int, message_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Редактирование по id (без объекта Message): пробуем текст, у фото — подпись."""
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
    except TelegramBadRequest as ex:
        if "no text in the message" not in str(ex).lower():
            raise
        await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text, reply_markup=reply_markup)


# --------------------------------------------------------------------------
# Меню
# --------------------------------------------------------------------------


async def send_menu(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> Message:
    """Отправляет меню: фото с подписью, если картинка задана (и подпись влезает), иначе текст."""
    image = get_menu_image()
    if image and not _caption_too_long(text):
        try:
            return await message.answer_photo(image, caption=text, reply_markup=reply_markup)
        except Exception:
            log.warning("[ui] не удалось отправить картинку меню — отправляю текстом", exc_info=True)
    return await message.answer(text, reply_markup=reply_markup)


async def show_menu_screen(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Возврат в меню по кнопке «Назад».

    Фото нельзя превратить в текст (и наоборот) редактированием, поэтому если тип сообщения
    не совпадает с текущим видом меню (картинку добавили/убрали или экран был заменён текстом
    из-за длинной подписи) — отправляем меню заново и удаляем старое сообщение."""
    has_image = bool(get_menu_image())
    if has_image == _is_text_message(message):
        await send_menu(message, text, reply_markup)
        await _safe_delete(message)
        return
    await edit_any(message, text, reply_markup)
