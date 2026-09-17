# SPDX-License-Identifier: Apache-2.0
"""Uniswap on Base: pool discovery and spot pricing, read directly from the AMM.

This is the Uniswap integration the Runtime submission points reviewers at. It
talks to the v3 factory and pool contracts on Base with raw eth_call, so it
needs no API key and anyone can re-run it against the same blocks.
"""
from __future__ import annotations

from . import chain

# Read off Base at run time by scripts/verify_addresses.py, never trusted from
# a doc. Both carry code on Base mainnet.
V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
V4_POOL_MANAGER = "0x498581fF718922c3f8e6A244956aF099B2652b2b"

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# v3 fee tiers in hundredths of a bip.
FEE_TIERS = [100, 500, 3000, 10000]

SEL_GET_POOL = "0x1698ee82"      # getPool(address,address,uint24)
SEL_SLOT0 = "0x3850c7bd"         # slot0()
SEL_LIQUIDITY = "0x1a686502"     # liquidity()
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_FEE = "0xddca3f43"

ZERO = "0x0000000000000000000000000000000000000000"


def get_pool(token_a: str, token_b: str, fee: int) -> str | None:
    """v3 factory lookup. Returns None when the pool was never created."""
    data = (SEL_GET_POOL + chain.enc_addr(token_a)
            + chain.enc_addr(token_b) + chain.enc_uint(fee))
    raw = chain.try_call(V3_FACTORY, data)
    if not raw:
        return None
    addr = chain.dec_addr(raw)
    return None if addr == ZERO else addr


def pool_state(pool: str) -> dict | None:
    """sqrtPriceX96, tick and in-range liquidity for a v3 pool."""
    slot0 = chain.try_call(pool, SEL_SLOT0)
    if not slot0:
        return None
    sqrt_price_x96 = chain.dec_uint(slot0, 0)
    tick_raw = chain.dec_uint(slot0, 1)
    tick = tick_raw - (1 << 256) if tick_raw >= (1 << 255) else tick_raw
    liq_raw = chain.try_call(pool, SEL_LIQUIDITY)
    t0 = chain.try_call(pool, SEL_TOKEN0)
    t1 = chain.try_call(pool, SEL_TOKEN1)
    return {
        "pool": pool,
        "sqrtPriceX96": sqrt_price_x96,
        "tick": tick,
        "liquidity": chain.dec_uint(liq_raw) if liq_raw else 0,
        "token0": chain.dec_addr(t0) if t0 else None,
        "token1": chain.dec_addr(t1) if t1 else None,
    }


def price_from_sqrt(sqrt_price_x96: int, dec0: int, dec1: int) -> float:
    """Price of token0 denominated in token1, adjusted for decimals."""
    if not sqrt_price_x96:
        return 0.0
    ratio = (sqrt_price_x96 / (2 ** 96)) ** 2
    return ratio * (10 ** dec0) / (10 ** dec1)


def find_pools(token: str, quotes=(USDC, WETH)) -> list[dict]:
    """Every v3 pool pairing `token` with a quote asset, across all fee tiers."""
    found = []
    for quote in quotes:
        for fee in FEE_TIERS:
            pool = get_pool(token, quote, fee)
            if not pool:
                continue
            st = pool_state(pool)
            if not st:
                continue
            st["fee"] = fee
            st["quote"] = quote
            st["base"] = token
            found.append(st)
    return found


def reserves(pool: str, token0: str, token1: str) -> tuple[int | None, int | None]:
    """Actual token balances held by the pool contract.

    In-range `liquidity()` says nothing about withdrawable depth on its own, so
    the balances are what an honest depth claim has to rest on.
    """
    return chain.balance_of(token0, pool), chain.balance_of(token1, pool)


_ETH_USD_CACHE: list = []


def eth_usd_price() -> float:
    """ETH priced in USDC, read off the deepest WETH/USDC pool on Base.

    A WETH-quoted pool's depth means nothing in dollars without this. A
    hardcoded ETH price is the kind of stale constant that silently misprices
    every downstream check.
    """
    if _ETH_USD_CACHE:
        return _ETH_USD_CACHE[0]
    best, best_depth = 0.0, -1
    for fee in FEE_TIERS:
        pool = get_pool(WETH, USDC, fee)
        if not pool:
            continue
        st = pool_state(pool)
        if not st or not st["liquidity"]:
            continue
        weth_is_0 = (st["token0"] or "").lower() == WETH.lower()
        d0, d1 = (18, 6) if weth_is_0 else (6, 18)
        p01 = price_from_sqrt(st["sqrtPriceX96"], d0, d1)
        px = p01 if weth_is_0 else (1 / p01 if p01 else 0.0)
        if st["liquidity"] > best_depth and px > 0:
            best, best_depth = px, st["liquidity"]
    if best:
        _ETH_USD_CACHE.append(best)
    return best
