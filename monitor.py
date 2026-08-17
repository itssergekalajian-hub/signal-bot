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


def _sell_amount(pos: positions.Position, pct: float) -> int:
    """How many raw tokens to sell (pct% of the live balance)."""
    held = pos.token_raw
    if not config.DRY_RUN:
        try:
            live, _ = executor.token_balance(pos.address, pos.chain)
            held = live  # on-chain balance is the source of truth
        except Exception:  # noqa: BLE001
            pass
    return int(held * (pct / 100.0))


async def _emergency_exit(pos: positions.Position, ratio: float, current: float, notify) -> None:
    """Rug/stop-loss: sell 100% and close the position."""
    down = (1 - ratio) * 100
    sell_raw = _sell_amount(pos, 100)
    if sell_raw <= 0:
        positions.update(pos.address, pos.opened_at, token_raw=0, notes="stop-loss: no balance")
        return
    await notify(f"🛑 *Stop-loss* {pos.symbol} ({pos.chain}) down {down:.0f}% "
                 f"(entry ${pos.entry_price_usd:.6g} → ${current:.6g}) — selling everything…")
    res = await asyncio.to_thread(executor.sell, pos.address, pos.chain, sell_raw)
    if res.ok:
        positions.update(pos.address, pos.opened_at, token_raw=0, sl_tried=True,
                         notes=f"stop-loss at -{down:.0f}%")
        await notify(f"✅ Exited {pos.symbol}: {res.detail}")
    else:
        # Mark tried so we DON'T retry every poll (that was the spam bug). A rug
        # that can't be sold stays put; you can still try /sell manually later.
        positions.update(pos.address, pos.opened_at, sl_tried=True,
                         notes=f"stop-loss failed at -{down:.0f}%: {res.detail[:60]}")
        await notify(f"❌ Stop-loss couldn't sell {pos.symbol} (likely rugged / un-sellable). "
                     f"Won't retry.")


async def _check_once(notify) -> None:
    for pos in positions.open_positions():
        current = await asyncio.to_thread(price_usd, pos.address)
        if current is None or not pos.entry_price_usd:
            continue
        ratio = current / pos.entry_price_usd

        # ---- rug / stop-loss: sell 100% and close (only ONE attempt) ----
        if (config.STOP_LOSS_PCT > 0 and not pos.sl_tried
                and ratio <= (1 - config.STOP_LOSS_PCT / 100.0)):
            await _emergency_exit(pos, ratio, current, notify)
            continue

        # ---- take-profit: sell a slice once at the target ----
        if not config.TAKE_PROFIT_ENABLED or pos.tp1_done:
            continue
        if ratio < config.TAKE_PROFIT_MULT:
            continue

        sell_raw = _sell_amount(pos, config.TAKE_PROFIT_SELL_PCT)
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
