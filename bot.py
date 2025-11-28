#!/usr/bin/env python3
"""
Fresh bot.py — async, PTB v20+, SQLite, admin + credits, inline buttons.

Usage:
- Set BOT_TOKEN in Railway / env
- (Optional) Set ADMIN_IDS as comma-separated Telegram user IDs
- Deploy to Railway with requirements pinned to a PTB 20.x release.
"""

import os
import logging
import sqlite3
import time
import traceback
from typing import Optional, Set, Tuple

import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# -------------------------
# Load environment
# -------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
RAW_ADMIN_IDS = os.getenv("ADMIN_IDS", "")  # comma separated (e.g. "123,456")
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
API_BASE = os.getenv("API_BASE", "https://earnindia.top/my.php?vehicle=")
LOOKUP_COOLDOWN = float(os.getenv("LOOKUP_COOLDOWN", "2"))  # seconds between lookups per user

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required in environment variables (Railway → Service → Variables).")

# -------------------------
# Helpers: admins, parse
# -------------------------
def parse_admins(raw: str) -> Set[int]:
    out: Set[int] = set()
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except Exception:
            # ignore invalid entries
            continue
    return out

ADMIN_IDS = parse_admins(RAW_ADMIN_IDS)

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("vehicle-bot")

# show installed PTB version (helps debug compatibility)
try:
    import telegram
    logger.info("python-telegram-bot version: %s", getattr(telegram, "__version__", "unknown"))
except Exception:
    logger.info("python-telegram-bot import failed for version logging")

# -------------------------
# Rate limiting (in-memory)
# -------------------------
_last_lookup_ts: dict[int, float] = {}

def can_lookup(user_id: int) -> Tuple[bool, Optional[int]]:
    now = time.time()
    last = _last_lookup_ts.get(user_id, 0)
    diff = now - last
    if diff >= LOOKUP_COOLDOWN:
        return True, None
    else:
        return False, int(LOOKUP_COOLDOWN - diff + 0.999)

def mark_lookup(user_id: int):
    _last_lookup_ts[user_id] = time.time()

