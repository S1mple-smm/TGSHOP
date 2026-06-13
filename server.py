import os
import json
import logging
from pathlib import Path
from aiohttp import web
import asyncpg

BASE_DIR = Path(__file__).resolve().parent.parent
# Fallback directories routing matching source setup rules
if (BASE_DIR / "webapp").exists():
    WEB_DIR = BASE_DIR / "webapp"
else:
    WEB_DIR = BASE_DIR / "web"

STATIC_DIR = WEB_DIR / "static"
DATABASE_URL = os.getenv("DATABASE_URL")

logger = logging.getLogger("server")

async def get_db_connection():
    return await asyncpg.connect(DATABASE_URL, ssl="require")

# --- API HANDLERS (Neon.tech migration conversions) ---

async def get_products(request):
    conn = await get_db_connection()
    try:
        query = """
            SELECT id, name, price, description, category, images, sizes, is_available as "isAvailable", size_chart as "sizeChart"
            FROM products 
            ORDER BY created_at DESC;
        """
        records = await conn.fetch(query)
        products = []
        for r in records:
            prod = dict(r)
            # Numeric conversion to integer/float type matching json interface standard rules
            prod['price'] = float(prod['price'])
            # Cast JSONB field directly back into clean structural dict layout mapping
            prod['sizes'] = json.loads(prod['sizes']) if isinstance(prod['sizes'], str) else prod['sizes']
            products.append(prod)
            
        return web.json_response(products)
    except Exception as e:
        logger.error(f"Failed pulling database inventory items list: {e}")
        return web.json_response({"error": str(e)}, status=500)
    finally:
        await conn.close()

async def add_product(request):
    try:
        data = await request.json()
        conn = await get_db_connection()
        
        prod_id = data.get('id') or f"p{int(time.time() * 1000)}"
        sizes_json = json.dumps(data.get('sizes', {}))
        
        query = """
            INSERT INTO products (id, name, price, description, category, images, sizes, is_available, size_chart)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);
        """
        await conn.execute(
            query,
            prod_id,
            data.get('name'),
            float(data.get('price', 0)),
            data.get('description', ''),
            data.get('category'),
            data.get('images', []),
            sizes_json,
            bool(data.get('isAvailable', True)),
            data.get('sizeChart', '')
        )
        await conn.close()
        return web.json_response({"status": "ok", "id": prod_id})
    except Exception as e:
        logger.error(f"Failed executing row insert into neon tables structure: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def update_product(request):
    try:
        prod_id = request.match_info['id']
        data = await request.json()
        conn = await get_db_connection()
        
        query = """
            UPDATE products 
            SET name=$1, price=$2, description=$3, is_available=$4 
            WHERE id=$5;
        """
        await conn.execute(query, data.get('name'), float(data.get('price')), data.get('description'), bool(data.get('isAvailable')), prod_id)
        await conn.close()
        return web.json_response({"status": "updated"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def delete_product(request):
    try:
        prod_id = request.match_info['id']
        conn = await get_db_connection()
        await conn.execute("DELETE FROM products WHERE id = $1;", prod_id)
        await conn.close()
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def index(request):
    paths = [WEB_DIR / "index.html", BASE_DIR / "index.html"]
    for p in paths:
        if p.exists():
            return web.Response(text=p.read_text(encoding='utf-8'), content_type='text/html')
    return web.Response(text="Index wrapper asset file structure could not be retrieved.", status=404)

def create_web_app():
    app = web.Application(client_max_size=1024**2 * 10)
    app.router.add_get('/', index)
    app.router.add_get('/api/products', get_products)
    app.router.add_post('/api/products', add_product)
    app.router.add_post('/api/products/{id}/update', update_product)
    app.router.add_post('/api/products/{id}/delete', delete_product)
    
    if STATIC_DIR.exists():
        app.router.add_static('/static/', path=STATIC_DIR, name='static')
    return app