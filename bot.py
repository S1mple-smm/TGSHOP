import asyncio
import json
import logging
import os
import sys
import sqlite3
from aiohttp import web
from dotenv import load_dotenv

# Драйвер для PostgreSQL (необходим для Neon.tech в облаке)
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

# Загружаем переменные из .env файла (для локального запуска)
load_dotenv()

class Settings:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    ADMIN_ID = os.getenv("ADMIN_ID")
    # Переменная DATABASE_URL должна быть в настройках Render (ссылка из Neon)
    DATABASE_URL = os.getenv("DATABASE_URL")
    # Порт для сервера (Render назначает его автоматически)
    PORT = int(os.getenv("PORT", 8000))
    # Публичная ссылка на ваш сайт (нужна для кнопки в боте)
    BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

settings = Settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
log = logging.getLogger("bot")

# --- УПРАВЛЕНИЕ БАЗОЙ ДАННЫХ (POSTGRES / SQLITE) ---
class DBManager:
    def __init__(self):
        # Если есть ссылка на базу, используем Postgres (Neon), иначе локальный файл
        self.is_pg = bool(settings.DATABASE_URL and psycopg2)
        self.init_db()

    def get_conn(self):
        if self.is_pg:
            return psycopg2.connect(settings.DATABASE_URL, cursor_factory=RealDictCursor)
        conn = sqlite3.connect("orders.db")
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        """Создание таблиц при первом запуске"""
        conn = self.get_conn()
        cur = conn.cursor()
        
        # Настройка типов данных под разные БД
        id_type = "SERIAL PRIMARY KEY" if self.is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        json_type = "JSONB" if self.is_pg else "TEXT"

        # Таблица товаров
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT,
                price REAL,
                category TEXT,
                image TEXT,
                sizes {json_type},
                is_available INTEGER DEFAULT 1,
                badge TEXT
            )
        """)
        # Таблица заказов
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS orders (
                id {id_type},
                user_id BIGINT,
                user_name TEXT,
                phone TEXT,
                address TEXT,
                items {json_type},
                total REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        
        # Добавляем стартовые товары, если база пустая
        cur.execute("SELECT COUNT(*) FROM products")
        count = cur.fetchone()[0] if self.is_pg else cur.fetchone()[0]
        if count == 0:
            self.seed_data(cur)
            conn.commit()
        conn.close()

    def seed_data(self, cur):
        placeholder = "%s" if self.is_pg else "?"
        shoes_sizes = json.dumps({str(i): True for i in range(38, 46)})
        data = [
            ('p1', 'KOS Runner V1', 1650000, 'Shoes', 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=800', shoes_sizes, 1, 'New'),
            ('p2', 'KOS Tech Tee', 420000, 'Apparel', 'https://images.unsplash.com/photo-1581655353564-df123a1eb820?w=800', json.dumps({'S':True, 'M':True, 'L':True}), 1, None)
        ]
        cur.executemany(f"INSERT INTO products VALUES ({placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder},{placeholder})", data)

    # Методы API для сайта
    def get_products(self):
        conn = self.get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM products")
        rows = cur.fetchall()
        res = []
        for r in rows:
            sizes = r['sizes']
            if isinstance(sizes, str): sizes = json.loads(sizes)
            res.append({
                "id": r['id'], "name": r['name'], "price": r['price'],
                "category": r['category'], "image": r['image'],
                "sizes": sizes, "isAvailable": bool(r['is_available']), "badge": r['badge']
            })
        conn.close()
        return res

    def save_product(self, p):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        if self.is_pg:
            sql = f"""INSERT INTO products (id, name, price, category, image, sizes, is_available, badge) 
                      VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph}) 
                      ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, price=EXCLUDED.price, is_available=EXCLUDED.is_available, sizes=EXCLUDED.sizes"""
        else:
            sql = f"INSERT OR REPLACE INTO products VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})"
        cur.execute(sql, (p['id'], p['name'], p['price'], p['category'], p['image'], json.dumps(p['sizes']), int(p['isAvailable']), p.get('badge')))
        conn.commit()
        conn.close()

    def delete_product(self, pid):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        cur.execute(f"DELETE FROM products WHERE id={ph}", (pid,))
        conn.commit()
        conn.close()

db = DBManager()

# --- API ЭНДПОИНТЫ (ДЛЯ ФРОНТЕНДА) ---
async def api_get_products(request):
    return web.json_response(db.get_products())

async def api_post_product(request):
    try:
        data = await request.json()
        db.save_product(data)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def api_delete_product(request):
    pid = request.match_info['id']
    db.delete_product(pid)
    return web.json_response({"status": "ok"})

async def serve_index(request):
    """Раздача HTML файла витрины"""
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")
    except FileNotFoundError:
        return web.Response(text="index.html not found", status=404)

# --- TELEGRAM БОТ ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage

async def cmd_start(m: Message):
    # Кнопка для открытия WebApp
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛒 Магазин KOS", web_app=WebAppInfo(url=settings.BASE_URL))]],
        resize_keyboard=True
    )
    await m.answer(
        f"👋 Привет, {m.from_user.first_name}!\nДобро пожаловать в KOS Sport. Жми кнопку ниже, чтобы открыть каталог.",
        reply_markup=kb
    )

async def on_webapp_data(m: Message):
    """Обработка данных, пришедших из WebApp после заказа"""
    try:
        data = json.loads(m.web_app_data.data)
        total = data.get('total', 0)
        await m.answer(f"✅ Заказ принят! Сумма: {total:,.0f} UZS. Менеджер свяжется с вами в ближайшее время.")
    except Exception as e:
        await m.answer("Ошибка при обработке заказа.")

async def main():
    # Настройка и запуск Веб-сервера
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/api/products", api_get_products)
    app.router.add_post("/api/products", api_post_product)
    app.router.add_delete("/api/products/{id}", api_delete_product)
    
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', settings.PORT).start()
    log.info(f"🚀 Сервер запущен на порту {settings.PORT}")

    # Настройка и запуск Бота
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.register(cmd_start, F.text == "/start")
    dp.message.register(on_webapp_data, F.web_app_data)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен")
