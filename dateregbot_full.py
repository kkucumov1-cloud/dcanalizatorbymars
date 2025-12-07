#!/usr/bin/env python3
"""
dateregbot_full.py
MTProto-powered Telegram "registration date" bot with automatic anchors (2013-current year)

Usage:
    - Set env vars BOT_TOKEN, TELETHON_API_ID, TELETHON_API_HASH
    - Run: python dateregbot_full.py
    - First run: Telethon will ask for phone/code to create session file (dateregbot.session)
Dependencies:
    pip install telethon aiogram requests beautifulsoup4 pillow
Author: Mars-style professional implementation
"""

import os
import sys
import json
import logging
import asyncio
from io import BytesIO
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests
from bs4 import BeautifulSoup
from PIL import Image
from PIL.ExifTags import TAGS

from telethon import TelegramClient
from telethon.tl.types import User, Message as TLMessage

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode

# ------------------------------
# Logging
# ------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dateregbot_full")

# ------------------------------
# Config / Env
# ------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELETHON_API_ID = os.environ.get("TELETHON_API_ID")
TELETHON_API_HASH = os.environ.get("TELETHON_API_HASH")
SESSION_NAME = os.environ.get("TELETHON_SESSION", "dateregbot")  # session filename prefix

if not BOT_TOKEN:
    logger.error("BOT_TOKEN is not set. Exiting.")
    sys.exit(1)
if not TELETHON_API_ID or not TELETHON_API_HASH:
    logger.error("TELETHON_API_ID and TELETHON_API_HASH are required. Exiting.")
    sys.exit(1)

# ------------------------------
# Anchors (2013-current)
# ------------------------------
ANCHORS_FILE = "anchors.json"

def ensure_anchors():
    """
    Создаёт anchors.json с автоматически рассчитанными датами с 2013 по текущий год.
    """
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    start_year = 2013
    end_year = now.year

    # Генерация якорей каждые N пользователей
    anchors = []
    total_anchors = end_year - start_year + 1
    user_id_start = 1000
    user_id_end = 2000000000  # примерно верхняя граница user_id

    step = (user_id_end - user_id_start) // total_anchors
    for i, year in enumerate(range(start_year, end_year + 1)):
        uid = user_id_start + i * step
        dt = datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        anchors.append({"id": uid, "ts": dt.isoformat()})
    
    # добавляем текущую дату с максимальным user_id
    anchors.append({"id": user_id_end, "ts": now.isoformat()})

    # сохраняем
    with open(ANCHORS_FILE, "w", encoding="utf-8") as f:
        json.dump(anchors, f, ensure_ascii=False, indent=2)
    logger.info("Anchors automatically generated from 2013 to %s", now.date())

