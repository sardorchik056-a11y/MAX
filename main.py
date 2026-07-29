import asyncio
import logging
import random
import sqlite3
from io import BytesIO
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ========== НАСТРОЙКИ ==========
TOKEN = "8651956926:AAG3ML1uGBPQOgrM5WAMl3kXaRLvVxTHCsw"  # ЗАМЕНИТЕ!

# ВКЛЮЧАЕМ LLM ДЛЯ УМНЫХ ОТВЕТОВ!
USE_LLM = True  # <--- СТАВИМ TRUE

# Путь к скачанной модели
MODEL_PATH = "/workspaces/MAX/saiga_7b_v2_q4_K.gguf"

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(level=logging.INFO)

# ========== БАЗА ДАННЫХ ==========
DB_PATH = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            current_section TEXT DEFAULT 'funny',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
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
    logging.info("База данных готова")

# ========== РАЗДЕЛЫ ==========
SECTIONS = {
    "funny": {
        "emoji": "🎉",
        "name": "Весёлый Макс",
        "description": "Шутит, иронизирует",
        "system_prompt": "Ты — Макс, весёлый собеседник. Шути, используй эмодзи. Отвечай кратко и живо."
    },
    "gopnik": {
        "emoji": "😈",
        "name": "Гопник Серёга",
        "description": "Дерзкий, матерится, уличный сленг",
        "system_prompt": "Ты — Гопник Серёга. Общайся на сленге, матерись, будь дерзким. Отвечай коротко, по-пацански."
    },
    "sarcastic": {
        "emoji": "😏",
        "name": "Доктор Сарказм",
        "description": "Циничный, язвительный",
        "system_prompt": "Ты — циничный доктор. Отвечай с сарказмом, используй чёрный юмор."
    }
}

# ========== ГЕНЕРАТОР МЕМОВ ==========
class MemeMaker:
    @staticmethod
    def make_meme(top_text: str, bottom_text: str) -> BytesIO:
        width, height = 600, 400
        img = Image.new('RGB', (width, height), color=(255, 215, 0))
        draw = ImageDraw.Draw(img)
        
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        except:
            font = ImageFont.load_default()
        
        draw.rectangle([10, 10, width-10, height-10], outline=(0,0,0), width=5)
        draw.text((30, 20), top_text.upper(), fill=(0,0,0), font=font)
        
        lines = bottom_text.split('\n')
        y = 150
        for line in lines[:3]:
            draw.text((30, y), line, fill=(0,0,0), font=font)
            y += 50
        
        bio = BytesIO()
        img.save(bio, format='PNG')
        bio.seek(0)
        return bio

# ========== ОСНОВНОЙ БОТ ==========
class BotInstance:
    def __init__(self):
        self.db_path = DB_PATH
        self.meme_maker = MemeMaker()
        self.use_llm = USE_LLM
        self.llm = None
        
        if self.use_llm:
            try:
                from llama_cpp import Llama
                self.llm = Llama(
                    model_path=MODEL_PATH,
                    n_ctx=4096,
                    n_threads=4,
                    verbose=False
                )
                logging.info("✅ LLM загружена!")
            except Exception as e:
                logging.error(f"❌ Ошибка загрузки LLM: {e}")
                self.use_llm = False
    
    # ===== РАБОТА С БАЗОЙ =====
    def register_user(self, telegram_id: int, username: str, first_name: str):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO users (telegram_id, username, first_name)
            VALUES (?, ?, ?)
        """, (telegram_id, username, first_name))
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
    
    # ===== ГЕНЕРАЦИЯ ОТВЕТА С ПАМЯТЬЮ =====
    def generate_response(self, telegram_id: int, message: str) -> tuple:
        # Сохраняем вопрос
        self.save_message(telegram_id, "user", message)
        
        # Получаем раздел
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
        
        if "мем" in message.lower():
            text = message.replace("мем", "").strip()
            if not text:
                text = "Смешной мем"
            meme = self.meme_maker.make_meme(
                top_text="МЕМ ОТ БОТА",
                bottom_text=text.upper()
            )
            response_text = f"😂 Держи мем: {text}"
            self.save_message(telegram_id, "assistant", response_text)
            return response_text, meme
        
        # ==== ГЕНЕРАЦИЯ УМНОГО ОТВЕТА С ПАМЯТЬЮ ====
        if self.use_llm and self.llm:
            # Берём историю (последние 10 сообщений)
            history = self.get_history(telegram_id, 10)
            
            # Формируем промпт с контекстом
            prompt = f"<|system|>\n{section['system_prompt']}\n"
            
            for msg in history:
                if msg["role"] == "user":
                    prompt += f"<|user|>\n{msg['content']}\n"
                else:
                    prompt += f"<|assistant|>\n{msg['content']}\n"
            
            prompt += f"<|user|>\n{message}\n<|assistant|>\n"
            
            try:
                response = self.llm(
                    prompt,
                    max_tokens=200,
                    temperature=0.85,
                    stop=["<|user|>", "<|system|>"],
                    echo=False
                )
                answer = response["choices"][0]["text"].strip()
                
                # Если ответ слишком короткий
                if len(answer) < 3:
                    answer = f"{section['emoji']} Хм, интересно... Расскажи ещё!"
                
            except Exception as e:
                logging.error(f"LLM ошибка: {e}")
                answer = f"{section['emoji']} Что-то я задумался... Давай ещё раз!"
        
        else:
            # Если LLM нет — используем шаблонные ответы
            answer = self.get_template_response(message, section_key)
        
        # Сохраняем ответ
        self.save_message(telegram_id, "assistant", answer)
        return answer, None
    
    def get_template_response(self, message: str, section: str) -> str:
        """Запасные шаблонные ответы (если нет LLM)"""
        msg = message.lower()
        emoji = SECTIONS[section]["emoji"]
        
        if section == "gopnik":
            replies = [
                f"{emoji} Чё надо, пацан? Говори давай!",
                f"{emoji} Слышь, ты чё такой серьёзный?",
                f"{emoji} Ну ты даёшь, братан!",
            ]
            return random.choice(replies)
        
        if "привет" in msg:
            return f"{emoji} Привет! Как дела?"
        if "как дел" in msg:
            return f"{emoji} Супер! А у тебя?"
        if "пока" in msg:
            return f"{emoji} Пока! Заходи ещё! 👋"
        if "спасиб" in msg:
            return f"{emoji} Всегда пожалуйста! 😊"
        
        return random.choice([
            f"{emoji} О, интересно! Расскажи подробнее!",
            f"{emoji} Хм, я такого ещё не слышал 🤔",
            f"{emoji} Вау! А что дальше?",
        ])

# ========== ИНИЦИАЛИЗАЦИЯ ==========
bot_instance = BotInstance()
bot = Bot(token=TOKEN)
dp = Dispatcher()

# ========== КЛАВИАТУРЫ ==========
def get_keyboard():
    builder = InlineKeyboardBuilder()
    for key, section in SECTIONS.items():
        builder.button(text=f"{section['emoji']} {section['name']}", callback_data=f"section_{key}")
    builder.button(text="ℹ️ Инфо", callback_data="info")
    builder.button(text="🧹 Очистить", callback_data="clear")
    builder.adjust(1)
    return builder.as_markup()

# ========== КОМАНДЫ ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    bot_instance.register_user(user.id, user.username, user.first_name)
    
    current = bot_instance.get_section(user.id)
    section = SECTIONS[current]
    
    await message.answer(
        f"""👋 Привет, {user.first_name}!

