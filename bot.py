# bot.py (updated)
import logging
import time
import asyncio
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from typing import Any, Dict, Tuple, Optional, List

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher.storage import FSMContext
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.callback_data import CallbackData

from config import BOT_TOKEN
from db import (
    create_pool, init_db, upsert_user, add_favorite, list_favorites, remove_favorite,
    get_user_role, set_user_role, get_popular_pairs
)
from services import convert as api_convert, explain_rate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)

CURRENCIES = ["USD", "EUR", "UAH", "PLN", "GBP", "JPY", "CHF", "CAD", "AUD"]
PAGE_SIZE = 10
CACHE_TTL = 60
FAV_TTL = 300
MAX_FAV_CACHE_USERS = 2000
API_RETRY_ATTEMPTS = 3
API_RETRY_BACKOFF = 0.5

getcontext().prec = 28

class ConversionStates(StatesGroup):
    waiting_for_amount = State()

cb_base = CallbackData("base", "currency")
cb_target = CallbackData("target", "currency")
cb_addfav = CallbackData("addfav", "base", "target")
cb_showfav = CallbackData("showfav", "fav_id")
cb_delfav = CallbackData("delfav", "fav_id")
cb_convert_from_fav = CallbackData("convfav", "base", "target")
cb_explain = CallbackData("explain", "base", "target")
cb_setrole = CallbackData("setrole", "tg_id", "role", "page")
cb_admin_users = CallbackData("admusers", "page")

class TTLCache:
    def __init__(self, ttl: int, max_items: Optional[int] = None):
        self.ttl = ttl
        self.max_items = max_items
        self._store: Dict[Any, Tuple[Any, float]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key):
        async with self._lock:
            v = self._store.get(key)
            if not v:
                return None
            value, ts = v
            if time.time() - ts > self.ttl:
                del self._store[key]
                return None
            return value

    async def set(self, key, value):
        async with self._lock:
            if self.max_items is not None and len(self._store) >= self.max_items:
                oldest_key = min(self._store.items(), key=lambda kv: kv[1][1])[0]
                del self._store[oldest_key]
            self._store[key] = (value, time.time())

    async def delete(self, key):
        async with self._lock:
            self._store.pop(key, None)

    async def get_or_set(self, key, factory):
        async with self._lock:
            v = self._store.get(key)
            if v and (time.time() - v[1] <= self.ttl):
                return v[0]
        val = await (factory() if asyncio.iscoroutinefunction(factory) else asyncio.get_event_loop().run_in_executor(None, factory))
        await self.set(key, val)
        return val

    async def clear(self):
        async with self._lock:
            self._store.clear()

rate_cache = TTLCache(ttl=CACHE_TTL, max_items=1000)
favorites_cache = TTLCache(ttl=FAV_TTL, max_items=MAX_FAV_CACHE_USERS)