def load_anchors():
    ensure_anchors()
    with open(ANCHORS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    parsed = []
    for it in data:
        uid = int(it["id"])
        ts = datetime.fromisoformat(it["ts"]).astimezone(timezone.utc)
        parsed.append((uid, ts))
    parsed.sort(key=lambda x: x[0])
    return parsed

def estimate_by_anchors(user_id: int) -> Tuple[datetime, str]:
    """
    Автоматическая интерполяция даты по anchors (2013-current year).
    """
    anchors = load_anchors()
    # exact match
    for uid, ts in anchors:
        if uid == user_id:
            return ts, "Exact anchor match"

    # user_id ниже минимального
    if user_id < anchors[0][0]:
        uid0, t0 = anchors[0]
        uid1, t1 = anchors[1]
        frac = (user_id - uid0) / (uid1 - uid0) if (uid1 - uid0) else 0.0
        est = t0 + (t1 - t0) * frac
        return est, "Extrapolated before first anchor (low confidence)"

    # user_id выше максимального
    if user_id > anchors[-1][0]:
        uid0, t0 = anchors[-2]
        uid1, t1 = anchors[-1]
        frac = (user_id - uid1) / (uid1 - uid0) if (uid1 - uid0) else 0.0
        est = t1 + (t1 - t0) * frac
        return est, "Extrapolated after last anchor (low confidence)"

    # interpolate between two nearest anchors
    lo, hi = anchors[0], anchors[-1]
    for i in range(len(anchors) - 1):
        if anchors[i][0] <= user_id <= anchors[i + 1][0]:
            lo = anchors[i]
            hi = anchors[i + 1]
            break
    uid_lo, t_lo = lo
    uid_hi, t_hi = hi
    frac = (user_id - uid_lo) / (uid_hi - uid_lo) if (uid_hi - uid_lo) else 0.0
    est = t_lo + (t_hi - t_lo) * frac
    return est, f"Interpolated between {uid_lo} and {uid_hi}"

# ------------------------------
# DC detection
# ------------------------------
def detect_dc_from_id(user_id: int) -> int:
    try:
        dc = (user_id >> 28) & 0xF
        if dc == 0:
            return 4
        return dc
    except Exception:
        return 4

# ------------------------------
# EXIF extraction helper
# ------------------------------
def extract_exif_datetime_from_bytes(image_bytes: bytes) -> Optional[datetime]:
    try:
        img = Image.open(BytesIO(image_bytes))
        exif = img._getexif()
        if not exif:
            return None
        val = None
        for k, v in exif.items():
            name = TAGS.get(k, k)
            if name in ("DateTimeOriginal", "DateTime", "DateTimeDigitized"):
                val = v
                break
        if not val:
            return None
        val = val.replace(":", "-", 2)
        dt = datetime.strptime(val, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

# ------------------------------
# t.me scraping (public posts)
# ------------------------------
def scrape_earliest_tme_post(username: str) -> Optional[datetime]:
    try:
        base = f"https://t.me/{username}"
        found = []
        for page in range(1, 6):
            url = base if page == 1 else f"{base}/{page}"
            r = requests.get(url, timeout=8)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for t in soup.find_all("time"):
                dt_attr = t.get("datetime") or t.get("title")
                if not dt_attr:
                    continue
                try:
                    parsed = datetime.fromisoformat(dt_attr.replace("Z", "+00:00")).astimezone(timezone.utc)
                    found.append(parsed)
                except Exception:
                    continue
        if not found:
            return None
        return min(found)
    except Exception:
        return None

# ------------------------------
# Telethon helpers (async)
# ------------------------------
async def resolve_entity_telethon(client: TelegramClient, identifier: str):
    identifier = identifier.strip()
    if identifier.startswith("@"):
        identifier = identifier[1:]
    try:
        ent = await client.get_entity(identifier)
        return ent
    except Exception:
        if identifier.isdigit():
            try:
                ent = await client.get_entity(int(identifier))
                return ent
            except Exception:
                pass
        raise

async def earliest_profile_photo_exif(client: TelegramClient, entity) -> Optional[datetime]:
    try:
        photos = await client.get_profile_photos(entity, limit=20)
        exif_dates = []
        for ph in photos:
            try:
                b = await client.download_media(ph, file=BytesIO())
                if isinstance(b, BytesIO):
                    data = b.getvalue()
                else:
                    data = b
                if not data:
                    continue
                exif_dt = extract_exif_datetime_from_bytes(data)
                if exif_dt:
                    exif_dates.append(exif_dt)
            except Exception:
                continue
        if exif_dates:
            return min(exif_dates)
        return None
    except Exception:
        return None

async def earliest_public_message_date(client: TelegramClient, entity) -> Optional[datetime]:
    username = getattr(entity, "username", None)
    if username:
        scraped = scrape_earliest_tme_post(username)
        if scraped:
            return scraped
    try:
        limit = 500
        async for msg in client.iter_messages(entity, limit=limit, reverse=True):
            if isinstance(msg, TLMessage) and getattr(msg, "date", None):
                return msg.date.astimezone(timezone.utc)
    except Exception:
        return None
    return None

# ------------------------------
# Aggregation logic
# ------------------------------
def choose_final_estimate(results: dict) -> Tuple[Optional[datetime], str, float]:
    if results.get("by_profile_photo"):
        return results["by_profile_photo"], "Earliest profile photo EXIF (high confidence)", 0.9
    if results.get("by_telethon_msg"):
        return results["by_telethon_msg"], "Earliest message found in shared dialogs (medium-high confidence)", 0.75
    if results.get("by_tme_scrape"):
        return results["by_tme_scrape"], "Earliest public t.me post (medium confidence)", 0.6
    if results.get("by_anchors"):
        return results["by_anchors"], "Anchors interpolation (low confidence)", 0.35
    return None, "No usable signal found", 0.0

# ------------------------------
# Bot + Telethon runtime
# ------------------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

tele_client: Optional[TelegramClient] = None

@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    await msg.reply(
        "Привет! Это MTProto-backed dateregbot-like service.\n"
        "Отправь @username, numeric user_id или пересланное сообщение — и я постараюсь определить DC и дату регистрации.\n\n"
        "🔐 Требуется MTProto-сессия (создаётся автоматически).",
        parse_mode=ParseMode.HTML
    )

@dp.message()
async def handle_request(msg: types.Message):
    global tele_client
    text = (msg.text or "").strip()
    identifier = None
    if msg.forward_from and getattr(msg.forward_from, "id", None):
        identifier = str(msg.forward_from.id)
    elif msg.forward_from_chat:
        identifier = getattr(msg.forward_from_chat, "username", None) or str(getattr(msg.forward_from_chat, "id", None))
    elif text:
        identifier = text.split()[0]
    else:
        await msg.reply("Пожалуйста, пришлите @username или numeric user_id или пересланное сообщение.")
        return

    await msg.reply("🔎 Иду собирать данные... это может занять несколько секунд (обычно <10s).")

    try:
        ent = await resolve_entity_telethon(tele_client, identifier)
    except Exception as e:
        await msg.reply(f"❌ Не удалось разрешить идентификатор: {identifier}\nОшибка: {e}")
        return

    results = {}
    user_id = None
    uname = getattr(ent, "username", None)
    name = getattr(ent, "first_name", None) or getattr(ent, "title", None) or str(ent)
    if isinstance(ent, User):
        user_id = int(ent.id)
    else:
        user_id = int(getattr(ent, "id", 0))

    dc = detect_dc_from_id(user_id)

    pf_dt = await earliest_profile_photo_exif(tele_client, ent)
    if pf_dt:
        results["by_profile_photo"] = pf_dt

    tm_dt = await earliest_public_message_date(tele_client, ent)
    if tm_dt:
        results["by_telethon_msg"] = tm_dt

    if uname and "by_tme_scrape" not in results:
        scraped = scrape_earliest_tme_post(uname)
        if scraped:
            results["by_tme_scrape"] = scraped

    anchors_dt, _ = estimate_by_anchors(user_id)
    results["by_anchors"] = anchors_dt

    final_dt, explanation_text, confidence = choose_final_estimate(results)

    lines = []
    lines.append(f"🔍 Результат проверки для <b>{name}</b> {('<code>@'+uname+'</code>') if uname else ''}")
    lines.append(f"ID: <code>{user_id}</code>")
    lines.append(f"DC (detected): <b>{dc}</b>\n")
    if results.get("by_profile_photo"):
        lines.append(f"• Profile photo EXIF: <b>{results['by_profile_photo'].strftime('%Y-%m-%d %H:%M:%S UTC')}</b> (high)")
    if results.get("by_telethon_msg"):
        lines.append(f"• Earliest message (telethon scan): <b>{results['by_telethon_msg'].strftime('%Y-%m-%d %H:%M:%S UTC')}</b> (medium-high)")
    if results.get("by_tme_scrape"):
        lines.append(f"• Earliest public t.me post: <b>{results['by_tme_scrape'].strftime('%Y-%m-%d %H:%M:%S UTC')}</b> (medium)")
    if results.get("by_anchors"):
        lines.append(f"• Anchors estimate: <b>{results['by_anchors'].strftime('%Y-%m-%d %H:%M:%S UTC')}</b> (low)")
    lines.append("")
    if final_dt:
        lines.append(f"✅ <b>Final estimate:</b> <code>{final_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}</code>")
        lines.append(f"ℹ️ Reason: {explanation_text}")
        lines.append(f"🔒 Confidence: {int(confidence*100)}%")
    else:
        lines.append("⚠️ Не удалось получить достаточных данных для оценки.")

    reply_text = "\n".join(lines)
    await msg.reply(reply_text, parse_mode=ParseMode.HTML)

# ------------------------------
# Startup
# ------------------------------
async def start_services():
    global tele_client
    tele_client = TelegramClient(SESSION_NAME, int(TELETHON_API_ID), TELETHON_API_HASH)
    await tele_client.connect()
    if not await tele_client.is_user_authorized():
        await tele_client.start()  # interactive sign-in
    me = await tele_client.get_me()
    logger.info("Telethon started as: %s (%s)", getattr(me, "username", None), getattr(me, "id", None))
    await dp.start_polling(bot)

async def main():
    try:
        await start_services()
    finally:
        try:
            if tele_client and await tele_client.is_connected():
                await tele_client.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down by user")
