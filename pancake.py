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
_APPROVE_ABI = [
    {"name": "approve", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
]


def _bsc():
    import evm_executor  # lazy (web3)
    chain = chains.get("bsc")
    return evm_executor._w3(chain), chain


def buy(address: str, amount_native: float):
    """Buy `address` with BNB via PancakeSwap. None if there's no Pancake pair."""
    import evm_executor
    try:
        w3, chain = _bsc()
        router = w3.eth.contract(address=w3.to_checksum_address(ROUTER), abi=_ROUTER_ABI)
        path = [w3.to_checksum_address(WBNB), w3.to_checksum_address(address)]
        value = int(amount_native * (10 ** 18))
        expected = int(router.functions.getAmountsOut(value, path).call()[-1])
    except Exception:  # noqa: BLE001 — no pair on Pancake; caller falls back to 0x
        return None
    if expected <= 0:
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
        path = [w3.to_checksum_address(address), w3.to_checksum_address(WBNB)]
        probe = raw_amount if raw_amount > 0 else 10 ** 18
        expected = int(router.functions.getAmountsOut(probe, path).call()[-1])
    except Exception:  # noqa: BLE001 — no pair on Pancake; caller falls back to 0x
        return None
    if expected <= 0:
        return None
    if not config.EVM_PRIVATE_KEY:
        return SwapResult(False, "no EVM_PRIVATE_KEY configured")
    if raw_amount <= 0:
        return SwapResult(False, "nothing to sell (zero balance)")
    try:
        acct = evm_executor._account()
        token = w3.eth.contract(address=w3.to_checksum_address(address), abi=_APPROVE_ABI)
        approve_data = token.encode_abi("approve", args=[w3.to_checksum_address(ROUTER), raw_amount])
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
