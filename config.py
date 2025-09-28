from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
API_BASE = os.getenv("API_BASE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set in .env")
if not API_BASE:
    raise RuntimeError("API_BASE is not set in .env")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set in .env")