import asyncio
import json
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime

from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

# PostgreSQL — используется на Render/Neon, если доступен и настроен.
# На локальной машине или без DATABASE_URL приложение автоматически откатывается на SQLite.
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

load_dotenv()

# --- 1. НАСТРОЙКИ ---
class Settings:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    ADMIN_ID = str(os.getenv("ADMIN_ID", "")).strip()
    DATABASE_URL = os.getenv("DATABASE_URL")
    PORT = int(os.getenv("PORT", 8000))
    BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    # Секретный код для входа в админ-панель через строку поиска на сайте.
    # Задаётся переменной окружения (например, в Render), а не хранится в исходниках фронтенда.
    ADMIN_SEARCH_CODE = os.getenv("ADMIN_SEARCH_CODE", "4443")


settings = Settings()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
log = logging.getLogger("bot")

if not settings.BOT_TOKEN:
    log.error("❌ Переменная BOT_TOKEN не найдена. Укажите её в .env перед запуском.")
    sys.exit(1)

# Глобальный экземпляр бота используется и в поллинге, и в веб-обработчиках API
bot = Bot(token=settings.BOT_TOKEN)


# --- 2. МЕНЕДЖЕР БАЗЫ ДАННЫХ ---
class DBManager:
    """
    Единый слой доступа к данным поверх PostgreSQL (Neon/Render) или SQLite (локально).
    Каждый публичный метод сам открывает и гарантированно закрывает соединение
    (через _connection), поэтому исключение внутри запроса не приведёт к утечке коннекшена.
    """

    def __init__(self):
        self.is_pg = bool(settings.DATABASE_URL and psycopg2)
        self._log_backend_status()
        self.init_db()

    # --- Подключение ---
    def _log_backend_status(self):
        log.info("------------------------------------------------")
        if not settings.DATABASE_URL:
            log.warning("❌ Переменная DATABASE_URL не найдена!")
        else:
            log.info("✅ Переменная DATABASE_URL найдена.")

        if not psycopg2:
            log.warning("❌ Библиотека 'psycopg2' не установлена!")
        else:
            log.info("✅ Библиотека psycopg2 успешно загружена")

        if self.is_pg:
            log.info("🚀 Подключаемся к PostgreSQL (Neon)...")
        else:
            log.warning("⚠️ ПЕРЕКЛЮЧЕНИЕ НА SQLite (файловая база)")
        log.info("------------------------------------------------")

    def _raw_conn(self):
        if self.is_pg:
            url = settings.DATABASE_URL
            if "sslmode=" not in url:
                # Гарантируем SSL для защищённого подключения к Neon, независимо от того,
                # есть ли в URL уже другие query-параметры.
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}sslmode=require"
            return psycopg2.connect(url, cursor_factory=RealDictCursor)

        conn = sqlite3.connect("orders.db")
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        """Контекстный менеджер: соединение всегда закрывается, даже при ошибке."""
        conn = self._raw_conn()
        try:
            yield conn
        finally:
            conn.close()

    # --- Инициализация и самовосстановление схемы ---
    def init_db(self):
        with self._connection() as conn:
            cur = conn.cursor()

            if self.is_pg:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS products (
                        id TEXT PRIMARY KEY,
                        name TEXT,
                        price REAL,
                        description TEXT,
                        category TEXT,
                        images JSONB,
                        sizes JSONB,
                        is_available INTEGER DEFAULT 1,
                        size_chart TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT,
                        user_name TEXT,
                        phone TEXT,
                        address TEXT,
                        items JSONB,
                        total REAL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            else:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS products (
                        id TEXT PRIMARY KEY,
                        name TEXT,
                        price REAL,
                        description TEXT,
                        category TEXT,
                        images TEXT,
                        sizes TEXT,
                        is_available INTEGER DEFAULT 1,
                        size_chart TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id BIGINT,
                        user_name TEXT,
                        phone TEXT,
                        address TEXT,
                        items TEXT,
                        total REAL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            conn.commit()

            # Автомиграция: добавляем колонки, которых могло не быть в более старой базе
            columns_to_add = [
                ("reviews", "JSONB DEFAULT '[]'" if self.is_pg else "TEXT DEFAULT '[]'"),
                ("ratings", "JSONB DEFAULT '[]'" if self.is_pg else "TEXT DEFAULT '[]'"),
                ("description", "TEXT"),
                ("size_chart", "TEXT"),
            ]
            for col_name, col_def in columns_to_add:
                try:
                    if self.is_pg:
                        cur.execute(f"ALTER TABLE products ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
                    else:
                        cur.execute("PRAGMA table_info(products)")
                        existing_cols = [row[1] for row in cur.fetchall()]
                        if col_name not in existing_cols:
                            cur.execute(f"ALTER TABLE products ADD COLUMN {col_name} {col_def}")
                except Exception as e:
                    log.warning(f"Не удалось добавить колонку {col_name} (возможно, уже есть): {e}")
            conn.commit()

            # Наполнение витрины дефолтными товарами при первом запуске
            cur.execute("SELECT COUNT(*) as count FROM products")
            count_row = cur.fetchone()
            count = count_row["count"] if isinstance(count_row, dict) else count_row[0]

            if count == 0:
                self._seed_default_products(conn, cur)

    def _seed_default_products(self, conn, cur):
        log.info("База данных пуста. Производится наполнение девайсами WEISI TECH...")
        default_products = [
            {
                "id": "p_mario",
                "name": "Чехол Super Mario для iPad Pro 13 M4/M5",
                "price": 345000.0,
                "category": "Accessories",
                "description": "Качественный защитный чехол с уникальным ярким дизайном легендарного Супер Марио. Идеально садится на iPad Pro 13 (процессоры M4 и M5). Имеет удобную складывающуюся подставку и отделение под Apple Pencil.",
                "images": ["https://images.unsplash.com/photo-1607604276583-eef5d076aa5f?auto=format&fit=crop&q=80&w=600"],
                "sizes": {"Красный": {"stock": 15, "price": 345000.0}, "Синий": {"stock": 5, "price": 320000.0}},
                "isAvailable": True,
                "sizeChart": "Характеристика,Значение\nСовместимость,iPad Pro 13 M4/M5\nМатериал,Силикон / Микрофибра\nПринт,Супер Марио\nВес,180 г",
                "reviews": [],
                "ratings": [5, 5, 5],
            },
            {
                "id": "mock1",
                "name": "Смартфон WEISI Phone 15 Ultra 512GB",
                "price": 14500000.0,
                "category": "Phones",
                "description": "Флагманский девайс с потрясающим 120Hz LTPO-экраном, новейшим процессором 3-нм класса и улучшенной оптической стабилизацией при съемке 8K видео.",
                "images": ["https://images.unsplash.com/photo-1511707171634-5f897ff02aa9?auto=format&fit=crop&q=80&w=600"],
                "sizes": {"128GB": {"stock": 20, "price": 12500000.0}, "256GB": {"stock": 15, "price": 13500000.0}, "512GB": {"stock": 0, "price": 14500000.0}},
                "isAvailable": True,
                "sizeChart": "Характеристика,Значение\nЭкран,6.7\" OLED 120Hz\nПроцессор,Super M4 Pro\nПамять,512 GB\nКамера,108+48+12 Мп\nБатарея,5000 мАч",
                "reviews": [],
                "ratings": [5, 4, 5, 5],
            },
            {
                "id": "mock2",
                "name": "Ультрабук WEISI Book Pro 14 Slate",
                "price": 18900000.0,
                "category": "Laptops",
                "description": "Производительный и тонкий ноутбук в алюминиевом корпусе. Батарея держит до 18 часов воспроизведения видео. Бесшумное охлаждение.",
                "images": ["https://images.unsplash.com/photo-1496181130204-7552cc1524e2?auto=format&fit=crop&q=80&w=600"],
                "sizes": {"8GB / 256GB": {"stock": 3, "price": 17500000.0}, "16GB / 512GB": {"stock": 10, "price": 18900000.0}},
                "isAvailable": True,
                "sizeChart": "Характеристика,Значение\nЭкран,14.2\" Liquid Retina XDR\nПроцессор,M3 Pro Max\nОЗУ,16 GB\nНакопитель,512 GB SSD",
                "reviews": [],
                "ratings": [5, 4, 5, 5],
            },
            {
                "id": "mock3",
                "name": "Наушники с шумоподавлением SoundMax Studio",
                "price": 2450000.0,
                "category": "Audio",
                "description": "Беспроводные полноразмерные наушники с лучшим на рынке гибридным шумоподавлением. Чистый детализированный звук Hi-Res Audio.",
                "images": ["https://images.unsplash.com/photo-1505740420928-5e560c06d30e?auto=format&fit=crop&q=80&w=600"],
                "sizes": {"Черный Carbon": {"stock": 12, "price": None}, "Белый Platinum": {"stock": 0, "price": None}},
                "isAvailable": True,
                "sizeChart": "Характеристика,Значение\nТип,Полноразмерные\nШумоподавление,Активное (ANC)\nВремя работы,до 30 часов\nВерсия Bluetooth,5.3",
                "reviews": [],
                "ratings": [5, 4, 5, 5],
            },
        ]

        for p in default_products:
            images_json = json.dumps(p["images"])
            sizes_json = json.dumps(p["sizes"])
            reviews_json = json.dumps(p["reviews"])
            ratings_json = json.dumps(p["ratings"])

            if self.is_pg:
                cur.execute(
                    """
                    INSERT INTO products (id, name, price, description, category, images, sizes, is_available, size_chart, reviews, ratings)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb)
                    """,
                    (p["id"], p["name"], p["price"], p["description"], p["category"], images_json, sizes_json,
                     1 if p["isAvailable"] else 0, p["sizeChart"], reviews_json, ratings_json),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO products (id, name, price, description, category, images, sizes, is_available, size_chart, reviews, ratings)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (p["id"], p["name"], p["price"], p["description"], p["category"], images_json, sizes_json,
                     1 if p["isAvailable"] else 0, p["sizeChart"], reviews_json, ratings_json),
                )
        conn.commit()
        log.info("Наполнение успешно завершено!")

    # --- Товары ---
    def get_products(self):
        with self._connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM products")
            rows = cur.fetchall()

            res = []
            for r in rows:
                images = self._as_json(r["images"], [])
                sizes = self._as_json(r["sizes"], {})
                reviews = self._as_json(r["reviews"], [])
                ratings = self._as_json(r["ratings"], [])

                res.append({
                    "id": r["id"], "name": r["name"], "price": r["price"],
                    "description": r["description"], "category": r["category"],
                    "images": images, "sizes": sizes,
                    "isAvailable": bool(r["is_available"]), "sizeChart": r["size_chart"],
                    "reviews": reviews, "ratings": ratings,
                })
            return res

    @staticmethod
    def _as_json(value, default):
        """Postgres(JSONB) отдаёт уже распарсенный объект, SQLite(TEXT) — строку."""
        if value is None:
            return default
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return default
        return value

    def save_product(self, p):
        with self._connection() as conn:
            cur = conn.cursor()
            images_json = json.dumps(p["images"])
            sizes_json = json.dumps(p["sizes"])

            # Подтягиваем текущие отзывы/рейтинги, чтобы не затереть их при редактировании товара
            if self.is_pg:
                cur.execute("SELECT reviews, ratings FROM products WHERE id=%s", (p["id"],))
            else:
                cur.execute("SELECT reviews, ratings FROM products WHERE id=?", (p["id"],))
            row = cur.fetchone()

            if row:
                reviews_json = json.dumps(self._as_json(row["reviews"], []))
                ratings_json = json.dumps(self._as_json(row["ratings"], []))
            else:
                reviews_json = json.dumps([])
                ratings_json = json.dumps([])

            if self.is_pg:
                cur.execute(
                    """
                    INSERT INTO products (id, name, price, description, category, images, sizes, is_available, size_chart, reviews, ratings)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb)
                    ON CONFLICT (id) DO UPDATE SET
                        name=EXCLUDED.name, price=EXCLUDED.price, description=EXCLUDED.description,
                        category=EXCLUDED.category, images=EXCLUDED.images, sizes=EXCLUDED.sizes,
                        is_available=EXCLUDED.is_available, size_chart=EXCLUDED.size_chart
                    """,
                    (p["id"], p["name"], p["price"], p.get("description", ""), p["category"], images_json,
                     sizes_json, int(p["isAvailable"]), p.get("sizeChart", ""), reviews_json, ratings_json),
                )
            else:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO products (id, name, price, description, category, images, sizes, is_available, size_chart, reviews, ratings)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (p["id"], p["name"], p["price"], p.get("description", ""), p["category"], images_json,
                     sizes_json, int(p["isAvailable"]), p.get("sizeChart", ""), reviews_json, ratings_json),
                )
            conn.commit()

    def delete_product(self, pid):
        with self._connection() as conn:
            cur = conn.cursor()
            if self.is_pg:
                cur.execute("DELETE FROM products WHERE id=%s", (pid,))
            else:
                cur.execute("DELETE FROM products WHERE id=?", (pid,))
            conn.commit()

    def toggle_stock(self, pid, status):
        with self._connection() as conn:
            cur = conn.cursor()
            if self.is_pg:
                cur.execute("UPDATE products SET is_available=%s WHERE id=%s", (int(status), pid))
            else:
                cur.execute("UPDATE products SET is_available=? WHERE id=?", (int(status), pid))
            conn.commit()

    def toggle_size_stock(self, pid, size, status):
        with self._connection() as conn:
            cur = conn.cursor()
            if self.is_pg:
                cur.execute("SELECT sizes FROM products WHERE id=%s", (pid,))
            else:
                cur.execute("SELECT sizes FROM products WHERE id=?", (pid,))
            row = cur.fetchone()
            if not row:
                return

            current_sizes = self._as_json(row["sizes"], {})
            if not isinstance(current_sizes, dict):
                current_sizes = {}

            if isinstance(status, dict):
                # Новый формат: {"stock": N, "price": N|null}
                current_sizes[size] = status
            elif size in current_sizes and isinstance(current_sizes[size], dict):
                current_sizes[size]["stock"] = int(status)
            else:
                current_sizes[size] = {"stock": int(status), "price": None}

            new_json = json.dumps(current_sizes)
            if self.is_pg:
                cur.execute("UPDATE products SET sizes=%s::jsonb WHERE id=%s", (new_json, pid))
            else:
                cur.execute("UPDATE products SET sizes=? WHERE id=?", (new_json, pid))
            conn.commit()

    def add_feedback(self, pid, author, text, rating):
        with self._connection() as conn:
            cur = conn.cursor()
            if self.is_pg:
                cur.execute("SELECT reviews, ratings FROM products WHERE id=%s", (pid,))
            else:
                cur.execute("SELECT reviews, ratings FROM products WHERE id=?", (pid,))
            row = cur.fetchone()
            if not row:
                return

            reviews = self._as_json(row["reviews"], [])
            ratings = self._as_json(row["ratings"], [])

            if text:
                reviews.append({
                    "author": author,
                    "text": text,
                    "date": datetime.now().strftime("%d.%m.%Y"),
                    "rating": rating,
                })
            ratings.append(rating)

            if self.is_pg:
                cur.execute(
                    "UPDATE products SET reviews=%s::jsonb, ratings=%s::jsonb WHERE id=%s",
                    (json.dumps(reviews), json.dumps(ratings), pid),
                )
            else:
                cur.execute(
                    "UPDATE products SET reviews=?, ratings=? WHERE id=?",
                    (json.dumps(reviews), json.dumps(ratings), pid),
                )
            conn.commit()

    # --- Заказы ---
    def add_order(self, uid, uname, phone, addr, items, total):
        with self._connection() as conn:
            cur = conn.cursor()
            items_json = json.dumps(items)

            if self.is_pg:
                cur.execute(
                    "INSERT INTO orders (user_id, user_name, phone, address, items, total) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                    (uid, uname, phone, addr, items_json, total),
                )
                order_id = cur.fetchone()["id"]
            else:
                cur.execute(
                    "INSERT INTO orders (user_id, user_name, phone, address, items, total) VALUES (?,?,?,?,?,?)",
                    (uid, uname, phone, addr, items_json, total),
                )
                order_id = cur.lastrowid

            conn.commit()
            return order_id

    def list_user_orders(self, uid, limit=5):
        with self._connection() as conn:
            cur = conn.cursor()
            if self.is_pg:
                cur.execute("SELECT * FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT %s", (uid, limit))
            else:
                cur.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit))
            rows = cur.fetchall()
            return [{"id": r["id"], "total": r["total"], "created_at": r["created_at"]} for r in rows]


