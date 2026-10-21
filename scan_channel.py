"""Read-only scan of a private Telegram channel (forum with topics).

Purpose: understand the channel before building anything — separate its topics,
count posts, measure cadence, see which instruments show up, and dump every
text caption so the narrative style can be calibrated. It does NOT download or
interpret images (those stay on the server); it reports how many posts carry a
chart so we know which topics are image-heavy.

Topics are reconstructed by grouping messages into their forum threads (works
even on older Telethon that lacks GetForumTopicsRequest). Topic titles come
from the service messages Telegram posts when a topic is created.

RUN (on the server):
    systemctl stop signalbot        # free the Telegram login
    cd ~/signal-bot
    python3 scan_channel.py
    systemctl start signalbot

Prints a per-topic summary and writes caption dumps to ./scan_output/.
"""
from __future__ import annotations

import asyncio
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import config

CHANNEL_ID = 4427092169        # from the link t.me/c/4427092169/1
MONTHS = 12                    # channel is new; read everything recent
MSG_CAP = 4000                 # safety cap on total messages read
OUT_DIR = "scan_output"
GENERAL = 1                    # Telegram's default "General" topic id

_PAIRS = {
    "EU": "EURUSD", "EURUSD": "EURUSD", "EUR/USD": "EURUSD",
    "GU": "GBPUSD", "GBPUSD": "GBPUSD", "GBP/USD": "GBPUSD", "CABLE": "GBPUSD",
    "DXY": "DXY", "DOLLAR INDEX": "DXY", "USDX": "DXY",
    "UJ": "USDJPY", "USDJPY": "USDJPY", "USD/JPY": "USDJPY",
    "GJ": "GBPJPY", "GBPJPY": "GBPJPY", "EJ": "EURJPY", "EURJPY": "EURJPY",
    "AU": "AUDUSD", "AUDUSD": "AUDUSD", "UC": "USDCAD", "USDCAD": "USDCAD",
    "NU": "NZDUSD", "NZDUSD": "NZDUSD",
    "GOLD": "XAUUSD", "XAU": "XAUUSD", "XAUUSD": "XAUUSD",
    "NAS": "NAS100", "NAS100": "NAS100", "US100": "NAS100",
    "SPX": "SPX500", "SP500": "SPX500", "US500": "SPX500",
}
_TERMS = ["FVG", "OB", "BOS", "CHOCH", "MSS", "LIQUIDITY", "SWEEP", "ORDER BLOCK",
          "FAIR VALUE", "IMBALANCE", "SUPPLY", "DEMAND", "WL", "WH", "PDH", "PDL",
          "SMT", "PREMIUM", "DISCOUNT", "BUY", "SELL", "BULLISH", "BEARISH"]


def _pairs_in(text: str) -> list[str]:
    up = f" {text.upper()} "
    return sorted({v for k, v in _PAIRS.items()
                   if re.search(rf"(?<![A-Z]){re.escape(k)}(?![A-Z])", up)})


def _terms_in(text: str) -> list[str]:
    up = text.upper()
    return sorted({t for t in _TERMS if t in up})


async def _get_channel(client):
    from telethon.tl.types import PeerChannel
    try:
        return await client.get_entity(PeerChannel(CHANNEL_ID))
    except Exception:
        async for d in client.iter_dialogs():
            if getattr(d.entity, "id", None) == CHANNEL_ID:
                return d.entity
    return None


def _topic_of(msg) -> int:
    """Which forum topic a message belongs to (GENERAL if top-level)."""
    r = getattr(msg, "reply_to", None)
    if r is not None:
        top = getattr(r, "reply_to_top_id", None)
        if top:
            return top
        if getattr(r, "forum_topic", False):
            return getattr(r, "reply_to_msg_id", None) or GENERAL
    return GENERAL


def _topic_title_from_action(msg):
    """If this is a 'topic created' service message, return (id, title)."""
    act = getattr(msg, "action", None)
    if act is not None and type(act).__name__ == "MessageActionTopicCreate":
        return msg.id, getattr(act, "title", f"topic {msg.id}")
    return None


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from telethon import TelegramClient
    cutoff = datetime.now(timezone.utc) - timedelta(days=MONTHS * 30)
    client = TelegramClient("user_session", config.TG_API_ID, config.TG_API_HASH)
    await client.start()
    entity = await _get_channel(client)
    if not entity:
        print(f"Could not find channel {CHANNEL_ID}. Are you a member?")
        await client.disconnect()
        return

    title = getattr(entity, "title", str(CHANNEL_ID))
    print(f"\nCHANNEL: {title}  (id {CHANNEL_ID})\nReading messages…")

    titles = {GENERAL: "General"}
    topics = defaultdict(lambda: {"count": 0, "img": 0, "pairs": Counter(),
                                  "terms": Counter(), "weeks": defaultdict(int),
                                  "captions": []})
    total = 0
    async for msg in client.iter_messages(entity, limit=MSG_CAP):
        if msg.date and msg.date < cutoff:
            break
        total += 1
        ct = _topic_title_from_action(msg)
        if ct:
            titles[ct[0]] = ct[1]
            continue
        tid = _topic_of(msg)
        d = topics[tid]
        d["count"] += 1
        if msg.photo or (msg.media and not getattr(msg, "web_preview", None)):
            d["img"] += 1
        if msg.date:
            d["weeks"][msg.date.strftime("%G-W%V")] += 1
        txt = (msg.message or "").strip()
        if txt:
            for p in _pairs_in(txt):
                d["pairs"][p] += 1
            for t in _terms_in(txt):
                d["terms"][t] += 1
            d["captions"].append((msg.date.strftime("%Y-%m-%d %H:%M"), txt))

    print(f"Scanned {total} messages. Topics: {len(topics)}\n" + "=" * 55)
    for tid, d in sorted(topics.items(), key=lambda kv: -kv[1]["count"]):
        ttitle = titles.get(tid, f"topic {tid}")
        safe = re.sub(r"[^A-Za-z0-9]+", "_", ttitle)[:40] or str(tid)
        with open(os.path.join(OUT_DIR, f"{safe}.txt"), "w") as f:
            f.write(f"# {ttitle} (topic {tid})\n\n")
            for dt, txt in reversed(d["captions"]):
                f.write(f"[{dt}]\n{txt}\n\n{'-'*40}\n")
        wk = d["weeks"]
        per_wk = (sum(wk.values()) / len(wk)) if wk else 0
        print(f"\n▶ TOPIC: {ttitle}  (id {tid})")
        print(f"   posts: {d['count']} · charts: {d['img']} · captions: {len(d['captions'])}")
        print(f"   cadence: ~{per_wk:.1f}/week over {len(wk)} active weeks")
        if d["pairs"]:
            print("   instruments: " +
                  ", ".join(f"{k}×{v}" for k, v in d["pairs"].most_common(8)))
        if d["terms"]:
            print("   method terms: " +
                  ", ".join(f"{k}×{v}" for k, v in d["terms"].most_common(10)))
        if d["captions"]:
            dt, s = d["captions"][0]
            one = s.replace("\n", " ")
            print(f"   latest [{dt}]: {one[:200]}{'…' if len(one) > 200 else ''}")
        print(f"   full captions -> {OUT_DIR}/{safe}.txt")

    await client.disconnect()
    print("\n" + "=" * 55)
    print(f"Done. Captions saved under ./{OUT_DIR}/ — `cat` the topic you want to share.")


if __name__ == "__main__":
    asyncio.run(main())
