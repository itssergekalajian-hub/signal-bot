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

# Jupiter
JUPITER_BASE    = os.getenv("JUPITER_BASE", "https://lite-api.jup.ag").rstrip("/")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")

# Execution
DRY_RUN        = _b("DRY_RUN", True)
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", "0.05"))
SLIPPAGE_BPS   = int(os.getenv("SLIPPAGE_BPS", "300"))

# Safety gate
MAX_RUGCHECK_SCORE   = int(os.getenv("MAX_RUGCHECK_SCORE", "2000"))
MIN_LIQUIDITY_USD    = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
MIN_PAIR_AGE_MIN     = float(os.getenv("MIN_PAIR_AGE_MIN", "0"))
REQ_MINT_REVOKED     = _b("REQUIRE_MINT_AUTHORITY_REVOKED", True)
REQ_FREEZE_REVOKED   = _b("REQUIRE_FREEZE_AUTHORITY_REVOKED", True)
MAX_TOP_HOLDER_PCT   = float(os.getenv("MAX_TOP_HOLDER_PCT", "25"))

WSOL_MINT   = "So11111111111111111111111111111111111111112"
LAMPORTS    = 1_000_000_000
