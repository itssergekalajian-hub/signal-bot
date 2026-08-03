"""Execution dispatcher — routes a trade to the right chain's adapter.

The rest of the bot never imports a chain-specific adapter directly; it calls
executor.buy / executor.sell / executor.token_balance with the chain the market
layer resolved, and this module dispatches:

    solana  -> solana_executor  (Jupiter Ultra)
    evm     -> evm_executor     (0x aggregator, any EVM chain)

Position sizing is in USD (BUY_AMOUNT_USD) so one setting works on every chain:
we price the chain's native coin via DexScreener and convert to a native amount
before handing off. DRY_RUN is handled here, once, for all chains.
"""
from __future__ import annotations

import chains
import config
import gas
import market
from swap_result import SwapResult

# Re-export for back-compat with older imports.
__all__ = ["SwapResult", "buy", "sell", "token_balance", "wallet_address", "native_price"]


def native_price(chain: chains.Chain) -> float | None:
    """USD price of the chain's native coin (via its wrapped-native token)."""
    return market.price_usd(chain.wrapped_native)


def wallet_address(chain_key: str) -> str | None:
    chain = chains.get(chain_key)
    if not chain:
        return None
    if chain.family == "solana":
        import solana_executor
        return solana_executor.wallet_address()
    import evm_executor
    return evm_executor.wallet_address()


def buy(address: str, chain_key: str, amount_usd: float) -> SwapResult:
    chain = chains.get(chain_key)
    if not chain:
        return SwapResult(False, f"unsupported chain '{chain_key}'")

    nat_price = native_price(chain)
    if not nat_price:
        return SwapResult(False, f"couldn't price {chain.native_symbol} to size the buy")
    amount_native = amount_usd / nat_price

    # Gas guard — applies in DRY_RUN too, so simulated runs show gas skips.
    ok, gas_note = gas.check_buy(chain)
    if not ok:
        return SwapResult(False, f"⛽ skipped — {gas_note}")

    if config.DRY_RUN:
        return SwapResult(
            True,
            f"[DRY_RUN] would buy {address} on {chain.name} with "
            f"~{amount_native:.6g} {chain.native_symbol} (${amount_usd:.2f}); {gas_note}. "
            f"No tx sent.",
            in_amount=int(amount_native * (10 ** 18)),
        )

    if chain.family == "solana":
        import solana_executor
        return solana_executor.buy(address, amount_native)
    import evm_executor
    return evm_executor.buy(chain, address, amount_native)


def sell(address: str, chain_key: str, raw_amount: int) -> SwapResult:
    chain = chains.get(chain_key)
    if not chain:
        return SwapResult(False, f"unsupported chain '{chain_key}'")
    if raw_amount <= 0:
        return SwapResult(False, "nothing to sell (zero balance)")

    if config.DRY_RUN:
        return SwapResult(
            True,
            f"[DRY_RUN] would sell {raw_amount} raw units of {address} on "
            f"{chain.name} back to {chain.native_symbol}. No tx sent.",
            in_amount=raw_amount,
        )

    if chain.family == "solana":
        import solana_executor
        return solana_executor.sell(address, raw_amount)
    import evm_executor
    return evm_executor.sell(chain, address, raw_amount)


def token_balance(address: str, chain_key: str) -> tuple[int, int]:
    """(raw_amount, decimals) held on `chain_key`; (0, 0) if none."""
    chain = chains.get(chain_key)
    if not chain:
        return 0, 0
    if chain.family == "solana":
        import solana_executor
        return solana_executor.token_balance(address)
    import evm_executor
    return evm_executor.token_balance(chain, address)
