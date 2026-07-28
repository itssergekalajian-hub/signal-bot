"""Execute a buy through Jupiter's Ultra API.

Ultra is RPC-less: you GET an /order (which returns an unsigned base64
transaction + requestId), sign it locally with your keypair, then POST it to
/execute and Jupiter handles priority fees, slippage, and landing the tx.

Flow:
  GET  {base}/ultra/v1/order?inputMint=SOL&outputMint=<mint>&amount=<lamports>&taker=<pubkey>
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
class BuyResult:
    ok: bool
    detail: str
    signature: str | None = None


def _keypair() -> Keypair:
    return Keypair.from_base58_string(config.SOLANA_PRIVATE_KEY)


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if config.JUPITER_API_KEY:
        h["x-api-key"] = config.JUPITER_API_KEY
    return h


def buy(mint: str, amount_sol: float) -> BuyResult:
    lamports = int(amount_sol * config.LAMPORTS)

    if config.DRY_RUN:
        return BuyResult(True, f"[DRY_RUN] would buy {mint} with {amount_sol} SOL "
                               f"({lamports} lamports). No transaction sent.")

    if not config.SOLANA_PRIVATE_KEY:
        return BuyResult(False, "no SOLANA_PRIVATE_KEY configured")

    kp = _keypair()
    taker = str(kp.pubkey())

    # 1) order
    try:
        order = requests.get(
            f"{config.JUPITER_BASE}/ultra/v1/order",
            params={
                "inputMint": config.WSOL_MINT,
                "outputMint": mint,
                "amount": lamports,
                "taker": taker,
                "slippageBps": config.SLIPPAGE_BPS,
            },
            headers=_headers(),
            timeout=_TIMEOUT,
        ).json()
    except Exception as e:  # noqa: BLE001
        return BuyResult(False, f"order request failed: {e}")

    tx_b64 = order.get("transaction")
    request_id = order.get("requestId")
    if not tx_b64 or not request_id:
        return BuyResult(False, f"no routable order: {order.get('error') or order}")

    # 2) sign locally
    try:
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(unsigned.message, [kp])
        signed_b64 = base64.b64encode(bytes(signed)).decode()
    except Exception as e:  # noqa: BLE001
        return BuyResult(False, f"signing failed: {e}")

    # 3) execute
    try:
        res = requests.post(
            f"{config.JUPITER_BASE}/ultra/v1/execute",
            json={"signedTransaction": signed_b64, "requestId": request_id},
            headers=_headers(),
            timeout=_TIMEOUT,
        ).json()
    except Exception as e:  # noqa: BLE001
        return BuyResult(False, f"execute request failed: {e}")

    status = str(res.get("status", "")).lower()
    sig = res.get("signature")
    if status == "success" or res.get("code") == 0:
        return BuyResult(True, f"filled — https://solscan.io/tx/{sig}", sig)
    return BuyResult(False, f"execute returned: {res}", sig)