Сейчас активен: {section['emoji']} **{section['name']}**

Выбери персонажа:""",
        reply_markup=get_keyboard(),
        parse_mode="Markdown"
    )

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    bot_instance.clear_history(message.from_user.id)
    await message.answer("🧹 История очищена! ✨")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    section_key = bot_instance.get_section(message.from_user.id)
    section = SECTIONS[section_key]
    history = bot_instance.get_history(message.from_user.id)
    
    await message.answer(
        f"""📊 **Статус:**
{section['emoji']} Раздел: **{section['name']}**
💬 Сообщений: {len(history)}
🔄 LLM: {'✅ Включена' if bot_instance.use_llm else '❌ Выключена'}""",
        parse_mode="Markdown"
    )

@dp.message(Command("meme"))
async def cmd_meme(message: types.Message):
    args = message.text.split(maxsplit=1)
    text = args[1] if len(args) > 1 else "Смешной мем!"
    
    meme = bot_instance.meme_maker.make_meme(
        top_text="МЕМ ОТ БОТА",
        bottom_text=text.upper()
    )
    
    await message.answer_photo(
        photo=BufferedInputFile(meme.getvalue(), filename="meme.png"),
        caption=f"😂 {text}"
    )

# ========== ОБРАБОТКА СООБЩЕНИЙ ==========
@dp.message()
async def handle_message(message: types.Message):
    telegram_id = message.from_user.id
    
    await bot.send_chat_action(telegram_id, "typing")
    
    response_text, image = bot_instance.generate_response(telegram_id, message.text)
    
    if image:
        await message.answer(response_text)
        await message.answer_photo(
            photo=BufferedInputFile(image.getvalue(), filename="meme.png"),
            caption="😂 Мем!"
        )
    else:
        await message.answer(response_text)

# ========== КНОПКИ ==========
@dp.callback_query()
async def handle_callback(callback: types.CallbackQuery):
    await callback.answer()
    
    data = callback.data
    telegram_id = callback.from_user.id
    
    if data == "info":
        await callback.message.edit_text(
            "📖 **Команды:**\n"
            "/start — меню\n"
            "/clear — очистить историю\n"
            "/status — статус бота\n"
            "/meme [текст] — создать мем\n\n"
            "🎭 **Персонажи:**\n"
            + "\n".join([f"{s['emoji']} {s['name']} — {s['description']}" for s in SECTIONS.values()]),
            parse_mode="Markdown"
        )
        return
    
    if data == "clear":
        bot_instance.clear_history(telegram_id)
        await callback.message.edit_text("🧹 Очищено!")
        return
    
    if data.startswith("section_"):
        section_key = data.replace("section_", "")
        if section_key in SECTIONS:
            bot_instance.set_section(telegram_id, section_key)
            section = SECTIONS[section_key]
            
            await callback.message.edit_text(
                f"✅ Переключил на {section['emoji']} **{section['name']}**\n\n{section['description']}",
                reply_markup=get_keyboard(),
                parse_mode="Markdown"
            )

# ========== ЗАПУСК ==========
async def main():
    init_db()
    logging.info("🚀 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
