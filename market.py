"""Universal, chain-agnostic market data via DexScreener.

DexScreener indexes every chain we care about (Solana + all the EVM chains),
so a single lookup does double duty:
  * resolves which chain an address actually trades on (we do NOT trust the
    chain slug in a gmgn/dexscreener link — "robinhood" isn't a chain), and
  * returns price / liquidity / age from that chain's deepest pool.

Everything downstream (safety gate, entry price, take-profit monitor) keys off
the chain this returns, which is what makes the bot work on any chain.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

_TIMEOUT = 8


@dataclass
class Market:
    address: str
    chain: str            # DexScreener chainId, e.g. "bsc", "solana", "ethereum"
    symbol: str | None
    price_usd: float | None
    liquidity_usd: float
    age_min: float | None


def _deepest(pairs: list[dict]) -> dict | None:
    pairs = [p for p in pairs if p.get("chainId")]
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def lookup(address: str) -> Market | None:
    """Resolve an address to its best market across all chains, or None."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
    except Exception:  # noqa: BLE001
        return None

    pair = _deepest(pairs)
    if not pair:
        return None

    price = None
    try:
        price = float(pair.get("priceUsd"))
    except (TypeError, ValueError):
        pass

    age_min = None
    created = pair.get("pairCreatedAt")
    if created:
        age_min = (time.time() - created / 1000) / 60

    return Market(
        address=address,
        chain=(pair.get("chainId") or "").lower(),
        symbol=(pair.get("baseToken") or {}).get("symbol"),
        price_usd=price,
        liquidity_usd=(pair.get("liquidity") or {}).get("usd") or 0,
        age_min=age_min,
    )


def price_usd(address: str) -> float | None:
    """Just the current USD price — used by the take-profit monitor."""
    m = lookup(address)
    return m.price_usd if m else None
