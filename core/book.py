# SPDX-License-Identifier: Apache-2.0
"""The book: strategists, followers, positions and what everyone is owed.

This is the product's memory. A mandate on its own is a document. The book is
what makes it a thing people use: a strategist publishes, followers subscribe
with a size, the agent runs on a cadence, and every fill lands here so a
follower can see what was done in their name and why.

Two rules hold the whole design together:

  Funds never move through this service. A subscription records authority and
  size, nothing else. Custody stays with the follower, and the record here is
  an account of decisions rather than a balance we hold.

  Every number a follower sees is traceable to a decision or a fill. Nothing is
  rolled up without keeping the rows that produced it, because a return you
  cannot break back down is exactly the number this product exists to replace.

Storage is a single JSON file. That is a deliberate limit, not an oversight:
the schema is small, the audit trail matters more than throughput, and a
reviewer can read the entire state of the system in one file.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field, asdict

STORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "book.json")

_lock = threading.RLock()

EMPTY = {"mandates": [], "subscriptions": [], "runs": [], "fills": []}


def _load() -> dict:
    if not os.path.exists(STORE):
        return json.loads(json.dumps(EMPTY))
    with open(STORE) as fh:
        d = json.load(fh)
    for k, v in EMPTY.items():
        d.setdefault(k, list(v))
    return d


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, STORE)


def read() -> dict:
    with _lock:
        return _load()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# --- mandates ---------------------------------------------------------------

def put_mandate(record: dict) -> dict:
    """Store a published mandate. Identical rules are never stored twice."""
    with _lock:
        d = _load()
        for row in d["mandates"]:
            if row["mandate_id"] == record["mandate_id"]:
                return {"stored": False, "reason": "identical rules already published",
                        "mandate": row}
        d["mandates"].append(record)
        _save(d)
        return {"stored": True, "mandate": record}


def get_mandate(mandate_id: str) -> dict | None:
    for row in read()["mandates"]:
        if row["mandate_id"] == mandate_id:
            return row
    return None


# --- subscriptions ----------------------------------------------------------

@dataclass
class Subscription:
    subscription_id: str
    mandate_id: str
    follower: str
    sleeve_usd: float
    wallet_kind: str              # dry-run | dynamic-delegated | dynamic-server
    status: str = "active"        # active | paused | cancelled
    created_block: int | None = None
    deployed_usd: float = 0.0     # of the sleeve, how much has been put to work
    fees_accrued_usd: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def subscribe(mandate_id: str, follower: str, sleeve_usd: float,
              wallet_kind: str = "dry-run", note: str = "") -> dict:
    """Record a follower's authority and size against a mandate."""
    if sleeve_usd <= 0:
        raise ValueError("sleeve must be positive")
    m = get_mandate(mandate_id)
    if not m:
        raise KeyError(f"unknown mandate {mandate_id}")
    with _lock:
        d = _load()
        for s in d["subscriptions"]:
            if (s["mandate_id"] == mandate_id
                    and s["follower"].lower() == follower.lower()
                    and s["status"] == "active"):
                return {"created": False,
                        "reason": "this follower is already subscribed to this mandate",
                        "subscription": s}
        sub = Subscription(
            subscription_id=_new_id("sub"), mandate_id=mandate_id,
            follower=follower.lower(), sleeve_usd=float(sleeve_usd),
            wallet_kind=wallet_kind, note=note).to_dict()
        d["subscriptions"].append(sub)
        _save(d)
        return {"created": True, "subscription": sub}


def set_status(subscription_id: str, status: str) -> dict:
    if status not in ("active", "paused", "cancelled"):
        raise ValueError("status must be active, paused or cancelled")
    with _lock:
        d = _load()
        for s in d["subscriptions"]:
            if s["subscription_id"] == subscription_id:
                s["status"] = status
                _save(d)
                return {"updated": True, "subscription": s}
    return {"updated": False, "reason": "unknown subscription"}


def subscriptions(mandate_id: str | None = None, follower: str | None = None,
                  active_only: bool = False) -> list:
    rows = read()["subscriptions"]
    if mandate_id:
        rows = [r for r in rows if r["mandate_id"] == mandate_id]
    if follower:
        rows = [r for r in rows if r["follower"].lower() == follower.lower()]
    if active_only:
        rows = [r for r in rows if r["status"] == "active"]
    return rows


# --- runs and fills ---------------------------------------------------------

@dataclass
class Fill:
    fill_id: str
    subscription_id: str
    mandate_id: str
    follower: str
    ticker: str
    address: str
    chain: str
    side: str
    spend_usd: float
    quantity: float
    price: float
    venue: str                    # flash | uniswap-v3
    order_type: str
    quote_id: str | None
    broadcast: bool
    tx_hash: str | None
    mark_certified: bool
    fee_usd: float = 0.0
    block: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def record_run(subscription_id: str, mandate_id: str, summary: dict) -> str:
    run_id = _new_id("run")
    with _lock:
        d = _load()
        d["runs"].append({
            "run_id": run_id, "subscription_id": subscription_id,
            "mandate_id": mandate_id, **summary,
        })
        _save(d)
    return run_id


def record_fills(rows: list) -> int:
    if not rows:
        return 0
    with _lock:
        d = _load()
        d["fills"].extend(rows)
        for s in d["subscriptions"]:
            spent = sum(r["spend_usd"] for r in rows
                        if r["subscription_id"] == s["subscription_id"]
                        and r["side"] == "buy")
            fees = sum(r.get("fee_usd", 0.0) for r in rows
                       if r["subscription_id"] == s["subscription_id"])
            if spent or fees:
                s["deployed_usd"] = round(s.get("deployed_usd", 0.0) + spent, 6)
                s["fees_accrued_usd"] = round(s.get("fees_accrued_usd", 0.0) + fees, 6)
        _save(d)
    return len(rows)


