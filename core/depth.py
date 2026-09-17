# SPDX-License-Identifier: Apache-2.0
"""Depth truth for tokenized equities.

The quoted tick of a Uniswap pool is not a price you can trade at. On Base,
tokenized equity pools include ones that are initialized, carry a tick, and
hold nothing. Routing by "a pool exists" or by fee tier walks straight into
them.

This module answers two questions honestly:

  1. What IS this pool?  live / thin / dead / trapped
  2. What would a given size actually fill at, right now?

The fill simulation uses the exact Uniswap v3 in-range formulas. While a swap
stays inside the current tick range, liquidity L is constant and the maths is
closed-form and exact:

    buying token0 with token1:  sqrtP' = sqrtP + dy/L
                                dx     = L * (1/sqrtP - 1/sqrtP')
    selling token0 for token1:  sqrtP' = L*sqrtP / (L + dx*sqrtP)
                                dy     = L * (sqrtP - sqrtP')

Crossing a tick boundary needs the initialized-tick bitmap, which these pools
do not justify fetching per quote. So when a size exceeds in-range capacity the
result is reported as `exceeds_in_range`, never silently extrapolated. Refusing
to answer is the correct behaviour for a router that moves other people's money.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from . import chain, uniswap

Q96 = 2 ** 96

# A pool initialized at (or near) the extreme ticks is not a market. MIN_TICK is
# -887272; anything out there is a price of effectively zero or infinity.
EXTREME_TICK = 800_000

# Classification thresholds, in whole US dollars. They are converted into the
# quote token's own units at call time. Writing them as raw integers in a
# 6-decimal base is how a $5,000 floor silently became $0.50 and passed a pool
# holding $5.50 as live.
DEAD_QUOTE_USD = 1
THIN_QUOTE_USD = 5_000

# How far a pool's price may sit from the cohort median before it is suspect.
OUTLIER_BPS = 2_000        # 20%


@dataclass
class PoolReport:
    pool: str
    fee: int
    base: str
    quote: str
    tick: int
    liquidity: int
    price: float                  # base priced in quote, decimal-adjusted
    base_balance: int
    quote_balance: int
    base_decimals: int
    quote_decimals: int
    quote_usd_held: float         # depth floor is measured in dollars
    status: str                   # live | thin | dead | trapped
    reasons: list

    def to_dict(self) -> dict:
        d = asdict(self)
        d["quote_balance_human"] = self.quote_balance / 10 ** self.quote_decimals
        d["base_balance_human"] = self.base_balance / 10 ** self.base_decimals
        return d


def classify(state: dict, base_decimals: int, quote_decimals: int,
             quote_usd: float = 1.0) -> PoolReport:
    """Turn raw pool state into a verdict with its reasons stated.

    `quote_usd` is what one whole quote token is worth in dollars, so a WETH
    pool and a USDC pool are measured against the same floor.
    """
    base, quote = state["base"], state["quote"]
    t0 = (state["token0"] or "").lower()
    base_is_0 = t0 == base.lower()

    bal0, bal1 = uniswap.reserves(state["pool"], state["token0"], state["token1"])
    bal0, bal1 = bal0 or 0, bal1 or 0
    base_bal, quote_bal = (bal0, bal1) if base_is_0 else (bal1, bal0)

    d0, d1 = (base_decimals, quote_decimals) if base_is_0 else (quote_decimals, base_decimals)
    p01 = uniswap.price_from_sqrt(state["sqrtPriceX96"], d0, d1)
    price = p01 if base_is_0 else (1 / p01 if p01 else 0.0)

    quote_usd_held = (quote_bal / 10 ** quote_decimals) * quote_usd

    reasons: list[str] = []
    status = "live"

    if abs(state["tick"]) >= EXTREME_TICK:
        status = "trapped"
        reasons.append(
            f"tick {state['tick']} sits at the extreme of the tick range, "
            "so the quoted price is not a market"
        )
    if state["liquidity"] == 0:
        reasons.append("in-range liquidity is zero")
        if status != "trapped":
            status = "dead"
    if quote_usd_held < DEAD_QUOTE_USD:
        reasons.append(f"pool holds ${quote_usd_held:,.2f} of the quote token")
        if status == "live":
            status = "dead"
    elif quote_usd_held < THIN_QUOTE_USD:
        reasons.append(f"only ${quote_usd_held:,.2f} of quote token held")
        if status == "live":
            status = "thin"

    return PoolReport(
        pool=state["pool"], fee=state["fee"], base=base, quote=quote,
        tick=state["tick"], liquidity=state["liquidity"], price=price,
        base_balance=base_bal, quote_balance=quote_bal,
        base_decimals=base_decimals, quote_decimals=quote_decimals,
        quote_usd_held=quote_usd_held,
        status=status, reasons=reasons,
    )


def flag_outliers(reports: list[PoolReport]) -> None:
    """Mark pools whose price disagrees with the cohort.

    Two live pools on the same asset should not disagree by 20%. When they do,
    one of them is stale and filling there is a donation.
    """
    priced = [r for r in reports if r.status in ("live", "thin") and r.price > 0]
    if len(priced) < 2:
        return
    prices = sorted(r.price for r in priced)
    mid = prices[len(prices) // 2]
    if mid <= 0:
        return
    for r in priced:
        drift = abs(r.price - mid) / mid
        if drift * 10_000 > OUTLIER_BPS:
            r.reasons.append(
                f"price {r.price:,.2f} is {drift * 100:.1f}% from the "
                f"{mid:,.2f} cohort median"
            )
            r.status = "trapped"


# --- exact in-range fill simulation -----------------------------------------

def _buy_base_with_quote(L: int, sqrtP: int, dy: int) -> tuple[int, int]:
    """Spend dy of token1 to receive dx of token0. Returns (dx, sqrtP')."""
    if L == 0:
        return 0, sqrtP
    sqrt_new = sqrtP + (dy * Q96) // L
    dx = (L * Q96 * (sqrt_new - sqrtP)) // (sqrt_new * sqrtP)
    return dx, sqrt_new


def _sell_base_for_quote(L: int, sqrtP: int, dx: int) -> tuple[int, int]:
    """Sell dx of token0 to receive dy of token1. Returns (dy, sqrtP')."""
    if L == 0:
        return 0, sqrtP
    denom = L * Q96 + dx * sqrtP
    sqrt_new = (L * Q96 * sqrtP) // denom
    dy = (L * (sqrtP - sqrt_new)) // Q96
    return dy, sqrt_new


def quote_buy(report: PoolReport, state: dict, quote_amount: int) -> dict:
    """What `quote_amount` of the quote token actually buys in this pool.

    quote_amount is in the quote token's smallest unit (USDC: 6dp).
    """
    out = {
        "pool": report.pool, "fee": report.fee, "status": report.status,
        "quote_in": quote_amount, "base_out": 0, "exceeds_in_range": False,
        "effective_price": None, "slippage_bps": None, "usable": False,
    }
    if report.status in ("dead", "trapped"):
        out["refused"] = f"pool is {report.status}: " + "; ".join(report.reasons)
        return out

    base_is_0 = (state["token0"] or "").lower() == report.base.lower()
    L = state["liquidity"]
    sqrtP = state["sqrtPriceX96"]

    # The fee is taken off the input before it touches the curve.
    after_fee = quote_amount - (quote_amount * report.fee) // 1_000_000

    if base_is_0:
        base_out, _ = _buy_base_with_quote(L, sqrtP, after_fee)
    else:
        base_out, _ = _sell_base_for_quote(L, sqrtP, after_fee)

    # Never claim more than the pool physically holds.
    if base_out > report.base_balance:
        out["exceeds_in_range"] = True
        out["refused"] = (
            f"fill of {base_out / 10 ** report.base_decimals:,.6f} exceeds the "
            f"{report.base_balance / 10 ** report.base_decimals:,.6f} the pool holds"
        )
        return out

    out["base_out"] = base_out
    if base_out > 0:
        human_in = quote_amount / 10 ** report.quote_decimals
        human_out = base_out / 10 ** report.base_decimals
        eff = human_in / human_out
        out["effective_price"] = eff
        if report.price > 0:
            out["slippage_bps"] = int(round((eff - report.price) / report.price * 10_000))
        out["usable"] = True
    return out


def analyse_token(token: str, decimals: int,
                  eth_usd: float | None = None) -> list[PoolReport]:
    """Every pool for a token, classified, with cohort outliers flagged."""
    states = uniswap.find_pools(token)
    if eth_usd is None and any(s["quote"].lower() == uniswap.WETH.lower()
                               for s in states):
        eth_usd = uniswap.eth_usd_price()
    reports = []
    for st in states:
        is_usdc = st["quote"].lower() == uniswap.USDC.lower()
        qdec = 6 if is_usdc else 18
        qusd = 1.0 if is_usdc else (eth_usd or 0.0)
        reports.append(classify(st, decimals, qdec, qusd))
    usdc_only = [r for r in reports if r.quote.lower() == uniswap.USDC.lower()]
    flag_outliers(usdc_only)
    return reports
