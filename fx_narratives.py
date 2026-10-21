"""FX narratives watcher — a free, read-the-chart → confirm → alert pipeline.

Separate from the crypto trading bot; it shares the same Telegram clients and
event loop (one login, no session conflict). Flow:

 1. A new post lands in the signal channel's "Narratives" topic with a chart.
 2. We read the instrument + bias from the caption (📈/📉 EU/GU/DXY/XAU) and send
    the chart image to Gemini (free tier) to read the drawn price zones off the
    axis — FVG / OB / highs / lows.
 3. We DM you the proposed zones with Confirm / Discard buttons; you can also
    just type corrections ("GU 1.2850 1.2870 OB").
 4. Confirmed zones are watched against live price (Yahoo Finance, free, no key).
    When price comes within FX_NEAR_PIPS of a zone, we alert — in the channel or
    by DM (FX_ALERT_TARGET).

Nothing here trades. It is an alerting layer only.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import time

import requests

import config

# instrument shorthand (as used in the captions) -> canonical symbol
_SHORT = {"EU": "EURUSD", "EURUSD": "EURUSD", "GU": "GBPUSD", "GBPUSD": "GBPUSD",
          "DXY": "DXY", "XAU": "XAUUSD", "XAUUSD": "XAUUSD", "GOLD": "XAUUSD"}
_INSTRUMENTS = ["EURUSD", "GBPUSD", "DXY", "XAUUSD"]
# one pip, per instrument (for the "within N pips" test)
_PIP = {"EURUSD": 0.0001, "GBPUSD": 0.0001, "XAUUSD": 0.1, "DXY": 0.01}
# Yahoo Finance symbols (tried in order; first that returns a price wins)
_YSYM = {"EURUSD": ["EURUSD=X"], "GBPUSD": ["GBPUSD=X"],
         "XAUUSD": ["XAUUSD=X", "GC=F"], "DXY": ["DX-Y.NYB", "DX=F"]}

_pending: dict[str, dict] = {}          # proposal id -> {instrument, bias, zones}
_awaiting_edit: dict[int, str] = {}     # owner id -> proposal id being edited


# ── storage ───────────────────────────────────────────────────────────
def _load() -> list[dict]:
    try:
        with open(config.FX_ZONES_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _save(zones: list[dict]) -> None:
    tmp = config.FX_ZONES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(zones, f, indent=2)
    os.replace(tmp, config.FX_ZONES_FILE)


def _add_zones(instrument: str, bias: str, zones: list[dict]) -> int:
    cur = _load()
    n = 0
    for z in zones:
        lo, hi = sorted((float(z["low"]), float(z["high"])))
        cur.append({"id": secrets.token_hex(3), "instrument": instrument, "bias": bias,
                    "label": str(z.get("label") or "AOI"), "low": lo, "high": hi,
                    "armed": True, "created": time.time()})
        n += 1
    _save(cur)
    return n


# ── caption parsing ───────────────────────────────────────────────────
def _instrument_of(caption: str):
    """(symbol, bias) from a caption header like '📉 GU' — or (None, None)."""
    if not caption:
        return None, None
    bias = "bearish" if "📉" in caption else ("bullish" if "📈" in caption else "?")
    head = caption.strip().split("\n", 1)[0].upper()
    for tok in re.findall(r"[A-Z]{2,6}", head):
        if tok in _SHORT:
            return _SHORT[tok], bias
    return None, bias


# ── Gemini vision ─────────────────────────────────────────────────────
def read_levels(image_bytes: bytes, mime: str, instrument: str) -> list[dict]:
    """Ask Gemini to read the drawn price zones off the chart. [] on failure."""
    if not config.GEMINI_API_KEY:
        return []
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}")
    prompt = (
        f"This is a TradingView chart screenshot of {instrument}. A trader has drawn "
        "horizontal levels and rectangular zones (FVG = fair value gap, OB = order "
        "block, and support/resistance lines, often labelled like OB, FVG, W, WL). "
        "Read each DRAWN level's price value from the right-hand price axis. "
        "Return ONLY JSON: {\"zones\":[{\"label\":\"OB\",\"low\":1.1240,\"high\":1.1250}]}. "
        "For a single horizontal line use the same number for low and high. Report "
        "prices on the axis's own scale. Include only the trader's drawn levels, not "
        "every candle. If you cannot read any, return {\"zones\":[]}."
    )
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode()}},
        ]}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    try:
        r = requests.post(url, json=body, timeout=45)
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        zones = json.loads(txt).get("zones", [])
    except Exception:  # noqa: BLE001 — bad key / quota / parse -> propose nothing
        return []
    out = []
    for z in zones:
        try:
            out.append({"label": z.get("label", "AOI"),
                        "low": float(z["low"]), "high": float(z["high"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ── live price (Yahoo Finance, free, no key) ──────────────────────────
def price(instrument: str):
    for sym in _YSYM.get(instrument, []):
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                params={"interval": "1m", "range": "1d"},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            meta = r.json()["chart"]["result"][0]["meta"]
            p = meta.get("regularMarketPrice")
            if p:
                return float(p)
        except Exception:  # noqa: BLE001 — try the next symbol / give up
            continue
    return None


def _fmt(instrument: str, v: float) -> str:
    dec = 5 if instrument in ("EURUSD", "GBPUSD") else (2 if instrument in ("DXY", "XAUUSD") else 4)
    return f"{v:.{dec}f}"


def _zone_text(instrument: str, z: dict) -> str:
    if abs(z["low"] - z["high"]) < 1e-9:
        return f"{z['label']} {_fmt(instrument, z['low'])}"
    return f"{z['label']} {_fmt(instrument, z['low'])}–{_fmt(instrument, z['high'])}"


def _zones_message() -> str:
    zs = _load()
    if not zs:
        return ("📭 No zones being watched. They're added when you confirm a narrative, "
                "or type one like `GU 1.2850 1.2870 OB`.")
    lines = ["🎯 *Watched zones*"]
    for z in zs:
        lines.append(f"  • `{z['id']}` {z['instrument']} {_zone_text(z['instrument'], z)}")
    lines.append("\nClear one: `/fxclear <id>` · a pair: `/fxclear EURUSD` · all: `/fxclear all`")
    return "\n".join(lines)


async def _health_message() -> str:
    lines = ["*Live prices (Yahoo):*"]
    for ins in _INSTRUMENTS:
        p = await asyncio.to_thread(price, ins)
        lines.append(f"  • {ins}: {_fmt(ins, p) if p else '❌ unavailable'}")
    gem = "set ✅" if config.GEMINI_API_KEY else "❌ not set"
    lines.append(f"\nGemini key: {gem} · model {config.GEMINI_MODEL}")
    dest = "the channel" if config.FX_ALERT_TARGET == "channel" else "you (DM)"
    lines.append(f"Alerts → {dest} · within {config.FX_NEAR_PIPS:g} pips")
    return "\n".join(lines)


# ── registration ──────────────────────────────────────────────────────
def register(user_client, bot_client, notify):
    """Attach all handlers. `notify(text)` DMs the owner via the bot."""
    from telethon import Button, events
    from telethon.tl.types import PeerChannel

    owner = config.TG_OWNER_ID
    chan_chat_id = int(f"-100{config.FX_CHANNEL_ID}")

    def _topic_of(msg) -> int:
        r = getattr(msg, "reply_to", None)
        if r is not None:
            top = getattr(r, "reply_to_top_id", None)
            if top:
                return top
            if getattr(r, "forum_topic", False):
                return getattr(r, "reply_to_msg_id", None) or 1
        return 1

    async def _propose(instrument, bias, zones):
        pid = secrets.token_hex(4)
        _pending[pid] = {"instrument": instrument, "bias": bias, "zones": zones}
        lines = [f"🧭 *New {instrument} narrative* ({bias})",
                 "Gemini read these levels off the chart:"]
        for z in zones:
            lines.append(f"  • {_zone_text(instrument, z)}")
        lines.append("\nConfirm to watch them, or type corrections like "
                     f"`{_short_for(instrument)} 1.2345 1.2360 OB`.")
        buttons = [[Button.inline("✅ Confirm", f"fxok:{pid}".encode()),
                    Button.inline("❌ Discard", f"fxno:{pid}".encode())]]
        await bot_client.send_message(owner, "\n".join(lines), buttons=buttons)

    def _short_for(instrument):
        for k, v in _SHORT.items():
            if v == instrument and len(k) <= 3:
                return k
        return instrument

    # 1) new chart in the Narratives topic
    @user_client.on(events.NewMessage)
    async def _on_post(event):
        if event.chat_id != chan_chat_id:
            return
        if _topic_of(event.message) != config.FX_NARRATIVES_TOPIC:
            return
        if not event.message.photo:
            return
        instrument, bias = _instrument_of(event.message.message or "")
        if instrument not in _INSTRUMENTS:
            return
        try:
            blob = await event.message.download_media(file=bytes)
        except Exception:  # noqa: BLE001
            return
        zones = await asyncio.to_thread(read_levels, blob, "image/jpeg", instrument)
        if not zones:
            await bot_client.send_message(
                owner, f"🧭 New *{instrument}* narrative posted, but I couldn't read the "
                f"levels. Type them in: `{_short_for(instrument)} 1.2345 1.2360 OB`")
            return
        if config.FX_AUTO_CONFIRM:
            n = _add_zones(instrument, bias, zones)
            lines = [f"🔒 *Locked in {instrument}* ({bias}) — read from the chart:"]
            for z in zones:
                lines.append(f"  • {_zone_text(instrument, z)}")
            lines.append(f"\nWatching · alert within {config.FX_NEAR_PIPS:g} pips. "
                         f"Wrong? `/fxclear {instrument}` then retype a level.")
            await bot_client.send_message(owner, "\n".join(lines))
        else:
            await _propose(instrument, bias, zones)

    # 2) confirm / discard buttons
    @bot_client.on(events.CallbackQuery(pattern=rb"^fx(ok|no):"))
    async def _on_btn(event):
        action, pid = event.data.decode().split(":")
        p = _pending.pop(pid, None)
        await event.answer()
        if not p:
            await event.edit("⌛ That proposal expired.")
            return
        if action == "fxno":
            await event.edit("❌ Discarded.")
            return
        n = _add_zones(p["instrument"], p["bias"], p["zones"])
        await event.edit(f"✅ Watching {n} {p['instrument']} zone(s). I'll alert within "
                         f"{config.FX_NEAR_PIPS:g} pips.")

    # 3) manual corrections / additions: "GU 1.2850 1.2870 OB"
    @bot_client.on(events.NewMessage(from_users=owner,
                                     pattern=r"(?i)^(EU|GU|DXY|XAU|GOLD|EURUSD|GBPUSD|XAUUSD)\s+[\d.]+"))
    async def _on_manual(event):
        added = 0
        for line in event.raw_text.splitlines():
            m = re.match(r"(?i)^\s*([A-Z]{2,6})\s+([\d.]+)(?:\s+([\d.]+))?\s*(.*)$", line.strip())
            if not m:
                continue
            sym = _SHORT.get(m.group(1).upper())
            if not sym:
                continue
            lo = float(m.group(2))
            hi = float(m.group(3)) if m.group(3) else lo
            label = (m.group(4) or "AOI").strip()[:20] or "AOI"
            added += _add_zones(sym, "?", [{"label": label, "low": lo, "high": hi}])
        if added:
            await event.reply(f"✅ Added {added} zone(s). Use /zones to see all.")

    # 4) commands
    @bot_client.on(events.NewMessage(from_users=owner, pattern=r"^/zones"))
    async def _on_zones(event):
        await event.reply(_zones_message())

    @bot_client.on(events.NewMessage(from_users=owner, pattern=r"^/fxclear"))
    async def _on_clear(event):
        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.reply("Usage: `/fxclear <id>` or `/fxclear EURUSD` or `/fxclear all`")
            return
        arg = parts[1].strip()
        zs = _load()
        if arg.lower() == "all":
            _save([])
            await event.reply("🧹 Cleared all zones.")
            return
        sym = _SHORT.get(arg.upper(), arg.upper())
        kept = [z for z in zs if z["id"] != arg and z["instrument"] != sym]
        _save(kept)
        await event.reply(f"🧹 Cleared {len(zs) - len(kept)} zone(s). {len(kept)} left.")

    @bot_client.on(events.NewMessage(from_users=owner, pattern=r"^/fxtest"))
    async def _on_test(event):
        m = await event.reply("🔧 Checking…")
        await m.edit(await _health_message())

    # ── FX menu ────────────────────────────────────────────────────────
    def _menu_markup():
        return [[Button.inline("🎯 Zones", b"fxmenu:zones"),
                 Button.inline("🧪 Health", b"fxmenu:health")],
                [Button.inline("🧹 Clear all", b"fxmenu:clearall")]]

    _menu_title = ("📡 *FX Signals menu*\n"
                   "Watching the Narratives topic · EURUSD / GBPUSD / DXY / XAUUSD.\n"
                   "Confirm levels when a narrative posts, or type one like "
                   "`GU 1.2850 1.2870 OB`.")

    @bot_client.on(events.NewMessage(from_users=owner, pattern=r"^/(menu|start)"))
    async def _on_menu(event):
        await event.reply(_menu_title, buttons=_menu_markup())

    @bot_client.on(events.CallbackQuery(pattern=rb"^fxmenu:"))
    async def _on_menu_click(event):
        what = event.data.decode().split(":", 1)[1]
        await event.answer()
        if what == "zones":
            await event.respond(_zones_message())
        elif what == "health":
            m = await event.respond("🔧 Checking…")
            await m.edit(await _health_message())
        elif what == "clearall":
            _save([])
            await event.respond("🧹 Cleared all zones.")


# ── watch loop ────────────────────────────────────────────────────────
async def run(bot_client, notify):
    """Poll live prices; alert when price nears a watched zone."""
    from telethon.tl.types import PeerChannel

    async def _alert(text):
        if config.FX_ALERT_TARGET == "channel":
            try:
                await bot_client.send_message(PeerChannel(config.FX_CHANNEL_ID), text,
                                              reply_to=config.FX_ALERT_TOPIC)
                return
            except Exception:  # noqa: BLE001 — bot not in channel -> fall back to DM
                await notify("⚠️ (couldn't post in the channel — is the bot added there "
                             "as admin? Falling back to DM.)")
        await notify(text)

    while True:
        try:
            zones = _load()
            if zones:
                prices = {}
                for ins in {z["instrument"] for z in zones}:
                    prices[ins] = await asyncio.to_thread(price, ins)
                changed = False
                for z in zones:
                    p = prices.get(z["instrument"])
                    if not p:
                        continue
                    pip = _PIP.get(z["instrument"], 0.0001)
                    near = config.FX_NEAR_PIPS * pip
                    inside = z["low"] <= p <= z["high"]
                    dist = 0.0 if inside else min(abs(p - z["low"]), abs(p - z["high"]))
                    if dist <= near and z.get("armed", True):
                        where = "inside" if inside else "approaching"
                        await _alert(
                            f"⚠️ *{z['instrument']}* {where} "
                            f"{_zone_text(z['instrument'], z)} — now {_fmt(z['instrument'], p)}")
                        z["armed"] = False
                        changed = True
                    elif dist > near * 2 and not z.get("armed", True):
                        z["armed"] = True            # moved away -> re-arm
                        changed = True
                if changed:
                    _save(zones)
        except Exception as e:  # noqa: BLE001 — never let the loop die
            try:
                await notify(f"fx watch error: {e}")
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(config.FX_POLL_SEC)
