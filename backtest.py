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

    # 1) collect setups from the channel history, deduped by zone (earliest wins)
    setups = {}  # (inst, round(lo,5), round(hi,5)) -> (inst, bias, label, post_ts, lo, hi)
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
        post_ts = msg.date.timestamp()
        for z in zones:
            try:
                lo, hi = sorted((float(z["low"]), float(z["high"])))
            except (KeyError, TypeError, ValueError):
                continue
            key = (inst, round(lo, 5), round(hi, 5))
            prev = setups.get(key)
            if prev is None or post_ts < prev[3]:   # keep the earliest sighting
                setups[key] = (inst, bias, z.get("label", "AOI"), post_ts, lo, hi)
        if charts >= max_charts:
            break
        await asyncio.sleep(0.8)

    if not setups:
        return "Backtest: couldn't read any past charts (Gemini quota?). Try again."

    # 2) replay each setup on historical candles
    results = []  # (inst, bias, outcome, R)
    for (inst, bias, label, post_ts, lo, hi) in setups.values():
        end_ts = min(now, post_ts + days * 86400)
        candles = await asyncio.to_thread(hist_candles, inst, post_ts, end_ts)
        if not candles:
            continue
        outcome, R = replay(inst, bias, {"low": lo, "high": hi}, candles)
        if outcome in ("win", "loss", "be"):
            results.append((inst, bias, outcome, R))

    closed = [r for r in results]
    if not closed:
        return (f"Backtest read {charts} charts / {len(setups)} setups, but none "
                "resolved on history yet (price hasn't reached them, or too recent).")
    wins = [r for r in closed if r[3] > 0]
    losses = [r for r in closed if r[3] < 0]
    be = [r for r in closed if r[3] == 0]
    netR = sum(r[3] for r in closed)
    wr = len(wins) / len(closed) * 100

    # per-instrument breakdown
    per = {}
    for inst, bias, outcome, R in closed:
        d = per.setdefault(inst, [0, 0, 0, 0.0])  # wins, losses, be, netR
        if R > 0:
            d[0] += 1
        elif R < 0:
            d[1] += 1
        else:
            d[2] += 1
        d[3] += R

    lines = [f"📊 *Historical backtest* — last {days} days of narratives",
             f"charts read: {charts} · setups: {len(setups)} · resolved: {len(closed)}",
             f"\n✅ wins {len(wins)} · 🛑 losses {len(losses)} · ➖ BE {len(be)}",
             f"win rate: {wr:.0f}%",
             f"*net: {netR:+.1f}R*  (each win = {config.PAPER_TP_R:g}R, loss = −1R)",
             "\n*By instrument*"]
    for inst, (w, l, b, nr) in per.items():
        lines.append(f"  {inst}: {w}W/{l}L/{b}BE · {nr:+.1f}R")
    lines.append("\n_Rules: confirmation entry, stop beyond the zone, "
                 f"{config.PAPER_TP_R:g}R target, BE at +{config.PAPER_BE_AT_R:g}R. "
                 "Conservative within each candle._")
    return "\n".join(lines)
