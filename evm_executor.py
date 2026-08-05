"""EVM execution adapter — one code path for every EVM chain.

Swaps route through the 0x Swap API (allowance-holder flow), which is
multichain: the only thing that changes per chain is the numeric chainId and
the RPC URL, both from chains.py. So BSC, Ethereum, Arbitrum, Base, Polygon,
Optimism and Avalanche all go through here.

  buy  (native -> token): 0x quote with sellToken = native pseudo-address;
                          sign + send the returned tx (no approval needed).
  sell (token -> native): 0x quote; if it reports an allowance issue, send an
                          ERC20 approve to the spender first, then the swap tx.

web3 / eth_account are imported lazily so a Solana-only deployment doesn't need
them installed. All functions assume real trading; the dispatcher handles
DRY_RUN.
"""
from __future__ import annotations

import requests

import chains
import config
from swap_result import SwapResult

_TIMEOUT = 20
_ZEROX_BASE = "https://api.0x.org"
_ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "o", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]


def _w3(chain: chains.Chain):
    from web3 import Web3  # lazy
    return Web3(Web3.HTTPProvider(chains.rpc_url(chain), request_kwargs={"timeout": 20}))


def _account():
    from eth_account import Account  # lazy
    return Account.from_key(config.EVM_PRIVATE_KEY)


def wallet_address() -> str | None:
    if not config.EVM_PRIVATE_KEY:
        return None
    return _account().address


def _zerox_headers() -> dict:
    h = {"0x-version": "v2"}
    if config.ZEROX_API_KEY:
        h["0x-api-key"] = config.ZEROX_API_KEY
    return h


def _quote(chain: chains.Chain, sell_token: str, buy_token: str,
           sell_amount: int, taker: str, slippage_bps: int) -> dict:
    r = requests.get(
        f"{_ZEROX_BASE}/swap/allowance-holder/quote",
        params={"chainId": chain.evm_chain_id, "sellToken": sell_token,
                "buyToken": buy_token, "sellAmount": str(sell_amount),
                "taker": taker, "slippageBps": slippage_bps},
        headers=_zerox_headers(), timeout=_TIMEOUT,
    )
    return r.json()


def _send(w3, acct, chain: chains.Chain, tx_fields: dict) -> str:
    """Sign + broadcast a tx dict from a 0x quote (or a built approve)."""
    tx = {
        "from": acct.address,
        "to": w3.to_checksum_address(tx_fields["to"]),
        "data": tx_fields.get("data", "0x"),
        "value": int(tx_fields.get("value", 0)),
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": chain.evm_chain_id,
    }
    if tx_fields.get("gas"):
        tx["gas"] = int(tx_fields["gas"])
    else:
        tx["gas"] = w3.eth.estimate_gas(tx)
    tx["gasPrice"] = int(tx_fields.get("gasPrice") or w3.eth.gas_price)

    signed = acct.sign_transaction(tx)
    # eth-account renamed this attribute (rawTransaction -> raw_transaction);
    # support both so we work across library versions.
    raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raw = signed.rawTransaction
    h = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    # A reverted tx is still mined — status 0 means it FAILED on-chain. Treat
    # that as an error so we never report a revert as a successful fill.
    if getattr(receipt, "status", None) != 1:
        raise RuntimeError(f"transaction reverted on-chain — {chain.explorer_tx}{h.hex()}")
    return h.hex()


def _approve_if_needed(w3, acct, chain: chains.Chain, token: str, quote: dict) -> None:
    allowance = (quote.get("issues") or {}).get("allowance")
    if not allowance:
        return
    spender = allowance.get("spender")
    if not spender:
        return
    erc20 = w3.eth.contract(address=w3.to_checksum_address(token), abi=_ERC20_ABI)
    max_uint = (1 << 256) - 1
    data = erc20.encode_abi("approve", args=[w3.to_checksum_address(spender), max_uint])
    _send(w3, acct, chain, {"to": token, "data": data})


def buy(chain: chains.Chain, address: str, amount_native: float) -> SwapResult:
    if not config.EVM_PRIVATE_KEY:
        return SwapResult(False, "no EVM_PRIVATE_KEY configured")
    try:
        w3, acct = _w3(chain), _account()
        sell_amount = int(amount_native * (10 ** 18))  # all supported natives are 18-dec
        q = _quote(chain, chains.EVM_NATIVE, address, sell_amount, acct.address, config.SLIPPAGE_BPS)
        tx_fields = q.get("transaction")
        if not tx_fields:
            return SwapResult(False, f"no route: {q.get('reason') or q.get('message') or q}")
        txh = _send(w3, acct, chain, tx_fields)
    except Exception as e:  # noqa: BLE001 — surface any failure as a clean result
        return SwapResult(False, f"buy failed: {e}")
    out = q.get("buyAmount")
    return SwapResult(True, f"filled — {chain.explorer_tx}{txh}", txh,
                      out_amount=int(out) if out else None)


def sell(chain: chains.Chain, address: str, raw_amount: int) -> SwapResult:
    if not config.EVM_PRIVATE_KEY:
        return SwapResult(False, "no EVM_PRIVATE_KEY configured")
    if raw_amount <= 0:
        return SwapResult(False, "nothing to sell (zero balance)")
    try:
        w3, acct = _w3(chain), _account()
        q = _quote(chain, address, chains.EVM_NATIVE, raw_amount, acct.address, config.SELL_SLIPPAGE_BPS)
        tx_fields = q.get("transaction")
        if not tx_fields:
            return SwapResult(False, f"no route: {q.get('reason') or q.get('message') or q}")
        _approve_if_needed(w3, acct, chain, address, q)
        txh = _send(w3, acct, chain, tx_fields)
    except Exception as e:  # noqa: BLE001 — surface any failure as a clean result
        return SwapResult(False, f"sell failed: {e}")
    return SwapResult(True, f"filled — {chain.explorer_tx}{txh}", txh)


def native_balance(chain: chains.Chain) -> float:
    """The wallet's native-coin balance (BNB/ETH/…) as a float, 0 if no wallet."""
    owner = wallet_address()
    if not owner:
        return 0.0
    w3 = _w3(chain)
    return w3.eth.get_balance(w3.to_checksum_address(owner)) / 1e18


def token_balance(chain: chains.Chain, address: str) -> tuple[int, int]:
    """(raw_amount, decimals) held for an ERC-20; (0, 0) if none / no wallet."""
    owner = wallet_address()
    if not owner:
        return 0, 0
    w3 = _w3(chain)
    erc20 = w3.eth.contract(address=w3.to_checksum_address(address), abi=_ERC20_ABI)
    raw = erc20.functions.balanceOf(w3.to_checksum_address(owner)).call()
    try:
        decimals = erc20.functions.decimals().call()
    except Exception:  # noqa: BLE001
        decimals = 18
    return int(raw), int(decimals)
