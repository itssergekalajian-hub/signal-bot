"""Shared result type for a swap, used by every execution adapter."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SwapResult:
    ok: bool
    detail: str
    signature: str | None = None       # tx hash / signature
    in_amount: int | None = None       # raw base units sent
    out_amount: int | None = None      # raw base units received (quote/estimate)