db = DBManager()


async def run_db(func, *args, **kwargs):
    """
    Выполняет блокирующий вызов DBManager в отдельном потоке, не давая ему
    заблокировать событийный цикл aiohttp/aiogram (важно под нагрузкой).
    """
    return await asyncio.to_thread(func, *args, **kwargs)


# --- 3. ВЕБ-СЕРВЕР (API) ---
async def api_get_products(request):
    products = await run_db(db.get_products)
    return web.json_response(products)


async def api_save_product(request):
    try:
        data = await request.json()
        await run_db(db.save_product, data)
        return web.json_response({"status": "ok"})
    except Exception as e:
        log.error(f"Save error: {e}")
        return web.json_response({"status": "error", "msg": str(e)}, status=500)


async def api_delete_product(request):
    pid = request.match_info["id"]
    await run_db(db.delete_product, pid)
    return web.json_response({"status": "ok"})


async def api_toggle_stock(request):
    try:
        data = await request.json()
        await run_db(db.toggle_stock, data["id"], data["status"])
        return web.json_response({"status": "ok"})
    except Exception as e:
        log.error(f"Toggle stock error: {e}")
        return web.json_response({"status": "error", "msg": str(e)}, status=500)


async def api_toggle_size(request):
    try:
        data = await request.json()
        await run_db(db.toggle_size_stock, data["id"], data["size"], data["status"])
        return web.json_response({"status": "ok"})
    except Exception as e:
        log.error(f"Toggle size error: {e}")
        return web.json_response({"status": "error", "msg": str(e)}, status=500)


