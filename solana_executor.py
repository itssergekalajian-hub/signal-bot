"""Solana execution adapter — swaps via Jupiter Ultra, balances via RPC.

Ultra is RPC-less for swapping: GET an /order (unsigned base64 tx + requestId),
sign locally, POST to /execute. Balance reads use a plain JSON-RPC call so the
take-profit monitor knows how much we actually hold.

All functions here assume real trading; DRY_RUN is handled by the dispatcher.
"""
from __future__ import annotations

import base64

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

import config
from swap_result import SwapResult

_TIMEOUT = 20
_WSOL = "So11111111111111111111111111111111111111112"
_LAMPORTS = 1_000_000_000


def _keypair() -> Keypair:
    return Keypair.from_base58_string(config.SOLANA_PRIVATE_KEY)


def wallet_address() -> str | None:
    if not config.SOLANA_PRIVATE_KEY:
        return None
    return str(_keypair().pubkey())


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if config.JUPITER_API_KEY:
        h["x-api-key"] = config.JUPITER_API_KEY
    return h


def _swap(input_mint: str, output_mint: str, raw_amount: int, slippage_bps: int) -> SwapResult:
    if not config.SOLANA_PRIVATE_KEY:
        return SwapResult(False, "no SOLANA_PRIVATE_KEY configured")
    kp = _keypair()
    taker = str(kp.pubkey())

    try:
        order = requests.get(
            f"{config.JUPITER_BASE}/ultra/v1/order",
            params={"inputMint": input_mint, "outputMint": output_mint,
                    "amount": raw_amount, "taker": taker, "slippageBps": slippage_bps},
            headers=_headers(), timeout=_TIMEOUT,
        ).json()
    except Exception as e:  # noqa: BLE001
        return SwapResult(False, f"order request failed: {e}")

    tx_b64 = order.get("transaction")
    request_id = order.get("requestId")
    if not tx_b64 or not request_id:
        return SwapResult(False, f"no routable order: {order.get('error') or order}")
    out_amount = order.get("outAmount")

    try:
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(unsigned.message, [kp])
        signed_b64 = base64.b64encode(bytes(signed)).decode()
    except Exception as e:  # noqa: BLE001
        return SwapResult(False, f"signing failed: {e}")

    try:
        res = requests.post(
            f"{config.JUPITER_BASE}/ultra/v1/execute",
            json={"signedTransaction": signed_b64, "requestId": request_id},
            headers=_headers(), timeout=_TIMEOUT,
        ).json()
    except Exception as e:  # noqa: BLE001
        return SwapResult(False, f"execute request failed: {e}")

    sig = res.get("signature")
    if str(res.get("status", "")).lower() == "success" or res.get("code") == 0:
        return SwapResult(True, f"filled — https://solscan.io/tx/{sig}", sig,
                          out_amount=int(out_amount) if out_amount else None)
    return SwapResult(False, f"execute returned: {res}", sig)


def buy(address: str, amount_native: float) -> SwapResult:
    return _swap(_WSOL, address, int(amount_native * _LAMPORTS), config.SLIPPAGE_BPS)


def sell(address: str, raw_amount: int) -> SwapResult:
    return _swap(address, _WSOL, raw_amount, config.SELL_SLIPPAGE_BPS)


def token_balance(address: str) -> tuple[int, int]:
    """(raw_amount, decimals) held for `address`; (0, 0) if none."""
    owner = wallet_address()
    if not owner:
        return 0, 0
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
               "params": [owner, {"mint": address}, {"encoding": "jsonParsed"}]}
    r = requests.post(config.SOLANA_RPC, json=payload, timeout=12)
    r.raise_for_status()
    accounts = (r.json().get("result") or {}).get("value") or []
    total, decimals = 0, 0
    for acc in accounts:
        amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
        total += int(amt["amount"])
        decimals = int(amt["decimals"])
    return total, decimals
