import aiohttp
import os
from openai import AsyncOpenAI
from config import API_BASE

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

async def convert(base: str, target: str, amount: float = 1.0):
    url = f"{API_BASE}/{base.upper()}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=30) as resp:
            if resp.status != 200:
                raise Exception(f"API returned status {resp.status}")
            data = await resp.json()
            if data.get("result") != "success":
                raise Exception(f"API error: {data}")
            
            rates = data.get("conversion_rates")
            if rates is None or target.upper() not in rates:
                raise Exception(f"Target currency {target} not found in API response")
            
            rate = rates[target.upper()]
            result = amount * rate
            return {"result": result, "rate": rate}

async def explain_rate(base: str, target: str, rate: float):
    prompt = f"""
    Ти фінансовий аналітик. Курс {base} → {target} зараз {rate:.4f}.
    Поясни коротко, які можуть бути причини такого курсу і чи він вигідний для звичайної людини.
    Пиши простою мовою, максимум 4 речення.
    """

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6
    )

    return response.choices[0].message.content.strip()