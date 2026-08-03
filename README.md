# Solana signal bot — detect · safety-check · open · take-profit

Watches a Telegram channel, pulls out Solana token mints, runs each through
**RugCheck + DexScreener**, and either **auto-buys** it or DMs you a **Buy /
Skip** button. Every position it opens is then tracked, and **50% is sold
automatically once the token reaches 2x (100% ROI)**. All swaps go through
**Jupiter Ultra**.

```
channel message → extract mints → safety gate ─┬─ AUTO_TRADE=true  → buy now
                                               └─ AUTO_TRADE=false → DM Buy/Skip → (tap) → buy
                                                                                            │
                                     open position ────────────────────────────────────────┘
                                            │
                     monitor re-prices every 30s → hits 2x? → sell 50% → keep the moonbag
```

## Read this first (money at risk)

- **Most memecoin calls go to zero.** The safety gate blocks obvious honeypots
  and rugs; it does **not** make a token a good bet. It's a scam filter, not an
  edge.
- **`AUTO_TRADE=true` spends real SOL with no human in the loop.** It will buy
  every call that passes the gate, including bad ones. Start with it `false`,
  and even when you turn it on, keep `DRY_RUN=true` first (below).
- **Use a dedicated throwaway wallet** funded with only what you're willing to
  lose. Never put your main wallet's private key in `.env`. The key sits in
  plaintext on the machine running this.
- **Keep `DRY_RUN=true`** until you've watched it parse, score, buy, and
  take-profit against real messages for a while and you trust every step.
- The take-profit only ever *sells* — it never adds a stop-loss. The other half
  (the "moonbag") can still go to zero. Add a stop-loss yourself if you want one.
- Following "aped X" posts means buying *after* the caller already did — you can
  be their exit liquidity. Size accordingly.
- Not financial advice. You own every trade this makes on your behalf.

## Setup

1. `pip install -r requirements.txt`
2. `cp .env.example .env` and fill it in:
   - **`TG_API_ID` / `TG_API_HASH`** — from https://my.telegram.org (needed to
     read a channel you're only a member of; a bot can't do that).
   - **`TG_BOT_TOKEN`** — make a bot via @BotFather. **DM your new bot once** so
     it's allowed to message you.
   - **`TG_OWNER_ID`** — your numeric id from @userinfobot.
   - **`SOLANA_PRIVATE_KEY`** — base58 secret of your throwaway wallet.
   - **`TG_CHANNEL`** — the channel to watch.
3. `python main.py`
   - First run asks for your phone + login code (creates `user_session`).
   - You'll get an "🤖 Online" DM showing the mode. Leave it running.

## Going from "watch" to "fully automatic" (do this in order)

The bot has two independent switches. Turn them on one at a time so you always
understand what it's doing before real money moves:

1. **Watch only.** `DRY_RUN=true`, `AUTO_TRADE=false`. It DMs you Buy/Skip
   buttons and simulates the buy + the 2x take-profit when you tap. Watch it for
   a day. Confirm the calls it surfaces are the ones you'd actually take.
2. **Auto, still simulated.** `DRY_RUN=true`, `AUTO_TRADE=true`. Now it "opens"
   every passing call on its own and simulates trimming 50% at 2x. You'll get
   the full sequence of DMs (`⏳ Opening…` → `✅` → `🎯 Take-profit hit` →
   `✅ Trimmed`) without spending anything. This is where you tune the safety
   gate and `BUY_AMOUNT_SOL`.
3. **Live.** `DRY_RUN=false`. Fund the throwaway wallet with a *small* amount
   first. Leave `AUTO_TRADE=false` for one real manual tap to confirm a real
   buy + real take-profit land, *then* flip `AUTO_TRADE=true` for hands-off.

## How the take-profit works

- When a position opens, the bot records the **entry price** (DexScreener) and
  writes it to `positions.json`.
- A background loop re-prices every open position every `POLL_INTERVAL_SEC`
  (default 30s). When `current / entry ≥ TAKE_PROFIT_MULT` (default `2.0` = 2x =
  +100% ROI) it sells `TAKE_PROFIT_SELL_PCT` (default 50%) of the tokens back to
  SOL — **exactly once** per position — and leaves the rest running.
- In live mode it reads your actual on-chain balance (via `SOLANA_RPC`) at sell
  time, so it always sells 50% of what you *really* hold.
- `positions.json` is your ledger of open holdings. It's git-ignored and
  survives restarts. Stop the bot before hand-editing it.
- "As per the signal channel" is implemented as a fixed, configurable rule
  (2x → 50%), **not** by waiting for the channel to post a "take profit"
  message. Change `TAKE_PROFIT_MULT` / `TAKE_PROFIT_SELL_PCT` to match your
  channel's convention.

## Tuning the safety gate (`.env`)

| Setting | What it does |
|---|---|
| `MAX_RUGCHECK_SCORE` | Reject above this RugCheck risk score (lower = stricter) |
| `MIN_LIQUIDITY_USD` | Reject thin pools you couldn't exit |
| `MIN_PAIR_AGE_MIN` | Reject brand-new pairs (set >0 to avoid 0-second snipes) |
| `REQUIRE_MINT_AUTHORITY_REVOKED` | Reject if creator can still mint supply |
| `REQUIRE_FREEZE_AUTHORITY_REVOKED` | Reject if creator can freeze your tokens |
| `MAX_TOP_HOLDER_PCT` | Reject if one wallet holds more than this % |
| `BUY_AMOUNT_SOL` | Fixed size per approved buy |
| `SLIPPAGE_BPS` | Max slippage (300 = 3%) |

The gate **fails closed**: if RugCheck or DexScreener errors or returns nothing,
the token is rejected rather than waved through.

## Files

- `main.py` — orchestration (channel reader + auto/confirm buy + monitor task)
- `extractor.py` — finds Solana mints, strips EVM/stablecoin noise
- `safety.py` — RugCheck + DexScreener → pass/fail verdict
- `executor.py` — Jupiter Ultra buy **and** sell (order → sign → execute)
- `prices.py` — current USD price of a mint (DexScreener)
- `solana_rpc.py` — reads your on-chain token balance (for the sell)
- `positions.py` — open-position ledger, persisted to `positions.json`
- `monitor.py` — background loop: re-price positions, sell 50% at 2x
- `config.py` — env-driven settings

## Notes / things to verify against live APIs

- Jupiter Ultra base URL is the free `lite-api.jup.ag`; add a key from
  portal.jup.ag and switch `JUPITER_BASE` to `https://api.jup.ag` for higher
  limits. There's also the official `jup-python-sdk` if you'd rather not hand-roll
  the order/execute calls.
- RugCheck's summary JSON field names (`score_normalised`, `risks`, `topHolders`)
  can shift — `safety.py` reads them defensively, but eyeball one real response
  and adjust the thresholds to match what you see.
- Consider adding a per-day spend cap and a dedupe so the same mint reposted
  five times doesn't ping you five times.