def fills(subscription_id: str | None = None, follower: str | None = None,
          mandate_id: str | None = None) -> list:
    rows = read()["fills"]
    if subscription_id:
        rows = [r for r in rows if r["subscription_id"] == subscription_id]
    if follower:
        rows = [r for r in rows if r["follower"].lower() == follower.lower()]
    if mandate_id:
        rows = [r for r in rows if r["mandate_id"] == mandate_id]
    return rows


# --- portfolio --------------------------------------------------------------

@dataclass
class Holding:
    ticker: str
    address: str
    chain: str
    quantity: float = 0.0
    cost_basis: float = 0.0
    spent_usd: float = 0.0
    received_usd: float = 0.0
    realised_pnl: float = 0.0
    fills: int = 0

    def apply(self, f: dict) -> None:
        if f["side"] == "buy":
            total = self.cost_basis * self.quantity + f["spend_usd"]
            self.quantity += f["quantity"]
            self.cost_basis = total / self.quantity if self.quantity else 0.0
            self.spent_usd += f["spend_usd"]
        else:
            sold = min(f["quantity"], self.quantity)
            self.realised_pnl += (f["price"] - self.cost_basis) * sold
            self.quantity -= sold
            self.received_usd += f["spend_usd"]
            if self.quantity <= 1e-12:
                self.quantity = 0.0
                self.cost_basis = 0.0
        self.fills += 1

    def value(self, mark: float | None) -> dict:
        invested = self.cost_basis * self.quantity
        unreal = (mark - self.cost_basis) * self.quantity if mark else 0.0
        return dict(
            asdict(self),
            mark=mark,
            market_value=(mark * self.quantity if mark else None),
            invested=invested,
            unrealised_pnl=unreal,
            total_pnl=self.realised_pnl + unreal,
            return_pct=((unreal / invested * 100) if invested else None),
        )


def portfolio(follower: str, marks: dict | None = None,
              mark_certified: bool | None = None) -> dict:
    """What a follower actually holds, rebuilt from their fills.

    Nothing is cached. The portfolio is a replay, so it cannot drift from the
    fills that produced it.
    """
    rows = sorted(fills(follower=follower), key=lambda r: (r.get("block") or 0))
    holdings: dict = {}
    for f in rows:
        h = holdings.setdefault(
            f["ticker"], Holding(ticker=f["ticker"], address=f["address"],
                                 chain=f["chain"]))
        h.apply(f)

    marks = marks or {}
    valued = [h.value(marks.get(t)) for t, h in holdings.items()]
    subs = subscriptions(follower=follower)
    return {
        "follower": follower.lower(),
        "subscriptions": subs,
        "active_subscriptions": sum(1 for s in subs if s["status"] == "active"),
        "committed_usd": sum(s["sleeve_usd"] for s in subs if s["status"] == "active"),
        "deployed_usd": sum(s.get("deployed_usd", 0.0) for s in subs),
        "fees_accrued_usd": sum(s.get("fees_accrued_usd", 0.0) for s in subs),
        "holdings": valued,
        "market_value": sum(h["market_value"] or 0.0 for h in valued),
        "realised_pnl": sum(h["realised_pnl"] for h in valued),
        "unrealised_pnl": sum(h["unrealised_pnl"] for h in valued),
        "total_pnl": sum(h["total_pnl"] for h in valued),
        "fill_count": len(rows),
        "marks_certified": mark_certified,
        "basis": ("rebuilt by replaying every recorded fill. No balance is "
                  "stored, so this cannot disagree with the fills below."),
    }


# --- strategist leaderboard -------------------------------------------------

def leaderboard(marks: dict | None = None) -> list:
    """Rank mandates by what actually happened under them.

    Ranking is on followed volume and realised outcome, never on a
    self-reported return, because a self-reported return is the thing this
    product replaces.
    """
    d = read()
    marks = marks or {}
    out = []
    for m in d["mandates"]:
        mid = m["mandate_id"]
        subs = [s for s in d["subscriptions"] if s["mandate_id"] == mid]
        rows = [f for f in d["fills"] if f["mandate_id"] == mid]
        by_ticker: dict = {}
        for f in sorted(rows, key=lambda r: (r.get("block") or 0)):
            h = by_ticker.setdefault(
                f["ticker"], Holding(ticker=f["ticker"], address=f["address"],
                                     chain=f["chain"]))
            h.apply(f)
        valued = [h.value(marks.get(t)) for t, h in by_ticker.items()]
        volume = sum(f["spend_usd"] for f in rows)
        out.append({
            "mandate_id": mid,
            "name": m["name"],
            "strategist": m["strategist"],
            "thesis": m["thesis"],
            "fee_bps": m["rules"]["fee_bps"],
            "followers": sum(1 for s in subs if s["status"] == "active"),
            "committed_usd": sum(s["sleeve_usd"] for s in subs
                                 if s["status"] == "active"),
            "deployed_usd": sum(s.get("deployed_usd", 0.0) for s in subs),
            "volume_usd": volume,
            "fills": len(rows),
            "realised_pnl": sum(h["realised_pnl"] for h in valued),
            "unrealised_pnl": sum(h["unrealised_pnl"] for h in valued),
            "total_pnl": sum(h["total_pnl"] for h in valued),
            "strategist_fees_usd": sum(f.get("fee_usd", 0.0) for f in rows),
            "holdings": valued,
        })
    out.sort(key=lambda r: (-r["volume_usd"], -r["followers"]))
    return out
