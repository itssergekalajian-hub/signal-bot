"""Entry point. Wires the whole detect -> resolve -> check -> open -> take-profit
loop, across any supported chain.

Two Telegram clients share one event loop:
  * user_client (your account) READS the signals channel.
  * bot_client  (your bot)     opens trades and DMs YOU status updates.

A background monitor task watches every open position and sells 50% at 2x.

Chain is never assumed: every candidate address is resolved to its real chain
by the safety layer (via DexScreener), so BSC, ETH, Arbitrum, Solana, etc. are
all handled by the same path — only the execution adapter differs.

Modes (config / .env):
  * AUTO_TRADE=true  -> a call that clears the safety gate is bought instantly.
  * AUTO_TRADE=false -> you get a Buy/Skip button to tap.
  * DRY_RUN=true     -> nothing ever swaps; buys/sells are simulated end to end.
"""
from __future__ import annotations

import asyncio
import secrets

from telethon import Button, TelegramClient, events

import config
import executor
import market
import monitor
import positions
from extractor import extract_candidates
from safety import Verdict, evaluate

user_client = TelegramClient("user_session", config.TG_API_ID, config.TG_API_HASH)
bot_client = TelegramClient("bot_session", config.TG_API_ID, config.TG_API_HASH)

# short-lived map: button id -> verdict (cleared once acted on)
_pending: dict[str, Verdict] = {}
# short-lived map: button id -> {address, chain, opened_at, symbol} for /sell
_pending_sell: dict[str, dict] = {}
# de-dupe addresses we've already handled recently (reposted calls)
_seen: set[str] = set()


async def _notify(text: str) -> None:
    await bot_client.send_message(config.TG_OWNER_ID, text)


def _entry_price(v: Verdict) -> float:
    try:
        return float(v.info.get("price_usd"))
    except (TypeError, ValueError):
        return 0.0


def _format(v: Verdict) -> str:
    i = v.info
    lines = [
        f"🪙 *{i.get('symbol') or 'Unknown'}*  ({v.chain})",
        f"`{v.address}`",
        f"💧 Liquidity: ${i.get('liquidity_usd', 0):,}",
        f"🏷️ Price: {i.get('price_usd', '?')}",
    ]
    if i.get("buy_tax_pct") is not None:
        lines.append(f"💸 Tax: buy {i['buy_tax_pct']}% / sell {i.get('sell_tax_pct', '?')}%")
    if i.get("age_min") is not None:
        lines.append(f"⏱️ Pair age: {i['age_min']}m")
    return "\n".join(lines)


async def _open_trade(v: Verdict) -> None:
    """Buy the token, record the position for take-profit tracking, and report."""
    await _notify(f"⏳ Opening {v.info.get('symbol') or v.address} on {v.chain}…")
    try:
        res = await asyncio.to_thread(executor.buy, v.address, v.chain, config.BUY_AMOUNT_USD)
    except Exception as e:  # noqa: BLE001
        await _notify(f"❌ Buy errored for `{v.address}` ({v.chain}): {e}")
        return

    if not res.ok:
        await _notify(f"❌ Buy failed for `{v.address}` ({v.chain}): {res.detail}")
        return

    pos = monitor.record_buy(
        v.address, v.chain, v.info.get("symbol") or "?",
        config.BUY_AMOUNT_USD, _entry_price(v), res,
    )
    if pos is None:
        tail = "\n⚠️ No entry price — take-profit tracking disabled for this one."
    else:
        tail = (f"\n📈 Tracking for {config.TAKE_PROFIT_MULT:g}x → "
                f"will sell {config.TAKE_PROFIT_SELL_PCT:.0f}%.")
    await _notify(f"✅ {res.detail}{tail}")


