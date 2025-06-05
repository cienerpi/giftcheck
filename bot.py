import os
import json
import asyncio
import logging
from typing import Optional, List, Dict

import cloudscraper
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import RetryAfter, TimedOut, TelegramError
from requests import HTTPError
from dotenv import load_dotenv

# Load configuration
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


async def send_alert(bot: Bot, chat_id: str, text: str, keyboard: InlineKeyboardMarkup):
    """Шлём сообщение с инлайн-кнопкой, обрабатывая rate-limit."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=False
        )
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=False
        )
    except TimedOut:
        await asyncio.sleep(5)
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=False
        )
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
    logger.info("🚀 Старт мониторинга Platinum…")
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

        # на первом проходе — только самый последний, потом — все
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

            # фильтрация по фоновой категории Platinum
            if not backdrop.startswith("Platinum"):
                continue

            # формируем ссылки
            gift_id     = g["gift_id"]
            market_link = f"https://t.me/tonnel_network_bot/gift?startapp={gift_id}"
            gif_url     = f"https://t.me/nft/{normalize_name(name)}-{gift_num}.gif"

            # считаем флоры
            floor_all   = await loop.run_in_executor(None, fetch_floor_price, scraper, name)
            floor_model = await loop.run_in_executor(None, fetch_floor_price, scraper, name, model)

            fa_str = fmt_floor(price, floor_all)
            fm_str = fmt_floor(price, floor_model)

            # готовим текст сообщения
            msg = (
                f"*🎁 {name}* `#{gift_num}`\n"
                f"*Price:* `{price} TON`\n\n"
                f"*all:* `{fa_str}`\n"
                f"*model:* `{fm_str}`\n\n"
                f"*Model:* `{model}`\n"
                f"*Symbol:* `{symbol}`\n"
                f"*Backdrop:* `{backdrop}`\n\n"
                f"🎬 [GIF]({gif_url})"
            )

            # инлайн-кнопка «Buy on Market»
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🛒 Buy on Market", url=market_link)
            ]])

            logger.debug("🔔 Publishing Platinum gift #%s", gift_num)
            await send_alert(bot, CHANNEL_ID, msg, keyboard)

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(monitor())
# bot.py
import os
import json
import asyncio
import logging
from typing import Optional, List, Dict

import cloudscraper
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
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


async def send_alert(bot: Bot, chat_id: str, text: str, keyboard: InlineKeyboardMarkup):
    """Шлём сообщение с инлайн-кнопкой, обрабатывая rate-limit."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=False
        )
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=False
        )
    except TimedOut:
        await asyncio.sleep(5)
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=False
        )
    except TelegramError as e:
        logger.error("Telegram error: %s", e)
    # throttle: не чаще 1 сообщения в 3 секунды
    await asyncio.sleep(3)


def fmt_floor(price: float, floor: Optional[float]) -> tuple[str, float]:
    """Готовим строку вида 'X TON (🔻+Y%)' или '— TON'."""
    if floor is None or floor == 0:
        return "— TON (+0.0%)", 0.0
    pct = (price - floor) / floor * 100
    arrow = "➖" if abs(pct) < 0.05 else ("🔻" if pct < 0 else "🔺")
    return f"{floor} TON ({arrow}{pct:+.1f}%)", pct


async def monitor():
    logger.info("🚀 Старт мониторинга …")
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

        # на первом проходе — только самый последний, потом — все
        to_proc = [docs[0]] if first_run else docs
        first_run = False

        for g in to_proc:
            gift_num = g.get("gift_num")
            if gift_num in seen:
                continue
            seen.add(gift_num)

            name     = g.get("name", "")
            price    = g.get("price", 0)
            model    = g.get("model", "")
            symbol   = g.get("symbol", "")
            backdrop = g.get("backdrop", "")
            key      = normalize_name(name)

            # вычисляем floor для коллекции и модели
            floor_all = await loop.run_in_executor(None, fetch_floor_price, scraper, name)
            floor_mod = await loop.run_in_executor(None, fetch_floor_price, scraper, name, model)

            # условие: цена <= 90% от любого флора
            cond_all = (floor_all is not None and price <= floor_all * 0.9)
            cond_mod = (floor_mod is not None and price <= floor_mod * 0.9)
            if not (cond_all or cond_mod):
                continue

            # формируем ссылки
            market_link = f"https://t.me/tonnel_network_bot/gift?startapp={g['gift_id']}"
            gif_url     = f"https://t.me/nft/{key}-{gift_num}.gif"

            fa_str, _ = fmt_floor(price, floor_all)
            fm_str, _ = fmt_floor(price, floor_mod)

            # готовим текст сообщения
            msg = (
                f"*🎁 {name}* `#{gift_num}`\n"
                f"*Price:* `{price} TON`\n\n"
                f"*all:* `{fa_str}`\n"
                f"*model:* `{fm_str}`\n\n"
                f"*Model:* `{model}`\n"
                f"*Symbol:* `{symbol}`\n"
                f"*Backdrop:* `{backdrop}`\n\n"
                f"🎬 [GIF]({gif_url})"
            )
            # инлайн-кнопка «Buy on Market»sdsd
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy on Market", url=market_link)]])

            logger.debug("🔔 Publishing discount gift #%s", gift_num)
            await send_alert(bot, CHANNEL_ID, msg, keyboard)

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(monitor())
