"""Economic-calendar / news awareness — free, no key.

Uses Forex Factory's public weekly calendar JSON to know when high-impact news
is due. The channel trades around news ("today's news would decide"), so this
lets the bot (a) show upcoming red-folder events and (b) flag/avoid trading into
them. Cached ~30 min.
"""
from __future__ import annotations

import time
from datetime import datetime

import requests

_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_cache = {"ts": 0.0, "data": []}

# currencies that move each instrument
_CCY = {"EURUSD": ["EUR", "USD"], "GBPUSD": ["GBP", "USD"], "AUDUSD": ["AUD", "USD"],
        "NZDUSD": ["NZD", "USD"], "USDCAD": ["USD", "CAD"], "USDJPY": ["USD", "JPY"],
        "GBPJPY": ["GBP", "JPY"], "EURJPY": ["EUR", "JPY"], "XAUUSD": ["USD"],
        "DXY": ["USD"], "NAS100": ["USD"], "SPX500": ["USD"]}


def _fetch() -> list[dict]:
    if _cache["data"] and time.time() - _cache["ts"] < 1800:
        return _cache["data"]
    try:
        r = requests.get(_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        data = r.json()
        if isinstance(data, list):
            _cache.update(ts=time.time(), data=data)
    except Exception:  # noqa: BLE001 — keep last good data on failure
        pass
    return _cache["data"]


def _ts(s: str):
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:  # noqa: BLE001
        return None


def upcoming_high(within_hours: float = 24, currencies=None) -> list[tuple]:
    """[(ts, currency, title)] high-impact events in the window, soonest first."""
    now = time.time()
    out = []
    for e in _fetch():
        if (e.get("impact") or "").lower() != "high":
            continue
        ts = _ts(e.get("date", ""))
        if not ts or ts < now or ts > now + within_hours * 3600:
            continue
        cur = e.get("country") or e.get("currency") or "?"
        if currencies and cur not in currencies:
            continue
        out.append((ts, cur, e.get("title", "")))
    return sorted(out)


def news_soon(instrument: str, minutes: float = 60):
    """Next high-impact event for this instrument's currencies within `minutes`."""
    cur = set(_CCY.get(instrument, ["USD"]))
    ev = upcoming_high(within_hours=minutes / 60.0, currencies=cur)
    return ev[0] if ev else None


def context_line(instrument: str, minutes: float = 180) -> str:
    """A short '⚠️ news in Xm' line for an instrument, or '' if nothing soon."""
    ev = news_soon(instrument, minutes)
    if not ev:
        return ""
    ts, cur, title = ev
    mins = int((ts - time.time()) / 60)
    return f"⚠️ high-impact {cur} news in {mins}m: {title}"


def news_message() -> str:
    ev = upcoming_high(within_hours=48)
    if not ev:
        return "📅 No high-impact news in the next 48h (or calendar unavailable)."
    lines = ["📅 *High-impact news — next 48h*"]
    for ts, cur, title in ev[:18]:
        when = datetime.fromtimestamp(ts).strftime("%a %H:%M")
        lines.append(f"  🔴 {when}  {cur}  —  {title}")
    return "\n".join(lines)
