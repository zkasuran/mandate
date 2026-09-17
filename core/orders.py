# SPDX-License-Identifier: Apache-2.0
"""Order planning: turn a mandate into a schedule of executable clips.

A market order into a pool holding $4,891 is how a follower loses money on the
way in. Every mandate therefore executes through a real order type. This
module turns the mandate's intent plus a follower's size into the individual
clips an agent will actually fire.

Order types implemented, matching the advanced-order set a professional
execution venue exposes:

  market       one clip, now
  dca          equal clips on a fixed interval
  twap         equal clips sized to the venue's proven depth
  limit        fires only at or under a price
  stop         protective exit below a reference
  take_profit  protective exit above a reference
  bracket      an entry plus both protective exits attached

Nothing here signs or sends. It produces a plan a human or an agent can read
before anything touches a wallet.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict


@dataclass
class Clip:
    index: int
    total: int
    symbol: str
    token: str
    side: str                       # buy | sell
    quote_amount: int               # USDC smallest unit
    not_before: int                 # seconds after plan start
    kind: str
    limit_price: float | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Protection:
    """A resting exit attached to a position."""
    kind: str                       # stop | take_profit
    symbol: str
    token: str
    trigger_price: float
    reference_price: float
    bps_from_reference: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    mandate_id: str
    follower: str
    sleeve_usdc: int
    clips: list
    protections: list
    warnings: list

    def to_dict(self) -> dict:
        return {
            "mandate_id": self.mandate_id,
            "follower": self.follower,
            "sleeve_usdc": self.sleeve_usdc,
            "clips": [c.to_dict() for c in self.clips],
            "protections": [p.to_dict() for p in self.protections],
            "warnings": self.warnings,
        }


def split_notional(total: int, slices: int) -> list[int]:
    """Split an integer amount into `slices` parts with no dust lost.

    The remainder lands on the earliest clips rather than being dropped, so the
    clips always sum back to exactly `total`.
    """
    if slices < 1:
        raise ValueError("slices must be >= 1")
    base, rem = divmod(total, slices)
    return [base + (1 if i < rem else 0) for i in range(slices)]


def depth_aware_slices(sleeve_usdc: int, depth_usd: float,
                       requested: int, min_depth_multiple: int) -> tuple[int, str | None]:
    """Raise the slice count when the requested clip is too big for the venue.

    A mandate asking for 3 clips against a pool that can only absorb a tenth of
    one is not an instruction to trade badly, so the plan widens instead.
    """
    if depth_usd <= 0:
        return requested, "venue depth is zero, clip sizing cannot be checked"
    max_clip_usd = depth_usd / max(min_depth_multiple, 1)
    if max_clip_usd <= 0:
        return requested, "depth cover leaves no room for any clip"
    needed = math.ceil((sleeve_usdc / 1e6) / max_clip_usd)
    if needed > requested:
        return needed, (
            f"widened from {requested} to {needed} clips: venue depth "
            f"${depth_usd:,.2f} only covers ${max_clip_usd:,.2f} per clip "
            f"at {min_depth_multiple}x"
        )
    return requested, None


def build_plan(mandate, follower: str, sleeve_usdc: int,
               marks: dict, depths: dict | None = None) -> Plan:
    """Turn a mandate plus a follower's sleeve into concrete clips.

    marks:  {symbol: current price} used for limits and protective triggers
    depths: {symbol: proven depth in USD} used to size clips to the venue
    """
    ex = mandate.execution
    risk = mandate.risk
    clips: list[Clip] = []
    protections: list[Protection] = []
    warnings: list[str] = []

    capped = sleeve_usdc * risk.max_position_bps // 10_000
    if capped < sleeve_usdc:
        warnings.append(
            f"position capped at {risk.max_position_bps}bps of the sleeve: "
            f"${capped / 1e6:,.2f} of ${sleeve_usdc / 1e6:,.2f}"
        )

    for leg in mandate.legs:
        leg_notional = capped * leg.weight_bps // 10_000
        if leg_notional <= 0:
            warnings.append(f"{leg.symbol}: weight rounds to nothing at this sleeve size")
            continue

        slices = 1 if ex.kind in ("market", "limit") else ex.slices
        if ex.kind == "twap" and depths and leg.symbol in depths:
            slices, note = depth_aware_slices(
                leg_notional, depths[leg.symbol], slices, ex.min_depth_multiple)
            if note:
                warnings.append(f"{leg.symbol}: {note}")

        mark = marks.get(leg.symbol)
        limit_price = None
        if ex.kind == "limit":
            if mark is None:
                warnings.append(f"{leg.symbol}: no mark available, limit clip not planned")
                continue
            limit_price = mark * (1 + ex.max_slippage_bps / 10_000)

        for i, amount in enumerate(split_notional(leg_notional, slices)):
            clips.append(Clip(
                index=i + 1, total=slices, symbol=leg.symbol, token=leg.token,
                side="buy", quote_amount=amount,
                not_before=i * ex.interval_seconds,
                kind=ex.kind, limit_price=limit_price,
                note=(f"clip {i + 1}/{slices} of ${leg_notional / 1e6:,.2f}"),
            ))

        if mark is not None:
            if risk.stop_loss_bps:
                protections.append(Protection(
                    kind="stop", symbol=leg.symbol, token=leg.token,
                    trigger_price=mark * (1 - risk.stop_loss_bps / 10_000),
                    reference_price=mark, bps_from_reference=-risk.stop_loss_bps))
            if risk.take_profit_bps:
                protections.append(Protection(
                    kind="take_profit", symbol=leg.symbol, token=leg.token,
                    trigger_price=mark * (1 + risk.take_profit_bps / 10_000),
                    reference_price=mark, bps_from_reference=risk.take_profit_bps))
        else:
            warnings.append(
                f"{leg.symbol}: no mark, so stop and take-profit were not attached")

    if len(clips) > risk.max_daily_orders:
        warnings.append(
            f"plan has {len(clips)} clips against a {risk.max_daily_orders} "
            "daily order cap, so it will run across more than one day"
        )

    return Plan(mandate_id=mandate.mandate_id(), follower=follower.lower(),
                sleeve_usdc=sleeve_usdc, clips=clips,
                protections=protections, warnings=warnings)
