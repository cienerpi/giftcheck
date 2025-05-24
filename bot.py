import os
import json
import asyncio
import logging
from typing import Optional, List, Dict

import cloudscraper
from telegram import Bot
from telegram.error import RetryAfter, TimedOut, TelegramError
from requests import HTTPError
from dotenv import load_dotenv

# Загрузка конфигурации
load_dotenv()
BOT_TOKEN     = os.getenv("BOT_TOKEN")
CHANNEL_ID    = os.getenv("CHANNEL_ID")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", 2))
USER_AUTH     = os.getenv("USER_AUTH")

if not BOT_TOKEN or not CHANNEL_ID:
    raise RuntimeError("В .env должны быть заданы BOT_TOKEN и CHANNEL_ID")
if not USER_AUTH:
    raise RuntimeError("В .env не задан USER_AUTH — скопируйте initData из DevTools")

API_BASE = "https://gifts2.tonnel.network"
API_URL  = f"{API_BASE}/api/pageGifts"

HEADERS = {
    "Accept":       "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin":       API_BASE,
    "Referer":      "https://market.tonnel.network/",
    "User-Agent":   (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)


def normalize_name(name: str) -> str:
    """Убираем всё, кроме букв и цифр, для формирования url-base."""
    return ''.join(ch for ch in name if ch.isalnum())


def fetch_listings(scraper) -> List[Dict]:
    """Получаем последние 30 листингов."""
    payload = {
        "page":        1,
        "limit":       30,
        "sort":        json.dumps({"message_post_time": -1, "gift_id": -1}),
        "filter":      json.dumps({
            "price":     {"$exists": True},
            "refunded":  {"$ne":    True},
            "buyer":     {"$exists": False},
            "export_at": {"$exists": True}
        }),
        "price_range": None,
        "ref":         0,
        "user_auth":   USER_AUTH,
    }
    try:
        resp = scraper.post(API_URL, json=payload, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data") or data.get("docs") or []
    except HTTPError as e:
        logger.error("HTTP error fetching listings: %s", e)
    except Exception:
        logger.exception("Unexpected error fetching listings")
    return []


def fetch_floor_price(scraper, name: str, model: Optional[str] = None) -> Optional[float]:
    """Берём минимальную цену (floor) для коллекции или конкретной модели."""
    flt = {
        "price":     {"$exists": True},
        "refunded":  {"$ne":    True},
        "buyer":     {"$exists": False},
        "export_at": {"$exists": True},
        "gift_name": name,
        "asset":     "TON",
    }
    if model:
        flt["model"] = model

    payload = {
        "page":        1,
        "limit":       1,
        "sort":        json.dumps({"price": 1}),
        "filter":      json.dumps(flt),
        "price_range": None,
        "ref":         0,
        "user_auth":   USER_AUTH,
    }
    try:
        resp = scraper.post(API_URL, json=payload, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
        docs = data if isinstance(data, list) else data.get("data") or data.get("docs") or []
        return docs[0]["price"] if docs else None
    except HTTPError as e:
        logger.error("HTTP error fetching floor price: %s", e)
    except Exception:
        logger.exception("Unexpected error fetching floor price")
    return None


async def send_alert(bot: Bot, chat_id: str, text: str):
    """Шлём сообщение, обрабатывая rate-limit."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except TimedOut:
        await asyncio.sleep(5)
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except TelegramError as e:
        logger.error("Telegram error: %s", e)
    # throttle: не чаще 1 сообщения в 3 секунды
    await asyncio.sleep(3)


def fmt_floor(price: float, floor: Optional[float]) -> str:
    """Готовим строку вида 'X TON (🔻+Y%)' или '— TON'."""
    if floor is None or floor == 0:
        return "— TON (+0.0%)"
    pct = (price - floor) / floor * 100
    arrow = "➖" if abs(pct) < 0.05 else ("🔻" if pct < 0 else "🔺")
    return f"{floor} TON ({arrow}{pct:+.1f}%)"


async def monitor():
    logger.info("🚀 Старт мониторинга Platinum фоновых подарков…")
    bot = Bot(token=BOT_TOKEN)
    scraper = cloudscraper.create_scraper()
    scraper.get(API_BASE)  # установить куки

    seen = set()
    first_run = True
    loop = asyncio.get_event_loop()

    while True:
        docs = await loop.run_in_executor(None, fetch_listings, scraper)
        if not docs:
            await asyncio.sleep(POLL_INTERVAL)
            continue

        # при первом запуске обрабатываем только самый последний
        to_proc = [docs[0]] if first_run else docs
        first_run = False

        for g in to_proc:
            gift_num = g.get("gift_num")
            if gift_num in seen:
                continue
            seen.add(gift_num)

            name     = g.get("name", "")
            model    = g.get("model", "")
            symbol   = g.get("symbol", "")
            backdrop = g.get("backdrop", "")
            price    = g.get("price", 0)

            # фильтр: только фон Platinum
            if not backdrop.startswith("Platinum"):
                continue

            # готовим ссылки
            gift_id     = g["gift_id"]
            market_link = f"https://t.me/tonnel_network_bot/gift?startapp={gift_id}"
            gif_link    = f"https://t.me/nft/{normalize_name(name)}-{gift_id}.gif"

            # берём floor-цены
            floor_all   = await loop.run_in_executor(None, fetch_floor_price, scraper, name)
            floor_model = await loop.run_in_executor(None, fetch_floor_price, scraper, name, model)

            fa_str = fmt_floor(price, floor_all)
            fm_str = fmt_floor(price, floor_model)

            msg = (
                f"*🎁 [{name}]({market_link})* `#{gift_id}`\n"
                f"*Price:* `{price} TON`\n\n"
                f"*Floor (all):* `{fa_str}`\n"
                f"*Floor (model «{model}»):* `{fm_str}`\n\n"
                f"*Model:* `{model}`\n"
                f"*Symbol:* `{symbol}`\n"
                f"*Backdrop:* `{backdrop}`\n\n"
                f"`#`{backdrop.split()[0]} `#`{symbol.split()[0]} `#5ton`\n"
                f"🎬 [GIF]({gif_link})"
            )

            logger.debug("🔔 Оповещение для %s (%s)", name, gift_id)
            await send_alert(bot, CHANNEL_ID, msg)

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(monitor())
