import os
import json
import asyncio
import logging
from collections import defaultdict

import cloudscraper
from telegram import Bot
from telegram.error import RetryAfter, TimedOut, TelegramError
from dotenv import load_dotenv
from requests import HTTPError

# Загрузка .env
load_dotenv()
BOT_TOKEN     = os.getenv("BOT_TOKEN")
CHANNEL_ID    = os.getenv("CHANNEL_ID")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", 30))
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

# Пропускаемые коллекции
SKIP_COLLECTIONS = {"DeskCalendar", "LolPop"}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)


def normalize_name(name: str) -> str:
    return ''.join(ch for ch in name if ch.isalnum())


def fetch_listings(scraper):
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
        "user_auth":   USER_AUTH
    }
    try:
        resp = scraper.post(API_URL, json=payload, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get('data') or data.get('docs') or []
    except HTTPError as e:
        logger.error("HTTP error fetching listings: %s", e)
    except Exception:
        logger.exception("Unexpected error fetching listings:")
    return []


def fmt(cur, flr):
    if flr is None or flr == 0:
        return "— TON", "😐+0.0%"
    pct = (cur - flr) / flr * 100
    if abs(pct) < 1e-6:
        arrow = "😐"
    else:
        arrow = "🔻" if pct < 0 else "🔺"
    return f"{flr} TON", f"{arrow}{abs(pct):.1f}%"


async def send_alert(bot: Bot, chat_id: str, text: str):
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
    await asyncio.sleep(3)  # не чаще 1 сообщения в 3 секунды


async def monitor():
    logger.info("🚀 Старт мониторинга… интервал %s сек.", POLL_INTERVAL)
    bot = Bot(token=BOT_TOKEN)
    scraper = cloudscraper.create_scraper()
    scraper.get(API_BASE)

    seen = set()
    first_run = True
    loop = asyncio.get_event_loop()

    while True:
        docs = await loop.run_in_executor(None, fetch_listings, scraper)
        if not docs:
            await asyncio.sleep(POLL_INTERVAL)
            continue

        by_coll = defaultdict(list)
        for g in docs:
            by_coll[normalize_name(g["name"])].append(g)

        to_proc = docs[:1] if first_run else docs
        first_run = False

        for g in to_proc:
            gift_num = g["gift_num"]
            if gift_num in seen:
                continue

            key = normalize_name(g["name"])
            if key in SKIP_COLLECTIONS:
                continue
            seen.add(gift_num)

            price    = g["price"]
            model    = g["model"]
            symbol   = g["symbol"]
            backdrop = g["backdrop"]

            coll        = by_coll[key]
            floor_all   = min((x["price"] for x in coll), default=None)
            same_model  = [x for x in coll if x["model"] == model]
            floor_model = min((x["price"] for x in same_model), default=None)

            floor_all_str, pct_all   = fmt(price, floor_all)
            floor_mod_str, pct_model = fmt(price, floor_model)

            name = g["name"]
            link = f"https://t.me/nft/{key}-{gift_num}"

            msg = (
                f"*🎁 {name}* `#{gift_num}`\n"
                f"*Price:* `{price} TON`\n\n"
                f"*Floor (all):* `{floor_all_str}` (`{pct_all}`)\n"
                f"*(model «{model}»):* `{floor_mod_str}` (`{pct_model}`)\n\n"
                f"*Model:* `{model}`\n"
                f"*Symbol:* `{symbol}`\n"
                f"*Backdrop:* `{backdrop}`\n\n"

                f"({link}.gif)"
            )

            logger.debug("Отправляем оповещение gift_num=%s", gift_num)
            await send_alert(bot, CHANNEL_ID, msg)

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(monitor())
