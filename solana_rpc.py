"""Minimal Solana JSON-RPC calls — just enough to read a token balance.

Jupiter Ultra handles swapping without an RPC, but to sell "50% of what we
hold" the take-profit monitor needs to know the actual on-chain balance. We
read it with getTokenAccountsByOwner (jsonParsed) so we get both the raw
amount and the token's decimals in one call.
"""
from __future__ import annotations

import requests

import config

_TIMEOUT = 12


def token_balance(owner_pubkey: str, mint: str) -> tuple[int, int]:
    """Return (raw_amount, decimals) held by `owner_pubkey` for `mint`.

    Returns (0, 0) if the wallet has no account for that mint yet.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            owner_pubkey,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ],
    }
    r = requests.post(config.SOLANA_RPC, json=payload, timeout=_TIMEOUT)
    r.raise_for_status()
    accounts = (r.json().get("result") or {}).get("value") or []

    total_raw = 0
    decimals = 0
    for acc in accounts:
        info = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
        total_raw += int(info["amount"])
        decimals = int(info["decimals"])
    return total_raw, decimals
