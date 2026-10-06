"""Pull candidate token addresses out of messy free-form channel messages.

This channel (and others like it) mixes real calls with bot notifications,
referral spam, tweets, and chatter — across multiple chains. We extract two
kinds of candidate address and let the market layer resolve the actual chain:

  * EVM contracts:  0x + 40 hex   (BSC, Ethereum, Arbitrum, Base, …)
  * Solana mints:   base58 that decodes to a 32-byte pubkey

We deliberately do NOT decide the chain here — DexScreener does that from the
address itself, so a gmgn "/robinhood/" or "/bsc/" slug can't mislead us.

Noise handling: obvious non-calls (SpyDefi "Achievement Unlocked" recaps, buy-
bot transaction alerts, "copy my trades" referral spam) rarely contain a full
contract address, so requiring one already drops most of them. We also skip a
message that has no address at all.
"""
from __future__ import annotations

import re

import base58

_EVM = re.compile(r"0x[0-9a-fA-F]{40}\b")
_B58 = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
_TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})")

# Stablecoins / wrapped natives we never want to "buy" if one leaks into text.
_IGNORE = {
    "So11111111111111111111111111111111111111112",  # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC (sol)
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT (sol)
}

# Messages that are clearly not calls even if they somehow contain an address.
_NOISE = re.compile(
    r"achievement unlocked|view call|view stats|copy my trades|copy traders|"
    r"partner\.blofin|1k to \d|→ \$?\d+k challenge",
    re.IGNORECASE,
)


def _is_solana_mint(s: str) -> bool:
    try:
        return len(base58.b58decode(s)) == 32
    except Exception:  # noqa: BLE001
        return False


def _dewrap(text: str) -> str:
    """Stitch an EVM address a Telegram client wrapped across lines/spaces.

    Many "NEW CALL" posts show the contract as `CA:\n0x1234…\n5678…`; the break
    is display wrapping, but it can be a real newline in the text. Scoped to
    0x-prefixed hex runs so it never merges unrelated words.
    """
    out, prev = text, None
    while out != prev:
        prev = out
        out = re.sub(r"(0x[0-9a-fA-F]+)\s+([0-9a-fA-F]+)", r"\1\2", out)
    return out


def extract_candidates(text: str) -> list[str]:
    """Return de-duplicated candidate addresses (EVM + Solana) from a message."""
    if not text or _NOISE.search(text):
        return []

    found: list[str] = []

    # EVM: scan the text as-is AND a de-wrapped copy, so addresses split across
    # two lines (common in "CA:\n0x…" call posts) are still caught.
    for source in (text, _dewrap(text)):
        for a in _EVM.findall(source):
            a = a.lower()
            if a not in found:
                found.append(a)

    # Solana mints: base58, must decode to 32 bytes, and not be an EVM 0x-string.
    for cand in _B58.findall(text):
        if cand.startswith("0x") or cand in found:
            continue
        if _is_solana_mint(cand) and cand not in _IGNORE:
            found.append(cand)

    return found


def extract_ticker(text: str) -> str | None:
    m = _TICKER.search(text or "")
    return m.group(1) if m else None


# Back-compat alias for older imports.
extract_mints = extract_candidates