@user_client.on(events.NewMessage(chats=[config.TG_CHANNEL]))
async def on_channel_message(event):
    for address in extract_candidates(event.raw_text):
        if address in _seen:
            continue
        _seen.add(address)

        # Resolve chain + run the safety gate (blocking HTTP -> off the loop).
        v = await asyncio.to_thread(evaluate, address)

        if not v.passed:
            await bot_client.send_message(
                config.TG_OWNER_ID,
                f"⛔ Skipped `{address}`" + (f" ({v.chain})" if v.chain else "") +
                "\n" + "\n".join(f"• {r}" for r in v.reasons),
            )
            continue

        if config.AUTO_TRADE:
            await _open_trade(v)
            continue

        sid = secrets.token_hex(4)
        _pending[sid] = v
        buttons = [[
            Button.inline(f"✅ Buy ${config.BUY_AMOUNT_USD:g}", f"buy:{sid}".encode()),
            Button.inline("❌ Skip", f"skip:{sid}".encode()),
        ]]
        header = "✅ *Passed safety checks* — your call:\n\n"
        await bot_client.send_message(config.TG_OWNER_ID, header + _format(v), buttons=buttons)


async def _positions_report() -> str:
    """Build the /positions summary: ROI + current value per open holding."""
    open_pos = positions.open_positions()
    if not open_pos:
        return "📭 No open positions."

    lines = ["📊 *Open positions*\n"]
    total_val = 0.0
    for p in open_pos:
        cur = await asyncio.to_thread(market.price_usd, p.address)
        tokens = p.token_raw / (10 ** p.decimals)
        val = tokens * cur if cur else 0.0
        total_val += val
        roi = ((cur / p.entry_price_usd) - 1) * 100 if cur and p.entry_price_usd else 0.0
        state = "½ sold @2x" if p.tp1_done else "open"
        tag = " (dry)" if p.dry_run else ""
        lines.append(
            f"• *{p.symbol}* ({p.chain}){tag} — {roi:+.0f}%  ~${val:.2f}  [{state}]\n"
            f"  entry ${p.entry_price_usd:.6g} → now ${cur:.6g}" if cur
            else f"• *{p.symbol}* ({p.chain}){tag} — price unavailable  [{state}]"
        )
    lines.append(f"\n💰 Total open value: ~${total_val:.2f}")
    return "\n".join(lines)


@bot_client.on(events.NewMessage(pattern=r"^/positions", from_users=config.TG_OWNER_ID))
async def on_positions(event):
    await event.reply(await _positions_report())


@bot_client.on(events.NewMessage(pattern=r"^/help", from_users=config.TG_OWNER_ID))
async def on_help(event):
    await event.reply(
        "*Commands*\n"
        "• /positions — your open holdings, ROI, and value\n"
        "• /sell — sell a position (50% or 100%) by tapping a button\n"
        "• /help — this message\n\n"
        f"Mode: {'DRY_RUN' if config.DRY_RUN else 'LIVE'} · "
        f"{'AUTO' if config.AUTO_TRADE else 'manual'} · "
        f"${config.BUY_AMOUNT_USD:g}/buy · TP {config.TAKE_PROFIT_SELL_PCT:.0f}%@"
        f"{config.TAKE_PROFIT_MULT:g}x"
    )


@bot_client.on(events.NewMessage(pattern=r"^/sell", from_users=config.TG_OWNER_ID))
async def on_sell_cmd(event):
    """List open positions, each with Sell 50% / Sell 100% buttons."""
    open_pos = positions.open_positions()
    if not open_pos:
        await event.reply("📭 No open positions to sell.")
        return
    await event.reply("Tap to sell a position:")
    for p in open_pos:
        cur = await asyncio.to_thread(market.price_usd, p.address)
        roi = ((cur / p.entry_price_usd) - 1) * 100 if cur and p.entry_price_usd else 0.0
        sid = secrets.token_hex(4)
        _pending_sell[sid] = {"address": p.address, "chain": p.chain,
                              "opened_at": p.opened_at, "symbol": p.symbol}
        buttons = [[
            Button.inline("Sell 50%", f"sell:{sid}:50".encode()),
            Button.inline("Sell 100%", f"sell:{sid}:100".encode()),
        ]]
        await bot_client.send_message(
            config.TG_OWNER_ID,
            f"*{p.symbol}* ({p.chain}) — {roi:+.0f}%\n`{p.address}`",
            buttons=buttons,
        )


