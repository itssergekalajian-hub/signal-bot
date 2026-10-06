"""Backtest @bschousesignal (or any configured channel) over recent history.

Answers: how many calls/month, win rate, % losers, average return, whether
buying every call would be net profitable, and the best fixed cash-out multiple.

HOW IT WORKS
------------
1. Pull the channel's message history for the last N months via your logged-in
   Telegram account (reuses user_session — no re-auth).
2. Extract token contract addresses from each message (same extractor the bot
   uses). The EARLIEST message mentioning a token = your entry point.
3. For each token: DexScreener resolves the chain + its main pool; GeckoTerminal
   gives hourly candles from the call time forward. From those we get:
       entry  = price right after the call
       peak   = highest price within WINDOW_DAYS after the call
       last   = most recent price (to see if it died / rugged)
4. Aggregate and simulate selling every call at a fixed multiple.

IMPORTANT, READ THIS
--------------------
* A token with NO DexScreener data left is treated as a RUG / total loss
  (-100%). That is the realistic assumption for a micro-cap that vanished.
* "Peak" is the theoretical best — you cannot actually sell the exact top.
  The fixed-multiple simulation is the realistic, actionable number.
* FRICTION models buy+sell tax + slippage + gas as a flat haircut per trade.
* This is historical; it does NOT predict the future. Memecoin channels are
  extremely high variance.

RUN IT (on the server)
----------------------
    systemctl stop signalbot        # free the Telegram session
    cd ~/signal-bot
    python3 analyze_channel.py      # takes a while (API rate limits)
    systemctl start signalbot       # turn the bot back on

Writes channel_analysis.csv (per-call detail) and prints a summary.
"""
from __future__ import annotations

import asyncio
import csv
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

import config
from extractor import extract_candidates

# ---- knobs ---------------------------------------------------------------
MONTHS = 6                 # how far back to analyse
WINDOW_DAYS = 14           # how long after a call we hunt for the peak
FRICTION = 0.10            # 10% round-trip haircut (tax + slippage + gas)
TARGETS = [1.5, 2, 3, 5, 10]   # cash-out multiples to simulate
GT_SLEEP = 2.3             # seconds between GeckoTerminal calls (free limit ~30/min)
DEX_SLEEP = 0.4
# --------------------------------------------------------------------------

_DEX = "https://api.dexscreener.com/latest/dex/tokens/"
_GT = "https://api.geckoterminal.com/api/v2"
# DexScreener chainId -> GeckoTerminal network id
_NET = {"ethereum": "eth", "bsc": "bsc", "solana": "solana", "arbitrum": "arbitrum",
        "base": "base", "polygon": "polygon_pos", "optimism": "optimism",
        "avalanche": "avax"}


def _get(url, **kw):
    for _ in range(3):
        try:
            r = requests.get(url, timeout=20, **kw)
            if r.status_code == 429:
                time.sleep(5)
                continue
            return r.json()
        except Exception:  # noqa: BLE001
            time.sleep(2)
    return None


def resolve(address: str):
    """(chainId, pool_address, current_price_usd) from DexScreener, or (None,...)."""
    d = _get(_DEX + address)
    time.sleep(DEX_SLEEP)
    pairs = (d or {}).get("pairs") or []
    if not pairs:
        return None, None, None
    # biggest-liquidity pair is the canonical market
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    return best.get("chainId"), best.get("pairAddress"), float(best.get("priceUsd") or 0) or None


