"""Open positions, persisted to a JSON file so a restart doesn't lose them.

A position is opened when we buy and closed (or trimmed) by the take-profit
monitor. We store the entry price so ROI is just current/entry, and a
`tp1_done` flag so the 2x take-profit fires exactly once per position.

This is intentionally a small, human-readable file — open positions.json any
time to see what the bot is holding.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

import config

_LOCK_NOTE = "edited while the bot runs? stop the bot first — it rewrites this file."


@dataclass
class Position:
    address: str                    # token contract / mint
    chain: str                      # resolved chain, e.g. "bsc", "solana"
    symbol: str
    entry_price_usd: float          # USD price at buy time
    amount_usd: float               # USD spent opening it
    token_raw: int                  # base-unit tokens acquired (dry-run estimate or quote)
    decimals: int
    opened_at: float                # unix seconds
    tp1_done: bool = False          # has the 2x / 50% take-profit already fired?
    dry_run: bool = True            # was this opened in DRY_RUN?
    notes: str = ""


def _load_raw() -> list[dict]:
    if not os.path.exists(config.POSITIONS_FILE):
        return []
    try:
        with open(config.POSITIONS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_raw(rows: list[dict]) -> None:
    tmp = config.POSITIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f, indent=2)
    os.replace(tmp, config.POSITIONS_FILE)  # atomic-ish write


def load() -> list[Position]:
    return [Position(**row) for row in _load_raw()]


def save(positions: list[Position]) -> None:
    _save_raw([asdict(p) for p in positions])


def add(pos: Position) -> None:
    positions = load()
    positions.append(pos)
    save(positions)


def open_positions() -> list[Position]:
    """Positions that still hold tokens and haven't been fully closed."""
    return [p for p in load() if p.token_raw > 0]


def update(address: str, opened_at: float, **changes) -> None:
    """Update the single position identified by (address, opened_at)."""
    positions = load()
    for p in positions:
        if p.address == address and p.opened_at == opened_at:
            for k, v in changes.items():
                setattr(p, k, v)
    save(positions)
