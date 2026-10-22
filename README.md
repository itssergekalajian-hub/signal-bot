# FX Signals Bot

A Telegram bot that turns a Smart-Money-Concepts analyst's chart posts into
live price alerts — for free.

## What it does
1. Watches the **Narratives** topic of a signal channel.
2. When a chart is posted, reads the instrument + bias from the caption
   (📈/📉 EU/GU/DXY/XAU) and sends the image to **Google Gemini** (free tier) to
   read the drawn zones (FVG / OB / highs / lows) off the price axis.
3. DMs you the proposed levels to **Confirm / Discard** (or type your own, e.g.
   `GU 1.2850 1.2870 OB`).
4. Watches live price from **Yahoo Finance** (free, no key) for
   EURUSD / GBPUSD / DXY / XAUUSD and alerts when price comes within
   `FX_NEAR_PIPS` of a zone — in the channel or by DM.

It does **not** place trades. Alerting only.

## Run
```
pip install -r requirements.txt
python3 signals.py
```

## .env
```
TG_API_ID=...
TG_API_HASH=...
TG_BOT_TOKEN=...
TG_OWNER_ID=...
GEMINI_API_KEY=AIza...            # free key from aistudio.google.com/apikey
FX_CHANNEL_ID=4427092169          # the signal channel (from t.me/c/<id>/..)
FX_NARRATIVES_TOPIC=2             # the Narratives topic id
FX_ALERT_TARGET=owner             # "channel" to post in the channel, "owner" to DM
FX_NEAR_PIPS=15
```

## Commands (in the Telegram chat with the bot)
- `/menu` — buttons: Zones · Health · Clear all
- `/zones` — list watched zones
- `/fxclear <id|PAIR|all>` — remove zones
- `/fxtest` — health check (live prices + Gemini key)
- Type a level anytime: `GU 1.2850 1.2870 OB`

## Utilities
- `list_chats.py` — list your channels/topics with their IDs.
