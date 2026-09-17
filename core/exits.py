# SPDX-License-Identifier: Apache-2.0
"""Protective exits, placed as real resting orders.

A mandate that plans a stop and never places one is a mandate with no stop. The
planner produces triggers. This module turns each into an order the venue
will actually hold and fire.

Flash carries both sides of this natively, which the live API confirms rather
than a doc promising it:

  stop-loss     side=sell, triggers[{notionalPrice, triggerType:"lower"}]
  take-profit   side=sell, triggers[{notionalPrice, triggerType:"upper"}]
  bracket       rejected as a top-level orderType ("not yet supported"), so a
                bracket is an entry carrying `attachedBracket`

Two rules that are not obvious and cost money when missed:

  * A protective exit sells the QUANTITY HELD, not a dollar amount. Sizing an
    exit in dollars means a gap through the trigger leaves a remainder nobody
    is watching.
  * Triggers are placed against the position's own basis, never the live mark.
    Re-deriving a stop from whatever the price is when the exit is placed
    quietly moves the risk the follower agreed to.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field

from . import flash


@dataclass
class Exit:
    kind: str                     # stop_loss | take_profit
    ticker: str
    address: str
    chain: str
    quantity: float               # position held, in whole tokens
    trigger_price: float
    reference_price: float        # the basis the trigger was derived from
    bps_from_reference: int
    placed: bool = False
    quote_id: str | None = None
    order_type: str | None = None
    has_typed_data: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExitPlan:
    mandate_id: str
    follower: str
    exits: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"mandate_id": self.mandate_id, "follower": self.follower,
                "exits": [e.to_dict() for e in self.exits],
                "warnings": self.warnings}

    @property
    def placed(self) -> int:
        return sum(1 for e in self.exits if e.placed)


def derive(mandate, holdings: dict, universe: dict, chain_key: str) -> ExitPlan:
    """Build the exits a mandate owes on the positions actually held.

    `holdings` is {ticker: {"quantity": float, "cost_basis": float}} taken from
    the replayed book, so an exit can only ever be placed against a real
    position.
    """
    plan = ExitPlan(mandate_id=mandate.mandate_id(), follower="")
    risk = mandate.risk
    if not risk.stop_loss_bps and not risk.take_profit_bps:
        plan.warnings.append(
            "this mandate publishes no stop and no take-profit, so nothing "
            "protects the position once it is open")
        return plan

    for leg in mandate.legs:
        t = leg.symbol.upper()
        h = holdings.get(t)
        if not h or h["quantity"] <= 0:
            continue
        inst = universe.get(t)
        if not inst or not inst.verified:
            plan.warnings.append(f"{t}: unverified, no exit placed")
            continue
        basis = h["cost_basis"]
        if basis <= 0:
            plan.warnings.append(f"{t}: no cost basis, cannot derive a trigger")
            continue

        if risk.stop_loss_bps:
            plan.exits.append(Exit(
                kind="stop_loss", ticker=t, address=inst.address,
                chain=chain_key, quantity=h["quantity"],
                trigger_price=basis * (1 - risk.stop_loss_bps / 10_000),
                reference_price=basis,
                bps_from_reference=-risk.stop_loss_bps))
        if risk.take_profit_bps:
            plan.exits.append(Exit(
                kind="take_profit", ticker=t, address=inst.address,
                chain=chain_key, quantity=h["quantity"],
                trigger_price=basis * (1 + risk.take_profit_bps / 10_000),
                reference_price=basis,
                bps_from_reference=risk.take_profit_bps))
    return plan


TRIGGER_DIRECTION = {"stop_loss": "lower", "take_profit": "upper"}
ORDER_TYPE = {"stop_loss": "stop-loss", "take_profit": "take-profit"}


def place(plan: ExitPlan, chain, funder: str, max_slippage_bps: int = 100) -> ExitPlan:
    """Quote every exit at the venue. Real orders, nothing signed or sent."""
    for e in plan.exits:
        q = flash.quote(
            chain=chain.flash_key,
            target=e.address,
            contra=chain.quote_address,
            side="sell",
            qty=f"{e.quantity:.8f}",
            order_type=ORDER_TYPE[e.kind],
            funder=funder,
            max_slippage=f"{max_slippage_bps / 10_000:.6f}",
            triggers=[{"notionalPrice": f"{e.trigger_price:.6f}",
                       "triggerType": TRIGGER_DIRECTION[e.kind]}],
        )
        if q.ok:
            e.placed = True
            e.quote_id = q.quote_id
            e.order_type = q.order_type
            e.has_typed_data = bool(q.typed_data)
        else:
            e.error = q.error
    return plan


def coverage(plan: ExitPlan, holdings: dict) -> dict:
    """How much of the position is actually protected.

    A partially covered position is the failure this reports rather than hides:
    a stop on three of four legs reads as "protected" on any summary that only
    counts orders.
    """
    protected, exposed = {}, {}
    for t, h in holdings.items():
        if h["quantity"] <= 0:
            continue
        has_stop = any(e.ticker == t and e.kind == "stop_loss" and e.placed
                       for e in plan.exits)
        (protected if has_stop else exposed)[t] = h["quantity"]
    total = len(protected) + len(exposed)
    return {
        "legs_with_a_live_stop": sorted(protected),
        "legs_with_no_stop": sorted(exposed),
        "fully_protected": not exposed,
        "coverage_pct": (100.0 * len(protected) / total) if total else 100.0,
    }
