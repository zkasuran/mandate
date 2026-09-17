# SPDX-License-Identifier: Apache-2.0
"""Depth-aware routing across fragmented tokenized-equity pools.

The router's job is not to find the best price. It is to find the best price
*that can actually be filled*, then to refuse when none can.

Order of operations. The order matters:
  1. classify every pool for the asset
  2. drop anything dead outright, likewise anything trapped
  3. simulate the clip against each survivor with exact in-range maths
  4. require proven depth of `min_depth_multiple` times the clip
  5. enforce the mandate's slippage ceiling
  6. pick the best executable price among what is left

A route that fails any gate comes back as a refusal carrying its reason, never
as a silently worse fill.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import depth, uniswap


@dataclass
class Route:
    ok: bool
    symbol: str
    token: str
    pool: str | None = None
    fee: int | None = None
    quote: str | None = None
    quote_in: int = 0
    base_out: int = 0
    effective_price: float | None = None
    mid_price: float | None = None
    slippage_bps: int | None = None
    depth_usd: float | None = None
    reason: str = ""
    rejected: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "symbol": self.symbol, "token": self.token,
            "pool": self.pool, "fee": self.fee, "quote": self.quote,
            "quote_in": self.quote_in, "base_out": self.base_out,
            "effective_price": self.effective_price,
            "mid_price": self.mid_price,
            "slippage_bps": self.slippage_bps,
            "depth_usd": self.depth_usd,
            "reason": self.reason,
            "rejected": self.rejected,
        }


def _states_by_pool(token: str) -> dict:
    return {s["pool"]: s for s in uniswap.find_pools(token)}


def plan_buy(symbol: str, token: str, decimals: int, usdc_amount: int,
             max_slippage_bps: int = 50, min_depth_multiple: int = 3,
             eth_usd: float | None = None) -> Route:
    """Choose where to buy `usdc_amount` (6dp) of `symbol`, else refuse."""
    route = Route(ok=False, symbol=symbol, token=token, quote_in=usdc_amount)
    reports = depth.analyse_token(token, decimals, eth_usd=eth_usd)
    if not reports:
        route.reason = "no Uniswap v3 pool exists for this asset"
        return route

    states = _states_by_pool(token)
    clip_usd = usdc_amount / 1e6
    best = None

    for r in reports:
        # Only USDC-quoted pools can fill a USDC clip without a second hop.
        if r.quote.lower() != uniswap.USDC.lower():
            route.rejected.append({
                "pool": r.pool, "fee": r.fee, "status": r.status,
                "why": "not quoted in USDC, a second hop is not modelled",
            })
            continue
        if r.status in ("dead", "trapped"):
            route.rejected.append({
                "pool": r.pool, "fee": r.fee, "status": r.status,
                "why": "; ".join(r.reasons) or r.status,
            })
            continue

        required = clip_usd * min_depth_multiple
        if r.quote_usd_held < required:
            route.rejected.append({
                "pool": r.pool, "fee": r.fee, "status": r.status,
                "why": (f"depth ${r.quote_usd_held:,.2f} is under the "
                        f"${required:,.2f} required for a ${clip_usd:,.2f} clip "
                        f"at {min_depth_multiple}x"),
            })
            continue

        sim = depth.quote_buy(r, states[r.pool], usdc_amount)
        if not sim["usable"]:
            route.rejected.append({
                "pool": r.pool, "fee": r.fee, "status": r.status,
                "why": sim.get("refused", "simulation produced no fill"),
            })
            continue
        if sim["slippage_bps"] is not None and sim["slippage_bps"] > max_slippage_bps:
            route.rejected.append({
                "pool": r.pool, "fee": r.fee, "status": r.status,
                "why": (f"slippage {sim['slippage_bps']}bps exceeds the "
                        f"{max_slippage_bps}bps ceiling"),
            })
            continue

        cand = (sim["effective_price"], r, sim)
        if best is None or cand[0] < best[0]:
            best = cand

    if best is None:
        route.reason = (
            f"no pool can fill ${clip_usd:,.2f} of {symbol} within "
            f"{max_slippage_bps}bps at {min_depth_multiple}x depth cover"
        )
        return route

    eff, r, sim = best
    route.ok = True
    route.pool, route.fee, route.quote = r.pool, r.fee, r.quote
    route.base_out = sim["base_out"]
    route.effective_price = eff
    route.mid_price = r.price
    route.slippage_bps = sim["slippage_bps"]
    route.depth_usd = r.quote_usd_held
    route.reason = (
        f"filled at {eff:,.4f} against a {r.status} pool holding "
        f"${r.quote_usd_held:,.2f}"
    )
    return route
