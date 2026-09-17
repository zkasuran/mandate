# SPDX-License-Identifier: Apache-2.0
"""The Mandate: a strategy as a signed, immutable ruleset.

A mandate is the unit the whole product is built on. It says what a strategy
buys, how it sizes, when it exits and what it refuses to do, in a form that
hashes to a stable id. Publish it once and the thesis cannot be quietly
rewritten later, which is the property every copy-trading leaderboard is
missing.

Canonicalisation rules, so two implementations agree on the id:
  * JSON, UTF-8, sorted keys, no insignificant whitespace
  * floats forbidden anywhere in the rules (basis points and integers only),
    because float formatting is not stable across languages
  * the id is sha256 over those exact bytes, hex, prefixed `mnd_`
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any

SCHEMA = "mandate/v1"

SIDES = ("long",)                      # short needs a venue that supports it
ORDER_KINDS = ("market", "dca", "twap", "bracket", "stop", "take_profit", "limit")
TRIGGERS = ("schedule", "drawdown", "breakout", "manual")


class MandateError(ValueError):
    pass


def _reject_floats(node: Any, path: str = "rules") -> None:
    """Floats make the id unstable across languages. Refuse them outright."""
    if isinstance(node, float):
        raise MandateError(
            f"{path}: float values are not allowed in a mandate. "
            "Use integers or basis points so the id is reproducible."
        )
    if isinstance(node, dict):
        for k, v in node.items():
            _reject_floats(v, f"{path}.{k}")
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            _reject_floats(v, f"{path}[{i}]")


def canonical_bytes(payload: dict) -> bytes:
    """The exact bytes an id is taken over."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


@dataclass(frozen=True)
class Leg:
    """One instrument the mandate trades."""
    symbol: str                      # "NVDAc"
    token: str                       # verified Base address
    weight_bps: int                  # share of the sleeve, 10000 = 100%

    def validate(self) -> None:
        if not self.token.startswith("0x") or len(self.token) != 42:
            raise MandateError(f"leg {self.symbol}: token must be a 20-byte address")
        if not 1 <= self.weight_bps <= 10_000:
            raise MandateError(f"leg {self.symbol}: weight_bps out of range")


@dataclass(frozen=True)
class Execution:
    """How orders reach the market. Thin equity pools make this load-bearing."""
    kind: str = "twap"
    slices: int = 6                  # for dca / twap
    interval_seconds: int = 900
    max_slippage_bps: int = 50
    # Refuse to fill if the route's proven depth cannot absorb the clip.
    min_depth_multiple: int = 3      # depth >= 3x the clip size
    # Refuse to fill while the token's basis to its underlying is unverifiable.
    require_market_hours: bool = True

    def validate(self) -> None:
        if self.kind not in ORDER_KINDS:
            raise MandateError(f"execution.kind must be one of {ORDER_KINDS}")
        if self.slices < 1:
            raise MandateError("execution.slices must be >= 1")
        if not 1 <= self.max_slippage_bps <= 1000:
            raise MandateError("execution.max_slippage_bps out of range (1..1000)")
        if self.min_depth_multiple < 1:
            raise MandateError("execution.min_depth_multiple must be >= 1")


@dataclass(frozen=True)
class Risk:
    """The exits. A mandate without exits is a hope, not a strategy."""
    stop_loss_bps: int | None = 800          # -8%
    take_profit_bps: int | None = 1500       # +15%
    max_position_bps: int = 10_000           # of the follower's sleeve
    max_daily_orders: int = 12

    def validate(self) -> None:
        for name in ("stop_loss_bps", "take_profit_bps"):
            v = getattr(self, name)
            if v is not None and not 1 <= v <= 100_000:
                raise MandateError(f"risk.{name} out of range")
        if not 1 <= self.max_position_bps <= 10_000:
            raise MandateError("risk.max_position_bps out of range")


