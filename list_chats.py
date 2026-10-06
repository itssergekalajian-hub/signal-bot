"""List every channel/group your account can see, with its numeric id.

Use this to find the id of a PRIVATE channel (paid signal channels have no
@username) to put in TG_CHANNEL. Reuses your existing login — no re-auth.

    python3 list_chats.py

Then copy the id (a negative number like -1001234567890) of the channel you
want and set it in .env:  TG_CHANNEL=-1001234567890

Stop the bot first (tmux attach -t bot, then Ctrl+C) so it isn't using the
session file at the same time.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

import config


async def main():
    client = TelegramClient("user_session", config.TG_API_ID, config.TG_API_HASH)
    await client.start()
    print(f"\n{'ID':>16}  TITLE")
    print("-" * 50)
    async for dialog in client.iter_dialogs():
        if dialog.is_channel or dialog.is_group:
            print(f"{dialog.id:>16}  {dialog.name}")
    print("\nCopy the id of your signal channel into .env as TG_CHANNEL.\n")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
