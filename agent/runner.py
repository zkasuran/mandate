# SPDX-License-Identifier: Apache-2.0
"""The execution agent.

One decision loop, stated in the order it actually runs, because the order is
the product:

  1. is the underlying equity market open?          basis guard
  2. what is the asset worth, and can that mark be certified?
  3. what does the mandate say to do at this sleeve size?    order plan
  4. where can each clip actually fill?              depth-aware route
  5. build the calldata
  6. broadcast, or refuse and say why

Every step can refuse. A refusal is a result, not a failure, and it is
returned with its reason attached so a follower can read why their agent chose
not to trade. An agent that always finds a reason to trade is not managing
anyone's money, it is generating fees.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from core import basis, depth, fees, orders, router, tokens, uniswap, wallet


@dataclass
class Decision:
    step: str
    ok: bool
    detail: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"step": self.step, "ok": self.ok,
                "detail": self.detail, "data": self.data}


@dataclass
class RunResult:
    mandate_id: str
    follower: str
    executed: bool
    decisions: list
    clips: list
    fees: dict
    warnings: list

    def to_dict(self) -> dict:
        return {
            "mandate_id": self.mandate_id,
            "follower": self.follower,
            "executed": self.executed,
            "decisions": [d.to_dict() for d in self.decisions],
            "clips": self.clips,
            "fees": self.fees,
            "warnings": self.warnings,
        }


class Agent:
    def __init__(self, wallet_adapter: wallet.Wallet | None = None,
                 registry: dict | None = None):
        self.wallet = wallet_adapter or wallet.DryRunWallet()
        self.registry = registry

    def _registry(self) -> dict:
        if self.registry is None:
            self.registry = tokens.verified()
        return self.registry

    def marks(self, symbols: list[str]) -> dict:
        """Best certifiable price per symbol, taken from the deepest live pool."""
        reg = self._registry()
        eth = None
        out = {}
        for sym in symbols:
            meta = reg.get(sym)
            if not meta or not meta.get("ok"):
                continue
            reports = depth.analyse_token(meta["address"], meta["decimals"],
                                          eth_usd=eth)
            usable = [r for r in reports
                      if r.status in ("live", "thin")
                      and r.quote.lower() == uniswap.USDC.lower()]
            if not usable:
                continue
            best = max(usable, key=lambda r: r.quote_usd_held)
            out[sym] = {"price": best.price, "depth_usd": best.quote_usd_held,
                        "pool": best.pool, "fee": best.fee,
                        "status": best.status}
        return out

    def run(self, mandate, follower: str, sleeve_usdc: int,
            now: dt.datetime | None = None) -> RunResult:
        decisions: list[Decision] = []
        symbols = [l.symbol for l in mandate.legs]

        # 1. market-hours guard
        allowed, why = basis.guard(mandate.execution.require_market_hours, now)
        decisions.append(Decision("market_hours", allowed, why,
                                  basis.session_state(now).to_dict()))
        if not allowed:
            return RunResult(mandate.mandate_id(), follower, False,
                             decisions, [], {}, [why])

        # 2. marks
        marks = self.marks(symbols)
        missing = [s for s in symbols if s not in marks]
        decisions.append(Decision(
            "marks", not missing,
            (f"priced {len(marks)}/{len(symbols)} legs off live pools"
             + (f"; no usable pool for {', '.join(missing)}" if missing else "")),
            {s: m["price"] for s, m in marks.items()}))

        certified = basis.certify_mark(0, now)["certified"]
        price_map = {s: m["price"] for s, m in marks.items()}
        depth_map = {s: m["depth_usd"] for s, m in marks.items()}

        # 3. order plan
        plan = orders.build_plan(mandate, follower, sleeve_usdc,
                                 price_map, depth_map)
        decisions.append(Decision(
            "plan", bool(plan.clips),
            f"{len(plan.clips)} clips, {len(plan.protections)} protective exits",
            plan.to_dict()))
        if not plan.clips:
            return RunResult(mandate.mandate_id(), follower, False,
                             decisions, [], {}, plan.warnings)

        # 4 + 5. route and build each clip
        reg = self._registry()
        built, refused = [], []
        for clip in plan.clips:
            meta = reg[clip.symbol]
            route = router.plan_buy(
                clip.symbol, meta["address"], meta["decimals"],
                clip.quote_amount,
                max_slippage_bps=mandate.execution.max_slippage_bps,
                min_depth_multiple=mandate.execution.min_depth_multiple)
            if not route.ok:
                refused.append({"clip": clip.to_dict(), "reason": route.reason,
                                "rejected": route.rejected})
                continue
            min_out = route.base_out * (10_000 - mandate.execution.max_slippage_bps) // 10_000
            tx = wallet.build_exact_input_single(
                token_in=uniswap.USDC, token_out=meta["address"],
                fee=route.fee, recipient=follower,
                amount_in=clip.quote_amount, amount_out_min=min_out)
            built.append({
                "clip": clip.to_dict(),
                "route": route.to_dict(),
                "mark_certified": certified,
                "tx": tx.to_dict(),
                "quote_in": clip.quote_amount,
            })

        decisions.append(Decision(
            "route", bool(built),
            f"{len(built)} clips routable, {len(refused)} refused",
            {"refused": refused}))

        # 6. broadcast (or refuse to)
        ok, why = self.wallet.available()
        sends = []
        for item in built:
            tx = wallet.UnsignedTx(**item["tx"])
            sends.append(self.wallet.send(tx))
        executed = any(s.get("broadcast") for s in sends)
        decisions.append(Decision("broadcast", executed,
                                  why if not executed else "clips broadcast",
                                  {"wallet": self.wallet.kind}))
        for item, send in zip(built, sends):
            item["send"] = send

        accrued = fees.accrue([{"quote_in": b["quote_in"]} for b in built],
                              mandate.fee_bps)
        warnings = list(plan.warnings)
        if refused:
            warnings.append(f"{len(refused)} clips could not be routed")
        if not certified:
            warnings.append(
                "marks are uncertified: the underlying equity market is closed")

        return RunResult(mandate.mandate_id(), follower, executed,
                         decisions, built, accrued, warnings)