async def api_save_feedback(request):
    try:
        data = await request.json()
        pid = data["id"]
        author = data["author"]
        text = data["text"]
        rating = int(data["rating"])

        await run_db(db.add_feedback, pid, author, text, rating)
        return web.json_response({"status": "ok"})
    except Exception as e:
        log.error(f"Feedback save error: {e}")
        return web.json_response({"status": "error", "msg": str(e)}, status=500)


def _build_admin_order_message(order_id, client_ref, phone, address, items, total):
    msg = (
        f"🆕 <b>ПОЛУЧЕН НОВЫЙ ЗАКАЗ №{order_id}</b>\n"
        f"──────────────────\n"
        f"👤 <b>Клиент:</b> {client_ref}\n"
        f"📞 <b>Телефон:</b> <code>{phone}</code>\n"
        f"📍 <b>Адрес:</b> {address}\n"
        f"──────────────────\n"
        f"📦 <b>Состав заказа:</b>\n"
    )
    for i in items:
        size_info = f" ({i['size']})" if i.get("size") and i.get("size") != "Standard" else ""
        msg += f"▪️ {i['name']}{size_info}\n   └ {i['qty']} шт. x {i['price']:,.0f} UZS\n"
    msg += f"──────────────────\n💰 <b>ИТОГО К ОПЛАТЕ: {total:,.0f} UZS</b>"
    return msg


