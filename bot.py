import sys
import os

# 1. THIS PATTERN MUST COME ABSOLUTELY FIRST
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
if os.path.dirname(current_dir) not in sys.path:
    sys.path.append(os.path.dirname(current_dir))

# 2. ONLY NOW YOU CAN IMPORT RAW MODULES
import asyncio
import json
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
# ... rest of your code ...
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from config import settings
from server import create_web_app
from orders_db import add_order, list_user_orders

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
log = logging.getLogger("bot")

# --- СОСТОЯНИЯ (ЭТАПЫ ЗАКАЗА) ---
class OrderStates(StatesGroup):
    waiting_for_contact = State()
    waiting_for_location = State()

# --- КЛАВИАТУРЫ ---
def webapp_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(
                text="🛒 Открыть магазин",
                web_app=WebAppInfo(url=f"{settings.PUBLIC_BASE_URL}/")
            )
        ]],
        resize_keyboard=True,
        input_field_placeholder="Нажмите кнопку ниже 👇"
    )

def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="📱 Отправить мой телефон", request_contact=True)
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Нажмите кнопку для отправки контакта"
    )

def location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="📍 Отправить локацию", request_location=True),
            KeyboardButton(text="⏩ Пропустить (введу адрес вручную)")
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Нажмите кнопку для отправки локации"
    )

# --- ХЕНДЛЕРЫ КОМАНД ---

async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Добро пожаловать в KOS Sport.\n"
        "Нажмите кнопку, чтобы собрать корзину.",
        reply_markup=webapp_keyboard()
    )

async def cmd_help(message: Message):
    await message.answer("Команды:\n/start — Открыть магазин\n/orders — История заказов")

async def cmd_orders(message: Message):
    orders = list_user_orders(message.from_user.id, 5)
    if not orders:
        await message.answer("Список заказов пуст.")
        return
    text = "📂 <b>Ваши заказы:</b>\n"
    for o in orders:
        text += f"- №{o.id}: {o.total:,.0f} UZS\n"
    await message.answer(text, parse_mode=ParseMode.HTML)

# --- ШАГ 1: ПОЛУЧЕНИЕ КОРЗИНЫ ИЗ WEB APP ---
async def on_webapp_data(message: types.Message, state: FSMContext):
    try:
        # 1. Получаем данные
        data = json.loads(message.web_app_data.data)
        items = data.get("cart") or data.get("items", [])
        
        if not items:
            await message.answer("⚠️ Корзина пуста")
            return

        # 2. Считаем сумму
        # Обратите внимание: сайт присылает 'qty' и 'size', а не 'quantity'
        total = sum(float(i.get("price", 0)) * int(i.get("qty", 1)) for i in items)

        # 3. Сохраняем корзину в память бота (FSM)
        await state.update_data(cart_items=items, total_price=total)

        # 4. Формируем предварительный просмотр для пользователя
        items_text = ""
        for item in items:
            # Получаем размер, если он есть
            size_val = item.get('size', '-')
            size_info = f"(Размер: {size_val})" if size_val and size_val != "-" else ""
            
            items_text += f"👟 {item.get('name')} {size_info}\n"
            items_text += f"   └ {item.get('qty')} шт. x {item.get('price')} UZS\n"

        # 5. Отправляем ответ и переходим к следующему шагу
        await state.set_state(OrderStates.waiting_for_contact)
        await message.answer(
            f"✅ <b>Заказ принят в обработку!</b>\n\n"
            f"{items_text}\n"
            f"💰 <b>Итого: {total:,.0f} UZS</b>".replace(",", " ") + "\n\n"
            "Теперь отправьте ваш номер телефона, чтобы мы могли связаться с вами.",
            reply_markup=contact_keyboard(),
            parse_mode="HTML"
        )

    except Exception as e:
        log.error(f"Error webapp: {e}")
        await message.answer("Ошибка данных. Попробуйте снова.")

# --- ШАГ 2: ПОЛУЧЕНИЕ КОНТАКТА ---
async def process_contact(message: Message, state: FSMContext):
    if message.contact:
        phone = message.contact.phone_number
        name = message.contact.first_name
    else:
        # Если юзер ввел текстом
        phone = message.text
        name = message.from_user.full_name

    await state.update_data(user_phone=phone, user_name=name)
    
    # Переходим к запросу локации
    await state.set_state(OrderStates.waiting_for_location)
    await message.answer(
        "Отлично! Теперь укажите, куда доставить заказ.\n"
        "Нажмите кнопку <b>«📍 Отправить локацию»</b> или напишите адрес текстом.",
        reply_markup=location_keyboard(),
        parse_mode=ParseMode.HTML
    )

