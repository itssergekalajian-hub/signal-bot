"""Current USD price for a mint, via DexScreener (no key needed).

Used both to record an entry price at buy time and to poll the live price in
the take-profit monitor. Returns None on any error so callers can decide what
to do (the monitor simply skips that tick and tries again next round).
"""
from __future__ import annotations

import requests

_TIMEOUT = 8


def price_usd(mint: str) -> float | None:
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=_TIMEOUT
        )
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
    except Exception:  # noqa: BLE001
        return None
    if not pairs:
        return None
    # Deepest-liquidity pair is the reference market.
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    try:
        return float(best.get("priceUsd"))
    except (TypeError, ValueError):
        return None
