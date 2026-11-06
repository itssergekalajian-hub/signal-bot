"""Copy-trader — mirror the trades the channel ACTUALLY takes, forward.

The analysts announce entries in words ("Order Placed", "entered", "executed",
"running"...). When such a message arrives for an instrument with a known bias,
we log a trade at the current price in their direction and track its real
outcome on live price — so paper results reflect *what they actually took*,
measured independently (not their self-report). Stops/targets use a default
structure since they don't post exact numbers.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time

import config
from fx_narratives import _PIP, _bias_emoji, _fmt, price, deliver

_TAKEN = ("ORDER PLACED", "ORDER IS PLACED", "ORDERS PLACED", "ENTERED", "EXECUTED",
          "POSITION OPEN", "IN THE TRADE", "RUNNING", "BOOKED", "SECURED PARTIAL",
          "TP1", "SL TO BE", "MOVED TO BE", "TAKING THIS", "TOOK THIS", "IN PROFIT")


def has_taken_signal(text: str) -> bool:
    u = (text or "").upper()
    return any(k in u for k in _TAKEN)


def _load() -> list[dict]:
    try:
        with open(config.COPY_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _save(rows: list[dict]) -> None:
    tmp = config.COPY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f, indent=2)
    os.replace(tmp, config.COPY_FILE)


async def open_from_signal(bot_client, notify, instrument: str, bias: str) -> bool:
    """Log a copy-trade in their direction at current price. True if opened."""
    if not config.COPY_ENABLED or instrument == "DXY" or bias not in ("bullish", "bearish"):
        return False
    p = await asyncio.to_thread(price, instrument)
    if not p:
        return False
    rows = _load()
    # don't double-log the same instrument+direction while one is already open
    for s in rows:
        if s["state"] == "open" and s["instrument"] == instrument and s["bias"] == bias:
            return False
    pip = _PIP.get(instrument, 0.0001)
    stop_d = config.COPY_STOP_PIPS * pip
    long = bias == "bullish"
    entry = p
    stop = entry - stop_d if long else entry + stop_d
    target = entry + config.COPY_TP_R * stop_d if long else entry - config.COPY_TP_R * stop_d
    rows.append({"id": secrets.token_hex(3), "instrument": instrument, "bias": bias,
                 "entry": entry, "stop": stop, "target": target, "state": "open",
                 "result_R": None, "opened_at": time.time(), "closed_at": None})
    _save(rows)
    await deliver(bot_client, notify,
                  f"📋 *COPY entry* — {_bias_emoji(bias)} {instrument} "
                  f"{'LONG' if long else 'SHORT'} (they posted an entry)\n"
                  f"🎯 Entry `{_fmt(instrument, entry)}` · SL `{_fmt(instrument, stop)}` · "
                  f"TP `{_fmt(instrument, target)}` ({config.COPY_TP_R:g}R)",
                  target=config.PAPER_ALERT_TARGET)
    return True


def stats_message() -> str:
    rows = _load()
    closed = [s for s in rows if s["state"] == "closed"]
    opens = [s for s in rows if s["state"] == "open"]
    wins = [s for s in closed if (s["result_R"] or 0) > 0]
    netR = sum(s["result_R"] or 0 for s in closed)
    wr = (len(wins) / len(closed) * 100) if closed else 0
    return ("📋 *Copy-trade scorecard* (mirroring their entries)\n"
            f"\nClosed: {len(closed)} · open: {len(opens)}\n"
            f"✅ wins {len(wins)} · 🛑 losses {len(closed) - len(wins)} · win rate {wr:.0f}%\n"
            f"*net: {netR:+.1f}R*")


def trades_message() -> str:
    rows = _load()
    opens = [s for s in rows if s["state"] == "open"]
    recent = [s for s in rows if s["state"] == "closed"][-8:]
    lines = ["📋 *Copy trades*"]
    for s in opens:
        lines.append(f"  ⏳ {_bias_emoji(s['bias'])} {s['instrument']} @ "
                     f"`{_fmt(s['instrument'], s['entry'])}`")
    for s in recent:
        r = s["result_R"] or 0
        lines.append(f"  {'✅' if r > 0 else '🛑'} {s['instrument']} {r:+.1f}R")
    if len(lines) == 1:
        lines.append("\n_No copy-trades yet — logged when they post an entry._")
    return "\n".join(lines)


def clear() -> None:
    _save([])


async def run(bot_client, notify):
    while True:
        try:
            rows = _load()
            opens = [s for s in rows if s["state"] == "open"]
            if opens:
                prices = {i: await asyncio.to_thread(price, i)
                          for i in {s["instrument"] for s in opens}}
                changed = False
                for s in opens:
                    p = prices.get(s["instrument"])
                    if not p:
                        continue
                    long = s["bias"] == "bullish"
                    hit = None
                    if long:
                        if p <= s["stop"]:
                            hit = -1.0
                        elif p >= s["target"]:
                            hit = config.COPY_TP_R
                    else:
                        if p >= s["stop"]:
                            hit = -1.0
                        elif p <= s["target"]:
                            hit = config.COPY_TP_R
                    if hit is not None:
                        s["state"] = "closed"
                        s["result_R"] = hit
                        s["closed_at"] = time.time()
                        changed = True
                        tag = "✅ *WIN*" if hit > 0 else "🛑 *LOSS*"
                        await deliver(bot_client, notify,
                                      f"{tag} — copy {s['instrument']} {s['bias']} "
                                      f"exit `{_fmt(s['instrument'], p)}` · *{hit:+.1f}R*",
                                      target=config.PAPER_ALERT_TARGET)
                if changed:
                    _save(rows)
        except Exception as e:  # noqa: BLE001
            try:
                await notify(f"copy-trade error: {e}")
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(config.COPY_POLL_SEC)
