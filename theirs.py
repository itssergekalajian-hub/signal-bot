"""Extract the channel's ACTUAL trades from their own words.

The analysts narrate what they really do: "Order Placed" (taken), "no
confirmation / didn't take", "failed / invalidated" (dead setup), "Reached ✅"
(hit target), "SL" (stopped), "BE" (break-even). This reads the Narratives +
Backtest topics and uses the LLM to turn that commentary into a structured log
of taken vs skipped trades and their outcomes — the ground truth for mirroring
them and for judging whether copying them is actually profitable.
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import config
from fx_narratives import _gemini_call

FX_BACKTEST_TOPIC = int(getattr(config, "FX_BACKTEST_TOPIC", 7) or 7)


async def _collect(user_client, days: int) -> str:
    from telethon.tl.types import PeerChannel
    cutoff = time.time() - days * 86400
    rows = []
    for topic in (config.FX_NARRATIVES_TOPIC, FX_BACKTEST_TOPIC):
        try:
            async for m in user_client.iter_messages(
                    PeerChannel(config.FX_CHANNEL_ID), reply_to=topic, limit=800):
                if m.date and m.date.timestamp() < cutoff:
                    break
                txt = (m.message or "").strip()
                if txt:
                    tag = "NARR" if topic == config.FX_NARRATIVES_TOPIC else "REPORT"
                    rows.append((m.date.timestamp(),
                                 f"[{datetime.utcfromtimestamp(m.date.timestamp()):%m-%d %H:%M} {tag}] {txt}"))
        except Exception:  # noqa: BLE001
            continue
    rows.sort()
    return "\n".join(r[1] for r in rows)[:16000]


async def analyze(user_client, days: int = 30) -> str:
    """Read the channel's commentary and report their real taken/skip/outcome log."""
    text = await _collect(user_client, days)
    if not text:
        return "Couldn't read the channel's messages."
    prompt = (
        "Below are a forex signal team's chronological notes (NARR = analysis, "
        "REPORT = their weekly results). Extract what they ACTUALLY DID — do not "
        "invent trades.\n"
        "- A trade is TAKEN when they say things like 'Order Placed', 'entered', "
        "'executed', 'in the trade', or report a result for it.\n"
        "- A setup is SKIPPED/NOT TAKEN when they say 'no confirmation', 'didn't "
        "take', 'not convinced', 'missed', or wait and it never triggers.\n"
        "- A setup is INVALIDATED when they say 'failed', 'invalidated', 'narrative "
        "invalid'.\n"
        "- Outcome of a taken trade: TP (hit target / 'Reached ✅' / +R), SL "
        "(stopped / -1R), BE (break-even / 0R), or OPEN.\n"
        "Use the REPORT lines (weekly totals in R) as the source of truth for "
        "outcomes when present. Return ONLY JSON:\n"
        "{\"taken\":[{\"instrument\":\"GBPUSD\",\"direction\":\"short\",\"outcome\":"
        "\"TP\",\"R\":3.2}], \"skipped\":[{\"instrument\":\"EURUSD\",\"reason\":\"no "
        "confirmation\"}], \"invalidated\":[{\"instrument\":\"...\"}]}.\n\n" + text
    )
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "response_mime_type": "application/json"}}
    j = _gemini_call(body, models=[config.AI_MODEL])
    if not j:
        return "The AI model is rate-limited right now — try again in a bit."
    try:
        d = json.loads(j["candidates"][0]["content"]["parts"][0]["text"])
    except Exception:  # noqa: BLE001
        return "Couldn't parse the trade log from the channel."

    taken = d.get("taken") or []
    skipped = d.get("skipped") or []
    invalid = d.get("invalidated") or []
    tp = [t for t in taken if str(t.get("outcome", "")).upper() == "TP"]
    sl = [t for t in taken if str(t.get("outcome", "")).upper() == "SL"]
    beq = [t for t in taken if str(t.get("outcome", "")).upper() == "BE"]
    netR = sum(float(t.get("R", 0) or 0) for t in taken)
    resolved = len(tp) + len(sl) + len(beq)
    wr = (len(tp) / resolved * 100) if resolved else 0

    lines = [f"📒 *Their actual trades* — last {days}d (from their own words)",
             f"\nTaken: *{len(taken)}*  ·  Skipped: {len(skipped)}  ·  Invalidated: {len(invalid)}",
             f"Of taken → ✅ TP {len(tp)} · 🛑 SL {len(sl)} · ➖ BE {len(beq)}",
             f"Win rate (resolved): {wr:.0f}%",
             f"*Net (as they report): {netR:+.1f}R*"]
    if taken:
        lines.append("\n*Taken trades:*")
        for t in taken[:20]:
            r = t.get("R")
            rtxt = f" {float(r):+.1f}R" if r not in (None, "") else ""
            lines.append(f"  {t.get('instrument','?')} {t.get('direction','')} → "
                         f"{t.get('outcome','?')}{rtxt}")
    lines.append("\n_This is their SELF-REPORTED record. Next step: verify each on "
                 "price history to confirm it's real before copying._")
    return "\n".join(lines)
