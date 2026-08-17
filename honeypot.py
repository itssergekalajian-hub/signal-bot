"""Sellability check via honeypot.is — it simulates a real buy AND sell.

This is the strongest anti-honeypot signal for DEX-traded (graduated) tokens:
instead of trusting metadata, honeypot.is executes a simulated buy+sell against
the token's live pool and reports whether the sell actually works and the tax.

Covers the EVM chains we trade. On-curve four.meme tokens have no DEX pool yet,
so honeypot.is can't simulate them — those are checked via four.meme's trySell
instead (see fourmeme.sellable). Returns None on any error so the caller decides
(fail-closed under STRICT_SAFETY).
"""
from __future__ import annotations

import requests

import chains

_TIMEOUT = 10


def check(chain: chains.Chain, address: str) -> dict | None:
    """Return {sim_ok, is_honeypot, buy_tax, sell_tax} or None if it couldn't run."""
    if chain.family != "evm" or not chain.evm_chain_id:
        return None
    try:
        r = requests.get(
            "https://api.honeypot.is/v2/IsHoneypot",
            params={"address": address, "chainID": chain.evm_chain_id},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
    except Exception:  # noqa: BLE001
        return None

    hp = (d.get("honeypotResult") or {}).get("isHoneypot")
    sim = d.get("simulationResult") or {}

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    return {
        "sim_ok": bool(d.get("simulationSuccess")),
        "is_honeypot": bool(hp) if hp is not None else None,
        "buy_tax": _f(sim.get("buyTax")),
        "sell_tax": _f(sim.get("sellTax")),
    }