@dataclass(frozen=True)
class Mandate:
    name: str
    strategist: str                          # the publishing wallet
    thesis: str                              # plain English, what and why
    legs: tuple[Leg, ...]
    execution: Execution = field(default_factory=Execution)
    risk: Risk = field(default_factory=Risk)
    trigger: str = "schedule"
    cadence_seconds: int = 86_400
    fee_bps: int = 100                       # strategist fee on followed volume
    chain_id: int = 8453                     # Base
    schema: str = SCHEMA
    version: int = 1

    # -- validation ----------------------------------------------------------
    def validate(self) -> None:
        if not self.name.strip():
            raise MandateError("name is required")
        if not self.thesis.strip():
            raise MandateError("thesis is required: say what this strategy does")
        if not self.legs:
            raise MandateError("a mandate needs at least one leg")
        if self.trigger not in TRIGGERS:
            raise MandateError(f"trigger must be one of {TRIGGERS}")
        if not 0 <= self.fee_bps <= 2_000:
            raise MandateError("fee_bps out of range (0..2000)")
        total = sum(l.weight_bps for l in self.legs)
        if total != 10_000:
            raise MandateError(f"leg weights must sum to 10000 bps, got {total}")
        seen = set()
        for leg in self.legs:
            leg.validate()
            key = leg.token.lower()
            if key in seen:
                raise MandateError(f"duplicate leg for token {leg.token}")
            seen.add(key)
        self.execution.validate()
        self.risk.validate()
        _reject_floats(self.rules())

    # -- identity ------------------------------------------------------------
    def rules(self) -> dict:
        """Everything that defines behaviour. Display-only text stays out."""
        return {
            "schema": self.schema,
            "version": self.version,
            "chain_id": self.chain_id,
            "trigger": self.trigger,
            "cadence_seconds": self.cadence_seconds,
            "fee_bps": self.fee_bps,
            "legs": [
                {"symbol": l.symbol, "token": l.token.lower(),
                 "weight_bps": l.weight_bps}
                for l in sorted(self.legs, key=lambda x: x.token.lower())
            ],
            "execution": asdict(self.execution),
            "risk": asdict(self.risk),
        }

    def canonical(self) -> bytes:
        return canonical_bytes(self.rules())

    def mandate_id(self) -> str:
        return "mnd_" + hashlib.sha256(self.canonical()).hexdigest()

    def to_dict(self) -> dict:
        return {
            "mandate_id": self.mandate_id(),
            "name": self.name,
            "strategist": self.strategist.lower(),
            "thesis": self.thesis,
            "rules": self.rules(),
        }

    # -- signing -------------------------------------------------------------
    def signing_message(self) -> str:
        """The exact human-readable text a strategist signs (EIP-191).

        The id is in the text, so a signature commits to the rules rather than
        to a name someone could re-point at different rules later.
        """
        return (
            "Mandate publication\n"
            f"id: {self.mandate_id()}\n"
            f"name: {self.name}\n"
            f"strategist: {self.strategist.lower()}\n"
            f"chain: {self.chain_id}\n"
            f"fee_bps: {self.fee_bps}\n"
            "I am publishing this strategy. The rules above are fixed and "
            "cannot be changed without producing a new id."
        )


def load(payload: dict) -> Mandate:
    """Rebuild a mandate from stored JSON and re-derive its id.

    A stored id is never trusted. It is recomputed and compared, so a tampered
    record fails loudly instead of serving different rules under a known id.
    """
    r = payload["rules"]
    m = Mandate(
        name=payload["name"],
        strategist=payload["strategist"],
        thesis=payload["thesis"],
        legs=tuple(Leg(**l) for l in r["legs"]),
        execution=Execution(**r["execution"]),
        risk=Risk(**r["risk"]),
        trigger=r["trigger"],
        cadence_seconds=r["cadence_seconds"],
        fee_bps=r["fee_bps"],
        chain_id=r["chain_id"],
        schema=r["schema"],
        version=r["version"],
    )
    m.validate()
    claimed = payload.get("mandate_id")
    if claimed and claimed != m.mandate_id():
        raise MandateError(
            f"mandate id mismatch: stored {claimed}, rules hash to {m.mandate_id()}"
        )
    return m
