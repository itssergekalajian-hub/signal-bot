"""Safety layer. Runs BEFORE you ever see a Buy button.

Two independent sources:
  - RugCheck  (api.rugcheck.xyz/v1) — mint/freeze authority, holder concentration,
    known risk flags, an overall risk score.
  - DexScreener (api.dexscreener.com) — liquidity, volume, pair age, price.

Philosophy: FAIL CLOSED. If a check errors or data is missing, treat the token
as unsafe rather than waving it through. A missed opportunity costs nothing; a
honeypot costs your whole position.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

import config

_TIMEOUT = 8


@dataclass
class Verdict:
    mint: str
    passed: bool
    reasons: list[str] = field(default_factory=list)   # why it failed
    info: dict = field(default_factory=dict)           # display data


def _rugcheck(mint: str) -> dict:
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
    r = requests.get(url, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _dexscreener(mint: str) -> dict:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    r = requests.get(url, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    pairs = data.get("pairs") or []
    if not pairs:
        return {}
    # Use the deepest-liquidity pair as the reference market
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def evaluate(mint: str) -> Verdict:
    v = Verdict(mint=mint, passed=True)

    # ---- RugCheck ----
    try:
        rc = _rugcheck(mint)
    except Exception as e:  # noqa: BLE001
        v.passed = False
        v.reasons.append(f"RugCheck unavailable ({e}) — failing closed")
        return v

    score = rc.get("score_normalised", rc.get("score"))
    v.info["rugcheck_score"] = score
    risks = rc.get("risks") or []
    v.info["risks"] = [r.get("name") for r in risks if isinstance(r, dict)]

    if score is not None and score > config.MAX_RUGCHECK_SCORE:
        v.passed = False
        v.reasons.append(f"RugCheck score {score} > {config.MAX_RUGCHECK_SCORE}")

    # Authority flags — names vary, so match loosely against risk labels
    labels = " ".join(v.info["risks"]).lower()
    if config.REQ_MINT_REVOKED and "mint authority" in labels:
        v.passed = False
        v.reasons.append("mint authority still enabled")
    if config.REQ_FREEZE_REVOKED and "freeze authority" in labels:
        v.passed = False
        v.reasons.append("freeze authority still enabled")

    # Top holder concentration if RugCheck exposes it
    top = rc.get("topHolders") or []
    if top:
        top_pct = max((h.get("pct") or 0) for h in top)
        v.info["top_holder_pct"] = round(top_pct, 1)
        if top_pct > config.MAX_TOP_HOLDER_PCT:
            v.passed = False
            v.reasons.append(f"top holder {top_pct:.0f}% > {config.MAX_TOP_HOLDER_PCT:.0f}%")

    # ---- DexScreener ----
    try:
        pair = _dexscreener(mint)
    except Exception as e:  # noqa: BLE001
        v.passed = False
        v.reasons.append(f"DexScreener unavailable ({e}) — failing closed")
        return v

    if not pair:
        v.passed = False
        v.reasons.append("no DEX pair found (nothing to trade / can't price)")
        return v

    liq = (pair.get("liquidity") or {}).get("usd") or 0
    v.info["liquidity_usd"] = round(liq)
    v.info["symbol"] = (pair.get("baseToken") or {}).get("symbol")
    v.info["price_usd"] = pair.get("priceUsd")
    if liq < config.MIN_LIQUIDITY_USD:
        v.passed = False
        v.reasons.append(f"liquidity ${liq:,.0f} < ${config.MIN_LIQUIDITY_USD:,.0f}")

    created_ms = pair.get("pairCreatedAt")
    if created_ms:
        age_min = (time.time() - created_ms / 1000) / 60
        v.info["age_min"] = round(age_min)
        if age_min < config.MIN_PAIR_AGE_MIN:
            v.passed = False
            v.reasons.append(f"pair age {age_min:.0f}m < {config.MIN_PAIR_AGE_MIN:.0f}m")

    return v
