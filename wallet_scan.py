"""Discover the tokens actually sitting in the wallet (on-chain truth).

A plain RPC node can't list "every token a wallet holds", so we use an explorer
(Etherscan V2, one free key for all EVM chains) to find every token the wallet
has ever received, then read the live balanceOf each and keep the ones with a
non-zero balance. This is what lets you sell tokens the bot bought but is no
longer tracking (e.g. after a ledger reset or a failed-but-reported sale).
"""
from __future__ import annotations

import requests

import chains
import config
import evm_executor

_TIMEOUT = 20


def _token_transfers(chain: chains.Chain, owner: str, key: str) -> list:
    """Fetch the wallet's ERC-20 transfer history. Tries the unified Etherscan
    V2 endpoint first, then the classic BscScan endpoint — so a key from either
    bscscan.com or etherscan.io works."""
    base_params = {"module": "account", "action": "tokentx", "address": owner,
                   "page": 1, "offset": 2000, "sort": "desc", "apikey": key}
    endpoints = [
        ("https://api.etherscan.io/v2/api", {"chainid": chain.evm_chain_id}),
        ("https://api.bscscan.com/api", {}),
    ]
    for base, extra in endpoints:
        try:
            r = requests.get(base, params={**base_params, **extra}, timeout=_TIMEOUT)
            r.raise_for_status()
            result = r.json().get("result")
            if isinstance(result, list) and result:
                return result
        except Exception:  # noqa: BLE001
            continue
    return []


def held_tokens(chain: chains.Chain, owner: str) -> list[dict]:
    """Return [{address, symbol, raw, decimals}] the wallet currently holds."""
    if not config.BSCSCAN_API_KEY:
        raise RuntimeError("BSCSCAN_API_KEY not set")

    txs = _token_transfers(chain, owner, config.BSCSCAN_API_KEY)

    # Unique token contracts the wallet has touched (most recent first).
    seen: dict[str, dict] = {}
    for t in txs:
        ca = (t.get("contractAddress") or "").lower()
        if ca and ca not in seen:
            seen[ca] = {"symbol": t.get("tokenSymbol") or ca[:8],
                        "decimals": int(t.get("tokenDecimal") or 18)}

    held: list[dict] = []
    for ca, meta in seen.items():
        try:
            raw, dec = evm_executor.token_balance(chain, ca)
        except Exception:  # noqa: BLE001
            continue
        if raw > 0:
            held.append({"address": ca, "symbol": meta["symbol"],
                         "raw": raw, "decimals": dec or meta["decimals"]})
    return held
