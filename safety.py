"""Safety layer. Runs BEFORE any buy, on whatever chain the token lives on.

Two multichain sources:
  - DexScreener (via market.lookup) — resolves the chain, then gives liquidity,
    price and pair age. Works for every supported chain.
  - GoPlus Security — honeypot / buy-sell tax / mintable / holder checks.
    EVM:    /token_security/<numeric_chain_id>
    Solana: /solana/token_security

Philosophy: FAIL CLOSED on the things that make a token untradeable — no
liquidity, no market, or a confirmed honeypot. For "data simply not indexed
yet" (common on brand-new calls you actually want to ape), we do NOT fail
closed unless STRICT_SAFETY is set, so fresh calls still reach you.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import requests

import chains
import config
import market

_TIMEOUT = 8
_GOPLUS = "https://api.gopluslabs.io/api/v1"


@dataclass
class Verdict:
    address: str
    chain: str = ""
    passed: bool = True
    reasons: list[str] = field(default_factory=list)   # why it failed
    info: dict = field(default_factory=dict)            # display data


def _goplus_evm(numeric_chain_id: int, address: str) -> dict | None:
    url = f"{_GOPLUS}/token_security/{numeric_chain_id}"
    r = requests.get(url, params={"contract_addresses": address}, timeout=_TIMEOUT)
    r.raise_for_status()
    result = (r.json().get("result") or {})
    return result.get(address.lower())


def _goplus_solana(address: str) -> dict | None:
    url = f"{_GOPLUS}/solana/token_security"
    r = requests.get(url, params={"contract_addresses": address}, timeout=_TIMEOUT)
    r.raise_for_status()
    result = (r.json().get("result") or {})
    return result.get(address)


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _check_security(v: Verdict, chain: chains.Chain) -> None:
    """Populate v with GoPlus findings; reject on hard danger flags."""
    try:
        if chain.family == "evm":
            data = _goplus_evm(chain.evm_chain_id, v.address)
        else:
            data = _goplus_solana(v.address)
    except Exception as e:  # noqa: BLE001
        if config.STRICT_SAFETY:
            v.passed = False
            v.reasons.append(f"security check unavailable ({e}) — failing closed")
        return

    if not data:
        if config.STRICT_SAFETY:
            v.passed = False
            v.reasons.append("no security data (token too new?) — failing closed")
        return

    # Field names differ a little across EVM/Solana; read defensively.
    honeypot = str(data.get("is_honeypot", data.get("honeypot", "0"))) == "1"
    buy_tax = _f(data.get("buy_tax")) * 100
    sell_tax = _f(data.get("sell_tax")) * 100
    mintable = str(data.get("is_mintable", "0")) == "1"

    v.info["buy_tax_pct"] = round(buy_tax, 1)
    v.info["sell_tax_pct"] = round(sell_tax, 1)

    if honeypot or str(data.get("cannot_sell_all", "0")) == "1":
        v.passed = False
        v.reasons.append("honeypot — you couldn't sell")
    if sell_tax > config.MAX_SELL_TAX_PCT:
        v.passed = False
        v.reasons.append(f"sell tax {sell_tax:.0f}% > {config.MAX_SELL_TAX_PCT:.0f}%")
    if buy_tax > config.MAX_BUY_TAX_PCT:
        v.passed = False
        v.reasons.append(f"buy tax {buy_tax:.0f}% > {config.MAX_BUY_TAX_PCT:.0f}%")
    if config.REQ_MINT_REVOKED and mintable:
        v.passed = False
        v.reasons.append("mint authority still enabled")


def evaluate(address: str) -> Verdict:
    v = Verdict(address=address)

    # ---- Resolve chain + market (fail closed if nothing trades) ----
    m = market.lookup(address)
    if not m:
        v.passed = False
        v.reasons.append("no market found on any chain (can't price / can't trade)")
        return v

    v.chain = m.chain
    v.info.update(
        symbol=m.symbol,
        price_usd=m.price_usd,
        liquidity_usd=round(m.liquidity_usd),
        age_min=round(m.age_min) if m.age_min is not None else None,
    )

    chain = chains.get(m.chain)
    if not chain:
        v.passed = False
        v.reasons.append(f"chain '{m.chain}' not supported by this bot")
        return v

    if m.liquidity_usd < config.MIN_LIQUIDITY_USD:
        v.passed = False
        v.reasons.append(f"liquidity ${m.liquidity_usd:,.0f} < ${config.MIN_LIQUIDITY_USD:,.0f}")

    if m.age_min is not None and m.age_min < config.MIN_PAIR_AGE_MIN:
        v.passed = False
        v.reasons.append(f"pair age {m.age_min:.0f}m < {config.MIN_PAIR_AGE_MIN:.0f}m")

    # ---- Token security (mintable / holder flags via GoPlus) ----
    _check_security(v, chain)

    # ---- Sellability: prove we could actually EXIT (anti-honeypot). Runs last
    #      so its simulation-based tax figures win over GoPlus metadata. ----
    _check_sellability(v, chain)

    return v


def _check_sellability(v: Verdict, chain: chains.Chain) -> None:
    """The biggest anti-honeypot lever: prove the token can be sold, not just bought."""
    if not config.HONEYPOT_CHECK:
        return

    # four.meme on-curve tokens have no DEX pool yet -> use four.meme's own
    # quotes (trySell to prove sellability, tryBuy to confirm it's a curve token).
    if chain.key == "bsc":
        import fourmeme
        ok, tax, _reason = fourmeme.sellable(v.address)
        if ok:  # it's a four.meme curve token AND sellable
            if tax is not None:
                v.info["sell_tax_pct"] = round(tax, 1)
                if tax > config.MAX_HONEYPOT_TAX_PCT:
                    v.passed = False
                    v.reasons.append(f"sell tax {tax:.0f}% > {config.MAX_HONEYPOT_TAX_PCT:.0f}%")
            return
        if fourmeme.buyable(v.address):  # four.meme token but can't sell -> honeypot
            v.passed = False
            v.reasons.append("four.meme: can't sell it back (honeypot)")
            return
        # else: not a four.meme token -> fall through to the DEX honeypot check

    # DEX / graduated tokens -> honeypot.is simulates a real buy+sell.
    import honeypot
    res = honeypot.check(chain, v.address)
    if res is None or not res["sim_ok"]:
        if config.STRICT_SAFETY:
            v.passed = False
            v.reasons.append("couldn't simulate a sell — failing closed")
        return
    if res["is_honeypot"]:
        v.passed = False
        v.reasons.append("HONEYPOT — simulated sell failed (you couldn't exit)")
    v.info["buy_tax_pct"] = round(res["buy_tax"], 1)
    v.info["sell_tax_pct"] = round(res["sell_tax"], 1)
    if res["sell_tax"] > config.MAX_HONEYPOT_TAX_PCT:
        v.passed = False
        v.reasons.append(f"sell tax {res['sell_tax']:.0f}% > {config.MAX_HONEYPOT_TAX_PCT:.0f}%")
