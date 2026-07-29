import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ========== НАСТРОЙКИ ==========
TOKEN = "8651956926:AAG3ML1uGBPQOgrM5WAMl3kXaRLvVxTHCsw"  # Замените на свой

# Если хотите использовать локальную LLM - поставьте True и укажите путь к модели
USE_LLM = False  # Поставьте True, если установили llama-cpp-python
MODEL_PATH = "/opt/models/saiga_7b_v2_q4_K.gguf"  # путь к модели

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(level=logging.INFO)

# ========== ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ==========
DB_PATH = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # Таблица пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            current_section TEXT DEFAULT 'funny',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Таблица истории сообщений
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    
    conn.commit()
    conn.close()
    logging.info("База данных инициализирована")

# ========== РАЗДЕЛЫ (ПЕРСОНАЖИ) ==========
SECTIONS = {
    "funny": {
        "emoji": "🎉",
        "name": "Весёлый Макс",
        "description": "Шутит, иронизирует, поднимает настроение",
        "system_prompt": "Ты — Макс, весёлый собеседник. Шути, используй эмодзи, отвечай кратко."
    },
    "philosopher": {
        "emoji": "🧐",
        "name": "Философ София",
        "description": "Глубокие размышления, мудрые вопросы",
        "system_prompt": "Ты — София, мудрый философ. Говори глубоко, задавай вопросы о смысле жизни."
    },
    "clown": {
        "emoji": "🤣",
        "name": "Клоун Тимофей",
        "description": "Безумные шутки, абсурд, мемы",
        "system_prompt": "Ты — безумный клоун! Шути, используй абсурд, мемы, смейся громко!"
    },
    "gopnik": {
        "emoji": "😈",
        "name": "Гопник Серёга",
        "description": "Дерзкий, матерится, уличный сленг",
        "system_prompt": "Ты — Гопник Серёга. Общайся на сленге, матерись, будь дерзким, но честным."
    },
    "sarcastic": {
        "emoji": "😏",
        "name": "Доктор Сарказм",
        "description": "Циничный, язвительный, чёрный юмор",
        "system_prompt": "Ты — циничный доктор. Отвечай с сарказмом, используй чёрный юмор."
    }
}

# ========== ГЕНЕРАТОР МЕМОВ ==========
class MemeMaker:
    @staticmethod
    def make_meme(top_text: str, bottom_text: str) -> BytesIO:
        """Создаёт мем с текстом сверху и снизу, возвращает BytesIO"""
        width, height = 600, 400
        img = Image.new('RGB', (width, height), color=(255, 215, 0))
        draw = ImageDraw.Draw(img)
        
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        except:
            font = ImageFont.load_default()
            font_small = font
        
        # Рамка
        draw.rectangle([10, 10, width-10, height-10], outline=(0,0,0), width=5)
        
        # Верхний текст
        draw.text((30, 20), top_text.upper(), fill=(0,0,0), font=font)
        
        # Большой текст по центру
        lines = bottom_text.split('\n')
        y = 150
        for line in lines[:3]:
            draw.text((30, y), line, fill=(0,0,0), font=font)
            y += 50
        
        # Нижний колонтитул
        draw.text((30, height-40), "#МемОтБота", fill=(80,80,80), font=font_small)
        
        # Сохраняем в BytesIO
        bio = BytesIO()
        img.save(bio, format='PNG')
        bio.seek(0)
        return bio

