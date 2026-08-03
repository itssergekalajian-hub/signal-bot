"""Central config, loaded from .env."""
from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()

def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "y"}

# Telegram — reader (user)
TG_API_ID   = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_CHANNEL  = os.getenv("TG_CHANNEL", "")

# Telegram — confirm bot
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_OWNER_ID  = int(os.getenv("TG_OWNER_ID", "0"))

# Solana wallet
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "")
# RPC endpoint — needed to read your token balances so the take-profit
# monitor knows how much to sell. Public endpoint works; a paid one (Helius,
# QuickNode) is more reliable under load.
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com").rstrip("/")

# Jupiter
JUPITER_BASE    = os.getenv("JUPITER_BASE", "https://lite-api.jup.ag").rstrip("/")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")

# Execution
DRY_RUN        = _b("DRY_RUN", True)
# AUTO_TRADE=true  -> buy automatically the instant a call clears the safety
#                     gate (what you asked for).
# AUTO_TRADE=false -> the original behaviour: DM you a Buy/Skip button to tap.
# Defaults to false so nothing trades on its own until you opt in.
AUTO_TRADE     = _b("AUTO_TRADE", False)
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", "0.05"))
SLIPPAGE_BPS   = int(os.getenv("SLIPPAGE_BPS", "300"))

# Take-profit — "sell 50% once the call reaches 2x / 100% ROI"
TAKE_PROFIT_MULT     = float(os.getenv("TAKE_PROFIT_MULT", "2.0"))    # 2.0 = 2x
TAKE_PROFIT_SELL_PCT = float(os.getenv("TAKE_PROFIT_SELL_PCT", "50")) # % of tokens to sell
SELL_SLIPPAGE_BPS    = int(os.getenv("SELL_SLIPPAGE_BPS", "500"))     # exits need more room
POLL_INTERVAL_SEC    = int(os.getenv("POLL_INTERVAL_SEC", "30"))      # price-check cadence
POSITIONS_FILE       = os.getenv("POSITIONS_FILE", "positions.json")

# Safety gate
MAX_RUGCHECK_SCORE   = int(os.getenv("MAX_RUGCHECK_SCORE", "2000"))
MIN_LIQUIDITY_USD    = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
MIN_PAIR_AGE_MIN     = float(os.getenv("MIN_PAIR_AGE_MIN", "0"))
REQ_MINT_REVOKED     = _b("REQUIRE_MINT_AUTHORITY_REVOKED", True)
REQ_FREEZE_REVOKED   = _b("REQUIRE_FREEZE_AUTHORITY_REVOKED", True)
MAX_TOP_HOLDER_PCT   = float(os.getenv("MAX_TOP_HOLDER_PCT", "25"))

WSOL_MINT   = "So11111111111111111111111111111111111111112"
LAMPORTS    = 1_000_000_000
