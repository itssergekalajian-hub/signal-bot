"""KyberSwap aggregator — gmgn-parity routing across every BSC DEX.

pancake.py only reaches PancakeSwap **V2** pairs. Graduated four.meme tokens
(and plenty of others) live on PancakeSwap **V3** or other DEXes, which neither
our V2 router nor 0x reliably reaches — that's why some tokens sold fine on
gmgn but failed here with "no route" / on-chain reverts.

KyberSwap's aggregator routes across V2, V3 and every other BSC DEX and returns
a ready-to-send transaction (calldata + router). We build and sign it exactly
like any other tx. If KyberSwap has no route (or its API is unreachable), we
return None so the caller falls through to the next adapter.

Two-step API:
  GET  /routes            -> best routeSummary + routerAddress
  POST /route/build       -> encoded calldata for that route
"""
from __future__ import annotations

import requests

import chains
import config
from swap_result import SwapResult

# KyberSwap's sentinel for the chain's native coin (BNB here).
NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
_BASE = "https://aggregator-api.kyberswap.com/bsc/api/v1"
_HEADERS = {"x-client-id": "signal-bot"}
_TIMEOUT = 20

_APPROVE_ABI = [
    {"name": "approve", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"name": "allowance", "stateMutability": "view", "type": "function",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "balanceOf", "stateMutability": "view", "type": "function",
     "inputs": [{"name": "o", "type": "address"}], "outputs": [{"type": "uint256"}]},
]
_MAX_UINT = (1 << 256) - 1


def _gas_hint(build: dict) -> dict:
    """Turn KyberSwap's gas estimate into a padded explicit gas field.

    Aggregator router calls sometimes make on-chain estimate_gas revert even
    though the real swap would succeed; using the API's own estimate with a
    50% buffer avoids that (falls back to estimate_gas if absent)."""
    try:
        g = int(build.get("gas") or 0)
    except (TypeError, ValueError):
        g = 0
    return {"gas": int(g * 1.5)} if g > 0 else {}


def _bsc():
    import evm_executor  # lazy (web3)
    return evm_executor._w3(chains.get("bsc")), chains.get("bsc")


def _routes(token_in: str, token_out: str, amount_in_wei: int) -> tuple[dict | None, str | None]:
    """Best route for the swap. (routeSummary, routerAddress) or (None, None)."""
    try:
        r = requests.get(f"{_BASE}/routes",
                         params={"tokenIn": token_in, "tokenOut": token_out,
                                 "amountIn": str(amount_in_wei)},
                         headers=_HEADERS, timeout=_TIMEOUT)
        d = r.json().get("data") or {}
    except Exception:  # noqa: BLE001 — API down / bad JSON -> caller falls through
        return None, None
    summary = d.get("routeSummary")
    router = d.get("routerAddress")
    if not summary or not router:
        return None, None
    return summary, router


def _build(route_summary: dict, sender: str, recipient: str, slippage_bps: int) -> dict | None:
    """Encode the route into calldata. Returns the build 'data' dict or None."""
    try:
        r = requests.post(f"{_BASE}/route/build",
                          json={"routeSummary": route_summary, "sender": sender,
                                "recipient": recipient, "slippageTolerance": slippage_bps},
                          headers=_HEADERS, timeout=_TIMEOUT)
        d = r.json().get("data") or {}
    except Exception:  # noqa: BLE001
        return None
    if not d.get("data") or not d.get("routerAddress"):
        return None
    return d


def diagnose(address: str) -> str:
    """Human-readable check: can KyberSwap route a sell of this token? No gas."""
    try:
        w3, _ = _bsc()
        token = w3.eth.contract(address=w3.to_checksum_address(address), abi=_APPROVE_ABI)
        import evm_executor
        owner = evm_executor.wallet_address()
        raw = int(token.functions.balanceOf(w3.to_checksum_address(owner)).call()) if owner else 0
    except Exception as e:  # noqa: BLE001
        return f"KyberSwap: balance read failed ({e})"
    probe = raw if raw > 0 else 10 ** 18
    summary, router = _routes(address, NATIVE, probe)
    if not summary:
        return f"KyberSwap: no route (balance raw {raw})"
    out = summary.get("amountOut")
    return f"KyberSwap: route OK · out {out} wei BNB · router {router} · balance raw {raw}"


def buy(address: str, amount_native: float):
    """Buy `address` with BNB via KyberSwap. None if no route (fall through)."""
    import evm_executor
    value = int(amount_native * (10 ** 18))
    summary, router = _routes(NATIVE, address, value)
    if not summary:
        return None
    if not config.EVM_PRIVATE_KEY:
        return SwapResult(False, "no EVM_PRIVATE_KEY configured")
    try:
        w3, chain = _bsc()
        acct = evm_executor._account()
        build = _build(summary, acct.address, acct.address, config.SLIPPAGE_BPS)
        if not build:
            return None
        txh = evm_executor._send(
            w3, acct, chain,
            {"to": build["routerAddress"], "data": build["data"],
             "value": int(build.get("transactionValue") or value), **_gas_hint(build)})
    except Exception as e:  # noqa: BLE001
        return SwapResult(False, f"kyberswap buy failed: {e}")
    out = build.get("amountOut")
    return SwapResult(True, f"filled (kyberswap) — {chain.explorer_tx}{txh}", txh,
                      out_amount=int(out) if out else None)


def sell(address: str, raw_amount: int):
    """Sell `raw_amount` of `address` to BNB via KyberSwap. None if no route."""
    import evm_executor
    if raw_amount <= 0:
        return SwapResult(False, "nothing to sell (zero balance)")
    if not config.EVM_PRIVATE_KEY:
        return SwapResult(False, "no EVM_PRIVATE_KEY configured")
    try:
        w3, chain = _bsc()
        acct = evm_executor._account()
        token = w3.eth.contract(address=w3.to_checksum_address(address), abi=_APPROVE_ABI)
        # Cap to the balance the executing node actually sees (defeats
        # TRANSFER_FROM_FAILED from block-lag / rounding).
        try:
            onchain = int(evm_executor._retry(
                lambda: token.functions.balanceOf(acct.address).call()))
        except Exception:  # noqa: BLE001
            onchain = raw_amount
        if onchain <= 0:
            return SwapResult(False, "nothing to sell (exec node sees 0 balance)")
        raw_amount = min(raw_amount, onchain)
    except Exception:  # noqa: BLE001 — RPC trouble; let a later adapter try
        return None

    summary, router = _routes(address, NATIVE, raw_amount)
    if not summary:
        return None
    try:
        # Approve the KyberSwap router (unlimited, once) before the swap.
        try:
            current = int(token.functions.allowance(
                acct.address, w3.to_checksum_address(router)).call())
        except Exception:  # noqa: BLE001
            current = 0
        if current < raw_amount:
            approve_data = token.encode_abi(
                "approve", args=[w3.to_checksum_address(router), _MAX_UINT])
            evm_executor._send(w3, acct, chain, {"to": address, "data": approve_data},
                               gas_mult=config.SELL_GAS_MULT)
        build = _build(summary, acct.address, acct.address, config.SELL_SLIPPAGE_BPS)
        if not build:
            return None
        txh = evm_executor._send(
            w3, acct, chain,
            {"to": build["routerAddress"], "data": build["data"],
             "value": int(build.get("transactionValue") or 0), **_gas_hint(build)},
            gas_mult=config.SELL_GAS_MULT)
    except Exception as e:  # noqa: BLE001
        return SwapResult(False, f"kyberswap sell failed: {e}")
    return SwapResult(True, f"filled (kyberswap) — {chain.explorer_tx}{txh}", txh)
