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

def _channels(raw: str) -> list:
    """TG_CHANNEL may be ONE or SEVERAL channels, comma-separated. Each entry is
    a public '@name' (str) or a private numeric id like -1001234567890 (int, so
    Telethon resolves it). Watching Mark + a private channel: '@MarkDegens,-100…'.
    """
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            out.append(part)
    return out

TG_CHANNELS = _channels(os.getenv("TG_CHANNEL", ""))
TG_CHANNEL_DISPLAY = ", ".join(str(c) for c in TG_CHANNELS) or "(none)"

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
# Explorer key for scanning the wallet's actual token holdings (/scan, /wallet).
# Get a free key at https://bscscan.com/myapikey (BscScan is run by Etherscan,
# so an etherscan.io key works too — either is accepted).
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "") or os.getenv("ETHERSCAN_API_KEY", "")
# Solana swaps: Jupiter Ultra.
JUPITER_BASE    = os.getenv("JUPITER_BASE", "https://lite-api.jup.ag").rstrip("/")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")

# ── Execution ──────────────────────────────────────────────────────────
DRY_RUN        = _b("DRY_RUN", True)      # true = never actually swaps
AUTO_TRADE     = _b("AUTO_TRADE", False)  # true = buy on pass; false = Buy/Skip button
BUY_AMOUNT_USD = float(os.getenv("BUY_AMOUNT_USD", "10"))  # USD per buy, any chain
SLIPPAGE_BPS      = int(os.getenv("SLIPPAGE_BPS", "300"))   # 3% entries
SELL_SLIPPAGE_BPS = int(os.getenv("SELL_SLIPPAGE_BPS", "1000"))  # 10% exits (tax tokens)

# four.meme launchpad (BSC): trade tokens still on the bonding curve, which 0x
# can't reach until they graduate to PancakeSwap. Routed automatically.
FOURMEME_ENABLED       = _b("FOURMEME_ENABLED", True)
FOURMEME_SLIPPAGE_BPS  = int(os.getenv("FOURMEME_SLIPPAGE_BPS", "1500"))  # 15% — curve moves fast

# ── Gas guard: don't let fees eat the position ─────────────────────────
# Skip a BUY when estimated gas exceeds MAX_GAS_PCT of BUY_AMOUNT_USD, or an
# optional hard cap MAX_GAS_USD (0 = no absolute cap). Matters mainly on
# Ethereum mainnet; cheap chains (BSC/Base/Solana) rarely trip it. Sells
# (take-profit exits) are never gas-guarded — we always want to be able to exit.
MAX_GAS_PCT    = float(os.getenv("MAX_GAS_PCT", "25"))       # gas < 25% of buy
MAX_GAS_USD    = float(os.getenv("MAX_GAS_USD", "0"))        # 0 = disabled
SWAP_GAS_UNITS = int(os.getenv("SWAP_GAS_UNITS", "250000"))  # assumed gas/swap

# ── Take-profit: "sell 50% at 2x / 100% ROI" ───────────────────────────
# TAKE_PROFIT_ENABLED=false -> the bot never auto-sells; you exit manually with
# /sell. No take-profit, no stop-loss — buy-only.
TAKE_PROFIT_ENABLED  = _b("TAKE_PROFIT_ENABLED", True)
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

# Sellability protection — the biggest anti-honeypot lever.
#  - DEX/graduated tokens: honeypot.is simulates a real buy+sell.
#  - four.meme on-curve tokens: four.meme's trySell must succeed.
# A token that can't be sold is rejected regardless of STRICT_SAFETY.
HONEYPOT_CHECK       = _b("HONEYPOT_CHECK", True)
MAX_HONEYPOT_TAX_PCT = float(os.getenv("MAX_HONEYPOT_TAX_PCT", "15"))  # reject if sell tax over this
MIN_HOLDERS          = int(os.getenv("MIN_HOLDERS", "0"))              # 0 = off