async def api_create_order(request):
    try:
        data = await request.json()
        name = data.get("name")
        phone = data.get("phone")
        address = data.get("address")
        items = data.get("items", [])
        total = data.get("total_price", 0)
        user_id = int(data.get("user_id", 0))

        order_id = await run_db(db.add_order, user_id, name, phone, address, items, total)

        if settings.ADMIN_ID:
            client_ref = f"<a href='tg://user?id={user_id}'>{name}</a>" if user_id > 0 else f"{name} (через Web)"
            admin_msg = _build_admin_order_message(order_id, client_ref, phone, address, items, total)
            try:
                await bot.send_message(settings.ADMIN_ID, admin_msg, parse_mode=ParseMode.HTML)
            except Exception as admin_err:
                log.error(f"Не удалось отправить уведомление админу: {admin_err}")

        return web.json_response({"status": "ok", "order_id": order_id})
    except Exception as e:
        log.error(f"Ошибка сохранения заказа через сайт: {e}")
        return web.json_response({"status": "error", "msg": str(e)}, status=500)


async def api_admin_check(request):
    """Строгая проверка Telegram ID администратора — выполняется только на бэкенде."""
    try:
        data = await request.json()
        user_id = str(data.get("user_id", "")).strip()

        if user_id and settings.ADMIN_ID and user_id == settings.ADMIN_ID:
            return web.json_response({"is_admin": True})

        return web.json_response({
            "is_admin": False,
            "msg": f"Доступ заблокирован: Ваш ID ({user_id or 'не определен'}) не совпадает с ADMIN_ID!",
        })
    except Exception as e:
        return web.json_response({"is_admin": False, "msg": str(e)}, status=500)


