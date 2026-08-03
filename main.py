"""Entry point. Wires the whole detect -> check -> open -> take-profit loop.

Two Telegram clients share one event loop:
  * user_client (your account) READS the signals channel.
  * bot_client  (your bot)     opens trades and DMs YOU status updates.

A background monitor task watches every open position and sells 50% at 2x.

Modes (config / .env):
  * AUTO_TRADE=true  -> a call that clears the safety gate is bought instantly.
  * AUTO_TRADE=false -> you get a Buy/Skip button to tap (the original flow).
  * DRY_RUN=true     -> nothing ever swaps; buys/sells are simulated end to end.
"""
from __future__ import annotations

import asyncio
import secrets

from telethon import Button, TelegramClient, events

import config
import executor
import monitor
from extractor import extract_mints
from safety import Verdict, evaluate

user_client = TelegramClient("user_session", config.TG_API_ID, config.TG_API_HASH)
bot_client = TelegramClient("bot_session", config.TG_API_ID, config.TG_API_HASH)

# short-lived map: button id -> verdict (cleared once acted on)
_pending: dict[str, Verdict] = {}


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
        f"🪙 *{i.get('symbol') or 'Unknown'}*  `{v.mint}`",
        f"💧 Liquidity: ${i.get('liquidity_usd', 0):,}",
        f"🏷️ Price: {i.get('price_usd', '?')}",
        f"🛡️ RugCheck score: {i.get('rugcheck_score', '?')}",
    ]
    if i.get("top_holder_pct") is not None:
        lines.append(f"👤 Top holder: {i['top_holder_pct']}%")
    if i.get("age_min") is not None:
        lines.append(f"⏱️ Pair age: {i['age_min']}m")
    if i.get("risks"):
        lines.append(f"⚠️ Flags: {', '.join(i['risks'][:5]) or 'none'}")
    return "\n".join(lines)


async def _open_trade(v: Verdict) -> None:
    """Buy the mint, record the position for take-profit tracking, and report."""
    await _notify(f"⏳ Opening {v.info.get('symbol') or v.mint}…")
    res = await asyncio.to_thread(executor.buy, v.mint, config.BUY_AMOUNT_SOL)

    if not res.ok:
        await _notify(f"❌ Buy failed for `{v.mint}`: {res.detail}")
        return

    pos = monitor.record_buy(
        v.mint, v.info.get("symbol") or "?", config.BUY_AMOUNT_SOL, _entry_price(v), res
    )
    tail = ""
    if pos is None:
        tail = "\n⚠️ No entry price — take-profit tracking disabled for this one."
    else:
        tail = (f"\n📈 Tracking for {config.TAKE_PROFIT_MULT:g}x → "
                f"will sell {config.TAKE_PROFIT_SELL_PCT:.0f}%.")
    await _notify(f"✅ {res.detail}{tail}")


@user_client.on(events.NewMessage(chats=[config.TG_CHANNEL]))
async def on_channel_message(event):
    mints = extract_mints(event.raw_text)
    for mint in mints:
        # Safety calls are blocking HTTP -> run off the event loop
        v = await asyncio.to_thread(evaluate, mint)

        if not v.passed:
            await bot_client.send_message(
                config.TG_OWNER_ID,
                f"⛔ Skipped `{mint}`\n" + "\n".join(f"• {r}" for r in v.reasons),
            )
            continue

        if config.AUTO_TRADE:
            # What you asked for: open the call automatically, no tap.
            await _open_trade(v)
            continue

        # Manual mode: DM a Buy/Skip button to tap.
        sid = secrets.token_hex(4)
        _pending[sid] = v
        buttons = [[
            Button.inline(f"✅ Buy {config.BUY_AMOUNT_SOL} SOL", f"buy:{sid}".encode()),
            Button.inline("❌ Skip", f"skip:{sid}".encode()),
        ]]
        header = "✅ *Passed safety checks* — your call:\n\n"
        await bot_client.send_message(config.TG_OWNER_ID, header + _format(v), buttons=buttons)


@bot_client.on(events.CallbackQuery)
async def on_click(event):
    try:
        action, sid = event.data.decode().split(":", 1)
    except ValueError:
        return
    v = _pending.pop(sid, None)
    if not v:
        await event.answer("Expired or already handled.", alert=True)
        return

    if action == "skip":
        await event.edit("❌ Skipped.")
        return

    await event.edit("⏳ Buying…")
    res = await asyncio.to_thread(executor.buy, v.mint, config.BUY_AMOUNT_SOL)
    if res.ok:
        monitor.record_buy(
            v.mint, v.info.get("symbol") or "?", config.BUY_AMOUNT_SOL, _entry_price(v), res
        )
        await event.edit(f"✅ {res.detail}\n📈 Tracking for {config.TAKE_PROFIT_MULT:g}x "
                         f"→ will sell {config.TAKE_PROFIT_SELL_PCT:.0f}%.")
    else:
        await event.edit(f"❌ {res.detail}")


async def main():
    await bot_client.start(bot_token=config.TG_BOT_TOKEN)
    await user_client.start()  # first run prompts for phone + code

    dry = "DRY_RUN (no real trades)" if config.DRY_RUN else "LIVE — real SOL"
    auto = "AUTO buy" if config.AUTO_TRADE else "manual confirm"
    mode = f"{dry} · {auto}"
    print(f"Bot running: {mode}. Watching {config.TG_CHANNEL}.")
    await _notify(
        f"🤖 Online. Mode: *{mode}*.\n"
        f"Watching {config.TG_CHANNEL}.\n"
        f"Take-profit: sell {config.TAKE_PROFIT_SELL_PCT:.0f}% at "
        f"{config.TAKE_PROFIT_MULT:g}x."
    )

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected(),
        monitor.run(_notify),  # background take-profit loop
    )


if __name__ == "__main__":
    asyncio.run(main())
