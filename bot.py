import logging
import time
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher.storage import FSMContext
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import BOT_TOKEN
from db import create_pool, init_db, upsert_user, add_favorite, list_favorites, remove_favorite
from services import convert as api_convert

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)
pool = None

CURRENCIES = ["USD", "EUR", "UAH", "PLN", "GBP", "JPY", "CHF", "CAD", "AUD"]

class ConversionStates(StatesGroup):
    waiting_for_amount = State()

favorites_cache = {}
rate_cache = {}

CACHE_TTL = 60

async def safe_edit_message(message: types.Message, text: str, reply_markup=None, parse_mode=None):
    try:
        await message.edit_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        if "Message is not modified" in str(e):
            return

def make_keyboard(buttons, row_width=1, back_button=True):
    kb = InlineKeyboardMarkup(row_width=row_width)
    kb.add(*[InlineKeyboardButton(text, callback_data=data) for text, data in buttons])
    if back_button:
        kb.add(InlineKeyboardButton("Назад в головне меню", callback_data="main_menu"))
    return kb

def get_main_menu_keyboard():
    return make_keyboard([
        ("Вибрати першу валюту", "select_base"),
        ("Список улюблених", "list_fav_menu")
    ], row_width=2, back_button=False)

def get_currency_keyboard(prefix: str):
    buttons = [(c, f"{prefix}_{c}") for c in CURRENCIES]
    return make_keyboard(buttons, row_width=3, back_button=True)

async def update_favorites_cache(user_id: int):
    rows = await list_favorites(pool, user_id)
    favorites_cache[user_id] = rows
    return rows

async def get_rate(base, target):
    key = (base, target)
    now = time.time()
    if key in rate_cache and now - rate_cache[key][1] < CACHE_TTL:
        return rate_cache[key][0]
    data = await api_convert(base, target, 1)
    rate_cache[key] = (data.get('rate'), now)
    return data.get('rate')

@dp.message_handler(commands=['start', 'menu'], state="*")
async def cmd_start_help_menu(message: types.Message, state: FSMContext):
    await state.finish()
    await upsert_user(pool, message.from_user)
    await message.reply(
        "Привіт! Я — валютний бот. Обери валюту для конвертації 👇",
        reply_markup=get_main_menu_keyboard()
    )

@dp.callback_query_handler(lambda c: c.data == 'main_menu', state="*")
async def back_to_main_menu(callback_query: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await safe_edit_message(callback_query.message, "Обери валюту для конвертації 👇", reply_markup=get_main_menu_keyboard())
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == 'select_base', state="*")
async def select_base_currency(callback_query: types.CallbackQuery, state: FSMContext):
    await state.update_data(base=None, target=None)
    await safe_edit_message(callback_query.message, "Обери першу (базову) валюту:", reply_markup=get_currency_keyboard('base'))
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('base_'), state="*")
async def set_base_currency(callback_query: types.CallbackQuery, state: FSMContext):
    currency = callback_query.data.split('_')[1]
    await state.update_data(base=currency)
    await safe_edit_message(
        callback_query.message,
        f"Ти обрав **{currency}**. Тепер обери другу (цільову) валюту:",
        reply_markup=get_currency_keyboard('target'),
        parse_mode="Markdown"
    )
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('target_'), state="*")
async def set_target_currency(callback_query: types.CallbackQuery, state: FSMContext):
    currency = callback_query.data.split('_')[1]
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
    await callback_query.answer()

@dp.message_handler(state=ConversionStates.waiting_for_amount)
async def process_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(',', '.'))
    except ValueError:
        await message.reply("Це не схоже на число. Будь ласка, введи коректну суму.")
        return
    user_data = await state.get_data()
    base = user_data.get('base')
    target = user_data.get('target')
    if not base or not target:
        await message.reply("Щось пішло не так. Почни знову.")
        await state.finish()
        return

    try:
        data = await api_convert(base, target, amount)
        result = data['result']
        rate = data.get('rate')
        msg = f"**{amount} {base}** = **{result:.4f} {target}**"
        if rate:
            msg += f"\n\nКурс: 1 {base} = {rate:.6f} {target}"

        keyboard = make_keyboard([
            (f"Додати в улюблені", f"addfav_{base}_{target}")
        ], row_width=1, back_button=True)

        await message.reply(msg, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.exception("convert failed")
        await message.reply(f"Помилка при конвертації: {e}")
    finally:
        await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith('addfav_'), state="*")