async def serve_index(request):
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html = f.read()
        # Подставляем секретный код администратора из переменной окружения вместо
        # хранения его в открытом виде в исходнике фронтенда.
        html = html.replace("__ADMIN_SEARCH_CODE__", json.dumps(settings.ADMIN_SEARCH_CODE))
        return web.Response(text=html, content_type="text/html")
    except FileNotFoundError:
        return web.Response(text="index.html не найден на сервере", status=404)


# --- 4. ТЕЛЕГРАМ БОТ ---
class OrderFlow(StatesGroup):
    contact = State()
    address = State()


def main_kb():
    # ВАЖНО (подтверждено официальной документацией Telegram, core.telegram.org/bots/webapps):
    # Telegram.WebApp.sendData() работает ТОЛЬКО если мини-приложение запущено через кнопку
    # клавиатуры (KeyboardButton.web_app). Как следствие, initData (Telegram ID, имя, username)
    # при таком запуске будет ПУСТ — это взаимоисключающие требования платформы, а не
    # ограничение нашего кода. Так как оформление заказа через MainButton намеренно использует
    # именно sendData() и чат-флоу с ботом (см. sendOrder() на фронтенде и on_webapp_data ниже),
    # здесь используется кнопка клавиатуры. Побочный эффект: проверка админа, автозаполнение
    # имени и привязка заказа к user_id, завязанные на initData, работать не будут.
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛒 Открыть магазин", web_app=WebAppInfo(url=f"{settings.BASE_URL}/"))]],
        resize_keyboard=True,
    )


