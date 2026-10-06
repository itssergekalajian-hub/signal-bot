"""Chain registry — the one place that knows about each supported chain.

Chains are identified by DexScreener's `chainId` string (e.g. "solana",
"bsc", "ethereum", "arbitrum"), because DexScreener is what we use to resolve
an address to a chain in the first place. Each entry says which execution
family it belongs to (evm vs solana) and carries the bits the EVM adapter
needs: numeric chain id (for the 0x aggregator), an RPC URL, and the wrapped
native token (used to price the native coin and as the swap's sell token).

Adding a new EVM chain is just one more row here — nothing else changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Chain:
    key: str            # DexScreener chainId
    name: str           # human label
    family: str         # "evm" | "solana"
    evm_chain_id: int | None       # numeric id for 0x / EVM RPC
    wrapped_native: str            # wrapped-native token address (for pricing)
    native_symbol: str
    rpc_env: str | None            # env var holding this chain's RPC URL
    default_rpc: str | None
    explorer_tx: str               # f-string-ish prefix for a tx link


# 0x uses this pseudo-address to mean "the chain's native coin".
EVM_NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

_CHAINS: dict[str, Chain] = {
    "solana": Chain(
        "solana", "Solana", "solana", None,
        "So11111111111111111111111111111111111111112", "SOL",
        "SOLANA_RPC", "https://api.mainnet-beta.solana.com",
        "https://solscan.io/tx/",
    ),
    "ethereum": Chain(
        "ethereum", "Ethereum", "evm", 1,
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "ETH",
        "ETH_RPC", "https://eth.llamarpc.com",
        "https://etherscan.io/tx/",
    ),
    "bsc": Chain(
        "bsc", "BNB Chain", "evm", 56,
        "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "BNB",
        # Binance's public seed node (proven working here). For best reliability
        # set BSC_RPC to a paid endpoint (QuickNode/Ankr/Alchemy).
        "BSC_RPC", "https://bsc-dataseed.binance.org",
        "https://bscscan.com/tx/",
    ),
    "arbitrum": Chain(
        "arbitrum", "Arbitrum", "evm", 42161,
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "ETH",
        "ARBITRUM_RPC", "https://arb1.arbitrum.io/rpc",
        "https://arbiscan.io/tx/",
    ),
    "base": Chain(
        "base", "Base", "evm", 8453,
        "0x4200000000000000000000000000000000000006", "ETH",
        "BASE_RPC", "https://mainnet.base.org",
        "https://basescan.org/tx/",
    ),
    "polygon": Chain(
        "polygon", "Polygon", "evm", 137,
        "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", "POL",
        "POLYGON_RPC", "https://polygon-rpc.com",
        "https://polygonscan.com/tx/",
    ),
    "optimism": Chain(
        "optimism", "Optimism", "evm", 10,
        "0x4200000000000000000000000000000000000006", "ETH",
        "OPTIMISM_RPC", "https://mainnet.optimism.io",
        "https://optimistic.etherscan.io/tx/",
    ),
    "avalanche": Chain(
        "avalanche", "Avalanche", "evm", 43114,
        "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7", "AVAX",
        "AVALANCHE_RPC", "https://api.avax.network/ext/bc/C/rpc",
        "https://snowtrace.io/tx/",
    ),
}


def get(chain_key: str) -> Chain | None:
    return _CHAINS.get((chain_key or "").lower())


def is_supported(chain_key: str) -> bool:
    return (chain_key or "").lower() in _CHAINS


def rpc_url(chain: Chain) -> str | None:
    if chain.rpc_env and os.getenv(chain.rpc_env):
        return os.getenv(chain.rpc_env).rstrip("/")
    return chain.default_rpc


def supported_keys() -> list[str]:
    return list(_CHAINS)
