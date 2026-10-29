"""Paper-trading simulator — tests the channel's method with no money.

A zone with a known bias (📈/📉) becomes a *setup*. The engine then models how
the channel trades it:

  watching  -> price reaches the zone (a tap)                -> armed
  armed     -> price reacts back OUT of the zone (confirm)   -> enter the trade
            -> or blows through the zone (invalidate)         -> cancelled
  in_trade  -> hits stop (-1R) / reaches +BE then returns (0R) / hits TP (+R)

Entry is at the confirmation price, stop just beyond the far edge of the zone,
target PAPER_TP_R in R. Everything is logged in R, like the channel's own
backtest, so results compare directly. No broker, no funds — pure simulation on
live Yahoo prices.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time

import config
from fx_narratives import _PIP, _bias_emoji, _fmt, price, deliver


def _load() -> list[dict]:
    try:
        with open(config.PAPER_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _save(rows: list[dict]) -> None:
    tmp = config.PAPER_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f, indent=2)
    os.replace(tmp, config.PAPER_FILE)


def add_setups(instrument: str, bias: str, zones: list[dict]) -> int:
    """Turn freshly locked-in zones (with a direction) into paper setups."""
    if not config.PAPER_ENABLED or bias not in ("bullish", "bearish"):
        return 0
    cur = _load()
    live = {(s["instrument"], round(s["low"], 5), round(s["high"], 5))
            for s in cur if s["state"] in ("watching", "armed", "in_trade")}
    n = 0
    for z in zones:
        try:
            lo, hi = sorted((float(z["low"]), float(z["high"])))
        except (KeyError, TypeError, ValueError):
            continue
        if (instrument, round(lo, 5), round(hi, 5)) in live:
            continue  # already tracking this zone
        cur.append({"id": secrets.token_hex(3), "instrument": instrument, "bias": bias,
                    "label": str(z.get("label", "AOI")), "low": lo, "high": hi,
                    "state": "watching", "entry": None, "sl": None, "tp": None,
                    "be_moved": False, "result_R": None,
                    "created": time.time(), "opened_at": None, "closed_at": None})
        n += 1
    if n:
        _save(cur)
    return n


def _pip(ins: str) -> float:
    return _PIP.get(ins, 0.0001)


def _fmtp(ins: str, v) -> str:
    return _fmt(ins, v) if v is not None else "—"


def stats_message() -> str:
    rows = _load()
    closed = [s for s in rows if s["state"] == "closed"]
    opens = [s for s in rows if s["state"] in ("watching", "armed", "in_trade")]
    wins = [s for s in closed if (s["result_R"] or 0) > 0]
    losses = [s for s in closed if (s["result_R"] or 0) < 0]
    be = [s for s in closed if (s["result_R"] or 0) == 0]
    netR = sum(s["result_R"] or 0 for s in closed)
    wr = (len(wins) / len(closed) * 100) if closed else 0
    lines = ["📊 *Paper-trading scorecard*",
             f"\nClosed trades: {len(closed)}",
             f"  ✅ wins: {len(wins)}   🛑 losses: {len(losses)}   ➖ BE: {len(be)}",
             f"  win rate: {wr:.0f}%",
             f"  *net: {netR:+.1f}R*",
             f"\nOpen now: {len(opens)} "
             f"(in trade: {sum(1 for s in opens if s['state']=='in_trade')}, "
             f"waiting: {sum(1 for s in opens if s['state']!='in_trade')})"]
    return "\n".join(lines)


def trades_message() -> str:
    rows = _load()
    live = [s for s in rows if s["state"] in ("armed", "in_trade")]
    recent = [s for s in rows if s["state"] == "closed"][-8:]
    lines = ["📝 *Paper trades*"]
    if live:
        lines.append("\n*Active*")
        for s in live:
            if s["state"] == "in_trade":
                lines.append(f"  {_bias_emoji(s['bias'])} {s['instrument']} {s['bias'][:4]} "
                             f"@ `{_fmtp(s['instrument'], s['entry'])}` · SL `{_fmtp(s['instrument'], s['sl'])}`"
                             f" · TP `{_fmtp(s['instrument'], s['tp'])}`")
            else:
                lines.append(f"  ⏳ {s['instrument']} {s['label']} armed — waiting for confirmation")
    if recent:
        lines.append("\n*Recent results*")
        for s in recent:
            r = s["result_R"] or 0
            tag = "✅" if r > 0 else ("🛑" if r < 0 else "➖")
            lines.append(f"  {tag} {s['instrument']} {s['bias'][:4]}  {r:+.1f}R")
    if not live and not recent:
        lines.append("\n_No trades yet — setups appear as biased zones lock in._")
    return "\n".join(lines)


def clear() -> None:
    _save([])


async def _close(bot_client, notify, s, result_R, price_now):
    s["state"] = "closed"
    s["result_R"] = result_R
    s["closed_at"] = time.time()
    ins = s["instrument"]
    tag = "✅ *WIN*" if result_R > 0 else ("🛑 *LOSS*" if result_R < 0 else "➖ *break-even*")
    await deliver(bot_client, notify,
                  f"{tag} — paper {ins} {s['bias']}\n"
                  f"📍 {s['label']} · exit `{_fmtp(ins, price_now)}`\n"
                  f"*Result: {result_R:+.1f}R*")


async def _open(bot_client, notify, s, entry, sl, tp):
    s.update(state="in_trade", entry=entry, sl=sl, tp=tp, opened_at=time.time())
    ins = s["instrument"]
    await deliver(bot_client, notify,
                  f"📝 *PAPER entry* — {_bias_emoji(s['bias'])} {ins} {s['bias']}\n"
                  f"📍 {s['label']}\n"
                  f"🎯 Entry `{_fmtp(ins, entry)}` · SL `{_fmtp(ins, sl)}` · "
                  f"TP `{_fmtp(ins, tp)}` ({config.PAPER_TP_R:g}R)")


async def run(bot_client, notify):
    while True:
        try:
            rows = _load()
            active = [s for s in rows if s["state"] in ("watching", "armed", "in_trade")]
            if active:
                prices = {}
                for ins in {s["instrument"] for s in active}:
                    prices[ins] = await asyncio.to_thread(price, ins)
                changed = False
                now = time.time()
                for s in active:
                    p = prices.get(s["instrument"])
                    if not p:
                        continue
                    ins = s["instrument"]
                    pip = _pip(ins)
                    lo, hi, bull = s["low"], s["high"], s["bias"] == "bullish"
                    confirm = config.PAPER_CONFIRM_PIPS * pip
                    inval = config.PAPER_INVALIDATE_PIPS * pip
                    buf = config.PAPER_SL_BUFFER_PIPS * pip

                    # expire un-triggered setups
                    if s["state"] in ("watching", "armed") and \
                            now - s["created"] > config.PAPER_EXPIRE_HOURS * 3600:
                        s["state"] = "expired"
                        changed = True
                        continue

                    if s["state"] == "watching":
                        if lo <= p <= hi:
                            s["state"] = "armed"
                            changed = True
                    elif s["state"] == "armed":
                        if bull and p >= hi + confirm:               # bounced up = confirm long
                            entry = p
                            sl = lo - buf
                            tp = entry + config.PAPER_TP_R * (entry - sl)
                            await _open(bot_client, notify, s, entry, sl, tp)
                            changed = True
                        elif (not bull) and p <= lo - confirm:        # rejected down = confirm short
                            entry = p
                            sl = hi + buf
                            tp = entry - config.PAPER_TP_R * (sl - entry)
                            await _open(bot_client, notify, s, entry, sl, tp)
                            changed = True
                        elif bull and p <= lo - inval:                # broke support = dead
                            s["state"] = "cancelled"
                            changed = True
                        elif (not bull) and p >= hi + inval:          # broke resistance = dead
                            s["state"] = "cancelled"
                            changed = True
                    elif s["state"] == "in_trade":
                        entry, sl, tp = s["entry"], s["sl"], s["tp"]
                        risk = abs(entry - sl)
                        be_trigger = config.PAPER_BE_AT_R * risk
                        if bull:
                            if not s["be_moved"] and p >= entry + be_trigger:
                                s["sl"] = entry
                                s["be_moved"] = True
                                changed = True
                            if p <= s["sl"]:
                                await _close(bot_client, notify, s,
                                             0.0 if s["be_moved"] and s["sl"] == entry else -1.0, p)
                                changed = True
                            elif p >= tp:
                                await _close(bot_client, notify, s, config.PAPER_TP_R, p)
                                changed = True
                        else:
                            if not s["be_moved"] and p <= entry - be_trigger:
                                s["sl"] = entry
                                s["be_moved"] = True
                                changed = True
                            if p >= s["sl"]:
                                await _close(bot_client, notify, s,
                                             0.0 if s["be_moved"] and s["sl"] == entry else -1.0, p)
                                changed = True
                            elif p <= tp:
                                await _close(bot_client, notify, s, config.PAPER_TP_R, p)
                                changed = True
                if changed:
                    _save(rows)
        except Exception as e:  # noqa: BLE001 — never let the loop die
            try:
                await notify(f"paper trade error: {e}")
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(config.PAPER_POLL_SEC)
