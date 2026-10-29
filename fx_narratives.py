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
                    "near_notified": False, "created": time.time()})
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
        f"This is a TradingView chart of {instrument}. A trader has drawn a FEW specific "
        "areas of interest: Fair Value Gaps (FVG, shaded rectangles), Order Blocks (OB, "
        "shaded rectangles), and a few key horizontal lines they labelled — e.g. W "
        "(weekly), WL (weekly low), WH (weekly high), BSL/SSL (liquidity), or prior "
        "highs/lows. Report ONLY the zones the trader actually drew or labelled. "
        "Do NOT invent generic support/resistance. Do NOT report the price-axis grid "
        "lines or candles. There are usually 2–6 such zones. Use the trader's own label "
        "('FVG','OB','W','WL',...). For a shaded box give its lower and upper edge; for a "
        "single line set low=high. Read values from the right-hand price axis. "
        "Return ONLY JSON: {\"zones\":[{\"label\":\"OB\",\"low\":1.1240,\"high\":1.1250}]}. "
        "If you cannot read any, return {\"zones\":[]}."
    )
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode()}},
        ]}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    zones = []
    for attempt in range(4):
        try:
            r = requests.post(url, json=body, timeout=90)
            if r.status_code == 429:           # rate-limited -> wait longer and retry
                time.sleep(15 * (attempt + 1))
                continue
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            zones = json.loads(txt).get("zones", [])
            break
        except Exception:  # noqa: BLE001 — transient/parse -> back off and retry
            time.sleep(3 * (attempt + 1))
    out = []
    for z in zones:
        try:
            out.append({"label": z.get("label", "AOI"),
                        "low": float(z["low"]), "high": float(z["high"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def gemini_probe(image_bytes: bytes, mime: str, instrument: str) -> str:
    """Diagnostic: run Gemini and return a human-readable outcome (not swallowed)."""
    if not config.GEMINI_API_KEY:
        return "GEMINI_API_KEY not set"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}")
    body = {
        "contents": [{"parts": [
            {"text": f"Read the drawn price levels on this {instrument} chart. "
                     "Return JSON {\"zones\":[{\"label\":\"OB\",\"low\":1.1,\"high\":1.1}]}."},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode()}},
        ]}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    try:
        r = requests.post(url, json=body, timeout=90)
    except Exception as e:  # noqa: BLE001
        return f"request failed: {e}"
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        return f"HTTP {r.status_code}, non-JSON body: {r.text[:300]}"
    if r.status_code != 200:
        return f"HTTP {r.status_code}: {(j.get('error') or {}).get('message', r.text[:300])}"
    try:
        return "OK → " + j["candidates"][0]["content"]["parts"][0]["text"][:350]
    except Exception:  # noqa: BLE001
        fin = ((j.get("candidates") or [{}])[0]).get("finishReason")
        return f"200 but no text (finishReason={fin}). Raw: {str(j)[:300]}"


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


def _range_text(instrument: str, z: dict) -> str:
    if abs(z["low"] - z["high"]) < 1e-9:
        return _fmt(instrument, z["low"])
    return f"{_fmt(instrument, z['low'])} – {_fmt(instrument, z['high'])}"


def _pips_away(instrument: str, p: float, z: dict) -> float:
    if z["low"] <= p <= z["high"]:
        return 0.0
    return min(abs(p - z["low"]), abs(p - z["high"])) / _PIP.get(instrument, 0.0001)


def _limit_zones(instrument: str, p, zones: list[dict]) -> list[dict]:
    """Keep only the FX_MAX_ZONES zones nearest current price (dedup first)."""
    seen, uniq = set(), []
    for z in zones:
        try:
            lo, hi = sorted((float(z["low"]), float(z["high"])))
        except (KeyError, TypeError, ValueError):
            continue
        key = (round(lo, 5), round(hi, 5))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(z)
    if not p or len(uniq) <= config.FX_MAX_ZONES:
        return uniq

    def _d(z):
        lo, hi = sorted((float(z["low"]), float(z["high"])))
        return 0.0 if lo <= p <= hi else min(abs(p - lo), abs(p - hi))
    return sorted(uniq, key=_d)[:config.FX_MAX_ZONES]


def _bias_emoji(bias: str) -> str:
    return {"bullish": "📈", "bearish": "📉"}.get(bias, "🧭")


def _near_msg(z: dict, p: float) -> str:
    ins = z["instrument"]
    away = _pips_away(ins, p, z)
    lines = [f"⚠️ *{ins} — approaching area of interest*",
             f"📍 {z['label']}:  `{_range_text(ins, z)}`",
             f"💵 Price:  `{_fmt(ins, p)}`  ({away:.0f} pips away)"]
    if z.get("bias") and z["bias"] != "?":
        lines.append(f"{_bias_emoji(z['bias'])} Bias:  {z['bias']}")
    return "\n".join(lines)


def _reached_msg(z: dict, p: float) -> str:
    ins = z["instrument"]
    lines = [f"🎯 *{ins} — reached area of interest*",
             f"📍 {z['label']}:  `{_range_text(ins, z)}`",
             f"💵 Tapped at:  `{_fmt(ins, p)}`"]
    if z.get("bias") and z["bias"] != "?":
        lines.append(f"{_bias_emoji(z['bias'])} Bias:  {z['bias']}")
    lines.append("✅ _Target hit — zone cleared automatically._")
    return "\n".join(lines)


_delivery_warned = set()


async def deliver(bot_client, notify, text: str) -> None:
    """Send an area-of-interest notification to EVERY configured destination.

    FX_ALERT_TARGET is a comma-separated list; each item is one of:
      "owner"       -> DM the owner via the bot
      "channel"     -> post into FX_CHANNEL_ID's FX_ALERT_TOPIC
      "@name" / id  -> that channel/group/user (bot must be able to reach it)
    e.g. FX_ALERT_TARGET=owner,8376538197,@mychannel
    Each destination is tried independently; failures warn once and don't stop
    the others.
    """
    targets = [t.strip() for t in (config.FX_ALERT_TARGET or "owner").split(",") if t.strip()]
    if not targets:
        targets = ["owner"]
    delivered = False
    for tgt in targets:
        try:
            if tgt == "owner":
                await notify(text)
            elif tgt == "channel":
                from telethon.tl.types import PeerChannel
                await bot_client.send_message(PeerChannel(config.FX_CHANNEL_ID), text,
                                              reply_to=config.FX_ALERT_TOPIC)
            else:
                dest = int(tgt) if tgt.lstrip("-").isdigit() else tgt
                await bot_client.send_message(dest, text)
            delivered = True
        except Exception:  # noqa: BLE001 — can't reach this one -> warn once, keep going
            if tgt not in _delivery_warned:
                _delivery_warned.add(tgt)
                try:
                    await notify(f"⚠️ Couldn't deliver to `{tgt}` — if it's a person they must "
                                 "press Start on the bot; if it's a channel/group, add the bot "
                                 "there (admin to post). Still sending to the others.")
                except Exception:  # noqa: BLE001
                    pass
    if not delivered:
        try:
            await notify(text)
        except Exception:  # noqa: BLE001
            pass


async def _zones_message() -> str:
    zs = _load()
    if not zs:
        return ("🎯 *Watchlist — empty*\n\nZones lock in automatically when a narrative "
                "posts. Scan the channel now with /fxsync, or add one yourself:\n"
                "`GU 1.2850 1.2870 OB`")
    by_ins = {}
    for z in zs:
        by_ins.setdefault(z["instrument"], []).append(z)
    prices = {ins: await asyncio.to_thread(price, ins) for ins in by_ins}
    lines = [f"🎯 *Watchlist* — {len(zs)} zone(s)"]
    for ins in _INSTRUMENTS:
        if ins not in by_ins:
            continue
        p = prices.get(ins)
        bias = next((z.get("bias") for z in by_ins[ins] if z.get("bias") not in (None, "?")), "?")
        head = f"\n{_bias_emoji(bias)} *{ins}*"
        if p:
            head += f"  ·  now `{_fmt(ins, p)}`"
        lines.append(head)
        for z in sorted(by_ins[ins], key=lambda z: _pips_away(ins, p, z) if p else 0):
            away = f"  ·  {_pips_away(ins, p, z):.0f} pips" if p else ""
            lines.append(f"   • {z['label']}  `{_range_text(ins, z)}`{away}   `{z['id']}`")
    lines.append("\n_Remove:_ `/fxclear <id>` · `/fxclear EURUSD` · `/fxclear all`")
    return "\n".join(lines)


async def _health_message() -> str:
    lines = ["🩺 *System status*", "\n*Live prices* (Yahoo Finance)"]
    for ins in _INSTRUMENTS:
        p = await asyncio.to_thread(price, ins)
        lines.append(f"   {'✅' if p else '❌'} {ins}:  `{_fmt(ins, p)}`" if p
                     else f"   ❌ {ins}:  unavailable")
    gkey = "✅" if config.GEMINI_API_KEY else "❌ no key"
    mode = "fully automatic" if config.FX_AUTO_CONFIRM else "confirm-first"
    _names = {"owner": "you (DM)", "channel": "the signal channel"}
    dest = ", ".join(_names.get(t.strip(), t.strip())
                     for t in (config.FX_ALERT_TARGET or "owner").split(",") if t.strip())
    lines += ["\n*Chart reading*",
              f"   Gemini `{config.GEMINI_MODEL}` {gkey}",
              "\n*Alerts*",
              f"   → {dest}  ·  within {config.FX_NEAR_PIPS:g} pips",
              f"   mode: {mode}  ·  checks every {config.FX_POLL_SEC}s",
              f"   watching: {len(_load())} zone(s)"]
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

    async def _ingest(message, announce_fail=True) -> bool:
        """Read one narrative chart and lock in / propose its zones. True if handled."""
        if not message.photo:
            return False
        instrument, bias = _instrument_of(message.message or "")
        if instrument not in _INSTRUMENTS:
            return False
        try:
            blob = await message.download_media(file=bytes)
        except Exception:  # noqa: BLE001
            return False
        zones = await asyncio.to_thread(read_levels, blob, "image/jpeg", instrument)
        if not zones:
            if announce_fail:
                await bot_client.send_message(
                    owner, f"🧭 *{instrument}* narrative, but I couldn't read the levels. "
                    f"Type them in: `{_short_for(instrument)} 1.2345 1.2360 OB`")
            return False
        p = await asyncio.to_thread(price, instrument)
        zones = _limit_zones(instrument, p, zones)
        if config.FX_AUTO_CONFIRM:
            _add_zones(instrument, bias, zones)
            import paper_trader
            paper_trader.add_setups(instrument, bias, zones)
            lines = [f"{_bias_emoji(bias)} *{instrument} — narrative locked in*",
                     "Areas of interest now being watched:"]
            for z in zones:
                lines.append(f"📍 {z.get('label', 'AOI')}:  `{_range_text(instrument, z)}`")
            lines.append(f"\n🔔 I'll alert within {config.FX_NEAR_PIPS:g} pips, and again when "
                         f"price reaches it (then it clears itself).")
            lines.append(f"_Wrong read? `/fxclear {instrument}` to drop these._")
            await deliver(bot_client, notify, "\n".join(lines))
        else:
            await _propose(instrument, bias, zones)
        return True

    # 1) new chart in the Narratives topic (live)
    @user_client.on(events.NewMessage)
    async def _on_post(event):
        if event.chat_id != chan_chat_id:
            return
        if _topic_of(event.message) != config.FX_NARRATIVES_TOPIC:
            return
        await _ingest(event.message)

    # 1b) backfill: process the last charts already posted in the topic
    async def _backfill(n: int) -> int:
        from telethon.tl.types import PeerChannel
        done = 0
        async for msg in user_client.iter_messages(
                PeerChannel(config.FX_CHANNEL_ID),
                reply_to=config.FX_NARRATIVES_TOPIC, limit=n * 5):
            if await _ingest(msg, announce_fail=False):
                done += 1
            if done >= n:
                break
        return done

    # 1c) full scan: find the CURRENT narrative per instrument (latest chart
    #     wins) and lock in only the levels price hasn't reached yet.
    async def _sync() -> str:
        from telethon.tl.types import PeerChannel
        latest = {}  # instrument -> (message, bias); first seen = newest
        async for msg in user_client.iter_messages(
                PeerChannel(config.FX_CHANNEL_ID),
                reply_to=config.FX_NARRATIVES_TOPIC, limit=1000):
            if not msg.photo:
                continue
            inst, bias = _instrument_of(msg.message or "")
            if inst in _INSTRUMENTS and inst not in latest:
                latest[inst] = (msg, bias)
            if len(latest) == len(_INSTRUMENTS):
                break
        if not latest:
            return "Scanned the topic but found no charts I recognise."

        # drop existing zones for the instruments we're refreshing
        _save([z for z in _load() if z["instrument"] not in latest])

        out = ["🧭 *Channel scan — current pending targets*"]
        any_pending = False
        for inst in _INSTRUMENTS:
            if inst not in latest:
                continue
            msg, bias = latest[inst]
            try:
                blob = await msg.download_media(file=bytes)
                zones = await asyncio.to_thread(read_levels, blob, "image/jpeg", inst)
            except Exception:  # noqa: BLE001
                zones = []
            p = await asyncio.to_thread(price, inst)
            zones = _limit_zones(inst, p, zones)
            out.append(f"\n{_bias_emoji(bias)} *{inst}* — now `{_fmt(inst, p) if p else '?'}`")
            if not zones:
                out.append("  _latest chart unreadable_")
                await asyncio.sleep(1.5)       # ease the free-tier rate limit
                continue
            pend = []
            for z in zones:
                lo, hi = sorted((float(z["low"]), float(z["high"])))
                zz = {"label": z.get("label", "AOI"), "low": lo, "high": hi}
                if p and lo <= p <= hi:
                    out.append(f"  ✔️ {zz['label']} `{_range_text(inst, zz)}` — price already here")
                else:
                    pend.append(z)
                    away = f" · {_pips_away(inst, p, zz):.0f} pips" if p else ""
                    out.append(f"  🎯 {zz['label']} `{_range_text(inst, zz)}`{away}")
            if pend:
                any_pending = True
                _add_zones(inst, bias, pend)
                import paper_trader
                paper_trader.add_setups(inst, bias, pend)
            await asyncio.sleep(1.5)            # ease the free-tier rate limit
        out.append(f"\n🔔 Locked in as targets — alerts within {config.FX_NEAR_PIPS:g} pips."
                   if any_pending else "\n_Nothing pending — price is already at every level._")
        return "\n".join(out)

    @bot_client.on(events.NewMessage(from_users=owner, pattern=r"^/fxsync"))
    async def _on_sync(event):
        m = await event.reply("🔎 Scanning the entire Narratives topic…")
        try:
            txt = await _sync()
        except Exception as e:  # noqa: BLE001
            await m.edit(f"Scan failed: {e}")
            return
        await m.edit(txt)

    @bot_client.on(events.NewMessage(from_users=owner, pattern=r"^/fxdiag"))
    async def _on_diag(event):
        from telethon.tl.types import PeerChannel
        await event.reply("🔧 Testing Gemini on the latest chart…")
        async for msg in user_client.iter_messages(
                PeerChannel(config.FX_CHANNEL_ID),
                reply_to=config.FX_NARRATIVES_TOPIC, limit=40):
            if not msg.photo:
                continue
            inst, _ = _instrument_of(msg.message or "")
            if inst not in _INSTRUMENTS:
                continue
            blob = await msg.download_media(file=bytes)
            res = await asyncio.to_thread(gemini_probe, blob, "image/jpeg", inst)
            await event.reply(f"Chart: {inst} · image {len(blob or b'')} bytes\n"
                              f"Model: {config.GEMINI_MODEL}\n\n{res}")
            return
        await event.reply("No chart found to test.")

    @bot_client.on(events.NewMessage(from_users=owner, pattern=r"^/fxlast"))
    async def _on_last(event):
        parts = event.raw_text.split()
        n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 3
        await event.reply(f"🔎 Reading the last {n} chart(s) from the Narratives topic…")
        try:
            done = await _backfill(n)
        except Exception as e:  # noqa: BLE001
            await event.reply(f"Couldn't read history: {e}")
            return
        await event.reply(f"✅ Locked in {done} recent chart(s). Use /zones to see them."
                          if done else "No readable charts found in the recent posts.")


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
        await event.reply(await _zones_message())

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

    # ── paper-trading commands ──────────────────────────────────────────
    @bot_client.on(events.NewMessage(from_users=owner, pattern=r"^/pstats"))
    async def _on_pstats(event):
        import paper_trader
        await event.reply(paper_trader.stats_message())

    @bot_client.on(events.NewMessage(from_users=owner, pattern=r"^/ptrades"))
    async def _on_ptrades(event):
        import paper_trader
        await event.reply(paper_trader.trades_message())

    @bot_client.on(events.NewMessage(from_users=owner, pattern=r"^/pclear"))
    async def _on_pclear(event):
        import paper_trader
        paper_trader.clear()
        await event.reply("🧹 Paper-trading ledger reset.")

    # ── FX menu ────────────────────────────────────────────────────────
    def _menu_markup():
        return [[Button.inline("🔁 Scan channel", b"fxmenu:sync"),
                 Button.inline("🎯 Watchlist", b"fxmenu:zones")],
                [Button.inline("🩺 Status", b"fxmenu:health"),
                 Button.inline("🧹 Clear all", b"fxmenu:clearall")],
                [Button.inline("ℹ️ How it works", b"fxmenu:help")]]

    _menu_title = (
        "📡 *FX Signals*\n\n"
        "I watch the *Narratives* topic, read each chart automatically, and alert you "
        "when price nears an area of interest — then again when it reaches it "
        "(auto-cleared).\n\n"
        "_Markets:_ EURUSD · GBPUSD · DXY · XAUUSD")

    _help_text = (
        "ℹ️ *How it works*\n\n"
        "1️⃣  An analyst posts a chart → I read the zones with AI and lock them in.\n"
        "2️⃣  ⚠️ I alert when price comes within "
        f"{config.FX_NEAR_PIPS:g} pips.\n"
        "3️⃣  🎯 I alert again when price *reaches* it, then clear it automatically.\n\n"
        "*Everything is automatic — you don't have to do anything.*\n\n"
        "*Commands*\n"
        "• /fxsync — scan the whole channel for current targets\n"
        "• /zones — your live watchlist\n"
        "• /fxtest — system status\n"
        "• /fxclear `id | PAIR | all` — remove zones\n"
        "• /fxlast `N` — re-read the last N charts\n"
        "• Add your own:  `GU 1.2850 1.2870 OB`")

    @bot_client.on(events.NewMessage(from_users=owner, pattern=r"^/(menu|start|help)"))
    async def _on_menu(event):
        await event.reply(_menu_title, buttons=_menu_markup())

    # Anyone else who starts the bot (e.g. the person you want alerts sent to)
    # gets their numeric ID, which you set as FX_ALERT_TARGET to receive alerts.
    @bot_client.on(events.NewMessage(pattern=r"^/(start|id|myid)"))
    async def _on_whoami(event):
        if event.sender_id == owner:
            return  # the owner's /start opens the menu above
        await event.reply(
            f"👋 You're connected to the FX Signals bot.\n\n"
            f"Your Telegram ID is:  `{event.sender_id}`\n\n"
            "Send this ID to the bot owner so they can route the alerts to you.")

    @bot_client.on(events.CallbackQuery(pattern=rb"^fxmenu:"))
    async def _on_menu_click(event):
        what = event.data.decode().split(":", 1)[1]
        await event.answer()
        if what == "zones":
            await event.respond(await _zones_message())
        elif what == "help":
            await event.respond(_help_text)
        elif what == "health":
            m = await event.respond("🩺 Checking…")
            await m.edit(await _health_message())
        elif what == "clearall":
            _save([])
            await event.respond("🧹 Cleared all zones.")
        elif what == "last":
            m = await event.respond("🔎 Reading the last 3 charts…")
            try:
                done = await _backfill(3)
            except Exception as e:  # noqa: BLE001
                await m.edit(f"Couldn't read history: {e}")
                return
            await m.edit(f"✅ Locked in {done} recent chart(s). /zones to see them."
                         if done else "No readable charts in the recent posts.")
        elif what == "sync":
            m = await event.respond("🔎 Scanning the entire Narratives topic…")
            try:
                await m.edit(await _sync())
            except Exception as e:  # noqa: BLE001
                await m.edit(f"Scan failed: {e}")


# ── watch loop ────────────────────────────────────────────────────────
async def run(bot_client, notify):
    """Poll live prices; alert when price nears a watched zone."""

    async def _alert(text):
        await deliver(bot_client, notify, text)

    while True:
        try:
            zones = _load()
            if zones:
                prices = {}
                for ins in {z["instrument"] for z in zones}:
                    prices[ins] = await asyncio.to_thread(price, ins)
                kept, changed = [], False
                for z in zones:
                    p = prices.get(z["instrument"])
                    if not p:
                        kept.append(z)
                        continue
                    pip = _PIP.get(z["instrument"], 0.0001)
                    near = config.FX_NEAR_PIPS * pip
                    # REACHED: price entered the zone -> alert + auto-clear (drop it)
                    if z["low"] <= p <= z["high"]:
                        await _alert(_reached_msg(z, p))
                        changed = True
                        continue  # not appended to kept = removed
                    dist = min(abs(p - z["low"]), abs(p - z["high"]))
                    # NEARING: within N pips, announced once
                    if dist <= near and not z.get("near_notified"):
                        await _alert(_near_msg(z, p))
                        z["near_notified"] = True
                        changed = True
                    elif dist > near * 2 and z.get("near_notified"):
                        z["near_notified"] = False       # moved away -> allow a fresh heads-up
                        changed = True
                    kept.append(z)
                if changed:
                    _save(kept)
        except Exception as e:  # noqa: BLE001 — never let the loop die
            try:
                await notify(f"fx watch error: {e}")
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(config.FX_POLL_SEC)
