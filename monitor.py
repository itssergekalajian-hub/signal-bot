"""Take-profit monitor: sell 50% of a position once it reaches 2x (100% ROI).

Runs as a background asyncio task. Every POLL_INTERVAL_SEC it re-prices every
open position via DexScreener (chain-agnostic) and, when current/entry >=
TAKE_PROFIT_MULT, sells TAKE_PROFIT_SELL_PCT of the tokens back to the chain's
native coin — exactly once per position (guarded by `tp1_done`). The remainder
is left running as a moonbag.

The rule is a fixed, configurable local rule, not driven by the channel posting
"take profit" — the channels this targets don't send explicit exits, they just
call entries. Change TAKE_PROFIT_MULT / TAKE_PROFIT_SELL_PCT to taste.
"""
from __future__ import annotations

import asyncio
import time

import config
import executor
import positions
from market import price_usd


def record_buy(address: str, chain: str, symbol: str, amount_usd: float,
               entry_price_usd: float, result: executor.SwapResult) -> positions.Position | None:
    """Persist a freshly-opened position so the monitor can track it.

    Real mode: read the actual on-chain balance + decimals we now hold.
    Dry run:   estimate token amount from entry price so the take-profit math
               still demonstrates end to end without touching a wallet.
    """
    if not entry_price_usd or entry_price_usd <= 0:
        return None  # can't compute ROI without an entry price

    decimals = 9 if chain == "solana" else 18
    token_raw = 0

    if config.DRY_RUN:
        tokens_ui = amount_usd / entry_price_usd
        token_raw = int(tokens_ui * (10 ** decimals))
    else:
        try:
            token_raw, dec = executor.token_balance(address, chain)
            if dec:
                decimals = dec
        except Exception:  # noqa: BLE001
            token_raw = result.out_amount or 0

    pos = positions.Position(
        address=address, chain=chain, symbol=symbol or "?",
        entry_price_usd=entry_price_usd, amount_usd=amount_usd,
        token_raw=token_raw, decimals=decimals,
        opened_at=time.time(), dry_run=config.DRY_RUN,
    )
    positions.add(pos)
    return pos


def _sell_amount(pos: positions.Position) -> int:
    """How many raw tokens to sell for the take-profit trim."""
    held = pos.token_raw
    if not config.DRY_RUN:
        try:
            live, _ = executor.token_balance(pos.address, pos.chain)
            held = live  # on-chain balance is the source of truth
        except Exception:  # noqa: BLE001
            pass
    return int(held * (config.TAKE_PROFIT_SELL_PCT / 100.0))


async def _check_once(notify) -> None:
    for pos in positions.open_positions():
        if pos.tp1_done:
            continue
        current = await asyncio.to_thread(price_usd, pos.address)
        if current is None:
            continue

        ratio = current / pos.entry_price_usd
        if ratio < config.TAKE_PROFIT_MULT:
            continue

        sell_raw = _sell_amount(pos)
        if sell_raw <= 0:
            positions.update(pos.address, pos.opened_at, tp1_done=True,
                             notes="TP reached but no balance to sell")
            continue

        await notify(
            f"🎯 *Take-profit hit* {pos.symbol} ({pos.chain}) `{pos.address}`\n"
            f"{ratio:.2f}x (entry ${pos.entry_price_usd:.6g} → ${current:.6g})\n"
            f"Selling {config.TAKE_PROFIT_SELL_PCT:.0f}%…"
        )
        res = await asyncio.to_thread(executor.sell, pos.address, pos.chain, sell_raw)

        if res.ok:
            positions.update(
                pos.address, pos.opened_at, tp1_done=True,
                token_raw=max(pos.token_raw - sell_raw, 0),
                notes=f"TP1 sold {config.TAKE_PROFIT_SELL_PCT:.0f}% at {ratio:.2f}x",
            )
            await notify(f"✅ Trimmed {pos.symbol}: {res.detail}")
        else:
            await notify(f"❌ Take-profit sell failed for {pos.symbol}: {res.detail}")


async def run(notify) -> None:
    """Forever loop. `notify(text)` is an async fn that DMs the owner."""
    while True:
        try:
            await _check_once(notify)
        except Exception as e:  # noqa: BLE001  — never let the monitor die
            print(f"[monitor] error: {e}")
        await asyncio.sleep(config.POLL_INTERVAL_SEC)
