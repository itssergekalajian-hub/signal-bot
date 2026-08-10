"""four.meme launchpad trading on BSC (bonding-curve tokens).

0x/PancakeSwap can't trade a four.meme token until it "graduates" (liquidity
added to PancakeSwap). Before that, trading goes through four.meme's per-token
TokenManager, quoted and routed via a Helper contract:

  getTokenInfo(token)  -> version, tokenManager, ..., liquidityAdded(bool)
      version 0 / reverts  -> not a four.meme token          (use 0x)
      liquidityAdded True  -> graduated to PancakeSwap        (use 0x)
      else                 -> still on the curve              (trade HERE)

  buy:  tryBuy(token, 0, funds) -> [tokenManager, quote, estAmount, estCost,
            estFee, msgValue, approval, funds]; then
        buyTokenAMAP(token, funds, minAmount)  payable, value = msgValue
  sell: approve(tokenManager, amount) on the token, then sellToken(token, amount)

Signatures/addresses are from four.meme's community integration reference.
Reuses evm_executor for signing/sending (which checks receipt status, so a
reverted tx is reported as a failure, never a false fill).
"""
from __future__ import annotations

import chains
import config
from swap_result import SwapResult

HELPER = "0xF251F83e40a78868FcfA3FA4599Dad6494E46034"

_HELPER_ABI = [
    {"name": "getTokenInfo", "stateMutability": "view", "type": "function",
     "inputs": [{"name": "token", "type": "address"}],
     "outputs": [{"type": "uint256"}, {"type": "address"}, {"type": "address"},
                 {"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"},
                 {"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"},
                 {"type": "uint256"}, {"type": "uint256"}, {"type": "bool"}]},
    {"name": "tryBuy", "stateMutability": "view", "type": "function",
     "inputs": [{"name": "token", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "funds", "type": "uint256"}],
     "outputs": [{"type": "address"}, {"type": "address"}, {"type": "uint256"},
                 {"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"},
                 {"type": "uint256"}, {"type": "uint256"}]},
    {"name": "trySell", "stateMutability": "view", "type": "function",
     "inputs": [{"name": "token", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "address"}, {"type": "address"},
                 {"type": "uint256"}, {"type": "uint256"}]},
]

_TM_ABI = [
    {"name": "buyTokenAMAP", "stateMutability": "payable", "type": "function",
     "inputs": [{"name": "token", "type": "address"}, {"name": "funds", "type": "uint256"},
                {"name": "minAmount", "type": "uint256"}], "outputs": []},
    {"name": "sellToken", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"name": "token", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": []},
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


def token_info(address: str) -> dict | None:
    """Return {token_manager, on_curve, version} or None if not a four.meme token.

    on_curve True means it must trade via four.meme (0x can't reach it yet).
    """
    if not config.FOURMEME_ENABLED:
        return None
    try:
        w3, _ = _bsc()
        helper = w3.eth.contract(address=w3.to_checksum_address(HELPER), abi=_HELPER_ABI)
        info = helper.functions.getTokenInfo(w3.to_checksum_address(address)).call()
    except Exception:  # noqa: BLE001
        return None
    version, token_manager, liquidity_added = int(info[0]), info[1], bool(info[11])
    if version == 0 or int(token_manager, 16) == 0:
        return None
    return {"token_manager": token_manager, "on_curve": not liquidity_added, "version": version}


def sellable(address: str) -> tuple[bool, float | None, str]:
    """Can this on-curve token be sold? Returns (ok, sell_tax_pct, reason).

    Uses four.meme's trySell: if it reverts or returns 0 funds, the token can't
    be exited on the curve — treat it as a honeypot and refuse to buy.
    """
    try:
        w3, _ = _bsc()
        helper = w3.eth.contract(address=w3.to_checksum_address(HELPER), abi=_HELPER_ABI)
        nominal = 10 ** 18  # 1 token (18-dec) is enough to prove sellability
        _, _, funds, fee = helper.functions.trySell(
            w3.to_checksum_address(address), nominal).call()
    except Exception:  # noqa: BLE001
        return (False, None, "four.meme trySell reverted — not sellable")
    if funds <= 0:
        return (False, None, "four.meme returns 0 on sell — not sellable")
    gross = funds + fee
    tax = (fee / gross * 100) if gross > 0 else 0.0
    return (True, tax, "")


def buy(address: str, amount_native: float, info: dict | None = None) -> SwapResult:
    """Buy `address` on the four.meme curve, spending ~amount_native BNB."""
    import evm_executor
    if not config.EVM_PRIVATE_KEY:
        return SwapResult(False, "no EVM_PRIVATE_KEY configured")
    try:
        w3, chain = _bsc()
        acct = evm_executor._account()
        helper = w3.eth.contract(address=w3.to_checksum_address(HELPER), abi=_HELPER_ABI)
        funds_wei = int(amount_native * (10 ** 18))
        token_cs = w3.to_checksum_address(address)
        q = helper.functions.tryBuy(token_cs, 0, funds_wei).call()
        token_manager, est_amount, msg_value = q[0], int(q[2]), int(q[5])
        if int(token_manager, 16) == 0 or est_amount <= 0:
            return SwapResult(False, "four.meme: token not buyable on the curve")
        min_amount = est_amount * (10_000 - config.FOURMEME_SLIPPAGE_BPS) // 10_000
        tm = w3.eth.contract(address=w3.to_checksum_address(token_manager), abi=_TM_ABI)
        data = tm.encode_abi("buyTokenAMAP", args=[token_cs, funds_wei, min_amount])
        txh = evm_executor._send(w3, acct, chain,
                                 {"to": token_manager, "data": data, "value": msg_value})
    except Exception as e:  # noqa: BLE001
        return SwapResult(False, f"four.meme buy failed: {e}")
    return SwapResult(True, f"filled (four.meme) — {chain.explorer_tx}{txh}", txh,
                      out_amount=est_amount)


def sell(address: str, raw_amount: int, info: dict | None = None) -> SwapResult:
    """Sell `raw_amount` base units of `address` back to BNB on the curve."""
    import evm_executor
    if not config.EVM_PRIVATE_KEY:
        return SwapResult(False, "no EVM_PRIVATE_KEY configured")
    if raw_amount <= 0:
        return SwapResult(False, "nothing to sell (zero balance)")
    info = info or token_info(address)
    if not info:
        return SwapResult(False, "four.meme: token not on the curve")
    try:
        w3, chain = _bsc()
        acct = evm_executor._account()
        tm_addr = info["token_manager"]
        token_cs = w3.to_checksum_address(address)
        # 1) approve the TokenManager to move our tokens
        token = w3.eth.contract(address=token_cs, abi=_APPROVE_ABI)
        approve_data = token.encode_abi("approve",
                                        args=[w3.to_checksum_address(tm_addr), raw_amount])
        evm_executor._send(w3, acct, chain, {"to": address, "data": approve_data},
                           gas_mult=config.SELL_GAS_MULT)
        # 2) sell on the curve
        tm = w3.eth.contract(address=w3.to_checksum_address(tm_addr), abi=_TM_ABI)
        sell_data = tm.encode_abi("sellToken", args=[token_cs, raw_amount])
        txh = evm_executor._send(w3, acct, chain, {"to": tm_addr, "data": sell_data},
                                 gas_mult=config.SELL_GAS_MULT)
    except Exception as e:  # noqa: BLE001
        return SwapResult(False, f"four.meme sell failed: {e}")
    return SwapResult(True, f"filled (four.meme) — {chain.explorer_tx}{txh}", txh)
