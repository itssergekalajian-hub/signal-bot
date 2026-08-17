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

import chains
import config
import executor
import market
import monitor
import positions
import wallet_scan
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
# runtime state you can toggle from Telegram (pause stops auto-buying)
_state: dict[str, bool] = {"paused": False}


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


def _buy_tail(pos) -> str:
    """The trailing line on a buy confirmation, per take-profit setting."""
    if not config.TAKE_PROFIT_ENABLED:
        return "\n💼 Recorded — sell anytime with /sell (no auto-sell)."
    if pos is None:
        return "\n⚠️ No entry price — take-profit tracking off for this one."
    return (f"\n📈 Tracking for {config.TAKE_PROFIT_MULT:g}x → "
            f"will sell {config.TAKE_PROFIT_SELL_PCT:.0f}%.")


async def _open_trade(v: Verdict) -> None:
    """Buy the token, record the position, and report."""
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
    await _notify(f"✅ {res.detail}{_buy_tail(pos)}")


@user_client.on(events.NewMessage(chats=config.TG_CHANNELS))
async def on_channel_message(event):
    if _state["paused"]:
        return  # trading paused from Telegram — ignore calls until resumed
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
    """/positions summary using the REAL on-chain balance as the source of truth.

    Positions whose wallet balance is 0 (ghosts from reverted buys, or ones you
    already sold) are pruned so the list always matches the blockchain.
    """
    open_pos = positions.open_positions()
    if not open_pos:
        return "📭 No open positions."

    lines = ["📊 *Open positions*\n"]
    total_val = 0.0
    shown = 0
    pruned = 0
    for p in open_pos:
        if p.dry_run:
            raw, dec = p.token_raw, p.decimals          # simulated — trust the ledger
        else:
            try:
                raw, dec = await asyncio.to_thread(executor.token_balance, p.address, p.chain)
            except Exception:  # noqa: BLE001
                raw, dec = p.token_raw, p.decimals      # RPC hiccup — fall back, don't prune
        if raw <= 0:
            # 0 on-chain: hide from the list, but DON'T delete — a flaky RPC read
            # can transiently return 0 for a token you really hold. It reappears
            # once the balance reads correctly.
            pruned += 1
            continue

        cur = await asyncio.to_thread(market.price_usd, p.address)
        tokens = raw / (10 ** (dec or p.decimals))
        val = tokens * cur if cur else 0.0
        total_val += val
        shown += 1
        roi = ((cur / p.entry_price_usd) - 1) * 100 if cur and p.entry_price_usd else 0.0
        tag = " (dry)" if p.dry_run else ""
        if cur:
            lines.append(f"• *{p.symbol}* ({p.chain}){tag} — {roi:+.0f}%  ~${val:.2f}\n"
                         f"  entry ${p.entry_price_usd:.6g} → now ${cur:.6g}")
        else:
            lines.append(f"• *{p.symbol}* ({p.chain}){tag} — held, price unavailable")

    if shown == 0:
        return ("📭 Nothing showing right now — the tracked tokens read 0 on-chain "
                f"({pruned} hidden). If you know you hold one, the RPC may be lagging; "
                "try again, or use /sell <address>.")
    lines.append(f"\n💰 Total value: ~${total_val:.2f}")
    if pruned:
        lines.append(f"_({pruned} hidden: 0 on-chain / RPC lag — not deleted)_")
    return "\n".join(lines)


@bot_client.on(events.NewMessage(pattern=r"^/positions", from_users=config.TG_OWNER_ID))
async def on_positions(event):
    await event.reply(await _positions_report())


def _sell_button(address: str, chain: str, symbol: str, subtitle: str) -> None:
    """Register a pending sell and DM the owner a Sell 50% / 100% card."""
    sid = secrets.token_hex(4)
    _pending_sell[sid] = {"address": address, "chain": chain, "symbol": symbol}
    buttons = [[
        Button.inline("Sell 50%", f"sell:{sid}:50".encode()),
        Button.inline("Sell 100%", f"sell:{sid}:100".encode()),
    ]]
    return (f"*{symbol}* ({chain})\n{subtitle}\n`{address}`", buttons)


