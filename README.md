# Vehicle Details Telegram Bot — System 3.0

## Overview
Telegram bot that fetches vehicle details from a target site, with:
- Per-search credits
- Admin panel (add credits, block/unblock, broadcast)
- SQLite logging
- Optional Flask health endpoint

## Files
- bot.py
- requirements.txt
- Procfile
- .env.example

## Deploy on Railway (no local Python needed)
1. Create GitHub repo and add these files (Create new file → paste contents).
2. Go to Railway (https://railway.app) → New Project → Deploy from GitHub → choose repo.
3. In Railway project → Variables, add:
   - BOT_TOKEN = <your_bot_token>
   - ADMIN_IDS = 345012569
   - (optional) FLASK_ENABLE = true
4. Ensure `requirements.txt` contains packages above.
5. Railway will build and start. Check Logs → Runtime.
6. Use `/start` in Telegram and test `/search KL70C1679`.

## Local testing
1. Copy `.env.example` → `.env` and fill values.
2. `python -m venv venv`
3. `source venv/bin/activate` (Windows: `venv\Scripts\activate`)
4. `pip install -r requirements.txt`
5. `python bot.py`

## Notes
- Do NOT commit `.env` to GitHub.
- To enable health endpoint, add `flask` to requirements and set `FLASK_ENABLE=true`.
