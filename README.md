# Solana signal watcher — detect · safety-check · confirm · buy

Watches a Telegram channel, pulls out Solana token mints, runs each through
**RugCheck + DexScreener**, and DMs you a **Buy / Skip** button only for tokens
that clear your safety gate. Nothing buys on its own — you tap to execute, and
the swap goes through **Jupiter Ultra**.

```
channel message → extract mints → safety gate → DM you Buy/Skip → (tap) → Jupiter Ultra buy
```

## Read this first (money at risk)

- **Most memecoin calls go to zero.** The safety gate blocks obvious honeypots
  and rugs; it does **not** make a token a good bet. It's a scam filter, not an
  edge.
- **Use a dedicated throwaway wallet** funded with only what you're willing to
  lose. Never put your main wallet's private key in `.env`. The key sits in
  plaintext on the machine running this.
- **Keep `DRY_RUN=true`** until you've watched it parse and score real messages
  for a while and you trust what reaches the Buy button.
- Following "aped X" posts means buying *after* the caller already did — you can
  be their exit liquidity. Size accordingly.
- Not financial advice. You own every tap.

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
   - You'll get an "🤖 Online" DM. Leave it running.

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

- `main.py` — orchestration (channel reader + confirm bot + button handler)
- `extractor.py` — finds Solana mints, strips EVM/stablecoin noise
- `safety.py` — RugCheck + DexScreener → pass/fail verdict
- `executor.py` — Jupiter Ultra order → sign → execute
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