# ========== ОСНОВНОЙ БОТ ==========
class BotInstance:
    def __init__(self):
        self.db_path = DB_PATH
        self.meme_maker = MemeMaker()
        self.use_llm = USE_LLM  # Сохраняем глобальную переменную как атрибут
        
        # Загружаем LLM, если включено
        self.llm = None
        if self.use_llm:
            try:
                from llama_cpp import Llama
                self.llm = Llama(
                    model_path=MODEL_PATH,
                    n_ctx=2048,
                    n_threads=4,
                    verbose=False
                )
                logging.info("LLM модель загружена")
            except Exception as e:
                logging.error(f"Не удалось загрузить LLM: {e}")
                self.use_llm = False
    
    # ===== РАБОТА С БАЗОЙ =====
    def get_user(self, telegram_id: int) -> dict:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                "id": row[0],
                "telegram_id": row[1],
                "username": row[2],
                "first_name": row[3],
                "last_name": row[4],
                "current_section": row[5],
                "created_at": row[6],
                "last_active": row[7]
            }
        return None
    
    def register_user(self, telegram_id: int, username: str, first_name: str, last_name: str = ""):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO users (telegram_id, username, first_name, last_name, last_active)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (telegram_id, username, first_name, last_name))
        conn.commit()
        conn.close()
    
    def get_section(self, telegram_id: int) -> str:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT current_section FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else "funny"
    
    def set_section(self, telegram_id: int, section: str):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("UPDATE users SET current_section = ? WHERE telegram_id = ?", (section, telegram_id))
        conn.commit()
        conn.close()
    
    def save_message(self, telegram_id: int, role: str, content: str):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO messages (user_id, role, content)
            SELECT id, ?, ? FROM users WHERE telegram_id = ?
        """, (role, content, telegram_id))
        conn.commit()
        conn.close()
    
    def get_history(self, telegram_id: int, limit: int = 10) -> list:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT role, content FROM messages
            WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?)
            ORDER BY created_at DESC LIMIT ?
        """, (telegram_id, limit))
        rows = cur.fetchall()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    
    def clear_history(self, telegram_id: int):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM messages
            WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?)
        """, (telegram_id,))
        conn.commit()
        conn.close()
    
    # ===== ГЕНЕРАЦИЯ ОТВЕТОВ =====
    def generate_response(self, telegram_id: int, message: str) -> tuple:
        """Возвращает (текст_ответа, BytesIO_с_картинкой_или_None)"""
        
        # Сохраняем сообщение пользователя
        self.save_message(telegram_id, "user", message)
        
        # Получаем раздел пользователя
        section_key = self.get_section(telegram_id)
        section = SECTIONS[section_key]
        
        # Проверяем ключевые слова для мемов
        if "девушк" in message.lower() or "сколько лет" in message.lower():
            response_text = "18... но ощущается как 48! 😂"
            meme = self.meme_maker.make_meme(
                top_text="Моей девушке 18 лет",
                bottom_text="Но ощущается как 48! 😂"
            )
            self.save_message(telegram_id, "assistant", response_text)
            return response_text, meme
        
        # Проверяем запрос на мем
        if "мем" in message.lower() or "картинк" in message.lower():
            text = message.replace("мем", "").replace("картинку", "").strip()
            if not text:
                text = "Смешной мем"
            meme = self.meme_maker.make_meme(
                top_text="МЕМ ОТ БОТА",
                bottom_text=text.upper()
            )
            response_text = f"Держи мем! 😂\n\nТема: {text}"
            self.save_message(telegram_id, "assistant", response_text)
            return response_text, meme
        
        # Если есть LLM — используем её
        if self.use_llm and self.llm:
            history = self.get_history(telegram_id, 10)
            prompt = f"<|system|>\n{section['system_prompt']}\n"
            for msg in history:
                if msg["role"] == "user":
                    prompt += f"<|user|>\n{msg['content']}\n"
                else:
                    prompt += f"<|assistant|>\n{msg['content']}\n"
            prompt += f"<|user|>\n{message}\n<|assistant|>\n"
            
            try:
                response = self.llm(prompt, max_tokens=150, temperature=0.85, stop=["<|user|>"])
                answer = response["choices"][0]["text"].strip()
                if len(answer) < 3:
                    answer = f"{section['emoji']} Интересно... Расскажи ещё!"
            except Exception as e:
                logging.error(f"LLM ошибка: {e}")
                answer = f"{section['emoji']} Что-то я задумался... Давай ещё раз!"
        else:
            # Шаблонные ответы
            answer = self.get_template_response(message, section_key)
        
        # Сохраняем ответ
        self.save_message(telegram_id, "assistant", answer)
        return answer, None
    
    def get_template_response(self, message: str, section: str) -> str:
        """Шаблонные ответы без LLM"""
        msg = message.lower()
        emoji = SECTIONS[section]["emoji"]
        
        # Дерзкие ответы для Гопника
        if section == "gopnik":
            gopnik_replies = [
                f"{emoji} Чё надо, пацан? Говори давай, не тяни!",
                f"{emoji} О, живой! Чё хотел? Быстро, у меня дела!",
                f"{emoji} Слышь, ты чё такой серьёзный? Расслабься, бля!",
                f"{emoji} А чё, нормально! Давай ещё, не тормози!",
                f"{emoji} Ну ты даёшь, братан! Колись, чё случилось?",
                f"{emoji} Ой, всё! Ты меня заколебал уже! Шучу, говори давай 😈",
            ]
            return random.choice(gopnik_replies)
        
        if "привет" in msg or "здрав" in msg:
            replies = [
                f"{emoji} Привет! Как дела?",
                f"{emoji} О, привет! Давно не виделись!",
                f"{emoji} Здорово! Чем порадуешь?"
            ]
        elif "как дела" in msg or "как жизнь" in msg:
            replies = [
                f"{emoji} Супер! А у тебя?",
                f"{emoji} Норм, погода классная!",
                f"{emoji} Отлично! Только что мемы смотрел 😂"
            ]
        elif "пока" in msg or "до свидан" in msg:
            replies = [
                f"{emoji} Пока-пока! Заходи ещё! 👋",
                f"{emoji} Удачи! Напиши, если что!",
                f"{emoji} До встречи! Обязательно возвращайся!"
            ]
        elif "спасиб" in msg:
            replies = [
                f"{emoji} Всегда пожалуйста! 😊",
                f"{emoji} Обращайся, я всегда рад помочь!",
                f"{emoji} Не за что! Приятно было поболтать!"
            ]
        elif "мем" in msg:
            replies = [
                f"{emoji} О, хочешь мем? Напиши /meme [текст]!",
                f"{emoji} Мемы — моя страсть! /meme тема",
            ]
        else:
            replies = [
                f"{emoji} О, интересно! Расскажи подробнее!",
                f"{emoji} Хм, я такого ещё не слышал 🤔",
                f"{emoji} Вау! А что дальше?",
                f"{emoji} Забавно! А как ты к этому относишься?",
                f"{emoji} Ух ты! Я аж задумался...",
                f"{emoji} Класс! А ещё что-нибудь расскажи!",
            ]
        
        return random.choice(replies)

# ========== ИНИЦИАЛИЗАЦИЯ ==========
bot_instance = BotInstance()
bot = Bot(token=TOKEN)
dp = Dispatcher()

# ========== КЛАВИАТУРЫ ==========
def get_section_keyboard():
    builder = InlineKeyboardBuilder()
    for key, section in SECTIONS.items():
        builder.add(InlineKeyboardButton(
            text=f"{section['emoji']} {section['name']}",
            callback_data=f"section_{key}"
        ))
    builder.add(InlineKeyboardButton(text="ℹ️ Инфо", callback_data="info"))
    builder.add(InlineKeyboardButton(text="🧹 Очистить", callback_data="clear"))
    builder.adjust(1, 1, 1, 1, 1, 1)  # По одной кнопке в ряд
    return builder.as_markup()

# ========== ОБРАБОТЧИКИ КОМАНД ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    bot_instance.register_user(user.id, user.username, user.first_name, user.last_name or "")
    
    current = bot_instance.get_section(user.id)
    section = SECTIONS[current]
    
    text = f"""👋 Привет, {user.first_name}!

