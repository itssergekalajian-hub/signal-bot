"""Entry point. Wires the whole detect -> check -> confirm -> execute loop.

Two Telegram clients share one event loop:
  * user_client (your account) READS the signals channel.
  * bot_client  (your bot)     DMs YOU a Buy/Skip prompt and runs the buy on tap.

Nothing buys automatically. A token only reaches you if it clears the safety
gate, and even then you tap the button.
"""
from __future__ import annotations

import asyncio
import secrets

from telethon import Button, TelegramClient, events

import config
import executor
from extractor import extract_mints
from safety import evaluate

user_client = TelegramClient("user_session", config.TG_API_ID, config.TG_API_HASH)
bot_client = TelegramClient("bot_session", config.TG_API_ID, config.TG_API_HASH)

# short-lived map: button id -> mint (cleared once acted on)
_pending: dict[str, str] = {}


def _format(v) -> str:
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


@user_client.on(events.NewMessage(chats=[config.TG_CHANNEL]))
async def on_channel_message(event):
    mints = extract_mints(event.raw_text)
    for mint in mints:
        # Safety calls are blocking HTTP -> run off the event loop
        v = await asyncio.to_thread(evaluate, mint)

        if not v.passed:
            # Optional: comment out to stay quiet on rejects
            await bot_client.send_message(
                config.TG_OWNER_ID,
                f"⛔ Skipped `{mint}`\n" + "\n".join(f"• {r}" for r in v.reasons),
            )
            continue

        sid = secrets.token_hex(4)
        _pending[sid] = mint
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
    mint = _pending.pop(sid, None)
    if not mint:
        await event.answer("Expired or already handled.", alert=True)
        return

    if action == "skip":
        await event.edit("❌ Skipped.")
        return

    await event.edit("⏳ Buying…")
    res = await asyncio.to_thread(executor.buy, mint, config.BUY_AMOUNT_SOL)
    await event.edit(("✅ " if res.ok else "❌ ") + res.detail)


async def main():
    await bot_client.start(bot_token=config.TG_BOT_TOKEN)
    await user_client.start()  # first run prompts for phone + code
    mode = "DRY_RUN (no real trades)" if config.DRY_RUN else "LIVE — real SOL"
    print(f"Bot running in {mode}. Watching {config.TG_CHANNEL}.")
    await bot_client.send_message(
        config.TG_OWNER_ID, f"🤖 Online. Mode: *{mode}*. Watching {config.TG_CHANNEL}."
    )
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
