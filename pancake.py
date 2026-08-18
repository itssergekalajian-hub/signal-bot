"""PancakeSwap V2 direct routing for BSC — with fee-on-transfer support.

Many BSC meme tokens charge a transfer tax. 0x's standard swap reverts on those
("Swap validation failed"), which is why the bot couldn't sell tokens that a UI
like gmgn sells fine — gmgn uses the *SupportingFeeOnTransferTokens router
functions. This module does the same. It sits in the BSC route between four.meme
(bonding curve) and 0x (aggregator), and returns None when there's no Pancake
pair so the caller can fall back to 0x.
"""
from __future__ import annotations

import time

import chains
import config
from swap_result import SwapResult

ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"  # PancakeSwap V2 router
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
USDT = "0x55d398326f99059fF775485246999027B3197955"
BUSD = "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56"
USDC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
# Common quote tokens to hop through. Many BSC memes pair with USDT/BUSD, not
# BNB directly — a direct token→WBNB path reverts for those, so we also try
# token→USDT→WBNB etc. and take whichever route gives the most output (this is
# what a real router / gmgn does).
_BASES = [USDT, BUSD, USDC]

_ROUTER_ABI = [
    {"name": "getAmountsOut", "stateMutability": "view", "type": "function",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "path", "type": "address[]"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactETHForTokensSupportingFeeOnTransferTokens",
     "stateMutability": "payable", "type": "function",
     "inputs": [{"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}], "outputs": []},
    {"name": "swapExactTokensForETHSupportingFeeOnTransferTokens",
     "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}], "outputs": []},
]
_TOKEN_ABI = [
    {"name": "approve", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"name": "balanceOf", "stateMutability": "view", "type": "function",
     "inputs": [{"name": "o", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "allowance", "stateMutability": "view", "type": "function",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
]
_MAX_UINT = (1 << 256) - 1


def _bsc():
    import evm_executor  # lazy (web3)
    chain = chains.get("bsc")
    return evm_executor._w3(chain), chain


def _best_route(w3, router, token: str, want: str):
    """Find the PancakeSwap-V2 path with the best output.

    want="out"  → selling token for BNB: paths token→…→WBNB.
    want="in"   → buying token with BNB: paths WBNB→…→token.
    The caller passes a probe amount via the closure; we return
    (path, amounts) for the best path, or (None, None) if no pair exists on
    any of the tried routes (so the caller can fall back to 0x).
    """
    tok = w3.to_checksum_address(token)
    wbnb = w3.to_checksum_address(WBNB)
    if want == "out":
        candidates = [[tok, wbnb]] + [[tok, w3.to_checksum_address(b), wbnb]
                                      for b in _BASES if b.lower() != token.lower()]
    else:
        candidates = [[wbnb, tok]] + [[wbnb, w3.to_checksum_address(b), tok]
                                      for b in _BASES if b.lower() != token.lower()]
    return candidates


def _quote_best(router, candidates, amount_in: int):
    """Return (best_path, out_amount) across candidate paths; (None, 0) if none."""
    best_path, best_out = None, 0
    for path in candidates:
        try:
            out = int(router.functions.getAmountsOut(amount_in, path).call()[-1])
        except Exception:  # noqa: BLE001 — this path has no pair; try the next
            continue
        if out > best_out:
            best_path, best_out = path, out
    return best_path, best_out


def buy(address: str, amount_native: float):
    """Buy `address` with BNB via PancakeSwap. None if there's no Pancake pair."""
    import evm_executor
    try:
        w3, chain = _bsc()
        router = w3.eth.contract(address=w3.to_checksum_address(ROUTER), abi=_ROUTER_ABI)
        value = int(amount_native * (10 ** 18))
        candidates = _best_route(w3, router, address, want="in")
        path, expected = _quote_best(router, candidates, value)
    except Exception:  # noqa: BLE001 — no pair on Pancake; caller falls back to 0x
        return None
    if not path or expected <= 0:
        return None
    if not config.EVM_PRIVATE_KEY:
        return SwapResult(False, "no EVM_PRIVATE_KEY configured")
    try:
        acct = evm_executor._account()
        min_out = expected * (10_000 - config.SLIPPAGE_BPS) // 10_000
        deadline = int(time.time()) + 600
        data = router.encode_abi("swapExactETHForTokensSupportingFeeOnTransferTokens",
                                 args=[min_out, path, acct.address, deadline])
        txh = evm_executor._send(w3, acct, chain, {"to": ROUTER, "data": data, "value": value})
    except Exception as e:  # noqa: BLE001
        return SwapResult(False, f"pancake buy failed: {e}")
    return SwapResult(True, f"filled (pancake) — {chain.explorer_tx}{txh}", txh, out_amount=expected)


def sell(address: str, raw_amount: int):
    """Sell `raw_amount` of `address` to BNB via PancakeSwap. None if no pair."""
    import evm_executor
    try:
        w3, chain = _bsc()
        router = w3.eth.contract(address=w3.to_checksum_address(ROUTER), abi=_ROUTER_ABI)
        token = w3.eth.contract(address=w3.to_checksum_address(address), abi=_TOKEN_ABI)
        candidates = _best_route(w3, router, address, want="out")
        probe = raw_amount if raw_amount > 0 else 10 ** 18
        path, expected = _quote_best(router, candidates, probe)
    except Exception:  # noqa: BLE001 — no pair on Pancake; caller falls back to 0x
        return None
    if not path or expected <= 0:
        return None
    if not config.EVM_PRIVATE_KEY:
        return SwapResult(False, "no EVM_PRIVATE_KEY configured")
    if raw_amount <= 0:
        return SwapResult(False, "nothing to sell (zero balance)")
    try:
        acct = evm_executor._account()
        # Cap the sell to the balance the EXECUTING node actually sees. The
        # position's raw_amount comes from a multi-RPC read that may be a hair
        # higher than what this node reports (block lag / rounding), and
        # transferFrom reverts the moment amountIn > balance. min() defeats
        # "TransferHelper: TRANSFER_FROM_FAILED".
        try:
            onchain = int(evm_executor._retry(
                lambda: token.functions.balanceOf(acct.address).call()))
        except Exception:  # noqa: BLE001 — if the read fails, trust the caller's amount
            onchain = raw_amount
        if onchain <= 0:
            return SwapResult(False, "nothing to sell (exec node sees 0 balance)")
        raw_amount = min(raw_amount, onchain)
        # Re-quote for the (possibly) capped amount on the best path so min_out
        # matches what we actually sell.
        path, expected = _quote_best(router, candidates, raw_amount)
        if not path or expected <= 0:
            return SwapResult(False, "no output for capped amount")

        # Approve the router UNLIMITED once (idempotent) rather than an exact
        # amount — an exact approve that's a wei short of the transferred amount
        # is another way transferFrom reverts.
        try:
            current = int(token.functions.allowance(acct.address, w3.to_checksum_address(ROUTER)).call())
        except Exception:  # noqa: BLE001
            current = 0
        if current < raw_amount:
            approve_data = token.encode_abi("approve", args=[w3.to_checksum_address(ROUTER), _MAX_UINT])
            evm_executor._send(w3, acct, chain, {"to": address, "data": approve_data},
                               gas_mult=config.SELL_GAS_MULT)
        min_out = expected * (10_000 - config.SELL_SLIPPAGE_BPS) // 10_000
        deadline = int(time.time()) + 600
        data = router.encode_abi("swapExactTokensForETHSupportingFeeOnTransferTokens",
                                 args=[raw_amount, min_out, path, acct.address, deadline])
        txh = evm_executor._send(w3, acct, chain, {"to": ROUTER, "data": data},
                                 gas_mult=config.SELL_GAS_MULT)
    except Exception as e:  # noqa: BLE001
        return SwapResult(False, f"pancake sell failed: {e}")
    return SwapResult(True, f"filled (pancake) — {chain.explorer_tx}{txh}", txh)
