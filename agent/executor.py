# SPDX-License-Identifier: Apache-2.0
"""The executor: one run of one mandate for one subscribed follower.

The order of the gates is the product. Each one can refuse. A refusal is
returned with the reason attached so a follower can read why their agent chose
not to trade in their name.

  1. subscription     is this follower actually subscribed and active
  2. bond             has the strategist posted enough to be slashable
  3. market hours     is the underlying equity market open
  4. universe         does every leg resolve to a verified contract
  5. plan             what clips does the mandate call for at this size
  6. quote            what would each clip actually fill at, on the venue
  7. gates            slippage ceiling and depth cover
  8. protect          place the stop and take-profit the mandate promised
  9. execute          sign and send, else refuse and say why

Steps 4 and 6 are where the two integrations meet. Ticker verification and
execution go through Flash, which aggregates venues and carries the advanced
order types. Depth truth comes from reading Uniswap v3 directly, because an
aggregator's own number is not something a follower can re-check at a block
height.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from core import basis, bond, book, chains, exits, fees, flash, orders, spec


@dataclass
class Step:
    name: str
    ok: bool
    detail: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"step": self.name, "ok": self.ok,
                "detail": self.detail, "data": self.data}


@dataclass
class Run:
    mandate_id: str
    subscription_id: str
    follower: str
    executed: bool
    steps: list
    fills: list
    refusals: list
    fee_summary: dict
    warnings: list
    exits: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "mandate_id": self.mandate_id,
            "subscription_id": self.subscription_id,
            "follower": self.follower,
            "executed": self.executed,
            "steps": [s.to_dict() for s in self.steps],
            "fills": self.fills,
            "refusals": self.refusals,
            "fees": self.fee_summary,
            "warnings": self.warnings,
            "exits": self.exits,
        }


class Executor:
    def __init__(self, universe: dict, chain_key: str = "robinhood",
                 broadcast: bool = False):
        self.universe = universe
        self.chain = chains.get(chain_key)
        self.broadcast = broadcast

    def marks(self, tickers: list) -> dict:
        out = {}
        for t in tickers:
            inst = self.universe.get(t.upper())
            if inst and inst.verified and inst.price:
                out[t.upper()] = inst.price
        return out

    def depths(self, tickers: list) -> dict:
        out = {}
        for t in tickers:
            inst = self.universe.get(t.upper())
            if inst and inst.verified and inst.liquidity:
                out[t.upper()] = inst.liquidity
        return out

    def run(self, subscription: dict, mandate_row: dict,
            posted_bond_usd: float = 0.0, now=None) -> Run:
        steps: list[Step] = []
        mandate = spec.load(mandate_row)
        follower = subscription["follower"]
        sub_id = subscription["subscription_id"]
        tickers = [leg.symbol.upper() for leg in mandate.legs]

        def done(executed=False, fills=None, refusals=None, warn=None):
            return Run(mandate.mandate_id(), sub_id, follower, executed, steps,
                       fills or [], refusals or [], {}, warn or [])

        # 1. subscription
        active = subscription["status"] == "active"
        steps.append(Step("subscription", active,
                          f"subscription is {subscription['status']}, "
                          f"sleeve ${subscription['sleeve_usd']:,.2f}"))
        if not active:
            return done()

        # 2. bond
        committed = sum(s["sleeve_usd"] for s in
                        book.subscriptions(mandate_id=mandate.mandate_id(),
                                           active_only=True))
        b = bond.bond_for(mandate.mandate_id(), mandate.strategist,
                          posted_bond_usd, committed)
        steps.append(Step(
            "bond", b.sufficient,
            (f"strategist posted ${b.posted_usd:,.2f} against ${b.required_usd:,.2f} "
             f"required for ${committed:,.2f} of committed follower money"),
            b.to_dict()))
        if not b.sufficient:
            return done(warn=[
                f"strategist bond short by ${b.required_usd - b.posted_usd:,.2f}. "
                "A mandate that cannot be slashed cannot take followers."])

        # 3. market hours
        allowed, why = basis.guard(mandate.execution.require_market_hours, now)
        session = basis.session_state(now)
        steps.append(Step("market_hours", allowed, why, session.to_dict()))
        if not allowed:
            return done(warn=[why])

        # 4. universe
        bad = [t for t in tickers
               if t not in self.universe or not self.universe[t].verified]
        steps.append(Step(
            "universe", not bad,
            (f"{len(tickers) - len(bad)}/{len(tickers)} legs resolve to a "
             "verified contract"
             + (f"; refused {', '.join(bad)}" if bad else "")),
            {t: self.universe[t].to_dict() for t in tickers
             if t in self.universe}))
        if bad:
            return done(warn=[f"unverified legs: {', '.join(bad)}"])

        # 5. plan
        sleeve_usdc = int(round(subscription["sleeve_usd"] * 1e6))
        plan = orders.build_plan(mandate, follower, sleeve_usdc,
                                 self.marks(tickers), self.depths(tickers))
        steps.append(Step("plan", bool(plan.clips),
                          f"{len(plan.clips)} clips, "
                          f"{len(plan.protections)} protective exits",
                          plan.to_dict()))
        if not plan.clips:
            return done(warn=plan.warnings)

        # 6 + 7. quote and gate each clip
        built, refused = [], []
        order_type = mandate.execution.kind
        for clip in plan.clips:
            inst = self.universe[clip.symbol.upper()]
            spend = clip.quote_amount / 1e6
            kw = {}
            if order_type == "twap":
                kw = {"duration_seconds": max(300, mandate.execution.interval_seconds
                                              * mandate.execution.slices),
                      "twap_buckets": max(2, mandate.execution.slices)}
            elif order_type == "limit":
                kw = {"limit_notional_price":
                      f"{inst.price * (1 + mandate.execution.max_slippage_bps / 10_000):.6f}"}

            q = flash.quote(
                chain=self.chain.flash_key, target=inst.address,
                contra=self.chain.quote_address, side="buy",
                qty=f"{spend:.6f}",
                order_type=order_type if order_type in flash.ORDER_TYPES else "market",
                funder=follower,
                max_slippage=f"{mandate.execution.max_slippage_bps / 10_000:.6f}",
                **kw)

            if not q.ok:
                refused.append({"ticker": clip.symbol, "spend_usd": spend,
                                "reason": q.error, "details": q.details})
                continue

            slip_bps = None
            if q.effective_price and inst.price:
                slip_bps = int(round((q.effective_price - inst.price)
                                     / inst.price * 10_000))
            if slip_bps is not None and slip_bps > mandate.execution.max_slippage_bps:
                refused.append({
                    "ticker": clip.symbol, "spend_usd": spend,
                    "reason": (f"quoted {slip_bps}bps against a "
                               f"{mandate.execution.max_slippage_bps}bps ceiling")})
                continue

            cover = (inst.liquidity / spend) if spend else 0
            if cover < mandate.execution.min_depth_multiple:
                refused.append({
                    "ticker": clip.symbol, "spend_usd": spend,
                    "reason": (f"venue depth ${inst.liquidity:,.0f} is "
                               f"{cover:.1f}x the clip against a required "
                               f"{mandate.execution.min_depth_multiple}x")})
                continue

            fee = fees.fee_on_volume(clip.quote_amount, mandate.fee_bps)
            built.append({
                "fill_id": f"fil_{uuid.uuid4().hex[:16]}",
                "subscription_id": sub_id,
                "mandate_id": mandate.mandate_id(),
                "follower": follower,
                "ticker": clip.symbol.upper(),
                "address": inst.address,
                "chain": self.chain.key,
                "side": "buy",
                "spend_usd": spend,
                "quantity": q.receive or 0.0,
                "price": q.effective_price or 0.0,
                "venue": "flash",
                "order_type": q.order_type,
                "quote_id": q.quote_id,
                "broadcast": False,
                "tx_hash": None,
                "mark_certified": session.open,
                "fee_usd": fee.gross_usdc / 1e6,
                "slippage_bps": slip_bps,
                "depth_cover": cover,
                "needs_approval": q.needs_approval,
                "has_typed_data": bool(q.typed_data),
            })

        steps.append(Step("quote", bool(built),
                          f"{len(built)} clips quoted and gated, "
                          f"{len(refused)} refused",
                          {"refused": refused}))
        if not built:
            return done(refusals=refused, warn=plan.warnings)

        book.record_fills(built)

        # 8. protect what was just opened. A planned stop that is never placed
        # is not a stop, so the position is read back from the book and the
        # exits are quoted against the basis it actually has.
        folio = book.portfolio(follower)
        holdings = {h["ticker"]: {"quantity": h["quantity"],
                                  "cost_basis": h["cost_basis"]}
                    for h in folio["holdings"]}
        eplan = exits.derive(mandate, holdings, self.universe, self.chain.key)
        eplan.follower = follower
        if eplan.exits:
            eplan = exits.place(eplan, self.chain, follower,
                                max_slippage_bps=max(100, mandate.execution.max_slippage_bps))
        cover = exits.coverage(eplan, holdings)
        steps.append(Step(
            "protect", cover["fully_protected"],
            (f"{eplan.placed}/{len(eplan.exits)} protective exits resting at the venue, "
             f"{cover['coverage_pct']:.0f}% of legs carry a live stop"
             + ("" if cover["fully_protected"]
                else f"; exposed: {', '.join(cover['legs_with_no_stop'])}")),
            {"plan": eplan.to_dict(), "coverage": cover}))

        # 9. execute
        if self.broadcast:
            detail = ("broadcast requires a signed userSignature from the "
                      "follower's wallet; no key is held by this service")
        else:
            detail = "dry run: quotes are real and signable, nothing was sent"
        steps.append(Step("execute", False, detail,
                          {"entries_signable": sum(1 for b in built if b["has_typed_data"]),
                           "exits_signable": sum(1 for e in eplan.exits if e.has_typed_data)}))

        accrued = fees.accrue([{"quote_in": int(b["spend_usd"] * 1e6)}
                               for b in built], mandate.fee_bps)
        warnings = list(plan.warnings) + eplan.warnings
        if not cover["fully_protected"]:
            warnings.append(
                "position is only partially protected: "
                f"{', '.join(cover['legs_with_no_stop'])} has no live stop")
        if refused:
            warnings.append(f"{len(refused)} clips refused at the venue")
        if not session.open:
            warnings.append("marks uncertified: the underlying market is closed")

        run = Run(mandate.mandate_id(), sub_id, follower, False, steps,
                  built, refused, accrued, warnings,
                  exits={"plan": eplan.to_dict(), "coverage": cover})
        book.record_run(sub_id, mandate.mandate_id(), {
            "fills": len(built), "refused": len(refused),
            "volume_usd": sum(b["spend_usd"] for b in built),
            "executed": False,
        })
        return run