@bot_client.on(events.NewMessage(pattern=r"^/sell", from_users=config.TG_OWNER_ID))
async def on_sell_cmd(event):
    """/sell → list tracked positions; /sell <address> → sell any token held."""
    parts = event.raw_text.split()

    # /sell <contract-address> — sell any token by its address (on-chain balance)
    if len(parts) >= 2:
        address = parts[1].strip().lower()
        if not (address.startswith("0x") and len(address) == 42):
            await event.reply("Send a token contract address: `/sell 0x…`")
            return
        m = await asyncio.to_thread(market.lookup, address)
        chain = m.chain if m else "bsc"
        symbol = (m.symbol if m else None) or address[:8]
        try:
            raw, dec = await asyncio.to_thread(executor.token_balance, address, chain)
        except Exception as e:  # noqa: BLE001
            await event.reply(f"Couldn't read that token on {chain}: {e}")
            return
        if raw <= 0:
            await event.reply(f"You hold 0 of that token on {chain}.")
            return
        text, buttons = _sell_button(address, chain, symbol, f"balance: {raw / 10**dec:.4g}")
        await bot_client.send_message(config.TG_OWNER_ID, text, buttons=buttons)
        return

    # /sell — list tracked positions
    if not await _send_sell_cards():
        await event.reply("📭 No tracked positions. To sell a token in your wallet "
                          "the bot isn't tracking, use `/sell <address>` or /scan.")


async def _send_sell_cards() -> bool:
    """Post a Sell 50%/100% card for each position you ACTUALLY hold.

    Reads each position's live on-chain balance and only shows cards for
    tokens with a non-zero balance, so the menu isn't cluttered with empties
    (already sold / rugged). Genuinely-zero positions are reconciled to
    token_raw=0 so /positions hides them too. False if nothing is sellable.
    """
    open_pos = positions.open_positions()
    if not open_pos:
        return False

    sellable, empty = [], 0
    for p in open_pos:
        try:
            raw, _ = await asyncio.to_thread(executor.token_balance, p.address, p.chain)
        except Exception:  # noqa: BLE001 — treat a read error as "unknown", keep the card
            raw = -1
        if raw == 0:
            empty += 1
            positions.update(p.address, p.opened_at, token_raw=0, notes="empty on-chain")
        else:
            sellable.append(p)

    if not sellable:
        note = f" ({empty} empty — already sold or rugged, hidden)" if empty else ""
        await bot_client.send_message(
            config.TG_OWNER_ID,
            f"📭 Nothing to sell{note}. Use `/sell 0x…` to sell any token by address.")
        return True

    header = "Tap to sell a position:"
    if empty:
        header += f"\n_({empty} empty position{'s' if empty != 1 else ''} hidden)_"
    await bot_client.send_message(config.TG_OWNER_ID, header)
    for p in sellable:
        cur = await asyncio.to_thread(market.price_usd, p.address)
        roi = ((cur / p.entry_price_usd) - 1) * 100 if cur and p.entry_price_usd else 0.0
        text, buttons = _sell_button(p.address, p.chain, p.symbol, f"{roi:+.0f}%")
        await bot_client.send_message(config.TG_OWNER_ID, text, buttons=buttons)
    return True


def _wallet_help() -> str:
    """How to see/sell your wallet when the auto-scan provider isn't available."""
    owner = executor.wallet_address("bsc") or "your_wallet"
    return (
        "🔍 Wallet auto-scan needs a paid BSC data plan, so it's off. "
        "You don't need it — here's how to see and sell everything:\n"
        "• */positions* — every token the bot bought, with P&L\n"
        "• */sell 0x…* — sell any token by pasting its contract address\n"
        f"• Full list in your browser: bscscan.com/address/{owner} → *Token Holdings*"
    )


@bot_client.on(events.NewMessage(pattern=r"^/scan", from_users=config.TG_OWNER_ID))
async def on_scan_cmd(event):
    """Auto-list wallet tokens if a working explorer key is set; else guide."""
    owner = executor.wallet_address("bsc")
    if not config.BSCSCAN_API_KEY or not owner:
        await event.reply(_wallet_help())
        return
    await event.reply("🔍 Scanning your wallet on BSC…")
    try:
        held = await asyncio.to_thread(wallet_scan.held_tokens, chains.get("bsc"), owner)
    except Exception:  # noqa: BLE001 — key rejected / chain not on plan
        await event.reply(_wallet_help())
        return
    if not held:
        await event.reply("No tokens with a balance found on BSC. (Try /positions.)")
        return
    for h in held:
        amt = h["raw"] / (10 ** h["decimals"])
        text, buttons = _sell_button(h["address"], "bsc", h["symbol"], f"balance: {amt:.4g}")
        await bot_client.send_message(config.TG_OWNER_ID, text, buttons=buttons)


