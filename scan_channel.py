"""Read-only scan of a private Telegram channel (forum with topics).

Purpose: understand the channel before building anything — list its topics,
count posts, measure cadence, see which instruments show up, and dump every
text caption so the narrative style can be calibrated. It does NOT download or
interpret images (those stay on the server); it reports how many posts carry a
chart so we know which topics are image-heavy.

RUN (on the server):
    systemctl stop signalbot        # free the Telegram login
    cd ~/signal-bot
    python3 scan_channel.py
    systemctl start signalbot

Prints a summary and writes per-topic caption dumps to ./scan_output/.
Paste the summary back. If you want a topic's full captions, cat its file.
"""
from __future__ import annotations

import asyncio
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import config

# The channel from the link t.me/c/4427092169/1
CHANNEL_ID = 4427092169
MONTHS = 6
PER_TOPIC_LIMIT = 800          # safety cap on messages read per topic
OUT_DIR = "scan_output"

# FX shorthands these desks use -> canonical pair
_PAIRS = {
    "EU": "EURUSD", "EURUSD": "EURUSD", "EUR/USD": "EURUSD",
    "GU": "GBPUSD", "GBPUSD": "GBPUSD", "GBP/USD": "GBPUSD", "CABLE": "GBPUSD",
    "DXY": "DXY", "DOLLAR INDEX": "DXY", "USDX": "DXY",
    "UJ": "USDJPY", "USDJPY": "USDJPY", "USD/JPY": "USDJPY",
    "GJ": "GBPJPY", "GBPJPY": "GBPJPY",
    "EJ": "EURJPY", "EURJPY": "EURJPY",
    "AU": "AUDUSD", "AUDUSD": "AUDUSD",
    "UC": "USDCAD", "USDCAD": "USDCAD",
    "NU": "NZDUSD", "NZDUSD": "NZDUSD",
    "GOLD": "XAUUSD", "XAU": "XAUUSD", "XAUUSD": "XAUUSD",
    "NAS": "NAS100", "NAS100": "NAS100", "US100": "NAS100",
    "SPX": "SPX500", "SP500": "SPX500", "US500": "SPX500",
}
# ICT / SMC vocabulary to gauge the methodology
_TERMS = ["FVG", "OB", "BOS", "CHOCH", "CHoCH", "MSS", "LIQUIDITY", "SWEEP",
          "ORDER BLOCK", "FAIR VALUE", "IMBALANCE", "SUPPLY", "DEMAND",
          "WL", "WH", "PDH", "PDL", "SMT", "PREMIUM", "DISCOUNT", "BUY", "SELL"]


def _pairs_in(text: str) -> list[str]:
    up = f" {text.upper()} "
    found = []
    for k, v in _PAIRS.items():
        # word-ish boundary so "EU" doesn't match inside "EUROPE"
        if re.search(rf"(?<![A-Z]){re.escape(k)}(?![A-Z])", up):
            found.append(v)
    return sorted(set(found))


def _terms_in(text: str) -> list[str]:
    up = text.upper()
    return sorted({t.upper() for t in _TERMS if t.upper() in up})


async def _get_channel(client):
    from telethon.tl.types import PeerChannel
    try:
        return await client.get_entity(PeerChannel(CHANNEL_ID))
    except Exception:
        async for d in client.iter_dialogs():
            if getattr(d.entity, "id", None) == CHANNEL_ID:
                return d.entity
    return None


async def _topics(client, entity):
    """[(id, title)] of forum topics, or [] if the channel isn't a forum."""
    from telethon.tl import functions, types
    try:
        res = await client(functions.channels.GetForumTopicsRequest(
            channel=entity, offset_date=None, offset_id=0, offset_topic=0, limit=100))
        return [(t.id, t.title) for t in res.topics if isinstance(t, types.ForumTopic)]
    except Exception as e:  # noqa: BLE001
        print(f"  (not a forum / cannot list topics: {e})")
        return []


async def _scan_topic(client, entity, topic_id, cutoff):
    kwargs = {"reply_to": topic_id} if topic_id else {}
    count = img = 0
    pair_ct, term_ct = Counter(), Counter()
    by_week = defaultdict(int)
    captions = []
    async for msg in client.iter_messages(entity, limit=PER_TOPIC_LIMIT, **kwargs):
        if msg.date and msg.date < cutoff:
            break
        count += 1
        if msg.photo or (msg.media and not msg.web_preview):
            img += 1
        by_week[msg.date.strftime("%G-W%V")] += 1
        txt = (msg.message or "").strip()
        if txt:
            for p in _pairs_in(txt):
                pair_ct[p] += 1
            for t in _terms_in(txt):
                term_ct[t] += 1
            captions.append((msg.date.strftime("%Y-%m-%d %H:%M"), txt))
    return {"count": count, "img": img, "pairs": pair_ct, "terms": term_ct,
            "weeks": by_week, "captions": captions}


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from telethon import TelegramClient
    cutoff = datetime.now(timezone.utc) - timedelta(days=MONTHS * 30)
    client = TelegramClient("user_session", config.TG_API_ID, config.TG_API_HASH)
    await client.start()
    entity = await _get_channel(client)
    if not entity:
        print(f"Could not find channel {CHANNEL_ID} in your account. Are you a member?")
        await client.disconnect()
        return
    title = getattr(entity, "title", str(CHANNEL_ID))
    print(f"\nCHANNEL: {title}  (id {CHANNEL_ID})")
    topics = await _topics(client, entity)
    if not topics:
        topics = [(None, "(whole channel — no topics)")]
    print(f"Topics found: {len(topics)}\n" + "=" * 55)

    for tid, ttitle in topics:
        data = await _scan_topic(client, entity, tid, cutoff)
        safe = re.sub(r"[^A-Za-z0-9]+", "_", ttitle)[:40] or str(tid)
        # per-topic full caption dump
        with open(os.path.join(OUT_DIR, f"{safe}.txt"), "w") as f:
            f.write(f"# {ttitle} (topic {tid})\n\n")
            for dt, txt in reversed(data["captions"]):
                f.write(f"[{dt}]\n{txt}\n\n{'-'*40}\n")
        wk = data["weeks"]
        per_wk = (sum(wk.values()) / len(wk)) if wk else 0
        print(f"\n▶ TOPIC: {ttitle}  (id {tid})")
        print(f"   posts (last {MONTHS}mo, cap {PER_TOPIC_LIMIT}): {data['count']}"
              f"  · with charts: {data['img']}  · text captions: {len(data['captions'])}")
        print(f"   cadence: ~{per_wk:.1f} posts/week over {len(wk)} active weeks")
        if data["pairs"]:
            top = ", ".join(f"{k}×{v}" for k, v in data["pairs"].most_common(8))
            print(f"   instruments: {top}")
        if data["terms"]:
            tops = ", ".join(f"{k}×{v}" for k, v in data["terms"].most_common(10))
            print(f"   method terms: {tops}")
        if data["captions"]:
            dt, sample = data["captions"][0]
            one = sample.replace("\n", " ")
            print(f"   latest caption [{dt}]: {one[:220]}{'…' if len(one)>220 else ''}")
        print(f"   full captions -> {OUT_DIR}/{safe}.txt")

    await client.disconnect()
    print("\n" + "=" * 55)
    print(f"Done. Full captions saved under ./{OUT_DIR}/ — `cat` any you want to share.")


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
