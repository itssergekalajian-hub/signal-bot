"""Execute swaps through Jupiter's Ultra API (buy and sell).

Ultra is RPC-less for swapping: you GET an /order (which returns an unsigned
base64 transaction + requestId), sign it locally with your keypair, then POST
it to /execute and Jupiter handles priority fees, slippage, and landing the tx.

Flow (buy = SOL -> token, sell = token -> SOL):
  GET  {base}/ultra/v1/order?inputMint=..&outputMint=..&amount=<raw>&taker=<pubkey>
  ->   sign transaction locally
  POST {base}/ultra/v1/execute  { signedTransaction, requestId }
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

import config

_TIMEOUT = 20


@dataclass
class SwapResult:
    ok: bool
    detail: str
    signature: str | None = None
    in_amount: int | None = None    # raw base units sent
    out_amount: int | None = None   # raw base units received (from the order quote)


# Kept as an alias so older imports of BuyResult still work.
BuyResult = SwapResult


def _keypair() -> Keypair:
    return Keypair.from_base58_string(config.SOLANA_PRIVATE_KEY)


def wallet_pubkey() -> str | None:
    """Public address of the configured wallet, or None if no key is set."""
    if not config.SOLANA_PRIVATE_KEY:
        return None
    return str(_keypair().pubkey())


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if config.JUPITER_API_KEY:
        h["x-api-key"] = config.JUPITER_API_KEY
    return h


def _swap(input_mint: str, output_mint: str, raw_amount: int, slippage_bps: int) -> SwapResult:
    """Core SOL<->token swap. `raw_amount` is in the input token's base units."""
    if not config.SOLANA_PRIVATE_KEY:
        return SwapResult(False, "no SOLANA_PRIVATE_KEY configured")

    kp = _keypair()
    taker = str(kp.pubkey())

    # 1) order
    try:
        order = requests.get(
            f"{config.JUPITER_BASE}/ultra/v1/order",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": raw_amount,
                "taker": taker,
                "slippageBps": slippage_bps,
            },
            headers=_headers(),
            timeout=_TIMEOUT,
        ).json()
    except Exception as e:  # noqa: BLE001
        return SwapResult(False, f"order request failed: {e}")

    tx_b64 = order.get("transaction")
    request_id = order.get("requestId")
    if not tx_b64 or not request_id:
        return SwapResult(False, f"no routable order: {order.get('error') or order}")

    out_amount = order.get("outAmount")
    out_amount = int(out_amount) if out_amount is not None else None

    # 2) sign locally
    try:
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(unsigned.message, [kp])
        signed_b64 = base64.b64encode(bytes(signed)).decode()
    except Exception as e:  # noqa: BLE001
        return SwapResult(False, f"signing failed: {e}")

    # 3) execute
    try:
        res = requests.post(
            f"{config.JUPITER_BASE}/ultra/v1/execute",
            json={"signedTransaction": signed_b64, "requestId": request_id},
            headers=_headers(),
            timeout=_TIMEOUT,
        ).json()
    except Exception as e:  # noqa: BLE001
        return SwapResult(False, f"execute request failed: {e}")

    status = str(res.get("status", "")).lower()
    sig = res.get("signature")
    if status == "success" or res.get("code") == 0:
        return SwapResult(True, f"filled — https://solscan.io/tx/{sig}", sig,
                          in_amount=raw_amount, out_amount=out_amount)
    return SwapResult(False, f"execute returned: {res}", sig)


def buy(mint: str, amount_sol: float) -> SwapResult:
    """Buy `mint` by swapping `amount_sol` of SOL into it."""
    lamports = int(amount_sol * config.LAMPORTS)

    if config.DRY_RUN:
        return SwapResult(
            True,
            f"[DRY_RUN] would buy {mint} with {amount_sol} SOL "
            f"({lamports} lamports). No transaction sent.",
            in_amount=lamports,
        )

    return _swap(config.WSOL_MINT, mint, lamports, config.SLIPPAGE_BPS)


def sell(mint: str, raw_token_amount: int) -> SwapResult:
    """Sell `raw_token_amount` (base units) of `mint` back into SOL."""
    if raw_token_amount <= 0:
        return SwapResult(False, "nothing to sell (zero balance)")

    if config.DRY_RUN:
        return SwapResult(
            True,
            f"[DRY_RUN] would sell {raw_token_amount} raw units of {mint} "
            f"back to SOL. No transaction sent.",
            in_amount=raw_token_amount,
        )

    return _swap(mint, config.WSOL_MINT, raw_token_amount, config.SELL_SLIPPAGE_BPS)
