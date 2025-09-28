# CurrXChangeBot 💸📈

**CurrXChangeBot** is a convenient and efficient Telegram bot for fast currency conversion. It features an interactive inline menu that allows users to easily select currency pairs, enter an amount, and save favorite combinations for quick access.

---

## 🚀 Features

- **Interactive conversion** — choose currencies via the menu, enter the amount, and get the result instantly.  
- **Up-to-date exchange rates** — the bot fetches the latest data for accurate calculations.  
- **Save favorite pairs** — add or remove favorite currency pairs for quick access.  
- **Database support** — uses **PostgreSQL** to reliably store user data and favorite pairs.
- **AI-powered explanations** — get brief, easy-to-understand insights about why a currency rate is at its current level, using AI (OpenAI GPT).
---

## ⚡ Usage

The bot provides a menu for interaction but also supports commands for direct access.

- **`/start`** — opens the main menu and initializes interaction with the bot.  
- **`/menu`** — reopens the main menu if it was closed.

---

## 🛠️ Technologies

- **Python 3.11+**  
- **aiogram** — framework for building asynchronous Telegram bots.  
- **aiohttp** — used for asynchronous HTTP requests to currency APIs.  
- **asyncpg** — asynchronous driver for **PostgreSQL**.  
- **python-dotenv** — for securely storing configuration variables.
- **OpenAI GPT / AI integration** — provides on-demand explanations of currency rates directly in the chat.
---

## ⚙️ Setup and Run

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/your-username/your-repo-name.git](https://github.com/your-username/your-repo-name.git)
    cd your-repo-name
    ```
2.  **Create and activate a virtual environment:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate  # For macOS/Linux
    venv\Scripts\activate.bat # For Windows
    ```
3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
4.  **Create a `.env`** file in the project root and add your variables:
    ```
    BOT_TOKEN=YOUR_BOT_TOKEN
    DATABASE_URL=postgres://user:password@host:port/dbname
    API_BASE=YOUR_API_EXCHANGE_TOKEN
    OPENAI_API_KEY=YOUR_OPENAI_API_KEY
    ```
5.  **Run the bot:**
    ```bash
    python bot.py
    ```
