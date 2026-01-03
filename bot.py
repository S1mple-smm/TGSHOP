import asyncio
import json
import logging
import os
import sys
import sqlite3
from aiohttp import web
from dotenv import load_dotenv

# Подключение PostgreSQL (для Render/Neon)
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
class DBManager:
    def __init__(self):
        self.is_pg = bool(settings.DATABASE_URL and psycopg2)
        if self.is_pg:
            log.info("✅ Используем PostgreSQL (Облако)")
        else:
            log.info("⚠️ Используем SQLite (Локально)")
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
        
        # Типы данных
        id_serial = "SERIAL PRIMARY KEY" if self.is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        json_type = "JSONB" if self.is_pg else "TEXT"

        # Таблица ТОВАРОВ (обновленная структура)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT,
                price REAL,
                description TEXT,
                category TEXT,
                images {json_type}, -- Массив ссылок/base64
                sizes {json_type},   -- Доступность размеров
                is_available INTEGER DEFAULT 1,
                size_chart TEXT      -- Ссылка или текст размерной сетки
            )
        """)
        
        # Таблица ЗАКАЗОВ
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS orders (
                id {id_serial},
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
        conn.close()

    # --- API МЕТОДЫ ---
    def get_products(self):
        conn = self.get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM products")
        rows = cur.fetchall()
        res = []
        for r in rows:
            # Десериализация JSON
            images = r['images']
            sizes = r['sizes']
            if isinstance(images, str): images = json.loads(images)
            if isinstance(sizes, str): sizes = json.loads(sizes)
            
            res.append({
                "id": r['id'],
                "name": r['name'],
                "price": r['price'],
                "description": r['description'],
                "category": r['category'],
                "images": images,
                "sizes": sizes,
                "isAvailable": bool(r['is_available']),
                "sizeChart": r['size_chart']
            })
        conn.close()
        return res

    def save_product(self, p):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        
        images_json = json.dumps(p['images'])
        sizes_json = json.dumps(p['sizes'])
        
        # Upsert (Вставка или Обновление)
        if self.is_pg:
            sql = f"""
                INSERT INTO products (id, name, price, description, category, images, sizes, is_available, size_chart)
                VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
                ON CONFLICT (id) DO UPDATE SET 
                name=EXCLUDED.name, price=EXCLUDED.price, description=EXCLUDED.description,
                category=EXCLUDED.category, images=EXCLUDED.images, sizes=EXCLUDED.sizes,
                is_available=EXCLUDED.is_available, size_chart=EXCLUDED.size_chart
            """
        else:
            sql = f"INSERT OR REPLACE INTO products VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})"
            
        cur.execute(sql, (
            p['id'], p['name'], p['price'], p.get('description', ''), 
            p['category'], images_json, sizes_json, 
            int(p['isAvailable']), p.get('sizeChart', '')
        ))
        conn.commit()
        conn.close()

    def delete_product(self, pid):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        cur.execute(f"DELETE FROM products WHERE id={ph}", (pid,))
        conn.commit()
        conn.close()
    
    def toggle_stock(self, pid, status):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        cur.execute(f"UPDATE products SET is_available={ph} WHERE id={ph}", (int(status), pid))
        conn.commit()
        conn.close()

    def toggle_size_stock(self, pid, size, status):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        
        # 1. Получаем текущие размеры
        cur.execute(f"SELECT sizes FROM products WHERE id={ph}", (pid,))
        row = cur.fetchone()
        if row:
            current_sizes = row[0]
            if isinstance(current_sizes, str): current_sizes = json.loads(current_sizes)
            elif hasattr(current_sizes, 'copy'): current_sizes = current_sizes.copy()
            
            # 2. Обновляем
            current_sizes[size] = status
            
            # 3. Сохраняем обратно
            new_json = json.dumps(current_sizes)
            cur.execute(f"UPDATE products SET sizes={ph} WHERE id={ph}", (new_json, pid))
            conn.commit()
        conn.close()

    def add_order(self, uid, uname, phone, addr, items, total):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        items_json = json.dumps(items)
        
        if self.is_pg:
            cur.execute(f"INSERT INTO orders (user_id, user_name, phone, address, items, total) VALUES ({ph},{ph},{ph},{ph},{ph},{ph}) RETURNING id", 
                       (uid, uname, phone, addr, items_json, total))
            oid = cur.fetchone()['id']
        else:
            cur.execute(f"INSERT INTO orders (user_id, user_name, phone, address, items, total) VALUES ({ph},{ph},{ph},{ph},{ph},{ph})", 
                       (uid, uname, phone, addr, items_json, total))
            oid = cur.lastrowid
            
        conn.commit()
        conn.close()
        return oid

db = DBManager()

# --- 3. ВЕБ-СЕРВЕР (API) ---
async def api_get_products(request):
    return web.json_response(db.get_products())

async def api_save_product(request):
    try:
        data = await request.json()
        db.save_product(data)
        return web.json_response({"status": "ok"})
    except Exception as e:
        log.error(f"Save error: {e}")
        return web.json_response({"status": "error", "msg": str(e)}, status=500)

async def api_delete_product(request):
    pid = request.match_info['id']
    db.delete_product(pid)
    return web.json_response({"status": "ok"})

async def api_toggle_stock(request):
    data = await request.json()
    db.toggle_stock(data['id'], data['status'])
    return web.json_response({"status": "ok"})

async def api_toggle_size(request):
    data = await request.json()
    db.toggle_size_stock(data['id'], data['size'], data['status'])
    return web.json_response({"status": "ok"})

async def serve_index(request):
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")
    except FileNotFoundError:
        return web.Response(text="index.html not found", status=404)

# --- 4. ТЕЛЕГРАМ БОТ ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.types import Message, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

class OrderFlow(StatesGroup):
    contact = State()
    address = State()

async def cmd_start(m: Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛒 Открыть магазин", web_app=WebAppInfo(url=f"{settings.BASE_URL}/"))]],
        resize_keyboard=True
    )
    await m.answer(f"👋 Привет, {m.from_user.first_name}!\nДобро пожаловать в KOS Sport. Нажмите кнопку ниже, чтобы открыть каталог.", reply_markup=kb)

async def on_webapp_data(m: Message, state: FSMContext):
    try:
        data = json.loads(m.web_app_data.data)
        items = data.get('items', [])
        total = data.get('total_price', 0)
        
        await state.update_data(items=items, total=total)
        await state.set_state(OrderFlow.contact)
        
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Отправить телефон", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
        
        # Формируем чек
        text = "📋 <b>Ваш заказ:</b>\n\n"
        for i in items:
            text += f"▪️ {i['name']} ({i['size']})\n   {i['qty']} x {i['price']:,.0f} UZS\n"
        text += f"\n<b>Итого: {total:,.0f} UZS</b>"
        text += "\n\n👇 Пожалуйста, отправьте ваш контакт для связи."
        
        await m.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.error(e)
        await m.answer("Ошибка обработки данных. Попробуйте снова.")

async def process_contact(m: Message, state: FSMContext):
    phone = m.contact.phone_number if m.contact else m.text
    await state.update_data(phone=phone, name=m.from_user.full_name)
    await state.set_state(OrderFlow.address)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Локация", request_location=True), KeyboardButton(text="Пропустить")]], resize_keyboard=True, one_time_keyboard=True)
    await m.answer("📍 Куда доставить заказ? (Отправьте локацию или напишите адрес текстом)", reply_markup=kb)

async def process_finish(m: Message, state: FSMContext):
    data = await state.get_data()
    addr = f"Гео: {m.location.latitude},{m.location.longitude}" if m.location else m.text
    
    # Сохраняем в БД
    order_id = db.add_order(m.from_user.id, data['name'], data['phone'], addr, data['items'], data['total'])
    
    # Ответ пользователю
    await m.answer(
        f"✅ <b>Заказ №{order_id} успешно оформлен!</b>\n\nСумма: {data['total']:,.0f} UZS\nМенеджер свяжется с вами по номеру: {data['phone']}", 
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🛒 Открыть магазин", web_app=WebAppInfo(url=f"{settings.BASE_URL}/"))]], resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )
    
    # Уведомление админу
    if settings.ADMIN_ID:
        txt = f"🆕 <b>Новый заказ №{order_id}</b>\n👤 {data['name']}\n📞 {data['phone']}\n📍 {addr}\n💰 <b>{data['total']:,.0f} UZS</b>\n\n📦 <b>Состав:</b>\n"
        for i in data['items']:
            txt += f"- {i['name']} ({i['size']}) x{i['qty']}\n"
            
        try: await m.bot.send_message(settings.ADMIN_ID, txt, parse_mode=ParseMode.HTML)
        except: pass
    
    await state.clear()

async def main():
    # WEB APP
    app = web.Application(client_max_size=1024**2*10) # 10MB upload limit
    app.router.add_get("/", serve_index)
    app.router.add_get("/api/products", api_get_products)
    app.router.add_post("/api/products", api_save_product)
    app.router.add_delete("/api/products/{id}", api_delete_product)
    app.router.add_post("/api/stock", api_toggle_stock)
    app.router.add_post("/api/size", api_toggle_size)
    
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', settings.PORT).start()
    
    # BOT
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