async def _wallet_report() -> str:
    """Overall wallet value: native coin + token holdings, in USD."""
    chain = chains.get("bsc")
    lines = ["💼 *Wallet value* (BSC)\n"]
    total = 0.0
    try:
        bnb = await asyncio.to_thread(executor.native_balance, "bsc")
        bnb_px = await asyncio.to_thread(executor.native_price, chain) or 0.0
        val = bnb * bnb_px
        total += val
        lines.append(f"• {bnb:.4g} {chain.native_symbol} → ${val:.2f}")
    except Exception:  # noqa: BLE001
        lines.append(f"• {chain.native_symbol}: balance unavailable")

    holdings = []
    if config.BSCSCAN_API_KEY:
        owner = executor.wallet_address("bsc")
        if owner:
            try:
                holdings = await asyncio.to_thread(wallet_scan.held_tokens, chain, owner)
            except Exception:  # noqa: BLE001
                holdings = []
    for h in holdings:
        px = await asyncio.to_thread(market.price_usd, h["address"])
        amt = h["raw"] / (10 ** h["decimals"])
        val = amt * px if px else 0.0
        total += val
        lines.append(f"• {amt:.4g} {h['symbol']} → ${val:.2f}" + ("" if px else " (no price)"))
    if not config.BSCSCAN_API_KEY:
        lines.append("_add BSCSCAN_API_KEY to include token holdings_")

    lines.append(f"\n💰 *Total: ~${total:.2f}*")
    return "\n".join(lines)


def _menu_title() -> str:
    state = "⏸ PAUSED" if _state["paused"] else "▶️ active"
    mode = f"{'DRY_RUN' if config.DRY_RUN else 'LIVE'} · {'AUTO' if config.AUTO_TRADE else 'manual'}"
    tp = (f"TP {config.TAKE_PROFIT_SELL_PCT:.0f}%@{config.TAKE_PROFIT_MULT:g}x"
          if config.TAKE_PROFIT_ENABLED else "manual sell")
    return (f"🤖 *Signal bot* — {state}\n"
            f"{mode} · ${config.BUY_AMOUNT_USD:g}/buy · {tp}\n"
            f"Pick an action:")


def _menu_markup():
    toggle = "▶️ Resume" if _state["paused"] else "⏸ Pause"
    return [
        [Button.inline("📊 Positions", b"menu:positions"),
         Button.inline("💼 Wallet", b"menu:wallet")],
        [Button.inline("💸 Sell", b"menu:sell"),
         Button.inline("🔍 Scan", b"menu:scan")],
        [Button.inline(toggle, b"menu:toggle")],
    ]


@bot_client.on(events.NewMessage(pattern=r"^/(menu|start)", from_users=config.TG_OWNER_ID))
async def on_menu(event):
    await event.reply(_menu_title(), buttons=_menu_markup())


@bot_client.on(events.NewMessage(pattern=r"^/wallet", from_users=config.TG_OWNER_ID))
async def on_wallet(event):
    msg = await event.reply("💼 Tallying your wallet…")
    await msg.edit(await _wallet_report())


@bot_client.on(events.NewMessage(pattern=r"^/pause", from_users=config.TG_OWNER_ID))
async def on_pause(event):
    _state["paused"] = True
    await event.reply("⏸ Paused — new calls will be ignored until /resume.")


@bot_client.on(events.NewMessage(pattern=r"^/resume", from_users=config.TG_OWNER_ID))
async def on_resume(event):
    _state["paused"] = False
    await event.reply("▶️ Resumed — watching for calls again.")


@bot_client.on(events.NewMessage(pattern=r"^/diag", from_users=config.TG_OWNER_ID))
async def on_diag(event):
    """/diag 0x… — probe how a token routes (four.meme vs 0x) and why it fails."""
    parts = event.raw_text.split()
    if len(parts) < 2 or not parts[1].lower().startswith("0x"):
        await event.reply("Usage: `/diag 0x<token address>`")
        return
    addr = parts[1].strip().lower()
    await event.reply("🔬 Probing…")
    import fourmeme
    m = await asyncio.to_thread(market.lookup, addr)
    chain = m.chain if m else "bsc"
    try:
        raw, dec = await asyncio.to_thread(executor.token_balance, addr, chain)
    except Exception as e:  # noqa: BLE001
        raw, dec = f"err {e}", "?"
    d = await asyncio.to_thread(fourmeme.diagnose, addr)
    lines = [f"🔬 `{addr}`"]
    lines.append(f"market: {m.chain} · liq ${m.liquidity_usd:,.0f} · ${m.price_usd}" if m
                 else "market: none (DexScreener)")
    lines.append(f"balance raw: {raw} · decimals: {dec}")
    lines.append("four.meme Helper:")
    for k, v in d.items():
        lines.append(f"  • {k}: {v}")
    await event.reply("\n".join(lines))