def price_path(net: str, pool: str, call_ts: int):
    """(entry, peak, last) USD prices in the WINDOW_DAYS after call_ts, or (None..)."""
    end = call_ts + WINDOW_DAYS * 86400
    hours = WINDOW_DAYS * 24
    d = _get(f"{_GT}/networks/{net}/pools/{pool}/ohlcv/hour",
             params={"aggregate": 1, "before_timestamp": end, "limit": min(hours, 1000),
                     "currency": "usd"})
    time.sleep(GT_SLEEP)
    lst = (((d or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    # each item: [ts, open, high, low, close, volume]; API returns newest-first
    rows = sorted([c for c in lst if c and c[0] >= call_ts], key=lambda c: c[0])
    if not rows:
        return None, None, None
    entry = rows[0][1] or rows[0][4]           # open of first candle after call
    peak = max((c[2] for c in rows if c[2]), default=None)   # highest high
    last = rows[-1][4]                          # close of last candle
    return (float(entry) if entry else None,
            float(peak) if peak else None,
            float(last) if last else None)


async def pull_calls():
    """[(datetime, address)] — earliest mention of each token in the last MONTHS."""
    from telethon import TelegramClient
    cutoff = datetime.now(timezone.utc) - timedelta(days=MONTHS * 30)
    client = TelegramClient("user_session", config.TG_API_ID, config.TG_API_HASH)
    await client.start()
    first_seen: dict[str, datetime] = {}
    total_msgs = 0
    for chan in config.TG_CHANNELS:
        try:
            async for msg in client.iter_messages(chan):
                if msg.date and msg.date < cutoff:
                    break
                total_msgs += 1
                for addr in extract_candidates(msg.message or ""):
                    a = addr.lower()
                    if a not in first_seen or msg.date < first_seen[a]:
                        first_seen[a] = msg.date
        except Exception as e:  # noqa: BLE001
            print(f"  ! could not read {chan}: {e}")
    await client.disconnect()
    print(f"Scanned {total_msgs} messages across {len(config.TG_CHANNELS)} channel(s).")
    return sorted(first_seen.items(), key=lambda kv: kv[1])


def analyse(calls):
    rows = []
    n = len(calls)
    for i, (addr, dt) in enumerate(calls, 1):
        print(f"[{i}/{n}] {addr} … ", end="", flush=True)
        chain, pool, cur = resolve(addr)
        rec = {"address": addr, "date": dt.strftime("%Y-%m-%d"), "month": dt.strftime("%Y-%m"),
               "chain": chain or "", "entry": None, "peak": None, "last": None,
               "mult_peak": None, "status": ""}
        if not chain or chain not in _NET or not pool:
            rec["status"] = "no-data (treated as rug / -100%)"
            rows.append(rec)
            print(rec["status"])
            continue
        entry, peak, last = price_path(_NET[chain], pool, int(dt.timestamp()))
        if not entry:
            rec["status"] = "no-history (treated as rug / -100%)"
            rows.append(rec)
            print(rec["status"])
            continue
        rec.update(entry=entry, peak=peak, last=last,
                   mult_peak=(peak / entry) if peak else None, status="ok")
        rows.append(rec)
        print(f"peak {rec['mult_peak']:.2f}x" if rec['mult_peak'] else "ok")
    return rows


def report(rows):
    n = len(rows)
    if not n:
        print("No calls found. Is TG_CHANNEL set and history reachable?")
        return
    out = []
    P = out.append
    P("\n" + "=" * 60)
    P(f"CHANNEL BACKTEST — {config.TG_CHANNEL_DISPLAY}")
    P(f"Window: last {MONTHS} months · peak measured {WINDOW_DAYS} days after each call")
    P(f"Friction per trade: {FRICTION*100:.0f}%  (tax+slippage+gas)")
    P("=" * 60)

    # monthly cadence
    by_month = defaultdict(int)
    for r in rows:
        by_month[r["month"]] += 1
    P(f"\nTotal unique calls: {n}")
    P(f"Average calls/month: {n / max(len(by_month),1):.1f}")
    P("Calls per month:")
    for m in sorted(by_month):
        P(f"   {m}: {by_month[m]}")

    resolved = [r for r in rows if r["status"] == "ok" and r["mult_peak"]]
    rugs = [r for r in rows if r["status"] != "ok"]
    P(f"\nResolved with price history: {len(resolved)}")
    P(f"No data left (treated as total loss): {len(rugs)}  ({len(rugs)/n*100:.0f}%)")

    def pct_hitting(mult):
        return sum(1 for r in resolved if r["mult_peak"] >= mult)

    P("\n--- How far calls ran (peak within window) ---")
    for t in TARGETS:
        c = pct_hitting(t)
        P(f"   reached ≥{t:g}x : {c}/{n}  ({c/n*100:.0f}% of all calls)")

    # winners / losers by the 2x definition
    winners = pct_hitting(2.0)
    P(f"\nWINNERS (hit ≥2x at some point): {winners}/{n}  ({winners/n*100:.0f}%)")
    P(f"LOSERS  (never hit 2x):          {n-winners}/{n}  ({(n-winners)/n*100:.0f}%)")

    # "all taken at full profit" — sell every call at its own peak (theoretical max)
    peak_mults = [r["mult_peak"] for r in resolved]
    if peak_mults:
        avg_peak = sum(peak_mults) / len(peak_mults)
        med_peak = sorted(peak_mults)[len(peak_mults)//2]
        P("\n--- If you SOLD EVERY CALL AT ITS PEAK (impossible best case) ---")
        P(f"   average peak multiple: {avg_peak:.2f}x · median: {med_peak:.2f}x")
        # net P/L per $1 buying every call, rugs = -100%
        tot = sum((r["mult_peak"] * (1 - FRICTION) - 1) for r in resolved) + \
              sum(-1 for _ in rugs)
        P(f"   net P/L buying $1 into all {n} calls: {tot:+.1f} units "
          f"({tot/n*100:+.0f}% per call on average)")

    # realistic: sell ALL at a fixed multiple T; miss -> exit at last price (often ~0)
    P("\n--- REALISTIC: sell every call at a fixed multiple T ---")
    P("   (if a call never reached T, you exit at its final price; rugs = -100%)")
    best = None
    for t in TARGETS:
        total = 0.0
        for r in rows:
            if r["status"] != "ok" or not r["mult_peak"]:
                total += -1.0                      # rug
            elif r["mult_peak"] >= t:
                total += t * (1 - FRICTION) - 1     # hit target, sold at T
            else:
                exit_m = (r["last"] / r["entry"]) if (r["last"] and r["entry"]) else 0.0
                total += exit_m * (1 - FRICTION) - 1
        per = total / n
        tag = ""
        if best is None or per > best[1]:
            best = (t, per)
        P(f"   cash out at {t:g}x : net {total:+.1f} units over {n} calls "
          f"({per*100:+.0f}% per call){tag}")
    if best:
        P(f"\n   >>> Best fixed cash-out in this window: {best[0]:g}x "
          f"({'PROFIT' if best[1] > 0 else 'still a LOSS'}, {best[1]*100:+.0f}%/call)")

    text = "\n".join(out)
    print(text)
    with open("channel_analysis_summary.txt", "w") as f:
        f.write(text + "\n")
    with open("channel_analysis.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("\nSaved: channel_analysis.csv (per-call) and channel_analysis_summary.txt")


def main():
    if not config.TG_CHANNELS:
        print("TG_CHANNEL is not set in .env — nothing to analyse.")
        sys.exit(1)
    print(f"Pulling {MONTHS} months of history from {config.TG_CHANNEL_DISPLAY} …")
    calls = asyncio.get_event_loop().run_until_complete(pull_calls())
    print(f"Found {len(calls)} unique token calls. Looking up price history "
          f"(~{len(calls)*GT_SLEEP/60:.0f} min at API limits) …\n")
    rows = analyse(calls)
    report(rows)


if __name__ == "__main__":
    main()
