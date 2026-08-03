"""Central config, loaded from .env."""
from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()

def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "y"}

# Telegram — reader (user account)
TG_API_ID   = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_CHANNEL  = os.getenv("TG_CHANNEL", "")

# Telegram — confirm/status bot
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_OWNER_ID  = int(os.getenv("TG_OWNER_ID", "0"))

# ── Wallets ────────────────────────────────────────────────────────────
# One EVM key covers ALL EVM chains (BSC/ETH/Arbitrum/Base/…). Separate
# Solana key for Solana. Use dedicated throwaway wallets only.
EVM_PRIVATE_KEY    = os.getenv("EVM_PRIVATE_KEY", "")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "")
# Solana RPC (per-EVM-chain RPCs are read by chains.py from *_RPC vars).
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com").rstrip("/")

# ── Aggregators ────────────────────────────────────────────────────────
# EVM swaps: 0x Swap API (multichain). Get a free key at dashboard.0x.org.
ZEROX_API_KEY = os.getenv("ZEROX_API_KEY", "")
# Solana swaps: Jupiter Ultra.
JUPITER_BASE    = os.getenv("JUPITER_BASE", "https://lite-api.jup.ag").rstrip("/")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")

# ── Execution ──────────────────────────────────────────────────────────
DRY_RUN        = _b("DRY_RUN", True)      # true = never actually swaps
AUTO_TRADE     = _b("AUTO_TRADE", False)  # true = buy on pass; false = Buy/Skip button
BUY_AMOUNT_USD = float(os.getenv("BUY_AMOUNT_USD", "25"))  # USD per buy, any chain
SLIPPAGE_BPS      = int(os.getenv("SLIPPAGE_BPS", "300"))   # 3% entries
SELL_SLIPPAGE_BPS = int(os.getenv("SELL_SLIPPAGE_BPS", "500"))  # 5% exits

# ── Take-profit: "sell 50% at 2x / 100% ROI" ───────────────────────────
TAKE_PROFIT_MULT     = float(os.getenv("TAKE_PROFIT_MULT", "2.0"))
TAKE_PROFIT_SELL_PCT = float(os.getenv("TAKE_PROFIT_SELL_PCT", "50"))
POLL_INTERVAL_SEC    = int(os.getenv("POLL_INTERVAL_SEC", "30"))
POSITIONS_FILE       = os.getenv("POSITIONS_FILE", "positions.json")

# ── Safety gate ────────────────────────────────────────────────────────
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
MIN_PAIR_AGE_MIN  = float(os.getenv("MIN_PAIR_AGE_MIN", "0"))
MAX_BUY_TAX_PCT   = float(os.getenv("MAX_BUY_TAX_PCT", "10"))
MAX_SELL_TAX_PCT  = float(os.getenv("MAX_SELL_TAX_PCT", "10"))
REQ_MINT_REVOKED  = _b("REQUIRE_MINT_AUTHORITY_REVOKED", True)
# STRICT_SAFETY=true rejects when security data is missing (safer, but skips
# very fresh calls that aren't indexed yet). Default false so fresh calls pass.
STRICT_SAFETY     = _b("STRICT_SAFETY", False)
