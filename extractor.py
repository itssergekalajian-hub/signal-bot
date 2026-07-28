"""Pull candidate Solana token mints out of messy free-form messages.

The channel mixes real calls with bot notifications, ads, tweets, EVM-chain
links, and chatter. We look for Solana mints while actively rejecting the
EVM-address noise ('0x...') this particular channel is full of.

Detection:
  1. Strip EVM addresses (0x + 40 hex) entirely, so they can't leak in.
  2. Grab mints from Solana-context links (gmgn /sol, dexscreener /solana,
     pump.fun, birdeye, solscan, jup.ag).
  3. Grab bare base58 strings that decode to a valid 32-byte pubkey.

We DON'T judge conviction here — the safety layer + your manual confirm gate
the actual buy. This just finds addresses worth checking.
"""
from __future__ import annotations

import re

import base58

_EVM = re.compile(r"0x[0-9a-fA-F]{40}")
_B58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
_HEXONLY = re.compile(r"^[0-9a-fA-F]+$")

# Mint inside a Solana-context link only
_LINK_MINT = re.compile(
    r"(?:"
    r"gmgn\.ai/sol/[^\s]*?|"
    r"dexscreener\.com/solana/[^\s]*?|"
    r"pump\.fun/[^\s]*?|"
    r"birdeye\.so/[^\s]*?|"
    r"solscan\.io/[^\s]*?|"
    r"jup\.ag/[^\s]*?"
    r")([1-9A-HJ-NP-Za-km-z]{32,44})",
    re.IGNORECASE,
)

_IGNORE = {
    "So11111111111111111111111111111111111111112",  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def _is_valid_mint(s: str) -> bool:
    if _HEXONLY.match(s):            # pure hex => EVM-style, never a Solana mint
        return False
    try:
        return len(base58.b58decode(s)) == 32
    except Exception:  # noqa: BLE001
        return False


def extract_mints(text: str) -> list[str]:
    if not text:
        return []
    text = _EVM.sub(" ", text)      # remove EVM addresses wherever they appear
    found: list[str] = []

    for m in _LINK_MINT.findall(text):
        if m not in found and _is_valid_mint(m):
            found.append(m)

    for cand in _B58.findall(text):
        if cand not in found and _is_valid_mint(cand):
            found.append(cand)

    return [m for m in found if m not in _IGNORE]