# -------------------------
# Database (SQLite)
# -------------------------
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            credits INTEGER DEFAULT 0,
            blocked INTEGER DEFAULT 0,
            access TEXT DEFAULT 'user'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            vehicle TEXT,
            success INTEGER,
            error TEXT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def ensure_user(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def get_user_info(user_id: int) -> Tuple[int, bool, str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT credits, blocked, access FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return int(row[0]), bool(row[1]), str(row[2])
    return 0, False, "user"

def add_credits(user_id: int, amount: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    cur.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (amount, user_id))
    conn.commit()
    conn.close()

def set_block(user_id: int, blocked: bool):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    cur.execute("UPDATE users SET blocked = ? WHERE user_id=?", (1 if blocked else 0, user_id))
    conn.commit()
    conn.close()

def deduct_credit(user_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row or row[0] <= 0:
        conn.close()
        return False
    cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return True

def log_search(user_id: int, vehicle: str, success: bool, error: str = ""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO logs (user_id, vehicle, success, error) VALUES (?, ?, ?, ?)",
                (user_id, vehicle, 1 if success else 0, error))
    conn.commit()
    conn.close()

# -------------------------
# Fetcher
# -------------------------
def fetch_vehicle(vehicle_no: str) -> str:
    url = f"{API_BASE}{vehicle_no}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                return r.json() if isinstance(r.json(), dict) else r.text
            except Exception:
                return r.text
        return r.text
    except Exception as e:
        logger.exception("fetch error")
        return f"❌ Error fetching vehicle data: {e}"

# -------------------------
# Format output
# -------------------------
def format_vehicle_msg(vehicle: str, raw: str, reveal_mobile: bool) -> str:
    # If raw is dict-like, convert to pretty text
    if isinstance(raw, dict):
        import json
        raw_text = json.dumps(raw, indent=2, ensure_ascii=False)
    else:
        raw_text = str(raw)
    mobile_note = "" if reveal_mobile else "\n🔒 Mobile: Available for premium users"
    msg = (
        f"🚗 *Vehicle Information*\n"
        f"➤ *Vehicle Number:* {vehicle}\n\n"
        f"🔎 *Raw data:*\n"
        f"```\n{raw_text}\n```\n"
        f"{mobile_note}"
    )
    return msg

# -------------------------
# Handlers
# -------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id)
    # mark admin access in DB (non-destructive)
    if user.id in ADMIN_IDS:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE users SET access='admin' WHERE user_id=?", (user.id,))
        conn.commit()
        conn.close()

    kb = [
        [InlineKeyboardButton("🔍 Search Vehicle", callback_data="search")],
        [InlineKeyboardButton("💰 Buy Credits", callback_data="buy")],
        [InlineKeyboardButton("💳 My Credits", callback_data="credits")],
    ]
    if user.id in ADMIN_IDS:
        kb.append([InlineKeyboardButton("⚙️ Admin", callback_data="admin")])

    await update.message.reply_text(
        "👋 Welcome! Use the menu below or send `/search <VEHICLE_NO>`.\nExample: `/search KL70C1679`",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "search":
        await q.edit_message_text("Send vehicle number (example: KL70C1679).")
        context.user_data["await_vehicle"] = True
        return

    if q.data == "buy":
        text = "💳 To buy credits contact admin:\n"
        if ADMIN_IDS:
            for aid in ADMIN_IDS:
                text += f"- {aid}\n"
        else:
            text += "No admin configured. Add ADMIN_IDS in Railway variables."
        await q.edit_message_text(text)
        return

    if q.data == "credits":
        credits, blocked, access = get_user_info(uid)
        await q.edit_message_text(f"💳 Credits: *{credits}*\nAccess: {access}", parse_mode="Markdown")
        return

    if q.data == "admin" and uid in ADMIN_IDS:
        kb = [
            [InlineKeyboardButton("➕ Add Credits (/addcredits)", callback_data="noop")],
            [InlineKeyboardButton("🚫 Block User (/block)", callback_data="noop")],
            [InlineKeyboardButton("📣 Broadcast (/broadcast)", callback_data="noop")],
        ]
        await q.edit_message_text("Admin Panel — use commands below", reply_markup=InlineKeyboardMarkup(kb))
        return

# text message handler (used for vehicle after pressing search or plain text)
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    # If waiting for vehicle input
    if context.user_data.get("await_vehicle"):
        context.user_data["await_vehicle"] = False
        vehicle = text.replace(" ", "").upper()
        allowed, wait = can_lookup(user_id)
        if not allowed:
            await update.message.reply_text(f"⏳ Wait {wait}s before next search.")
            return
        credits, blocked, access = get_user_info(user_id)
        if blocked:
            await update.message.reply_text("⛔ You are blocked.")
            return
        if credits <= 0 and user_id not in ADMIN_IDS and access != "premium":
            await update.message.reply_text("❌ No credits. Contact admin.")
            return

        await update.message.reply_text("⏳ Fetching vehicle data...")
        raw = fetch_vehicle(vehicle)
        if user_id not in ADMIN_IDS and access != "premium":
            if not deduct_credit(user_id):
                await update.message.reply_text("❌ Failed to deduct credit. Contact admin.")
                return
        mark_lookup(user_id)
        log_search(user_id, vehicle, True)
        reveal_mobile = user_id in ADMIN_IDS or access == "premium"
        msg = format_vehicle_msg(vehicle, raw, reveal_mobile)
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # default response
    await update.message.reply_text("Send /start to open menu or /search <VEHICLE_NO>")

# /search command
async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /search KL70C1679")
        return
    vehicle = context.args[0].replace(" ", "").upper()
    allowed, wait = can_lookup(user_id)
    if not allowed:
        await update.message.reply_text(f"⏳ Wait {wait}s before next search.")
        return
    credits, blocked, access = get_user_info(user_id)
    if blocked:
        await update.message.reply_text("⛔ You are blocked.")
        return
    if credits <= 0 and user_id not in ADMIN_IDS and access != "premium":
        await update.message.reply_text("❌ No credits. Contact admin.")
        return

    await update.message.reply_text("⏳ Fetching vehicle data...")
    raw = fetch_vehicle(vehicle)
    if user_id not in ADMIN_IDS and access != "premium":
        if not deduct_credit(user_id):
            await update.message.reply_text("❌ Failed to deduct a credit.")
            return
    mark_lookup(user_id)
    log_search(user_id, vehicle, True)
    reveal_mobile = user_id in ADMIN_IDS or access == "premium"
    msg = format_vehicle_msg(vehicle, raw, reveal_mobile)
    await update.message.reply_text(msg, parse_mode="Markdown")

# -------------------------
# Admin commands
# -------------------------
async def addcredits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized")
        return
    try:
        uid = int(context.args[0])
        amt = int(context.args[1])
        add_credits(uid, amt)
        await update.message.reply_text(f"✅ Added {amt} credits to {uid}")
    except Exception:
        await update.message.reply_text("Usage: /addcredits <user_id> <amount>")

async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    try:
        uid = int(context.args[0])
        set_block(uid, True)
        await update.message.reply_text(f"🚫 User {uid} blocked.")
    except Exception:
        await update.message.reply_text("Usage: /block <user_id>")

async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    try:
        uid = int(context.args[0])
        set_block(uid, False)
        await update.message.reply_text(f"✔ User {uid} unblocked.")
    except Exception:
        await update.message.reply_text("Usage: /unblock <user_id>")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    credits, blocked, access = get_user_info(uid)
    await update.message.reply_text(f"💳 Credits: {credits}\nBlocked: {blocked}\nAccess: {access}")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    rows = cur.fetchall()
    conn.close()
    sent = 0
    failed = 0
    for (uid,) in rows:
        try:
            await context.bot.send_message(uid, f"📣 Broadcast:\n\n{text}")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"Sent: {sent}, Failed: {failed}")

# -------------------------
# Error handler
# -------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update", exc_info=context.error)
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"⚠️ Bot error:\n<pre>{tb[:3000]}</pre>", parse_mode="HTML")
        except Exception:
            pass

# -------------------------
# Main
# -------------------------
async def main():
    init_db()
    # ensure admins exist in db
    for aid in ADMIN_IDS:
        ensure_user(aid)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands & handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("addcredits", addcredits_cmd))
    app.add_handler(CommandHandler("block", block_cmd))
    app.add_handler(CommandHandler("unblock", unblock_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))
    app.add_error_handler(error_handler)

    logger.info("Starting bot polling...")
    await app.run_polling()

if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by KeyboardInterrupt")
    except Exception:
        logger.exception("Fatal error in main")