Я — бот с разными режимами общения!

🎭 **Выбери свой раздел:**

Сейчас активен: {section['emoji']} **{section['name']}**

Нажми на кнопку, чтобы сменить персонажа! 👇"""
    
    await message.answer(text, reply_markup=get_section_keyboard(), parse_mode="Markdown")

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    bot_instance.clear_history(message.from_user.id)
    await message.answer("🧹 История очищена! Начинаем заново ✨")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    section_key = bot_instance.get_section(message.from_user.id)
    section = SECTIONS[section_key]
    
    history = bot_instance.get_history(message.from_user.id)
    
    await message.answer(
        f"""📊 **Твой статус:**

{section['emoji']} Раздел: **{section['name']}**
📝 Описание: {section['description']}
💬 Сообщений в истории: {len(history)}

Изменить раздел: /start""",
        parse_mode="Markdown"
    )

@dp.message(Command("meme"))
async def cmd_meme(message: types.Message):
    args = message.text.split(maxsplit=1)
    text = args[1] if len(args) > 1 else "Смешной мем от бота!"
    
    meme = bot_instance.meme_maker.make_meme(
        top_text="МЕМ ОТ БОТА",
        bottom_text=text.upper()
    )
    
    await message.answer_photo(
        photo=types.BufferedInputFile(meme.getvalue(), filename="meme.png"),
        caption=f"😂 Держи мем! Тема: {text}"
    )

# ========== ОБРАБОТЧИК СООБЩЕНИЙ ==========
@dp.message()
async def handle_message(message: types.Message):
    telegram_id = message.from_user.id
    
    # Показываем "печатает..."
    await bot.send_chat_action(telegram_id, "typing")
    
    # Получаем ответ
    response_text, image = bot_instance.generate_response(telegram_id, message.text)
    
    if image:
        # Отправляем текст + картинку
        await message.answer(response_text)
        await message.answer_photo(
            photo=types.BufferedInputFile(image.getvalue(), filename="meme.png"),
            caption="😂 Мем от бота!"
        )
    else:
        # Только текст
        await message.answer(response_text)

# ========== ОБРАБОТЧИК КНОПОК ==========
@dp.callback_query()
async def handle_callback(callback: types.CallbackQuery):
    await callback.answer()
    
    data = callback.data
    telegram_id = callback.from_user.id
    
    if data == "info":
        info = """📖 **Как пользоваться ботом:**

1️⃣ Выбери персонажа в меню /start
2️⃣ Просто пиши сообщения — бот отвечает в выбранном стиле
3️⃣ Меняй раздел в любой момент через /start
4️⃣ Команда /clear — очистить историю
5️⃣ Команда /status — узнать текущий раздел
6️⃣ Команда /meme [текст] — создать мем

🎭 **Доступные разделы:**
"""
        for key, section in SECTIONS.items():
            info += f"\n{section['emoji']} **{section['name']}** — {section['description']}"
        
        await callback.message.edit_text(info, parse_mode="Markdown")
        return
    
    if data == "clear":
        bot_instance.clear_history(telegram_id)
        await callback.message.edit_text("🧹 История очищена! ✨")
        return
    
    if data.startswith("section_"):
        section_key = data.replace("section_", "")
        if section_key in SECTIONS:
            bot_instance.set_section(telegram_id, section_key)
            section = SECTIONS[section_key]
            
            await callback.message.edit_text(
                f"""✅ Переключился на: {section['emoji']} **{section['name']}**

{section['description']}

Теперь я буду отвечать в этом стиле! Напиши что-нибудь 😊""",
                reply_markup=get_section_keyboard(),
                parse_mode="Markdown"
            )

# ========== ЗАПУСК ==========
async def main():
    init_db()
    logging.info("🤖 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
