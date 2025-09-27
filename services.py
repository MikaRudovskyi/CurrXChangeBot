import aiohttp

API_BASE = "https://v6.exchangerate-api.com/v6/1f66b6bb41c2ea4408ea21a5/latest"

async def convert(base: str, target: str, amount: float = 1.0):
    url = f"{API_BASE}/{base.upper()}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=10) as resp:
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