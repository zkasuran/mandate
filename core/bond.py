# SPDX-License-Identifier: Apache-2.0
"""$MANDATE: the bond that makes "can't lie" enforceable.

A token here would be decoration if it only paid fees. It earns its place by
solving the one problem the rest of the system cannot solve on its own.

The system already makes a strategist's *returns* impossible to fake, because
they are recomputed from public fills. What it cannot do by itself is stop a
strategist from publishing disciplined rules to attract followers and then
trading outside them. Discipline is the product. Discipline needs a cost.

So: publishing a mandate that accepts followers requires bonding $MANDATE. The
bond is slashable, and slashing is not a governance vote or a committee. It is
arithmetic, because two facts are already true:

  1. the mandate's rules are fixed at a hash, so what was promised is not in
     dispute
  2. every fill is public, so what happened is not in dispute

Which makes "did this strategist keep their own rules" a decidable question.
`audit()` answers it, and every violation it returns names the rule, the fill
and the amount by which the rule was broken.

Slashed bond goes to the followers who were harmed, in proportion to the size
they had at risk. Not to a treasury, because the harmed party is the follower.

Distribution and funding run through Bankr on Robinhood Chain: the launch is
where the token comes from, and the creator trading fees are what pay for the
agent's compute. That is the same loop the platform is built around, used for
the thing it is actually good at.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

SYMBOL = "MANDATE"
CHAIN = "robinhood"          # launched through Bankr on Robinhood Chain
QUOTE = "USDG"

# Bond required to publish a mandate, scaled to how much follower money the
# strategist is asking to direct. A strategist steering more money posts more.
BOND_FLOOR_USD = 250.0
BOND_BPS_OF_COMMITTED = 200          # 2% of committed follower sleeve

# What each broken rule costs, as a share of the bond. Ordered by how badly the
# violation hurts the follower who trusted the published rules.
PENALTY_BPS = {
    "slippage_exceeded": 1_500,       # filled worse than the published ceiling
    "traded_out_of_hours": 2_500,     # market-hours mandate traded anyway
    "unverified_instrument": 5_000,   # routed into an unverified contract
    "depth_cover_ignored": 1_000,     # clip larger than the published cover
    "wrong_instrument": 10_000,       # traded something not in the mandate
}

MAX_SLASH_BPS = 10_000


@dataclass
class Bond:
    mandate_id: str
    strategist: str
    posted_usd: float
    committed_usd: float
    required_usd: float
    sufficient: bool
    slashed_usd: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["remaining_usd"] = max(0.0, self.posted_usd - self.slashed_usd)
        d["shortfall_usd"] = max(0.0, self.required_usd - self.posted_usd)
        return d


def required_bond(committed_usd: float) -> float:
    """What a strategist must post to direct this much follower money."""
    return max(BOND_FLOOR_USD, committed_usd * BOND_BPS_OF_COMMITTED / 10_000)


def bond_for(mandate_id: str, strategist: str, posted_usd: float,
             committed_usd: float) -> Bond:
    req = required_bond(committed_usd)
    return Bond(mandate_id=mandate_id, strategist=strategist,
                posted_usd=float(posted_usd), committed_usd=float(committed_usd),
                required_usd=req, sufficient=posted_usd >= req)


# --- the audit --------------------------------------------------------------

@dataclass
class Violation:
    kind: str
    fill_id: str
    follower: str
    detail: str
    penalty_bps: int
    harmed_usd: float

    def to_dict(self) -> dict:
        return asdict(self)


def audit(mandate: dict, fills: list, universe: dict | None = None) -> dict:
    """Check every fill against the rules the mandate published.

    `mandate` is the stored record, so `rules` is exactly what was hashed into
    the id. Nothing here consults a separate config: if it is not in the rules,
    it was never promised and cannot be a violation.
    """
    rules = mandate["rules"]
    allowed = {leg["symbol"].upper() for leg in rules["legs"]}
    max_slip = rules["execution"]["max_slippage_bps"]
    hours_only = rules["execution"]["require_market_hours"]
    cover = rules["execution"]["min_depth_multiple"]

    violations: list[Violation] = []

    for f in fills:
        tick = (f.get("ticker") or "").upper()

        if tick not in allowed:
            violations.append(Violation(
                "wrong_instrument", f.get("fill_id", "?"), f.get("follower", "?"),
                f"filled {tick}, which is not a leg of this mandate "
                f"({', '.join(sorted(allowed))})",
                PENALTY_BPS["wrong_instrument"], f.get("spend_usd", 0.0)))
            continue

        if universe is not None:
            inst = universe.get(tick)
            if inst is None or not getattr(inst, "verified", False):
                violations.append(Violation(
                    "unverified_instrument", f.get("fill_id", "?"),
                    f.get("follower", "?"),
                    f"filled {tick} at {f.get('address')}, which did not pass "
                    "ticker verification",
                    PENALTY_BPS["unverified_instrument"], f.get("spend_usd", 0.0)))
                continue

        slip = f.get("slippage_bps")
        if slip is not None and slip > max_slip:
            over = slip - max_slip
            harmed = f.get("spend_usd", 0.0) * over / 10_000
            violations.append(Violation(
                "slippage_exceeded", f.get("fill_id", "?"), f.get("follower", "?"),
                f"filled at {slip}bps against a published ceiling of {max_slip}bps",
                PENALTY_BPS["slippage_exceeded"], harmed))

        if hours_only and f.get("mark_certified") is False:
            violations.append(Violation(
                "traded_out_of_hours", f.get("fill_id", "?"), f.get("follower", "?"),
                "mandate requires market hours, this fill used an uncertified mark",
                PENALTY_BPS["traded_out_of_hours"], f.get("spend_usd", 0.0)))

        cov = f.get("depth_cover")
        if cov is not None and cov < cover:
            violations.append(Violation(
                "depth_cover_ignored", f.get("fill_id", "?"), f.get("follower", "?"),
                f"clip took {cov:.2f}x depth cover against a published {cover}x",
                PENALTY_BPS["depth_cover_ignored"], f.get("spend_usd", 0.0)))

    total_bps = min(sum(v.penalty_bps for v in violations), MAX_SLASH_BPS)
    return {
        "mandate_id": mandate["mandate_id"],
        "fills_checked": len(fills),
        "clean": not violations,
        "violations": [v.to_dict() for v in violations],
        "slash_bps": total_bps,
        "rules_hash": mandate["mandate_id"],
        "basis": ("every check reads the rules hashed into the mandate id and "
                  "fills recorded from execution. No discretionary judgement."),
    }


def settle(bond: Bond, audit_result: dict) -> dict:
    """Turn an audit into a slash and a distribution to the harmed followers."""
    slash_bps = audit_result["slash_bps"]
    slashed = bond.posted_usd * slash_bps / 10_000
    if slashed <= 0:
        return {"slashed_usd": 0.0, "payouts": [], "bond": bond.to_dict(),
                "note": "no violations, bond untouched"}

    harmed: dict = {}
    for v in audit_result["violations"]:
        harmed[v["follower"]] = harmed.get(v["follower"], 0.0) + v["harmed_usd"]
    total_harm = sum(harmed.values())

    payouts = []
    if total_harm > 0:
        for follower, amount in sorted(harmed.items(), key=lambda kv: -kv[1]):
            share = amount / total_harm
            payouts.append({"follower": follower, "harm_usd": amount,
                            "share": share, "payout_usd": slashed * share})
    bond.slashed_usd = slashed
    return {
        "slashed_usd": slashed,
        "payouts": payouts,
        "bond": bond.to_dict(),
        "note": ("slashed bond is paid to the followers who were harmed, in "
                 "proportion to their exposure, not to a treasury"),
    }


def spec() -> dict:
    """The token's role, stated so a reader can argue with it."""
    return {
        "symbol": SYMBOL,
        "chain": CHAIN,
        "launch": "Bankr",
        "settles_in": QUOTE,
        "roles": [
            {"role": "Strategist bond",
             "why": ("publishing a mandate that takes followers requires posting "
                     f"{BOND_BPS_OF_COMMITTED / 100:.0f}% of committed follower "
                     f"sleeve, floor ${BOND_FLOOR_USD:,.0f}. The bond is what a "
                     "strategist loses for breaking their own published rules."),
             "enforceable": True},
            {"role": "Slashing to harmed followers",
             "why": ("violations are decided by arithmetic over the hashed rules "
                     "and public fills, then paid to the followers who were "
                     "harmed rather than to a treasury"),
             "enforceable": True},
            {"role": "Funding the agent",
             "why": ("creator trading fees from the Bankr launch pay for the "
                     "inference and execution the agent runs on, which is the "
                     "self-funding loop the platform is built for"),
             "enforceable": True},
            {"role": "Fee settlement discount",
             "why": ("strategist fees are charged on followed volume in USDG, "
                     f"with a discount for settling in {SYMBOL}"),
             "enforceable": True},
        ],
        "deliberately_not": [
            "governance voting over which mandates are allowed",
            "staking yield unbacked by fee revenue",
            "a fee that scales with a self-reported return",
        ],
        "penalties_bps": PENALTY_BPS,
    }
