"""Historical backtest — replay the channel's past narratives on past prices.

Unlike paper_trader (a forward/live test), this reads every past narrative chart,
reads its zones, and replays historical 15-minute candles from the moment each
was posted to see how the trade would have gone under the same rules
(confirmation entry, stop beyond the zone, target in R, break-even). It produces
an R scorecard right away, like the channel's own Backtest topic.

Rules match paper_trader (config.PAPER_*). Conservative within a candle: an
adverse touch is assumed to happen before a favourable one.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

import requests

import config
import fx_narratives as fn


def hist_candles(instrument: str, start_ts: float, end_ts: float, interval: str = "15m"):
    """Yahoo historical OHLC candles [(ts,o,h,l,c)] for the window, or []."""
    for sym in fn._YSYM.get(instrument, []):
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                params={"period1": int(start_ts), "period2": int(end_ts),
                        "interval": interval},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            res = r.json()["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]
            out = []
            for i, t in enumerate(ts):
                h, l = q["high"][i], q["low"][i]
                if h is None or l is None:
                    continue
                o = q["open"][i] if q["open"][i] is not None else q["close"][i]
                c = q["close"][i] if q["close"][i] is not None else q["open"][i]
                out.append((t, float(o), float(h), float(l), float(c)))
            if out:
                return out
        except Exception:  # noqa: BLE001
            continue
    return []


def replay(instrument: str, bias: str, zone: dict, candles):
    """Return (outcome, R) for one setup over candles. outcome in
    win/loss/be/cancelled/open."""
    pip = fn._PIP.get(instrument, 0.0001)
    lo, hi = sorted((float(zone["low"]), float(zone["high"])))
    confirm = config.PAPER_CONFIRM_PIPS * pip
    inval = config.PAPER_INVALIDATE_PIPS * pip
    buf = config.PAPER_SL_BUFFER_PIPS * pip
    bull = bias == "bullish"
    state = "watching"
    entry = sl = tp = None
    be = False
    # Only trade zones that are actual ENTRIES, not targets: a bullish setup buys
    # a demand zone at/below price; a bearish setup sells a supply zone at/above
    # price. A zone on the wrong side is a target the channel wouldn't enter.
    if candles:
        price0 = candles[0][1]
        if bull and price0 < lo:
            return "skip", None
        if (not bull) and price0 > hi:
            return "skip", None
    for (_t, _o, h, l, _c) in candles:
        if state == "watching":
            if l <= hi and h >= lo:            # candle overlapped the zone = tap
                state = "armed"
            else:
                continue
        if state == "armed":
            if bull:
                if l <= lo - inval:            # broke support first (conservative)
                    return "cancelled", 0.0
                if h >= hi + confirm:
                    entry = hi + confirm
                    sl = lo - buf
                    tp = entry + config.PAPER_TP_R * (entry - sl)
                    state = "in_trade"
            else:
                if h >= hi + inval:
                    return "cancelled", 0.0
                if l <= lo - confirm:
                    entry = lo - confirm
                    sl = hi + buf
                    tp = entry - config.PAPER_TP_R * (sl - entry)
                    state = "in_trade"
            if state != "in_trade":
                continue
        if state == "in_trade":
            risk = abs(entry - sl)
            be_trig = config.PAPER_BE_AT_R * risk
            if bull:
                if not be and h >= entry + be_trig:
                    be, sl = True, entry
                if l <= sl:
                    return ("be", 0.0) if (be and sl == entry) else ("loss", -1.0)
                if h >= tp:
                    return "win", config.PAPER_TP_R
            else:
                if not be and l <= entry - be_trig:
                    be, sl = True, entry
                if h >= sl:
                    return ("be", 0.0) if (be and sl == entry) else ("loss", -1.0)
                if l <= tp:
                    return "win", config.PAPER_TP_R
    return "open", None


async def run_backtest(user_client, days: int = 14, max_charts: int = 50) -> str:
    """Read past narrative charts, replay each zone on history, return a report."""
    from telethon.tl.types import PeerChannel
    now = time.time()

    # 1) collect ONE setup per narrative post: the single best (nearest-price,
    #    correct-side) zone. This mirrors the channel's selectivity instead of
    #    firing on every drawn level.
    posts = []  # (inst, bias, post_ts, [ (label, lo, hi) ])
    charts = 0
    async for msg in user_client.iter_messages(
            PeerChannel(config.FX_CHANNEL_ID),
            reply_to=config.FX_NARRATIVES_TOPIC, limit=max_charts * 4):
        if not msg.photo:
            continue
        inst, bias = fn._instrument_of(msg.message or "")
        if inst not in fn._INSTRUMENTS or bias not in ("bullish", "bearish") or inst == "DXY":
            continue
        try:
            blob = await msg.download_media(file=bytes)
            zones = await asyncio.to_thread(fn.read_levels, blob, "image/jpeg", inst)
        except Exception:  # noqa: BLE001
            zones = []
        if not zones:
            continue
        charts += 1
        clean = []
        for z in zones:
            try:
                lo, hi = sorted((float(z["low"]), float(z["high"])))
            except (KeyError, TypeError, ValueError):
                continue
            clean.append((z.get("label", "AOI"), lo, hi))
        if clean:
            posts.append((inst, bias, msg.date.timestamp(), clean))
        if charts >= max_charts:
            break
        await asyncio.sleep(0.8)

    if not posts:
        return "Backtest: couldn't read any past charts (Gemini quota?). Try again."

    # 2) for each post, pick the single best valid zone and replay it; dedup so
    #    the same area of interest isn't traded twice.
    results = []  # (inst, bias, outcome, R)
    seen = set()
    for (inst, bias, post_ts, clean) in posts:
        end_ts = min(now, post_ts + days * 86400)
        candles = await asyncio.to_thread(hist_candles, inst, post_ts, end_ts)
        if not candles:
            continue
        price0 = candles[0][1]
        bull = bias == "bullish"
        # keep only zones that are entries (correct side), then the nearest one
        valid = []
        for (label, lo, hi) in clean:
            if bull and price0 < lo:        # zone above price = target, not entry
                continue
            if (not bull) and price0 > hi:  # zone below price = target
                continue
            dist = 0.0 if lo <= price0 <= hi else min(abs(price0 - lo), abs(price0 - hi))
            valid.append((dist, label, lo, hi))
        if not valid:
            continue
        _, label, lo, hi = min(valid)
        key = (inst, round(lo, 5), round(hi, 5))
        if key in seen:
            continue
        seen.add(key)
        outcome, R = replay(inst, bias, {"low": lo, "high": hi}, candles)
        if outcome in ("win", "loss", "be"):
            pip = fn._PIP.get(inst, 0.0001)
            width = (hi - lo) / pip
            hour = datetime.utcfromtimestamp(post_ts).hour
            results.append((inst, bias, outcome, R, hour, width))

    setups = seen  # for the count in the report
    closed = [r for r in results]
    if not closed:
        return (f"Backtest read {charts} charts / {len(seen)} setups, but none "
                "resolved on history yet (price hasn't reached them, or too recent).")
    wins = [r for r in closed if r[3] > 0]
    losses = [r for r in closed if r[3] < 0]
    be = [r for r in closed if r[3] == 0]
    netR = sum(r[3] for r in closed)
    wr = len(wins) / len(closed) * 100

    # per-instrument breakdown
    per = {}
    for r in closed:
        inst, R = r[0], r[3]
        d = per.setdefault(inst, [0, 0, 0, 0.0])  # wins, losses, be, netR
        if R > 0:
            d[0] += 1
        elif R < 0:
            d[1] += 1
        else:
            d[2] += 1
        d[3] += R

    def _bucket(name_fn):
        b = {}
        for r in closed:
            k = name_fn(r)
            d = b.setdefault(k, [0, 0, 0.0])   # trades, wins, netR
            d[0] += 1
            d[1] += 1 if r[3] > 0 else 0
            d[2] += r[3]
        return b

    def _session(hour):
        if 7 <= hour < 12:
            return "London"
        if 12 <= hour < 21:
            return "NewYork"
        return "Asia/other"

    med_w = sorted(r[5] for r in closed)[len(closed) // 2] if closed else 0
    by_sess = _bucket(lambda r: _session(r[4]))
    by_dir = _bucket(lambda r: r[1])
    by_w = _bucket(lambda r: "tight" if r[5] <= med_w else "wide")

    lines = [f"📊 *Historical backtest* — last {days} days of narratives",
             f"charts read: {charts} · setups: {len(setups)} · resolved: {len(closed)}",
             f"\n✅ wins {len(wins)} · 🛑 losses {len(losses)} · ➖ BE {len(be)}",
             f"win rate: {wr:.0f}%",
             f"*net: {netR:+.1f}R*  (each win = {config.PAPER_TP_R:g}R, loss = −1R)",
             "\n*By instrument*"]
    for inst, (w, l, b, nr) in per.items():
        lines.append(f"  {inst}: {w}W/{l}L/{b}BE · {nr:+.1f}R")

    def _fmt_bucket(title, b):
        out = [f"\n*By {title}*"]
        for k, (n, w, nr) in sorted(b.items(), key=lambda kv: kv[1][2]):
            out.append(f"  {k}: {w}/{n} win · {nr:+.1f}R")
        return out

    lines += _fmt_bucket("session", by_sess)
    lines += _fmt_bucket("direction", by_dir)
    lines += _fmt_bucket(f"zone (≤{med_w:.0f}p=tight)", by_w)
    lines.append("\n💡 _The bucket with the worst R is where the losses cluster — "
                 "a candidate filter to test (not auto-applied)._")
    lines.append("\n_Rules: confirmation entry, stop beyond the zone, "
                 f"{config.PAPER_TP_R:g}R target, BE at +{config.PAPER_BE_AT_R:g}R. "
                 "Conservative within each candle._")
    return "\n".join(lines)
