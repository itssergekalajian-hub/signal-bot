"""AI trade analyst — judge a setup like a conservative discretionary trader.

Instead of mechanical rules, this sends the chart + the analyst's narrative to
Gemini and asks it to act as a disciplined ICT/Smart-Money trader who takes
FEW, high-conviction trades and SKIPS most. It returns a structured verdict
(take / skip, direction, entry, stop, target, confidence, reason). The caller
only acts on high-confidence 'take' verdicts — so the bot is selective and
reasons about context, not just "price touched a box".

Point-in-time safe: the model only sees the chart as it was posted and the
note — never future prices — so it's valid for backtesting too.
"""
from __future__ import annotations

import base64
import json

import config
from fx_narratives import _gemini_call


def judge_setup(image_bytes: bytes, mime: str, instrument: str,
                narrative: str, price, news: str = "") -> dict | None:
    """Ask the AI trader to rate this setup. Returns a verdict dict or None."""
    px = f"{price}" if price is not None else "unknown"
    prompt = (
        "You are a conservative professional forex trader using ICT / Smart-Money "
        f"Concepts, reviewing a {instrument} chart a signal analyst just posted.\n"
        f"Analyst's note: \"{(narrative or '').strip()[:500]}\"\n"
        f"Current price: {px}.\n"
        + (f"Upcoming news: {news}\n" if news else "")
        + (f"Trade in THIS team's style: {config.AI_STYLE}\n" if getattr(config, "AI_STYLE", "") else "")
        + "Judge this ONLY as a disciplined trader who takes FEW, A+ trades and skips "
        "the rest — most setups should be SKIPPED. Take it ONLY if: price is reacting "
        "at a clear Fair Value Gap or Order Block in the direction of the higher-"
        "timeframe bias, there is confluence, the risk:reward to the next logical "
        "target is at least 2:1, and there's no obvious reason to avoid (about to hit "
        "opposing structure, or imminent high-impact news). Prefer precise, tight "
        "stops just beyond the zone/structure.\n"
        "Return ONLY JSON: {\"take\": true|false, \"direction\": \"long\"|\"short\", "
        "\"entry\": <price>, \"stop\": <price>, \"target\": <price>, "
        "\"confidence\": <1-10>, \"reason\": \"<one sentence>\"}. "
        "If you would not take it, set take=false and confidence low. Read all prices "
        "from the chart's right-hand axis."
    )
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode()}},
        ]}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    j = _gemini_call(body, models=[config.AI_MODEL])
    if not j:
        return None
    try:
        v = json.loads(j["candidates"][0]["content"]["parts"][0]["text"])
        out = {"take": bool(v.get("take")),
               "direction": str(v.get("direction", "")).lower(),
               "confidence": float(v.get("confidence", 0) or 0),
               "reason": str(v.get("reason", ""))[:200]}
        if out["take"]:
            out["entry"] = float(v["entry"])
            out["stop"] = float(v["stop"])
            out["target"] = float(v["target"])
        return out
    except Exception:  # noqa: BLE001 — malformed verdict -> treat as no decision
        return None


def replay_ai(candles, direction: str, entry: float, stop: float, target: float):
    """Replay a limit-entry trade on candles. Returns R (reward in risk units),
    -1 on stop, or None if never filled / unresolved. Conservative: stop before
    target within the same candle."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    r_target = abs(target - entry) / risk
    long = direction == "long"
    entered = False
    for (_t, _o, h, l, _c) in candles:
        if not entered:
            if l <= entry <= h:        # price traded through the entry = limit fill
                entered = True
            else:
                continue
        if long:
            if l <= stop:
                return -1.0
            if h >= target:
                return r_target
        else:
            if h >= stop:
                return -1.0
            if l <= target:
                return r_target
    return None
