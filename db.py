import asyncpg
from config import DATABASE_URL

async def create_pool():
    return await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

async def init_db(pool):
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id BIGINT PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            created_at TIMESTAMP DEFAULT now()
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id SERIAL PRIMARY KEY,
            tg_id BIGINT NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            base VARCHAR(10) NOT NULL,
            target VARCHAR(10) NOT NULL,
            UNIQUE (tg_id, base, target)
        );
        """)

async def upsert_user(pool, user):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users(tg_id, first_name, username) VALUES($1,$2,$3)
            ON CONFLICT (tg_id) DO UPDATE SET first_name = $2, username = $3
        """, user.id, user.first_name, user.username)

async def add_favorite(pool, tg_id, base, target):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO favorites (tg_id, base, target) VALUES($1,$2,$3)
            ON CONFLICT DO NOTHING
        """, tg_id, base, target)

async def list_favorites(pool, tg_id):
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT id, base, target FROM favorites WHERE tg_id=$1 ORDER BY id", tg_id)

async def remove_favorite(pool, tg_id, fav_id):
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM favorites WHERE id=$1 AND tg_id=$2", fav_id, tg_id)
        return res