async def safe_edit_message(message: types.Message, text: str, reply_markup=None, parse_mode=None):
    try:
        await message.edit_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        msg = str(e)
        if "Message is not modified" in msg:
            logger.debug("Message not modified - ignoring.")
            return
        if "message to edit not found" in msg or "Chat not found" in msg or "message can't be edited" in msg:
            logger.info("Edit failed (message missing or too old) — sending new message instead.")
            await message.chat.send_message(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        logger.exception("Unexpected error while editing message")
        try:
            await message.chat.send_message(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            logger.exception("Also failed to send fallback message")

def make_keyboard(buttons: List[Tuple[str, str]], row_width: int = 1, back_button: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=row_width)
    kb.add(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in buttons])
    if back_button:
        kb.add(InlineKeyboardButton("Назад в головне меню", callback_data="main_menu"))
    return kb

async def is_admin(user_id: int):
    pool = dp.data.get("pool")
    if not pool:
        return False
    role = await get_user_role(pool, user_id)
    return role == "admin"

def get_main_menu_keyboard(admin: bool = False):
    buttons = [
        ("Вибрати першу валюту", cb_base.new(currency="select_base")),
        ("Список улюблених", "list_fav_menu")
    ]
    if admin:
        buttons.append(("Адмін-панель", "admin_panel"))
    return make_keyboard(buttons, row_width=2, back_button=False)

def get_currency_keyboard(prefix: str):
    buttons = []
    for c in CURRENCIES:
        if prefix == "base":
            cb = cb_base.new(currency=c)
        else:
            cb = cb_target.new(currency=c)
        buttons.append((c, cb))
    return make_keyboard(buttons, row_width=3, back_button=True)

async def _api_call_with_retry(func, *args, **kwargs):
    last_exc = None
    for attempt in range(1, API_RETRY_ATTEMPTS + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            backoff = API_RETRY_BACKOFF * (2 ** (attempt - 1))
            logger.warning("API call failed (attempt %d/%d): %s — retrying in %.2fs", attempt, API_RETRY_ATTEMPTS, e, backoff)
            await asyncio.sleep(backoff)
    logger.exception("All API retries failed")
    raise last_exc

async def get_rate(base: str, target: str) -> Optional[float]:
    key = (base, target)
    cached = await rate_cache.get(key)
    if cached is not None:
        return cached
    data = await _api_call_with_retry(api_convert, base, target, 1)
    rate = data.get("rate") or data.get("conversion_rate") or None
    if rate is not None:
        await rate_cache.set(key, rate)
    return rate

async def update_favorites_cache(user_id: int):
    pool = dp.data.get("pool")
    if not pool:
        return []
    rows = await list_favorites(pool, user_id)
    await favorites_cache.set(user_id, rows)
    return rows

AMOUNT_RE = re.compile(r"^\s*([\d\.\, ]+)\s*$")
def parse_amount(text: str) -> Decimal:
    m = AMOUNT_RE.match(text)
    if not m:
        raise ValueError("Invalid number format")
    s = m.group(1)
    s = s.replace(" ", "")
    if "." in s and "," in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "")
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        if "," in s:
            s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError("Invalid numeric value")
    d = d.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return d

@dp.message_handler(commands=['start', 'menu'], state="*")
async def cmd_start_help_menu(message: types.Message, state: FSMContext):
    await state.finish()
    pool = dp.data.get("pool")
    if pool:
        try:
            await upsert_user(pool, message.from_user)
        except Exception:
            logger.exception("Failed upsert_user on /start")
    admin = await is_admin(message.from_user.id)
    await message.reply(
        "Привіт! Я — валютний бот. Обери валюту для конвертації 👇",
        reply_markup=get_main_menu_keyboard(admin)
    )

@dp.callback_query_handler(lambda c: c.data == 'main_menu', state="*")
async def back_to_main_menu(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.finish()
    admin = await is_admin(callback_query.from_user.id)
    await safe_edit_message(callback_query.message, "Обери валюту для конвертації 👇", reply_markup=get_main_menu_keyboard(admin))

@dp.callback_query_handler(cb_base.filter(), state="*")
async def select_or_set_base_currency(callback_query: types.CallbackQuery, callback_data: Dict[str, str], state: FSMContext):
    await callback_query.answer()
    currency = callback_data.get("currency")
    if currency == "select_base":
        await state.update_data(base=None, target=None)
        await safe_edit_message(callback_query.message, "Оберiть першу (базову) валюту:", reply_markup=get_currency_keyboard('base'))
        return
    await state.update_data(base=currency)
    await safe_edit_message(
        callback_query.message,
        f"Ти обрав **{currency}**. Тепер обери другу (цільову) валюту:",
        reply_markup=get_currency_keyboard('target'),
        parse_mode="Markdown"
    )

@dp.callback_query_handler(cb_target.filter(), state="*")
async def set_target_currency(callback_query: types.CallbackQuery, callback_data: Dict[str, str], state: FSMContext):
    await callback_query.answer()
    currency = callback_data.get("currency")
    await state.update_data(target=currency)
    data = await state.get_data()
    base_currency = data.get('base')
    if not base_currency:
        await callback_query.answer("Будь ласка, спочатку оберіть першу валюту.", show_alert=True)
        return
    await safe_edit_message(
        callback_query.message,
        f"Ти обрав пару **{base_currency}** → **{currency}**.\n\nТепер введи суму, яку хочеш конвертувати:",
        parse_mode="Markdown"
    )
    await ConversionStates.waiting_for_amount.set()

@dp.message_handler(state=ConversionStates.waiting_for_amount)
async def process_amount(message: types.Message, state: FSMContext):
    try:
        amount_dec = parse_amount(message.text)
    except ValueError:
        await message.reply("Це не схоже на число. Будь ласка, введи коректну суму (наприклад: 1234.56 або 1 234,56).")
        return
    user_data = await state.get_data()
    base = user_data.get('base')
    target = user_data.get('target')
    if not base or not target:
        await message.reply("Щось пішло не так. Почни знову через /menu.")
        await state.finish()
        return
    try:
        data = await _api_call_with_retry(api_convert, base, target, float(amount_dec))
        result = data.get('result')
        rate = data.get('rate') or await get_rate(base, target)
        if result is None and rate is not None:
            result_dec = (amount_dec * Decimal(str(rate))).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            result = f"{result_dec:.6f}"
        msg = f"**{amount_dec} {base}** = **{result} {target}**"
        if rate:
            msg += f"\n\nКурс: 1 {base} = {Decimal(str(rate)):.6f} {target}"
        keyboard = make_keyboard([
            (f"Додати в улюблені", cb_addfav.new(base=base, target=target)),
            (f"Пояснити курс 🤖", cb_explain.new(base=base, target=target))
        ], row_width=1, back_button=True)
        await message.reply(msg, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.exception("convert failed")
        await message.reply(f"Помилка при конвертації: {e}")
    finally:
        await state.finish()

@dp.callback_query_handler(lambda c: c.data == "admin_panel", state="*")
async def admin_panel(callback_query: types.CallbackQuery):
    await callback_query.answer()
    if not await is_admin(callback_query.from_user.id):
        await callback_query.answer("Тільки для адмінів!", show_alert=True)
        return
    keyboard = make_keyboard([
        ("Список користувачів", cb_admin_users.new(page="1")),
        ("Статистика", "admin_stats")
    ], row_width=1)
    await safe_edit_message(callback_query.message, "Адмін-панель:", reply_markup=keyboard)

@dp.callback_query_handler(cb_admin_users.filter(), state="*")
async def admin_users(callback_query: types.CallbackQuery, callback_data: Dict[str, str]):
    await callback_query.answer()
    if not await is_admin(callback_query.from_user.id):
        await callback_query.answer("Тільки для адмінів!", show_alert=True)
        return
    page = int(callback_data.get("page") or 1)
    pool = dp.data.get("pool")
    if not pool:
        await callback_query.answer("DB not initialized", show_alert=True)
        return

    offset = (page - 1) * PAGE_SIZE
    try:
        users_page = await pool.fetch(
            "SELECT tg_id, username, role FROM users ORDER BY tg_id LIMIT $1 OFFSET $2",
            PAGE_SIZE, offset
        )
        total_users = await pool.fetchval("SELECT COUNT(*) FROM users")
    except Exception:
        logger.exception("Failed fetching users for admin")
        await callback_query.answer("Помилка при отриманні списку користувачів.", show_alert=True)
        return

    if not users_page:
        await safe_edit_message(callback_query.message, "Користувачів ще немає.", reply_markup=get_main_menu_keyboard(admin=True))
        return

    text_lines = []
    for u in users_page:
        tg_username = f"@{u['username']}" if u['username'] else f"(ID: {u['tg_id']})"
        role = u["role"] or "user"
        text_lines.append(f"{tg_username} — роль: {role}")
    total_pages = (int(total_users) - 1) // PAGE_SIZE + 1 if total_users else 1
    text = f"Сторінка {page} / {total_pages}\n\n" + "\n".join(text_lines)

    keyboard = InlineKeyboardMarkup(row_width=2)
    for u in users_page:
        tg_username = f"@{u['username']}" if u['username'] else f"(ID: {u['tg_id']})"
        keyboard.add(
            InlineKeyboardButton(f"{tg_username} → Admin", callback_data=cb_setrole.new(tg_id=str(u['tg_id']), role="admin", page=str(page))),
            InlineKeyboardButton(f"{tg_username} → User", callback_data=cb_setrole.new(tg_id=str(u['tg_id']), role="user", page=str(page)))
        )

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Попередня", callback_data=cb_admin_users.new(page=str(page-1))))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("➡️ Наступна", callback_data=cb_admin_users.new(page=str(page+1))))
    if nav_buttons:
        keyboard.row(*nav_buttons)

    keyboard.add(InlineKeyboardButton("Назад в Адмін-панель", callback_data="admin_panel"))

    await safe_edit_message(callback_query.message, text, reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(cb_setrole.filter(), state="*")
async def set_user_role_callback(callback_query: types.CallbackQuery, callback_data: Dict[str, str]):
    await callback_query.answer()
    if not await is_admin(callback_query.from_user.id):
        await callback_query.answer("Тільки для адмінів!", show_alert=True)
        return
    tg_id = int(callback_data["tg_id"])
    role = callback_data["role"]
    page = int(callback_data.get("page") or 1)
    pool = dp.data.get("pool")
    try:
        await set_user_role(pool, tg_id, role)
        await callback_query.answer("Роль змінена.", show_alert=False)
    except Exception:
        logger.exception("Failed to set role")
        await callback_query.answer("Не вдалось змінити роль.", show_alert=True)
        return
    await admin_users(callback_query, {"page": str(page)})

@dp.callback_query_handler(lambda c: c.data == "admin_stats", state="*")
async def admin_stats(callback_query: types.CallbackQuery):
    await callback_query.answer()
    if not await is_admin(callback_query.from_user.id):
        await callback_query.answer("Тільки для адмінів!", show_alert=True)
        return
    pool = dp.data.get("pool")
    if not pool:
        await callback_query.answer("DB not initialized", show_alert=True)
        return
    try:
        total_users = await pool.fetchval("SELECT COUNT(*) FROM users")
        popular_pairs = await get_popular_pairs(pool)
    except Exception:
        logger.exception("Failed to fetch admin stats")
        await callback_query.answer("Помилка при отриманні статистики.", show_alert=True)
        return

    text = f"Кількість користувачів: {total_users}\nНайпопулярніші пари:\n"
    for p in popular_pairs:
        text += f"{p['base']} → {p['target']} ({p['count']})\n"

    keyboard = make_keyboard([("Назад в Адмін-панель", "admin_panel")], row_width=1, back_button=False)
    await safe_edit_message(callback_query.message, text, reply_markup=keyboard)

@dp.callback_query_handler(cb_addfav.filter(), state="*")
async def add_fav_from_callback(callback_query: types.CallbackQuery, callback_data: Dict[str, str], state: FSMContext):
    await callback_query.answer()
    base = callback_data["base"]
    target = callback_data["target"]
    pool = dp.data.get("pool")
    try:
        await add_favorite(pool, callback_query.from_user.id, base, target)
    except Exception:
        logger.exception("Adding favorite failed; attempting upsert_user and retry")
        try:
            await upsert_user(pool, callback_query.from_user)
            await add_favorite(pool, callback_query.from_user.id, base, target)
        except Exception:
            logger.exception("Retry add_favorite failed")
            await callback_query.answer("Не вдалося додати улюблене.", show_alert=True)
            return
    try:
        await update_favorites_cache(callback_query.from_user.id)
    except Exception:
        logger.exception("Failed to update favorites cache after add")
    admin = await is_admin(callback_query.from_user.id)
    await safe_edit_message(
        callback_query.message,
        f"Пара **{base}** → **{target}** додана в улюблені.",
        reply_markup=get_main_menu_keyboard(admin),
        parse_mode="Markdown"
    )

@dp.callback_query_handler(lambda c: c.data == 'list_fav_menu', state="*")
async def list_fav_from_menu(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.finish()
    pool = dp.data.get("pool")
    if pool:
        try:
            pass
        except Exception:
            logger.debug("upsert skipped")
    rows = await favorites_cache.get(callback_query.from_user.id)
    if rows is None:
        rows = await update_favorites_cache(callback_query.from_user.id)
    if not rows:
        keyboard = make_keyboard([], row_width=1)
        await safe_edit_message(callback_query.message, "У тебе немає улюблених пар.", reply_markup=keyboard)
        return
    buttons = [(f"{r['base']} → {r['target']}", cb_showfav.new(fav_id=str(r['id']))) for r in rows]
    keyboard = make_keyboard(buttons, row_width=1)
    await safe_edit_message(callback_query.message, "Твої улюблені пари:", reply_markup=keyboard)

@dp.callback_query_handler(cb_showfav.filter(), state="*")
async def show_fav_from_callback(callback_query: types.CallbackQuery, callback_data: Dict[str, str], state: FSMContext):
    await callback_query.answer()
    await state.finish()
    fav_id = int(callback_data["fav_id"])
    rows = await favorites_cache.get(callback_query.from_user.id)
    if rows is None:
        rows = await update_favorites_cache(callback_query.from_user.id)
    fav = next((r for r in rows if r['id'] == fav_id), None)
    if not fav:
        await callback_query.answer("Вибрана улюблена пара не знайдена.", show_alert=True)
        return
    base, target = fav['base'], fav['target']
    try:
        rate = await get_rate(base, target)
        if rate is None:
            raise RuntimeError("No rate available")
        msg = f"Курс для улюбленої пари:\n1 **{base}** = **{Decimal(str(rate)):.6f} {target}**"
        buttons = [
            ("Конвертувати", cb_convert_from_fav.new(base=base, target=target)),
            ("Пояснити курс 🤖", cb_explain.new(base=base, target=target)),
            ("Видалити з улюблених", cb_delfav.new(fav_id=str(fav_id)))
        ]
        keyboard = make_keyboard(buttons, row_width=1)
        await safe_edit_message(callback_query.message, msg, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.exception("Failed preparing favorite conversion")
        await callback_query.answer("Не вдалося отримати курс для цієї пари.", show_alert=True)

@dp.callback_query_handler(cb_convert_from_fav.filter(), state="*")
async def convert_from_fav(callback_query: types.CallbackQuery, callback_data: Dict[str, str], state: FSMContext):
    await callback_query.answer()
    base = callback_data["base"]
    target = callback_data["target"]
    await state.update_data(base=base, target=target)
    await safe_edit_message(
        callback_query.message,
        f"Ти обрав пару **{base}** → **{target}**.\n\nТепер введи суму, яку хочеш конвертувати:",
        parse_mode="Markdown"
    )
    await ConversionStates.waiting_for_amount.set()

@dp.callback_query_handler(cb_delfav.filter(), state="*")
async def delete_fav_from_callback(callback_query: types.CallbackQuery, callback_data: Dict[str, str], state: FSMContext):
    await callback_query.answer()
    await state.finish()
    fav_id = int(callback_data["fav_id"])
    pool = dp.data.get("pool")
    try:
        res = await remove_favorite(pool, callback_query.from_user.id, fav_id)
        await update_favorites_cache(callback_query.from_user.id)
    except Exception:
        logger.exception("Failed deleting favorite")
        await callback_query.answer("Не вдалося видалити фаворита.", show_alert=True)
        return
    if res in ("DELETE 1", 1, "1"):
        await callback_query.answer("Фаворит видалено.", show_alert=True)
        await list_fav_from_menu(callback_query, state)
    else:
        await callback_query.answer("Не знайдено фаворита з таким id.", show_alert=True)

@dp.callback_query_handler(cb_explain.filter(), state="*")
async def explain_currency_rate(callback_query: types.CallbackQuery, callback_data: Dict[str, str]):
    await callback_query.answer()
    base = callback_data["base"]
    target = callback_data["target"]
    try:
        rate = await get_rate(base, target)
        if rate is None:
            await callback_query.answer("Не вдалося отримати курс для пояснення.", show_alert=True)
            return
        explanation = await _api_call_with_retry(explain_rate, base, target, rate)
        await bot.send_message(
            callback_query.message.chat.id,
            f"🤖 Ось пояснення для {base} → {target}:\n\n{explanation}"
        )
    except Exception as e:
        logger.exception("Explanation failed")
        await bot.send_message(callback_query.message.chat.id, f"Помилка при поясненні: {e}")

async def on_startup(dp_):
    logger.info("Creating DB pool...")
    pool = await create_pool()
    dp.data["pool"] = pool
    logger.info("Init DB (create tables if needed)...")
    await init_db(pool)
    logger.info("Bot started and DB initialized")

async def on_shutdown(dp_):
    logger.info("Shutting down...")
    pool = dp.data.get("pool")
    if pool:
        try:
            await pool.close()
            logger.info("DB pool closed.")
        except Exception:
            logger.exception("Error closing pool")
    await bot.close()
    logger.info("Bot closed")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)