async def add_fav_from_callback(callback_query: types.CallbackQuery, state: FSMContext):
    _, base, target = callback_query.data.split('_')
    await upsert_user(pool, callback_query.from_user)
    await add_favorite(pool, callback_query.from_user.id, base, target)
    await update_favorites_cache(callback_query.from_user.id)
    await callback_query.answer(f"Пара {base} → {target} додана у фаворити.", show_alert=True)
    await state.finish()
    await safe_edit_message(callback_query.message, f"Пара **{base}** → **{target}** додана в улюблені.", reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")

@dp.callback_query_handler(lambda c: c.data == 'list_fav_menu', state="*")
async def list_fav_from_menu(callback_query: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await upsert_user(pool, callback_query.from_user)
    rows = favorites_cache.get(callback_query.from_user.id) or await update_favorites_cache(callback_query.from_user.id)

    if not rows:
        keyboard = make_keyboard([], row_width=1)
        await safe_edit_message(callback_query.message, "У тебе немає улюблених пар.", reply_markup=keyboard)
        await callback_query.answer()
        return

    buttons = [(f"{r['base']} → {r['target']}", f"showfav_{r['id']}") for r in rows]
    keyboard = make_keyboard(buttons, row_width=1)
    await safe_edit_message(callback_query.message, "Твої улюблені пари:", reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('showfav_'), state="*")
async def show_fav_from_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await state.finish()
    fav_id = int(callback_query.data.split('_')[1])
    rows = favorites_cache.get(callback_query.from_user.id) or await update_favorites_cache(callback_query.from_user.id)
    fav = next((r for r in rows if r['id'] == fav_id), None)
    if not fav:
        await callback_query.answer("Вибрана улюблена пара не знайдена.", show_alert=True)
        return
    base = fav['base']
    target = fav['target']
    try:
        rate = await get_rate(base, target)
        msg = f"Курс для улюбленої пари:\n1 **{base}** = **{rate:.6f} {target}**"
        buttons = [
            ("Конвертувати", f"convert_from_fav_{base}_{target}"),
            ("Видалити з улюблених", f"delfav_{fav_id}")
        ]
        keyboard = make_keyboard(buttons, row_width=1)
        await safe_edit_message(callback_query.message, msg, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.exception("convert failed")
        await bot.send_message(callback_query.message.chat.id, f"Помилка при конвертації: {e}")
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('convert_from_fav_'), state="*")
async def convert_from_fav(callback_query: types.CallbackQuery, state: FSMContext):
    _, _, _, base, target = callback_query.data.split('_')
    await state.update_data(base=base, target=target)
    await safe_edit_message(callback_query.message, f"Ти обрав пару **{base}** → **{target}**.\n\nТепер введи суму, яку хочеш конвертувати:", parse_mode="Markdown")
    await ConversionStates.waiting_for_amount.set()
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('delfav_'), state="*")
async def delete_fav_from_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await state.finish()
    fav_id = int(callback_query.data.split('_')[1])
    res = await remove_favorite(pool, callback_query.from_user.id, fav_id)
    await update_favorites_cache(callback_query.from_user.id)
    if res == "DELETE 1":
        await callback_query.answer("Фаворит видалено.", show_alert=True)
        await list_fav_from_menu(callback_query, state)
    else:
        await callback_query.answer("Не знайдено фаворита з таким id.", show_alert=True)

async def on_startup(dp):
    global pool
    logger.info("Creating DB pool...")
    pool = await create_pool()
    logger.info("Init DB (create tables if needed)...")
    await init_db(pool)
    logger.info("Bot started")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)