# SPDX-License-Identifier: Apache-2.0
"""Chain configuration. Two networks matter for tokenized equities.

Base carries the B20 equities and Uniswap v3. Robinhood Chain carries the
issuer's own tokenized stocks and ETFs, and that is where the real depth sits:
NVDA alone shows more 24h volume there than every B20 pool on Base combined.

Nothing here is trusted until it is read off the chain. `chain_id` is checked
against `eth_chainId` on first use, so a wrong endpoint fails loudly instead of
quietly serving another network's state.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chain:
    key: str
    name: str
    chain_id: int
    endpoints: tuple
    explorer: str
    native: str
    quote_symbol: str          # the stablecoin trades settle against here
    quote_address: str
    quote_decimals: int
    flash_key: str | None = None      # Definitive Flash's name for this chain
    v3_factory: str | None = None     # Uniswap v3, where deployed
    swap_router: str | None = None


BASE = Chain(
    key="base",
    name="Base",
    chain_id=8453,
    endpoints=(
        "https://mainnet.base.org",
        "https://base-rpc.publicnode.com",
        "https://base.drpc.org",
        "https://1rpc.io/base",
    ),
    explorer="https://basescan.org",
    native="ETH",
    quote_symbol="USDC",
    quote_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    quote_decimals=6,
    flash_key="base",
    v3_factory="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
    swap_router="0x2626664c2603336E57B271c5C0b26F421741e481",
)

ROBINHOOD = Chain(
    key="robinhood",
    name="Robinhood Chain",
    chain_id=4663,
    endpoints=(
        "https://robinhood-rpc.publicnode.com",
        "https://rpc.mainnet.chain.robinhood.com",
        "https://rpc.arrowrpc.com",
    ),
    explorer="https://robinscan.io",
    native="ETH",
    quote_symbol="USDG",
    quote_address="0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
    quote_decimals=6,
    flash_key="robinhood",
    v3_factory=None,        # no Uniswap v3 deployment read here
    swap_router=None,
)

CHAINS = {c.key: c for c in (BASE, ROBINHOOD)}
DEFAULT = ROBINHOOD


def get(key: str) -> Chain:
    if key not in CHAINS:
        raise KeyError(f"unknown chain {key!r}, known: {sorted(CHAINS)}")
    return CHAINS[key]
