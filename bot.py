import logging

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.dispatcher.filters.builtin import CommandStart
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

def get_main_menu_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("Вибрати першу валюту", callback_data="select_base"),
        InlineKeyboardButton("Список улюблених", callback_data="list_fav_menu")
    )
    return keyboard

def get_currency_keyboard(prefix: str):
    keyboard = InlineKeyboardMarkup(row_width=3)
    buttons = [InlineKeyboardButton(c, callback_data=f"{prefix}_{c}") for c in CURRENCIES]
    keyboard.add(*buttons)
    keyboard.add(InlineKeyboardButton("Назад в головне меню", callback_data="main_menu"))
    return keyboard

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
    await bot.edit_message_text(
        "Обери валюту для конвертації 👇",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        reply_markup=get_main_menu_keyboard()
    )

@dp.callback_query_handler(lambda c: c.data == 'select_base', state="*")
async def select_base_currency(callback_query: types.CallbackQuery, state: FSMContext):
    await state.update_data(base=None, target=None)
    await bot.edit_message_text(
        "Обери першу (базову) валюту:",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        reply_markup=get_currency_keyboard('base')
    )

@dp.callback_query_handler(lambda c: c.data.startswith('base_'), state="*")
async def set_base_currency(callback_query: types.CallbackQuery, state: FSMContext):
    currency = callback_query.data.split('_')[1]
    await state.update_data(base=currency)
    await bot.edit_message_text(
        f"Ти обрав **{currency}**. Тепер обери другу (цільову) валюту:",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        reply_markup=get_currency_keyboard('target')
    )

@dp.callback_query_handler(lambda c: c.data.startswith('target_'), state="*")
async def set_target_currency(callback_query: types.CallbackQuery, state: FSMContext):
    currency = callback_query.data.split('_')[1]
    await state.update_data(target=currency)
    data = await state.get_data()
    base_currency = data.get('base')
    if not base_currency:
        await callback_query.answer("Будь ласка, спочатку оберіть першу валюту.", show_alert=True)
        return
    await bot.edit_message_text(
        f"Ти обрав пару **{base_currency}** → **{currency}**.\n\nТепер введи суму, яку хочеш конвертувати:",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        parse_mode="Markdown"
    )
    await ConversionStates.waiting_for_amount.set()

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
        await message.reply("Щось пішло не так. Будь ласка, почни знову, вибравши валюти.")
        await state.finish()
        return
    try:
        data = await api_convert(base, target, amount)
        result = data['result']
        rate = data.get('rate')
        msg = f"**{amount} {base}** = **{result:.4f} {target}**"
        if rate:
            msg += f"\n\nКурс: 1 {base} = {rate:.6f} {target}"
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(InlineKeyboardButton("Додати в улюблені", callback_data=f"addfav_{base}_{target}"))
        keyboard.add(InlineKeyboardButton("Назад в головне меню", callback_data="main_menu"))
        await message.reply(
            msg,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
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
    await callback_query.answer(f"Пара {base} → {target} додана у фаворити.", show_alert=True)
    await state.finish()
    await bot.edit_message_text(
        f"Пара **{base}** → **{target}** додана в улюблені.",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        reply_markup=get_main_menu_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query_handler(lambda c: c.data == 'list_fav_menu', state="*")
async def list_fav_from_menu(callback_query: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await upsert_user(pool, callback_query.from_user)
    rows = await list_favorites(pool, callback_query.from_user.id)
    if not rows:
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(InlineKeyboardButton("Назад в головне меню", callback_data="main_menu"))
        await bot.edit_message_text(
            "У тебе немає улюблених пар.",
            callback_query.message.chat.id,
            callback_query.message.message_id,
            reply_markup=keyboard
        )
        return
    keyboard = InlineKeyboardMarkup(row_width=1)
    for r in rows:
        keyboard.add(InlineKeyboardButton(f"{r['base']} → {r['target']}", callback_data=f"showfav_{r['id']}"))
    keyboard.add(InlineKeyboardButton("Назад в головне меню", callback_data="main_menu"))
    await bot.edit_message_text(
        "Твої улюблені пари:",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('showfav_'), state="*")
async def show_fav_from_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await state.finish()
    fav_id = int(callback_query.data.split('_')[1])
    rows = await list_favorites(pool, callback_query.from_user.id)
    fav = next((r for r in rows if r['id'] == fav_id), None)
    if not fav:
        await callback_query.answer("Вибрана улюблена пара не знайдена.", show_alert=True)
        return
    base = fav['base']
    target = fav['target']
    try:
        data = await api_convert(base, target, 1)
        rate = data.get('rate')
        msg = f"Курс для улюбленої пари:\n1 **{base}** = **{rate:.6f} {target}**"
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(InlineKeyboardButton("Конвертувати", callback_data=f"convert_from_fav_{base}_{target}"))
        keyboard.add(InlineKeyboardButton("Видалити з улюблених", callback_data=f"delfav_{fav_id}"))
        keyboard.add(InlineKeyboardButton("Назад до списку улюблених", callback_data="list_fav_menu"))
        await bot.edit_message_text(
            msg,
            callback_query.message.chat.id,
            callback_query.message.message_id,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception("convert failed")
        await bot.send_message(callback_query.message.chat.id, f"Помилка при конвертації: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('convert_from_fav_'), state="*")
async def convert_from_fav(callback_query: types.CallbackQuery, state: FSMContext):
    _, _, _, base, target = callback_query.data.split('_')
    
    await state.update_data(base=base, target=target)
    await bot.edit_message_text(
        f"Ти обрав пару **{base}** → **{target}**.\n\nТепер введи суму, яку хочеш конвертувати:",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        parse_mode="Markdown"
    )
    await ConversionStates.waiting_for_amount.set()

@dp.callback_query_handler(lambda c: c.data.startswith('delfav_'), state="*")
async def delete_fav_from_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await state.finish()
    fav_id = int(callback_query.data.split('_')[1])
    res = await remove_favorite(pool, callback_query.from_user.id, fav_id)
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