# --- ШАГ 3: ПОЛУЧЕНИЕ ЛОКАЦИИ И ФИНАЛ ---
async def process_location(message: Message, state: FSMContext):
    user_data = await state.get_data()
    cart_items = user_data.get('cart_items')
    total = user_data.get('total_price')
    phone = user_data.get('user_phone')
    name = user_data.get('user_name')

    # Определяем адрес
    if message.location:
        lat = message.location.latitude
        lon = message.location.longitude
        address = f"Геопозиция: https://maps.google.com/?q={lat},{lon}"
    else:
        address = message.text if message.text != "⏩ Пропустить (введу адрес вручную)" else "Адрес не указан (уточнить по телефону)"

    # --- СОХРАНЕНИЕ В БАЗУ ---
    # !!! ИСПРАВЛЕНИЕ: Используем правильные ключи 'qty' и 'size', которые пришли с сайта !!!
    items_for_db = []
    for item in cart_items:
        items_for_db.append({
            "title": item.get("name", "Товар"),
            "price": float(item.get("price", 0)),
            "qty": int(item.get("qty", 1)),          # ИСПРАВЛЕНО: было quantity -> стало qty
            "size": item.get("size", "-")            # ИСПРАВЛЕНО: было selectedSize -> стало size
        })

    # Сохраняем
    order_id = add_order(
        user_id=message.from_user.id,
        username=message.from_user.username,
        name=f"{name} ({phone})", 
        address=address,
        items=items_for_db,
        total=total
    )

    # --- ФИНАЛЬНЫЙ ЧЕК ПОЛЬЗОВАТЕЛЮ ---
    receipt = [
        f"✅ <b>Заказ №{order_id} успешно оформлен!</b>",
        "--------------------------------",
        f"👤 <b>Заказчик:</b> {name}",
        f"📞 <b>Телефон:</b> {phone}",
        f"🚚 <b>Доставка:</b> {address}",
        "--------------------------------",
        "<b>Товары:</b>"
    ]
    
    for it in items_for_db:
        # Показываем размер только если он не пустой и не "-"
        size_str = f"[{it['size']}] " if it['size'] and it['size'] != "-" else ""
        receipt.append(f"• {it['title']} {size_str}x{it['qty']} — {it['price']*it['qty']:,.0f} UZS")
    
    receipt.append(f"\n💰 <b>ИТОГО К ОПЛАТЕ: {total:,.0f} UZS</b>")
    receipt.append("\n<i>Менеджер свяжется с вами в ближайшее время.</i>")

    # Сбрасываем клавиатуру и состояние
    await message.answer("\n".join(receipt), reply_markup=webapp_keyboard(), parse_mode=ParseMode.HTML)
    await state.clear()

    # --- УВЕДОМЛЕНИЕ АДМИНУ ---
    if settings.ADMIN_CHAT_ID:
        try:
            admin_msg = [
                f"🆕 <b>Новый заказ №{order_id}</b>",
                f"👤 Клиент: <a href='tg://user?id={message.from_user.id}'>{name}</a>",
                f"📞 Тел: <code>{phone}</code>",
                f"📍 Адрес: {address}",
                f"💰 Сумма: {total:,.0f} UZS",
                "",
                "<b>Состав:</b>"
            ]
            for it in items_for_db:
                s_txt = f"({it['size']})" if it['size'] and it['size'] != "-" else ""
                admin_msg.append(f"- {it['title']} {s_txt} x{it['qty']}")
            
            await message.bot.send_message(settings.ADMIN_CHAT_ID, "\n".join(admin_msg), parse_mode=ParseMode.HTML)
            
            if message.location:
                await message.bot.send_location(settings.ADMIN_CHAT_ID, latitude=message.location.latitude, longitude=message.location.longitude)
                
        except Exception as e:
            log.error(f"Ошибка уведомления админа: {e}")


async def main():
    # Запуск сайта
    web_app = create_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, settings.WEB_HOST, settings.WEB_PORT)
    await site.start()
    
    log.info(f"🚀 Сервер: http://{settings.WEB_HOST}:{settings.WEB_PORT}")

    # Запуск бота
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    
    await bot.delete_webhook(drop_pending_updates=True)

    # Регистрация хендлеров
    dp.message.register(cmd_start,  CommandStart())
    dp.message.register(cmd_help,   Command("help"))
    dp.message.register(cmd_orders, Command("orders"))
    
    dp.message.register(on_webapp_data, F.web_app_data)
    dp.message.register(process_contact, OrderStates.waiting_for_contact)
    dp.message.register(process_location, OrderStates.waiting_for_location)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