def _held_raw(pos) -> int:
    """Raw tokens we actually hold (on-chain when live, recorded when dry)."""
    if config.DRY_RUN:
        return pos.token_raw
    try:
        live, _ = executor.token_balance(pos.address, pos.chain)
        return live
    except Exception:  # noqa: BLE001
        return pos.token_raw


async def _do_sell(address: str, chain: str, opened_at: float, pct: float) -> str:
    pos = next((p for p in positions.load()
                if p.address == address and p.opened_at == opened_at), None)
    if not pos:
        return "Position not found (already closed?)."
    held = _held_raw(pos)
    raw = int(held * (pct / 100.0))
    if raw <= 0:
        return "Nothing to sell (zero balance)."
    try:
        res = await asyncio.to_thread(executor.sell, address, chain, raw)
    except Exception as e:  # noqa: BLE001
        return f"❌ Sell errored: {e}"
    if not res.ok:
        return f"❌ Sell failed: {res.detail}"
    remaining = 0 if pct >= 100 else max(held - raw, 0)
    positions.update(address, opened_at, token_raw=remaining,
                     notes=f"manual sell {pct:.0f}%")
    return f"✅ Sold {pct:.0f}% of {pos.symbol}: {res.detail}"


@bot_client.on(events.CallbackQuery)
async def on_click(event):
    parts = event.data.decode().split(":")
    action = parts[0]

    # ---- manual sell:  sell:<sid>:<pct> ----
    if action == "sell":
        info = _pending_sell.pop(parts[1], None) if len(parts) >= 3 else None
        if not info:
            await event.answer("Expired or already handled.", alert=True)
            return
        pct = float(parts[2])
        await event.edit(f"⏳ Selling {pct:.0f}% of {info['symbol']}…")
        msg = await _do_sell(info["address"], info["chain"], info["opened_at"], pct)
        await event.edit(msg)
        return

    # ---- buy / skip:  buy:<sid>  or  skip:<sid> ----
    sid = parts[1] if len(parts) >= 2 else None
    v = _pending.pop(sid, None) if sid else None
    if not v:
        await event.answer("Expired or already handled.", alert=True)
        return

    if action == "skip":
        await event.edit("❌ Skipped.")
        return

    await event.edit("⏳ Buying…")
    try:
        res = await asyncio.to_thread(executor.buy, v.address, v.chain, config.BUY_AMOUNT_USD)
    except Exception as e:  # noqa: BLE001
        await event.edit(f"❌ Buy errored: {e}")
        return
    if res.ok:
        monitor.record_buy(
            v.address, v.chain, v.info.get("symbol") or "?",
            config.BUY_AMOUNT_USD, _entry_price(v), res,
        )
        await event.edit(f"✅ {res.detail}\n📈 Tracking for {config.TAKE_PROFIT_MULT:g}x "
                         f"→ will sell {config.TAKE_PROFIT_SELL_PCT:.0f}%.")
    else:
        await event.edit(f"❌ {res.detail}")


async def main():
    await bot_client.start(bot_token=config.TG_BOT_TOKEN)
    await user_client.start()  # first run prompts for phone + code

    dry = "DRY_RUN (no real trades)" if config.DRY_RUN else "LIVE — real funds"
    auto = "AUTO buy" if config.AUTO_TRADE else "manual confirm"
    mode = f"{dry} · {auto}"
    print(f"Bot running: {mode}. Watching {config.TG_CHANNEL}.")
    await _notify(
        f"🤖 Online. Mode: *{mode}*.\n"
        f"Watching {config.TG_CHANNEL} (all supported chains).\n"
        f"Buy size ${config.BUY_AMOUNT_USD:g} · Take-profit: sell "
        f"{config.TAKE_PROFIT_SELL_PCT:.0f}% at {config.TAKE_PROFIT_MULT:g}x.\n"
        f"Send /positions or /sell anytime · /help for commands."
    )

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected(),
        monitor.run(_notify),  # background take-profit loop
    )


if __name__ == "__main__":
    asyncio.run(main())
