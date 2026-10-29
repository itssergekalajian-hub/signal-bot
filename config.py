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
GEMINI_MODEL        = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
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

# ── Paper trading (simulation only — no money, no broker) ──────────────
# When a zone with a known bias (📈/📉) locks in, it becomes a paper setup.
# Entry waits for a confirmation (price reacting back out of the zone), stop
# sits beyond the zone, target is PAPER_TP_R in R. Break-even moves the stop to
# entry at +PAPER_BE_AT_R. Results are logged in R, like the channel's backtest.
PAPER_ENABLED         = _b("PAPER_ENABLED", True)
PAPER_CONFIRM_PIPS    = float(os.getenv("PAPER_CONFIRM_PIPS", "5"))     # reaction out of zone = confirm
PAPER_INVALIDATE_PIPS = float(os.getenv("PAPER_INVALIDATE_PIPS", "10")) # through the zone = setup dead
PAPER_SL_BUFFER_PIPS  = float(os.getenv("PAPER_SL_BUFFER_PIPS", "3"))   # stop sits this far past the zone
PAPER_TP_R            = float(os.getenv("PAPER_TP_R", "2"))             # take profit in R
PAPER_BE_AT_R         = float(os.getenv("PAPER_BE_AT_R", "1"))          # move stop to entry at +this R
PAPER_EXPIRE_HOURS    = float(os.getenv("PAPER_EXPIRE_HOURS", "72"))    # drop un-triggered setups after
PAPER_FILE            = os.getenv("PAPER_FILE", "paper_trades.json")
PAPER_POLL_SEC        = int(os.getenv("PAPER_POLL_SEC", "60"))
# Where paper-trading messages (entries, results) go — SEPARATE from the
# area-of-interest alerts. Default "owner" = you only. Same format as
# FX_ALERT_TARGET (owner / channel / id / @username, comma-separated).
PAPER_ALERT_TARGET = os.getenv("PAPER_ALERT_TARGET", "owner")
