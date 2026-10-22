"""Config for the FX signals bot, loaded from .env."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "y"}


# ── Telegram ───────────────────────────────────────────────────────────
TG_API_ID    = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH  = os.getenv("TG_API_HASH", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_OWNER_ID  = int(os.getenv("TG_OWNER_ID", "0"))

# ── FX narratives watcher ──────────────────────────────────────────────
# Reads the signal channel's Narratives topic, uses Gemini to read the drawn
# zones off each chart, you confirm, then it alerts when live price (Yahoo
# Finance, free) nears a zone. All free: Gemini free tier + Yahoo.
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL        = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
FX_CHANNEL_ID       = int(os.getenv("FX_CHANNEL_ID", "4427092169"))
FX_NARRATIVES_TOPIC = int(os.getenv("FX_NARRATIVES_TOPIC", "2"))
# Where "price nearing" alerts go: "channel" = post into the signal channel
# (the bot must be added there as an admin); "owner" = DM you via the bot.
FX_ALERT_TARGET     = os.getenv("FX_ALERT_TARGET", "owner")
# Which topic in the channel to post alerts into (defaults to the Narratives topic).
FX_ALERT_TOPIC      = int(os.getenv("FX_ALERT_TOPIC", str(FX_NARRATIVES_TOPIC)))
FX_NEAR_PIPS        = float(os.getenv("FX_NEAR_PIPS", "15"))
FX_POLL_SEC         = int(os.getenv("FX_POLL_SEC", "60"))
FX_ZONES_FILE       = os.getenv("FX_ZONES_FILE", "fx_zones.json")
# Fully automatic: when true, levels Gemini reads are locked in and watched
# with no confirm tap (the bot just tells you what it locked). false = you tap
# ✅ Confirm on each proposal first.
FX_AUTO_CONFIRM = _b("FX_AUTO_CONFIRM", True)
# Cap how many zones to watch per instrument (keeps the nearest-to-price ones,
# avoids alert spam when a chart has many lines).
FX_MAX_ZONES = int(os.getenv("FX_MAX_ZONES", "6"))
