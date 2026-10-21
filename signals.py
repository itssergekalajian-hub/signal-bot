"""FX signals bot — the narratives watcher, and nothing else.

A lean entry point that runs ONLY the FX feature: watch the channel's Narratives
topic, read each chart with Gemini, confirm the levels, and alert when live price
nears a zone. No crypto trading, no /sell, /positions, /menu — just signals.

Run this instead of main.py for a signals-only bot:
    python3 signals.py

(The systemd service's ExecStart points here.) It reuses the same Telegram login
(user_session / bot_session), so only one of main.py / signals.py runs at a time.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

import config
import fx_narratives

user_client = TelegramClient("user_session", config.TG_API_ID, config.TG_API_HASH)
bot_client = TelegramClient("bot_session", config.TG_API_ID, config.TG_API_HASH)


async def _notify(text: str) -> None:
    await bot_client.send_message(config.TG_OWNER_ID, text)


async def _register_commands() -> None:
    """Show only the FX commands in Telegram's '/' menu."""
    try:
        from telethon.tl import functions, types
        cmds = [("menu", "FX menu"),
                ("zones", "Watched price zones"),
                ("fxlast", "Lock in recent charts"),
                ("fxclear", "Clear zones (id / pair / all)"),
                ("fxtest", "Health check: prices + Gemini")]
        await bot_client(functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(), lang_code="",
            commands=[types.BotCommand(c, d) for c, d in cmds]))
    except Exception as e:  # noqa: BLE001
        print(f"[commands] could not register menu: {e}")


async def main():
    await bot_client.start(bot_token=config.TG_BOT_TOKEN)
    await user_client.start()
    await _register_commands()

    fx_narratives.register(user_client, bot_client, _notify)

    gem = "Gemini auto-read ON" if config.GEMINI_API_KEY else "Gemini key missing — manual entry only"
    print(f"FX signals bot running. Narratives topic {config.FX_NARRATIVES_TOPIC} "
          f"of channel {config.FX_CHANNEL_ID}. {gem}.")
    await _notify(
        "📡 *FX signals bot online.*\n"
        "Watching the Narratives topic · EURUSD / GBPUSD / DXY / XAUUSD\n"
        f"Alerts when price comes within {config.FX_NEAR_PIPS:g} pips of a zone.\n"
        f"_{gem}._\n"
        "Commands: /zones · /fxclear · /fxtest")

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected(),
        fx_narratives.run(bot_client, _notify),
    )


if __name__ == "__main__":
    asyncio.run(main())
