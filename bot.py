import asyncio
import json
import logging
import os
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
        self.check_connection_status()
        self.is_pg = bool(settings.DATABASE_URL and psycopg2)
        self.init_db()

    def check_connection_status(self):
        print("------------------------------------------------")
        if not settings.DATABASE_URL:
            log.warning("❌ [ОШИБКА] Переменная DATABASE_URL не найдена!")
        else:
            log.info("✅ Переменная DATABASE_URL найдена.")

        if not psycopg2:
            log.warning("❌ [ОШИБКА] Библиотека 'psycopg2' не установлена!")
        else:
            log.info("✅ Библиотека psycopg2 успешно загружена")
        
        if settings.DATABASE_URL and psycopg2:
            log.info("🚀 Подключаемся к PostgreSQL (Neon)...")
        else:
            log.warning("⚠️ ПЕРЕКЛЮЧЕНИЕ НА SQLite (Файловая база)")
        print("------------------------------------------------")

    def get_conn(self):
        if self.is_pg:
            return psycopg2.connect(settings.DATABASE_URL, cursor_factory=RealDictCursor)
        conn = sqlite3.connect("weisi_orders.db")
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        conn = self.get_conn()
        cur = conn.cursor()
        
        id_serial = "SERIAL PRIMARY KEY" if self.is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        json_type = "JSONB" if self.is_pg else "TEXT"

        # Таблица продуктов (БЕЗ РАЗМЕРОВ)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT,
                price REAL,
                description TEXT,
                category TEXT,
                images {json_type},
                is_available INTEGER DEFAULT 1
            )
        """)
        
        # Таблица заказов (БЕЗ РАЗМЕРОВ)
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

        # НОВАЯ Таблица отзывов и оценок
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS reviews (
                id {id_serial},
                product_id TEXT,
                user_name TEXT,
                rating INTEGER,
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def get_products(self):
        conn = self.get_conn()
        cur = conn.cursor()
        
        # Получаем продукты
        cur.execute("SELECT * FROM products")
        rows = cur.fetchall()
        
        res = []
        for r in rows:
            images = r['images']
            if isinstance(images, str): 
                images = json.loads(images)
            
            # Подсчитываем отзывы и средний рейтинг для каждого товара в один проход
            pid = r['id']
            ph = "%s" if self.is_pg else "?"
            cur.execute(f"SELECT rating FROM reviews WHERE product_id={ph}", (pid,))
            review_rows = cur.fetchall()
            
            ratings = [rev['rating'] if isinstance(rev, dict) else rev[0] for rev in review_rows]
            avg_rating = sum(ratings) / len(ratings) if ratings else 0.0
            reviews_count = len(ratings)

            res.append({
                "id": r['id'], 
                "name": r['name'], 
                "price": r['price'],
                "description": r['description'], 
                "category": r['category'],
                "images": images, 
                "isAvailable": bool(r['is_available']),
                "avgRating": avg_rating,
                "reviewsCount": reviews_count
            })
        conn.close()
        return res

    def save_product(self, p):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        
        images_json = json.dumps(p['images'])
        
        if self.is_pg:
            sql = f"""
                INSERT INTO products (id, name, price, description, category, images, is_available)
                VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})
                ON CONFLICT (id) DO UPDATE SET 
                name=EXCLUDED.name, price=EXCLUDED.price, description=EXCLUDED.description,
                category=EXCLUDED.category, images=EXCLUDED.images, is_available=EXCLUDED.is_available
            """
        else:
            sql = f"INSERT OR REPLACE INTO products VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})"
            
        cur.execute(sql, (
            p['id'], p['name'], p['price'], p.get('description', ''), 
            p['category'], images_json, int(p['isAvailable'])
        ))
        conn.commit()
        conn.close()

    def delete_product(self, pid):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        cur.execute(f"DELETE FROM products WHERE id={ph}", (pid,))
        # Удаляем также связанные отзывы
        cur.execute(f"DELETE FROM reviews WHERE product_id={ph}", (pid,))
        conn.commit()
        conn.close()
    
    def toggle_stock(self, pid, status):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        cur.execute(f"UPDATE products SET is_available={ph} WHERE id={ph}", (int(status), pid))
        conn.commit()
        conn.close()

    def add_order(self, uid, uname, phone, addr, items, total):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        items_json = json.dumps(items)
        db_uid = None if uid == 0 else uid
        
        if self.is_pg:
            cur.execute(f"INSERT INTO orders (user_id, user_name, phone, address, items, total) VALUES ({ph},{ph},{ph},{ph},{ph},{ph}) RETURNING id", 
                       (db_uid, uname, phone, addr, items_json, total))
            row = cur.fetchone()
            oid = row['id'] if isinstance(row, dict) else row[0]
        else:
            cur.execute(f"INSERT INTO orders (user_id, user_name, phone, address, items, total) VALUES ({ph},{ph},{ph},{ph},{ph},{ph})", 
                       (db_uid, uname, phone, addr, items_json, total))
            oid = cur.lastrowid
            
        conn.commit()
        conn.close()
        return oid
    
    def add_review(self, product_id, user_name, rating, comment):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        
        cur.execute(f"INSERT INTO reviews (product_id, user_name, rating, comment) VALUES ({ph},{ph},{ph},{ph})", 
                   (product_id, user_name, int(rating), comment))
        conn.commit()
        conn.close()

    def get_reviews(self, product_id):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        cur.execute(f"SELECT * FROM reviews WHERE product_id={ph} ORDER BY created_at DESC", (product_id,))
        rows = cur.fetchall()
        reviews = []
        for r in rows:
            reviews.append({
                "id": r['id'],
                "product_id": r['product_id'],
                "user_name": r['user_name'],
                "rating": r['rating'],
                "comment": r['comment'],
                "created_at": str(r['created_at'])
            })
        conn.close()
        return reviews

    def list_user_orders(self, uid, limit=5):
        conn = self.get_conn()
        cur = conn.cursor()
        ph = "%s" if self.is_pg else "?"
        cur.execute(f"SELECT * FROM orders WHERE user_id={ph} ORDER BY id DESC LIMIT {limit}", (uid,))
        rows = cur.fetchall()
        orders = []
        for r in rows:
            orders.append({"id": r['id'], "total": r['total'], "created_at": r['created_at']})
        conn.close()
        return orders

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

async def api_get_reviews(request):
    pid = request.match_info['product_id']
    return web.json_response(db.get_reviews(pid))

async def api_post_review(request):
    try:
        pid = request.match_info['product_id']
        data = await request.json()
        db.add_review(pid, data['user_name'], data['rating'], data['comment'])
        return web.json_response({"status": "ok"})
    except Exception as e:
        log.error(f"Review API error: {e}")
        return web.json_response({"status": "error", "msg": str(e)}, status=500)

async def api_create_order(request):
    try:
        data = await request.json()
        name = data.get('user_name', 'Не указано')
        phone = data.get('phone', 'Не указано')
        address = data.get('address', 'Не указано')
        items = data.get('items', [])
        total = data.get('total_price', 0)

        order_id = db.add_order(0, name, phone, address, items, total)

        # Отправка уведомления администратору в Telegram
        try:
            bot = request.app['bot']
            if settings.ADMIN_ID:
                admin_msg = (
                    f"🌐 <b>WEISI — НОВЫЙ ЗАКАЗ С САЙТА №{order_id}</b>\n"
                    f"──────────────────\n"
                    f"👤 Клиент: {name}\n"
                    f"📞 Тел: <code>{phone}</code>\n"
                    f"📍 Адрес: {address}\n"
                    "──────────────────\n"
                    "📦 <b>Состав заказа:</b>\n"
                )
                for i in items:
                    admin_msg += f"- {i['name']} x{i['qty']} — {i['price']:,.0f} UZS\n"
                
                admin_msg += f"\n💰 <b>Сумма к оплате: {total:,.0f} UZS</b>"

                await bot.send_message(settings.ADMIN_ID, admin_msg, parse_mode="HTML")
            else:
                log.warning("⚠️ Заказ сохранен, но ADMIN_ID не настроен!")
        except Exception as telegram_error:
            log.error(f"❌ Ошибка уведомления в Telegram: {telegram_error}")

        return web.json_response({"status": "ok", "order_id": order_id})
    except Exception as e:
        log.error(f"Order API error: {e}")
        return web.json_response({"status": "error", "msg": str(e)}, status=500)

async def serve_index(request):
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")
    except FileNotFoundError:
        return web.Response(text="index.html not found", status=404)

# --- 4. ТЕЛЕГРАМ БОТ ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

class OrderFlow(StatesGroup):
    contact = State()
    address = State()

def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛒 Магазин WEISI", web_app=WebAppInfo(url=f"{settings.BASE_URL}/"))]],
        resize_keyboard=True,
        input_field_placeholder="Нажмите кнопку ниже 👇"
    )

def contact_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Отправить контакт", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)

def location_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Отправить геопозицию", request_location=True)], [KeyboardButton(text="⏩ Напишу вручную")]], resize_keyboard=True, one_time_keyboard=True)

async def cmd_start(m: Message):
    await m.answer(f"👋 <b>Привет, {m.from_user.first_name}!</b>\n\nДобро пожаловать в интернет-магазин сетевого оборудования и цифровой периферии <b>WEISI</b>.\n\nНажмите кнопку ниже, чтобы перейти к каталогу товаров 👇", reply_markup=main_kb(), parse_mode=ParseMode.HTML)

async def cmd_help(m: Message):
    await m.answer("Доступные команды:\n/start - Главное меню\n/orders - Ваши недавние заказы")

async def cmd_orders(m: Message):
    orders = db.list_user_orders(m.from_user.id)
    if not orders:
        await m.answer("📭 У вас пока нет оформленных заказов.")
        return
    text = "📂 <b>История ваших заказов:</b>\n\n"
    for o in orders:
        text += f"🔹 <b>Заказ №{o['id']}</b>\n💰 {o['total']:,.0f} UZS\n📅 {o['created_at']}\n\n"
    await m.answer(text, parse_mode=ParseMode.HTML)

# Обработка данных, если открывают через стандартное веб-приложение
async def on_webapp_data(m: Message, state: FSMContext):
    try:
        data = json.loads(m.web_app_data.data)
        items = data.get('items', [])
        total = data.get('total_price', 0)
        
        await state.update_data(items=items, total=total)
        await state.set_state(OrderFlow.contact)
        
        text = "📝 <b>Ваша корзина WEISI:</b>\n\n"
        for i in items:
            text += f"▪️ {i['name']}\n   └ {i['qty']} шт. x {i['price']:,.0f} UZS\n"
        
        text += f"\n💳 <b>Итого к оплате: {total:,.0f} UZS</b>"
        text += "\n\n📞 <b>Шаг 1/2:</b> Пожалуйста, отправьте ваш номер телефона с помощью кнопки."
        
        await m.answer(text, reply_markup=contact_kb(), parse_mode=ParseMode.HTML)
    except Exception as e:
        log.error(e)
        await m.answer("❌ Ошибка обработки данных. Попробуйте снова.")

async def process_contact(m: Message, state: FSMContext):
    phone = m.contact.phone_number if m.contact else m.text
    await state.update_data(phone=phone, name=m.from_user.full_name)
    await state.set_state(OrderFlow.address)
    
    await m.answer("📍 <b>Шаг 2/2:</b> Куда доставить заказ?\n\nОтправьте геолокацию или напишите ваш адрес текстом.", reply_markup=location_kb(), parse_mode=ParseMode.HTML)

async def process_finish(m: Message, state: FSMContext):
    data = await state.get_data()
    
    if m.location:
        addr_text = "📍 По геометке на карте"
        lat = m.location.latitude
        lon = m.location.longitude
        maps_link = f"https://maps.google.com/?q={lat},{lon}"
    else:
        addr_text = f"🏠 {m.text}"
        lat, lon, maps_link = None, None, None
    
    order_id = db.add_order(m.from_user.id, data['name'], data['phone'], addr_text, data['items'], data['total'])
    
    receipt = (
        f"✅ <b>Заказ №{order_id} в магазине WEISI успешно оформлен!</b>\n"
        "──────────────────\n"
        f"👤 <b>Заказчик:</b> {data['name']}\n"
        f"📞 <b>Телефон:</b> {data['phone']}\n"
        f"🚚 <b>Доставка:</b> {addr_text}\n"
        "──────────────────\n"
        f"💰 <b>К ОПЛАТЕ: {data['total']:,.0f} UZS</b>\n\n"
        "<i>Наш менеджер скоро свяжется с вами для подтверждения доставки!</i>"
    )
    
    await m.answer(receipt, reply_markup=main_kb(), parse_mode=ParseMode.HTML)
    
    if settings.ADMIN_ID:
        try:
            admin_msg = (
                f"🆕 <b>НОВЫЙ ЗАКАЗ №{order_id} (WEISI)</b>\n"
                f"👤 Клиент: <a href='tg://user?id={m.from_user.id}'>{data['name']}</a>\n"
                f"📞 Тел: <code>{data['phone']}</code>\n"
                f"📍 Адрес: {addr_text}\n"
                f"🔗 Карта: {maps_link if maps_link else 'Нет'}\n\n"
                "📦 <b>Состав:</b>\n"
            )
            for i in data['items']:
                admin_msg += f"- {i['name']} x{i['qty']}\n"
            
            admin_msg += f"\n💰 <b>Сумма: {data['total']:,.0f} UZS</b>"

            await m.bot.send_message(settings.ADMIN_ID, admin_msg, parse_mode=ParseMode.HTML)
            if lat and lon:
                await m.bot.send_location(settings.ADMIN_ID, latitude=lat, longitude=lon)
                
        except Exception as e:
            log.error(f"Ошибка уведомления админа: {e}")
    
    await state.clear()

async def main():
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    app = web.Application(client_max_size=1024**2*20)
    app['bot'] = bot 

    # Маршруты сервера
    app.router.add_get("/", serve_index)
    app.router.add_get("/api/products", api_get_products)
    app.router.add_post("/api/products", api_save_product)
    app.router.add_delete("/api/products/{id}", api_delete_product)
    app.router.add_post("/api/stock", api_toggle_stock)
    app.router.add_post("/api/orders", api_create_order)
    app.router.add_get("/api/products/{product_id}/reviews", api_get_reviews)
    app.router.add_post("/api/products/{product_id}/reviews", api_post_review)
    
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', settings.PORT).start()
    
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_orders, Command("orders"))
    dp.message.register(on_webapp_data, F.web_app_data)
    dp.message.register(process_contact, OrderFlow.contact)
    dp.message.register(process_finish, OrderFlow.address)
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())