def contact_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def location_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
            [KeyboardButton(text="⏩ Пропустить (введу вручную)")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def cmd_start(m: Message):
    await m.answer(
        f"👋 <b>Привет, {m.from_user.first_name}!</b>\n\nДобро пожаловать в WEISI TECH.\nНажмите кнопку ниже, чтобы открыть каталог 👇",
        reply_markup=main_kb(),
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(m: Message):
    await m.answer("Команды:\n/start - Главное меню\n/orders - История заказов")


async def cmd_orders(m: Message):
    orders = await run_db(db.list_user_orders, m.from_user.id)
    if not orders:
        await m.answer("📭 У вас пока нет заказов.")
        return

    text = "📂 <b>История заказов:</b>\n\n"
    for o in orders:
        text += f"🔹 <b>Заказ №{o['id']}</b>\n💰 {o['total']:,.0f} UZS\n📅 {o['created_at']}\n\n"
    await m.answer(text, parse_mode=ParseMode.HTML)


async def on_webapp_data(m: Message, state: FSMContext):
    try:
        data = json.loads(m.web_app_data.data)
        items = data.get("items", [])
        total = data.get("total_price", 0)

        await state.update_data(items=items, total=total)
        await state.set_state(OrderFlow.contact)

        text = "📝 <b>Ваша корзина:</b>\n\n"
        for i in items:
            size_info = f"({i['size']})" if i["size"] and i["size"] != "Standard" else ""
            text += f"▪️ {i['name']} {size_info}\n   └ {i['qty']} шт. x {i['price']:,.0f} UZS\n"

        text += f"\n💳 <b>Итого: {total:,.0f} UZS</b>"
        text += "\n\n📞 <b>Шаг 1/2:</b> Отправьте ваш номер телефона."

        await m.answer(text, reply_markup=contact_kb(), parse_mode=ParseMode.HTML)
    except Exception as e:
        log.error(f"Ошибка разбора данных из WebApp: {e}")
        await m.answer("❌ Ошибка данных. Попробуйте снова.")


async def process_contact(m: Message, state: FSMContext):
    phone = m.contact.phone_number if m.contact else m.text
    await state.update_data(phone=phone, name=m.from_user.full_name)
    await state.set_state(OrderFlow.address)

    await m.answer(
        "📍 <b>Шаг 2/2:</b> Куда доставить заказ?\n\nНажмите <b>«Отправить геолокацию»</b> или напишите адрес текстом.",
        reply_markup=location_kb(),
        parse_mode=ParseMode.HTML,
    )


async def process_finish(m: Message, state: FSMContext):
    data = await state.get_data()

    if m.location:
        addr_text = "📍 Геолокация (см. карту)"
        lat, lon = m.location.latitude, m.location.longitude
        maps_link = f"https://maps.google.com/?q={lat},{lon}"
    else:
        addr_text = f"🏠 {m.text}"
        lat = lon = maps_link = None

    order_id = await run_db(db.add_order, m.from_user.id, data["name"], data["phone"], addr_text, data["items"], data["total"])

    receipt = (
        f"✅ <b>Заказ №{order_id} успешно оформлен!</b>\n"
        "──────────────────\n"
        f"👤 <b>Заказчик:</b> {data['name']}\n"
        f"📞 <b>Телефон:</b> {data['phone']}\n"
        f"🚚 <b>Доставка:</b> {addr_text}\n"
        "──────────────────\n"
        f"💰 <b>К ОПЛАТЕ: {data['total']:,.0f} UZS</b>\n\n"
        "<i>Менеджер свяжется с вами в ближайшее время.</i>"
    )
    await m.answer(receipt, reply_markup=main_kb(), parse_mode=ParseMode.HTML)

    if settings.ADMIN_ID:
        try:
            admin_msg = (
                f"🆕 <b>НОВЫЙ ЗАКАЗ №{order_id}</b>\n"
                f"👤 Клиент: <a href='tg://user?id={m.from_user.id}'>{data['name']}</a>\n"
                f"📞 Тел: <code>{data['phone']}</code>\n"
                f"📍 Адрес: {addr_text}\n"
                f"🔗 Карты: {maps_link if maps_link else 'Нет'}\n\n"
                "📦 <b>Состав:</b>\n"
            )
            for i in data["items"]:
                size_info = f"({i['size']})" if i["size"] and i["size"] != "Standard" else ""
                admin_msg += f"- {i['name']} {size_info} x{i['qty']}\n"
            admin_msg += f"\n💰 <b>Сумма: {data['total']:,.0f} UZS</b>"

            await m.bot.send_message(settings.ADMIN_ID, admin_msg, parse_mode=ParseMode.HTML)
            if lat and lon:
                await m.bot.send_location(settings.ADMIN_ID, latitude=lat, longitude=lon)
        except Exception as e:
            log.error(f"Ошибка уведомления админа: {e}")

    await state.clear()


async def main():
    app = web.Application(client_max_size=1024 ** 2 * 20)
    app.router.add_get("/", serve_index)
    app.router.add_get("/api/products", api_get_products)
    app.router.add_post("/api/products", api_save_product)
    app.router.add_delete("/api/products/{id}", api_delete_product)
    app.router.add_post("/api/stock", api_toggle_stock)
    app.router.add_post("/api/size", api_toggle_size)
    app.router.add_post("/api/products/feedback", api_save_feedback)
    app.router.add_post("/api/orders", api_create_order)
    app.router.add_post("/api/admin_check", api_admin_check)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.PORT).start()
    log.info(f"🌐 Веб-сервер запущен на порту {settings.PORT}")

    dp = Dispatcher(storage=MemoryStorage())
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
