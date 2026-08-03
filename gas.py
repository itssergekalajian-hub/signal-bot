"""Estimate the gas cost of a swap in USD, and gate buys on it.

Kept deliberately light: it uses a plain JSON-RPC `eth_gasPrice` call (no web3,
no wallet) so the guard also works in DRY_RUN — you'll see gas-based skips while
simulating, before any key is involved. Solana fees are negligible, so they're
treated as a tiny flat cost.

Buys are gated; sells are not — we never want a fee cap to trap us in a position.
"""
from __future__ import annotations

import requests

import chains
import config
import market

_TIMEOUT = 8


def estimate_gas_usd(chain: chains.Chain) -> float | None:
    """Rough USD cost of one swap on `chain`, or None if it can't be priced."""
    if chain.family == "solana":
        return 0.01  # Solana priority fees are fractions of a cent

    native_price = market.price_usd(chain.wrapped_native)
    if not native_price:
        return None
    try:
        r = requests.post(
            chains.rpc_url(chain),
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        gas_price_wei = int(r.json()["result"], 16)
    except Exception:  # noqa: BLE001
        return None
    eth_cost = gas_price_wei * config.SWAP_GAS_UNITS / 1e18
    return eth_cost * native_price


def buy_budget_usd() -> float:
    """Max gas we'll tolerate for a buy, from the % and optional hard cap."""
    budget = config.BUY_AMOUNT_USD * (config.MAX_GAS_PCT / 100.0)
    if config.MAX_GAS_USD > 0:
        budget = min(budget, config.MAX_GAS_USD)
    return budget


def check_buy(chain: chains.Chain) -> tuple[bool, str]:
    """(ok, reason). ok=False means skip this buy — gas too high vs the size."""
    gas_usd = estimate_gas_usd(chain)
    if gas_usd is None:
        # Can't estimate. Don't block on cheap chains; do block on ETH mainnet
        # where gas is the whole risk.
        if chain.key == "ethereum":
            return False, "can't estimate ETH gas — skipping to be safe"
        return True, "gas estimate unavailable (proceeding on low-fee chain)"
    budget = buy_budget_usd()
    if gas_usd > budget:
        return False, f"gas ~${gas_usd:.2f} > ${budget:.2f} budget ({config.MAX_GAS_PCT:g}% of ${config.BUY_AMOUNT_USD:g})"
    return True, f"gas ~${gas_usd:.2f}"
