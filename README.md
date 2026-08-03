# Multichain signal bot — detect · resolve chain · safety-check · open · take-profit

Watches a Telegram channel, pulls out **any** token address it posts (EVM *or*
Solana), automatically figures out **which chain** each one lives on, runs it
through a safety gate, then either **auto-buys** it or DMs you a **Buy / Skip**
button. Every position it opens is tracked, and **50% is sold automatically once
the token reaches 2x (100% ROI)**.

```
channel message → extract addresses → resolve chain (DexScreener) → safety gate ─┬─ AUTO_TRADE=true  → buy now
                                                                                 └─ AUTO_TRADE=false → DM Buy/Skip → buy
                                                                                                                     │
                                              open position (any chain) ──────────────────────────────────────────┘
                                                     │
                              monitor re-prices every 30s → hits 2x? → sell 50% → keep the moonbag
```

## Chains

The bot never trusts a link slug (a `gmgn.ai/robinhood/…` or `/bsc/…` prefix)
to decide the chain — it resolves the chain from the **address itself** via
DexScreener, then routes to the right DEX:

| Family | Chains | Swaps via | Wallet |
|---|---|---|---|
| EVM | BNB Chain, Ethereum, Arbitrum, Base, Polygon, Optimism, Avalanche | 0x aggregator | one `EVM_PRIVATE_KEY` for all of them |
| Solana | Solana | Jupiter Ultra | `SOLANA_PRIVATE_KEY` |

Adding another EVM chain is one row in `chains.py`. A token on a chain the bot
doesn't support is safely skipped, not traded blindly.

## Read this first (money at risk)

- **Most memecoin calls go to zero.** The safety gate blocks honeypots, high-tax
  tokens, and dead liquidity — it's a scam filter, **not** an edge.
- **`AUTO_TRADE=true` spends real funds with no human in the loop.** It buys
  every call that passes the gate, good or bad. Start with it `false`.
- **Use dedicated throwaway wallets** funded with only what you can lose. Never
  put a main wallet's key in `.env` — it sits in plaintext on this machine.
- **Keep `DRY_RUN=true`** until you've watched it resolve, score, buy, and
  take-profit against real messages and you trust every step.
- The take-profit only ever *sells* — there's no stop-loss. The 50% moonbag can
  still go to zero.
- Following "aped X" posts means buying *after* the caller did — you can be their
  exit liquidity. Size accordingly.
- Not financial advice. You own every trade it makes for you.

## Setup

1. `pip install -r requirements.txt`
   (Trading Solana only? You can skip `web3`. Trading EVM only? You can skip
   `solders`.)
2. `cp .env.example .env` and fill it in:
   - **`TG_API_ID` / `TG_API_HASH`** — https://my.telegram.org (a user account,
     not a bot, is required to read a channel).
   - **`TG_BOT_TOKEN`** — from @BotFather; **DM your new bot once**.
   - **`TG_OWNER_ID`** — your numeric id from @userinfobot.
   - **`EVM_PRIVATE_KEY`** and/or **`SOLANA_PRIVATE_KEY`** — throwaway wallets.
   - **`ZEROX_API_KEY`** — free from https://dashboard.0x.org (for EVM swaps).
   - **`TG_CHANNEL`** — the channel to watch.
3. `python main.py` — first run asks for your phone + login code.

## Going from "watch" to "fully automatic" (do this in order)

Two independent switches. Turn them on one at a time so you always understand
what the bot is doing before real money moves.

| Stage | `DRY_RUN` | `AUTO_TRADE` | What happens |
|---|---|---|---|
| 1. Watch | `true` | `false` | DMs you Buy/Skip; buy + 2x take-profit are simulated |
| 2. Auto-sim | `true` | `true` | Opens every passing call on its own, still simulated |
| 3. Live | `false` | `false`→`true` | Real funds. One manual buy first, then flip to hands-off |

At Stage 3, fund each chain's wallet with a **small** amount and native gas
(BNB on BSC, ETH on Arbitrum/Base, SOL on Solana) before going hands-off.

## How the take-profit works

- On open, the bot records the **entry price** (DexScreener) into
  `positions.json`.
- A background loop re-prices every open position every `POLL_INTERVAL_SEC`.
  When `current / entry ≥ TAKE_PROFIT_MULT` (default `2.0`) it sells
  `TAKE_PROFIT_SELL_PCT` (default 50%) back to the chain's native coin —
  **exactly once** per position — and leaves the rest running.
- Live mode reads your **actual on-chain balance** at sell time, so it always
  sells 50% of what you really hold.
- "As per the signal channel" is a fixed, configurable rule (2x → 50%), **not**
  driven by the channel posting a "take profit" message — these channels call
  entries, not exits. Tune `TAKE_PROFIT_MULT` / `TAKE_PROFIT_SELL_PCT`.

## Telegram commands

DM these to your bot:

- **/positions** — your open holdings: ROI, current value, and whether the 2x
  trim has fired, plus total open value.
- **/help** — quick command list and the current mode/settings.

## Gas guard

A buy is skipped when estimated gas would eat too much of the position —
`gas > MAX_GAS_PCT` of `BUY_AMOUNT_USD` (default 25%), or an optional hard cap
`MAX_GAS_USD`. It runs in DRY_RUN too, so you'll see `⛽ skipped` messages while
simulating. This mostly bites on Ethereum mainnet; BSC/Base/Solana rarely trip
it. **Sells are never gas-guarded** — the bot must always be able to exit.

## Files

- `main.py` — orchestration (channel reader + auto/confirm buy + monitor task)
- `extractor.py` — finds EVM + Solana addresses, drops bot/referral noise
- `market.py` — DexScreener: resolves chain + price + liquidity for any address
- `chains.py` — the chain registry (add a chain here)
- `safety.py` — DexScreener + GoPlus → pass/fail verdict, any chain
- `executor.py` — dispatcher: USD sizing + DRY_RUN, routes to the adapter
- `evm_executor.py` — 0x aggregator swaps + web3 signing (all EVM chains)
- `solana_executor.py` — Jupiter Ultra swaps + balance reads (Solana)
- `positions.py` — open-position ledger, persisted to `positions.json`
- `monitor.py` — background loop: re-price positions, sell 50% at 2x
- `gas.py` — estimate swap gas in USD; gate buys so fees don't eat the position
- `config.py` — env-driven settings

## Notes / verify against live APIs

- The EVM live-swap path (0x quote → approve → sign → send) is standard but
  **swap it a tiny amount first** on one chain before trusting it hands-off;
  gas/nonce/approval behavior varies by chain and RPC.
- GoPlus field names differ slightly across EVM vs Solana; `safety.py` reads
  them defensively. Eyeball one real response and adjust thresholds.
- `positions.json` is your holdings ledger — git-ignored, survives restarts.
  Stop the bot before hand-editing it.
- Consider a per-day spend cap and a stop-loss (neither is built in yet). Gas
  guarding and a /positions view are built in.