async def _execute_sell(address: str, chain: str, pct: float, symbol: str) -> str:
    """Sell pct% of the wallet's live on-chain balance of a token."""
    try:
        held, _ = await asyncio.to_thread(executor.token_balance, address, chain)
    except Exception as e:  # noqa: BLE001
        return f"❌ Couldn't read your on-chain balance: {e}"
    if held <= 0:
        return (f"⚠️ 0 {symbol} on-chain right now — nothing to sell. If you *did* buy it, "
                "the RPC may be lagging — try /sell again in a moment.")
    raw = int(held * (pct / 100.0))
    if raw <= 0:
        return "Nothing to sell (amount rounds to zero)."
    try:
        res = await asyncio.to_thread(executor.sell, address, chain, raw)
    except Exception as e:  # noqa: BLE001
        return f"❌ Sell errored: {e}"
    if not res.ok:
        return f"❌ Sell failed: {res.detail}"
    # Reconcile any tracked position for this address.
    for p in positions.load():
        if p.address == address:
            positions.update(p.address, p.opened_at,
                             token_raw=0 if pct >= 100 else max(held - raw, 0),
                             notes=f"sold {pct:.0f}%")
    return f"✅ Sold {pct:.0f}% of {symbol}: {res.detail}"


@bot_client.on(events.CallbackQuery)
async def on_click(event):
    parts = event.data.decode().split(":")
    action = parts[0]

    # ---- menu buttons:  menu:<what> ----
    if action == "menu":
        what = parts[1] if len(parts) >= 2 else ""
        await event.answer()
        if what == "positions":
            await event.respond(await _positions_report())
        elif what == "wallet":
            m = await event.respond("💼 Tallying…")
            await m.edit(await _wallet_report())
        elif what == "sell":
            if not await _send_sell_cards():
                await event.respond("📭 No tracked positions. Use `/sell <address>` or /scan.")
        elif what == "scan":
            await on_scan_cmd(event)
        elif what == "toggle":
            _state["paused"] = not _state["paused"]
            try:
                await event.edit(_menu_title(), buttons=_menu_markup())
            except Exception:  # noqa: BLE001
                await event.respond("⏸ Paused." if _state["paused"] else "▶️ Resumed.")
        return

    # ---- manual sell:  sell:<sid>:<pct> ----
    if action == "sell":
        info = _pending_sell.pop(parts[1], None) if len(parts) >= 3 else None
        if not info:
            await event.answer("Expired or already handled.", alert=True)
            return
        pct = float(parts[2])
        await event.edit(f"⏳ Selling {pct:.0f}% of {info['symbol']}…")
        msg = await _execute_sell(info["address"], info["chain"], pct, info["symbol"])
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
        pos = monitor.record_buy(
            v.address, v.chain, v.info.get("symbol") or "?",
            config.BUY_AMOUNT_USD, _entry_price(v), res,
        )
        await event.edit(f"✅ {res.detail}{_buy_tail(pos)}")
    else:
        await event.edit(f"❌ {res.detail}")


async def _register_commands():
    """Populate Telegram's "/" command menu (best-effort)."""
    try:
        from telethon.tl import functions, types
        cmds = [
            ("menu", "Button menu"), ("wallet", "Total wallet value"),
            ("positions", "Open positions"), ("sell", "Sell a position"),
            ("scan", "Scan wallet tokens"), ("pause", "Pause auto-buying"),
            ("resume", "Resume auto-buying"),
        ]
        await bot_client(functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(), lang_code="",
            commands=[types.BotCommand(c, d) for c, d in cmds]))
    except Exception as e:  # noqa: BLE001
        print(f"[commands] could not register menu: {e}")


async def main():
    await bot_client.start(bot_token=config.TG_BOT_TOKEN)
    await user_client.start()  # first run prompts for phone + code
    await _register_commands()

    dry = "DRY_RUN (no real trades)" if config.DRY_RUN else "LIVE — real funds"
    auto = "AUTO buy" if config.AUTO_TRADE else "manual confirm"
    mode = f"{dry} · {auto}"
    sell_bits = []
    if config.TAKE_PROFIT_ENABLED:
        sell_bits.append(f"TP {config.TAKE_PROFIT_SELL_PCT:.0f}%@{config.TAKE_PROFIT_MULT:g}x")
    if config.STOP_LOSS_PCT > 0:
        sell_bits.append(f"stop-loss -{config.STOP_LOSS_PCT:g}%")
    tp = ("Selling: " + " · ".join(sell_bits)) if sell_bits else "Selling: manual only"
    print(f"Bot running: {mode}. Watching {config.TG_CHANNEL_DISPLAY}.")
    await _notify(
        f"🤖 Online. Mode: *{mode}*.\n"
        f"Watching {len(config.TG_CHANNELS)} channel(s): {config.TG_CHANNEL_DISPLAY}.\n"
        f"Buy size ${config.BUY_AMOUNT_USD:g} · {tp}\n"
        f"Send /menu for controls."
    )

    tasks = [
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected(),
    ]
    if config.TAKE_PROFIT_ENABLED or config.STOP_LOSS_PCT > 0:
        tasks.append(monitor.run(_notify))  # background take-profit / stop-loss loop
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
