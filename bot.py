import asyncio
import json
import logging
import os
import sys
import sqlite3
from aiohttp import web
from dotenv import load_dotenv

# Драйвер для PostgreSQL (необходим для Neon.tech)
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

load_dotenv()

# --- 1. НАСТРОЙКИ ---
class Settings:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    ADMIN_ID = os.getenv("ADMIN_ID")
    DATABASE_URL = os.getenv("DATABASE_URL")
    PORT = int(os.getenv("PORT", 8000))
    BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

settings = Settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
log = logging.getLogger("bot")

# --- 2. МЕНЕДЖЕР БАЗЫ ДАННЫХ ---
class DB:
    def __init__(self):
        self.is_pg = bool(settings.DATABASE_URL and psycopg2)
        self.init_db()

    def get_conn(self):
        if self.is_pg:
            return psycopg2.connect(settings.DATABASE_URL, cursor_factory=RealDictCursor)
        conn = sqlite3.connect("orders.db")
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        conn = self.get_conn()
        cur = conn.cursor()
        id_t = "SERIAL PRIMARY KEY" if self.is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        js_t = "JSONB" if self.is_pg else "TEXT"
        
        cur.execute(f"CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, name TEXT, price REAL, category TEXT, image TEXT, sizes {js_t}, is_available INTEGER DEFAULT 1, badge TEXT)")
        cur.execute(f"CREATE TABLE IF NOT EXISTS orders (id {id_t}, user_id BIGINT, user_name TEXT, phone TEXT, address TEXT, items {js_t}, total REAL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.commit()
        
        cur.execute("SELECT COUNT(*) FROM products")
        if (cur.fetchone()[0] if self.is_pg else cur.fetchone()[0]) == 0:
            self.seed(cur)
            conn.commit()
        conn.close()

    def seed(self, cur):
        ph = "%s" if self.is_pg else "?"
        s_shoes = json.dumps({str(i): True for i in range(38, 46)})
        s_clothes = json.dumps({s: True for s in ['S', 'M', 'L', 'XL', 'XXL']})
        data = [
            ('p1', 'KOS Runner V1', 1650000, 'Shoes', 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=800', s_shoes, 1, 'New'),
            ('p2', 'KOS Tech Tee', 420000, 'Apparel', 'https://images.unsplash.com/photo-1581655353564-df123a1eb820?w=800', s_clothes, 1, None)
        ]
        cur.executemany(f"INSERT INTO products VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})", data)

    def get_products(self):
        conn = self.get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM products")
        rows = cur.fetchall()
        res = []
        for r in rows:
            sz = r['sizes']
            if isinstance(sz, str): sz = json.loads(sz)
            res.append({"id":r['id'], "name":r['name'], "price":r['price'], "category":r['category'], "image":r['image'], "sizes":sz, "isAvailable":bool(r['is_available']), "badge":r['badge']})
        conn.close()
        return res

    def save_product(self, p):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        sql = f"INSERT INTO products VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})"
        if self.is_pg:
            sql += " ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, price=EXCLUDED.price, is_available=EXCLUDED.is_available, sizes=EXCLUDED.sizes, image=EXCLUDED.image, category=EXCLUDED.category"
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

    def add_order(self, u_id, u_name, phone, addr, items, total):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        cur.execute(f"INSERT INTO orders (user_id, user_name, phone, address, items, total) VALUES ({ph},{ph},{ph},{ph},{ph},{ph})", (u_id, u_name, phone, addr, json.dumps(items), total))
        conn.commit()
        conn.close()

db_manager = DB()

# --- 3. API ЭНДПОИНТЫ ---
async def api_get_products(request):
    return web.json_response(db_manager.get_products())

async def api_post_product(request):
    try:
        data = await request.json()
        db_manager.save_product(data)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def api_delete_product(request):
    db_manager.delete_product(request.match_info['id'])
    return web.json_response({"status": "ok"})

async def serve_index(request):
    with open("index.html", "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")

# --- 4. ТЕЛЕГРАМ БОТ ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.types import Message, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

class OrderFlow(StatesGroup):
    contact = State()
    address = State()

async def cmd_start(m: Message):
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🛒 Открыть магазин", web_app=WebAppInfo(url=settings.BASE_URL))]], resize_keyboard=True)
    await m.answer(f"👋 Привет, {m.from_user.first_name}!\nДобро пожаловать в KOS Sport. Жми кнопку ниже 👇", reply_markup=kb)

async def on_webapp_data(m: Message, state: FSMContext):
    data = json.loads(m.web_app_data.data)
    items = data.get('items', [])
    total = sum(float(i['price']) * int(i['qty']) for i in items)
    await state.update_data(items=items, total=total)
    await state.set_state(OrderFlow.contact)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Отправить контакт", request_contact=True)]], resize_keyboard=True)
    await m.answer(f"📦 Заказ на {total:,.0f} UZS принят.\nПожалуйста, отправьте ваш номер телефона.", reply_markup=kb, parse_mode=ParseMode.HTML)

async def process_contact(m: Message, state: FSMContext):
    phone = m.contact.phone_number if m.contact else m.text
    await state.update_data(phone=phone, user_name=m.from_user.full_name)
    await state.set_state(OrderFlow.address)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Локация", request_location=True), KeyboardButton(text="Введу вручную")]], resize_keyboard=True)
    await m.answer("📍 Куда доставить заказ? Отправьте локацию или напишите адрес текстом.", reply_markup=kb)

async def process_finish(m: Message, state: FSMContext):
    data = await state.get_data()
    addr = f"Гео: {m.location.latitude},{m.location.longitude}" if m.location else m.text
    
    db_manager.add_order(m.from_user.id, m.from_user.full_name, data['phone'], addr, data['items'], data['total'])
    
    await m.answer(f"✅ Спасибо! Заказ оформлен.\nСумма: {data['total']:,.0f} UZS.\nМенеджер свяжется с вами.", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🛒 Магазин", web_app=WebAppInfo(url=settings.BASE_URL))]], resize_keyboard=True))
    
    if settings.ADMIN_ID:
        try:
            admin_text = f"🆕 <b>Новый заказ!</b>\n👤 {data.get('user_name')}\n📞 {data['phone']}\n📍 {addr}\n💰 {data['total']:,.0f} UZS"
            await m.bot.send_message(settings.ADMIN_ID, admin_text, parse_mode=ParseMode.HTML)
        except: pass
    await state.clear()

async def main():
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/api/products", api_get_products)
    app.router.add_post("/api/products", api_post_product)
    app.router.add_delete("/api/products/{id}", api_delete_product)
    
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', settings.PORT).start()

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.register(cmd_start, F.text == "/start")
    dp.message.register(on_webapp_data, F.web_app_data)
    dp.message.register(process_contact, OrderFlow.contact)
    dp.message.register(process_finish, OrderFlow.address)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
