# SPDX-License-Identifier: Apache-2.0
"""Definitive Flash: asset resolution, aggregated depth and advanced orders.

Two integrations do two different jobs in this product, and keeping them
separate is deliberate:

  Uniswap v3, read directly     tells the truth about one venue's depth, with
                                every input re-checkable at a block height.
  Flash                         aggregates venues and executes the order types
                                a thin equity market actually needs.

Measured on a $500 AAPLc clip: routing on Uniswap v3 alone filled at $335.33,
Flash quoted $333.87 for the same size. The aggregated venue fills better, so
Flash is the executor and the direct AMM read is the auditor.

Order types the API accepts, from its own OpenAPI enum:
`market`, `limit`, `twap`, `stop`, `stop-loss`, `take-profit`, `bracket`.
`bracket` as a top-level type returns "not yet supported", so a bracket is
placed as `attachedBracket` on a market or limit order. That is the API's
answer, not a guess, and `probe_order_types()` re-checks it live.

Nothing here can move funds. Every order carries a `userSignature` produced by
the funder's wallet, and this module never holds a key: it builds the quote and
hands back the EIP-712 payload to be signed elsewhere.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

BASE_URL = "https://flash.definitive.fi/v1"

# The endpoint sits behind Cloudflare, which rejects a default urllib
# User-Agent with a 1010 before the request ever reaches the API. Same shape as
# the public Base RPC. Send a real browser UA or every call looks like an
# outage.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# Definitive publishes this integrator key in their own docs for trade-only
# use. Set FLASH_API_KEY to be credited as the integrator on orders instead.
PUBLIC_DEMO_KEY = "dpka_513a2bd7_57a2_46d2_927b_2a3857fe271b"

ORDER_TYPES = ("market", "limit", "twap", "stop",
               "stop-loss", "take-profit", "bracket")

# Order types this product uses, with what each one needs beyond the basics.
REQUIREMENTS = {
    "market": (),
    "limit": ("limitNotionalPrice",),
    "twap": ("durationSeconds",),
    "stop-loss": ("triggers", "side=sell"),
    "take-profit": ("triggers", "side=sell"),
}


class FlashError(RuntimeError):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}


def api_key() -> str:
    return os.environ.get("FLASH_API_KEY") or PUBLIC_DEMO_KEY


def using_own_key() -> bool:
    return bool(os.environ.get("FLASH_API_KEY"))


def _request(method: str, path: str, body: dict | None = None,
             timeout: int = 45) -> dict:
    url = BASE_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"x-definitive-api-key": api_key(),
                 "content-type": "application/json",
                 "accept": "application/json",
                 "user-agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error", {})
            raise FlashError(err.get("code", f"HTTP_{e.code}"),
                             err.get("message", raw[:200]),
                             err.get("details")) from None
        except json.JSONDecodeError:
            raise FlashError(f"HTTP_{e.code}", raw[:200]) from None


# --- asset resolution -------------------------------------------------------

@dataclass
class Asset:
    chain: str
    address: str
    symbol: str
    name: str
    decimals: int
    price: float | None
    liquidity: float | None
    volume24h: float | None
    holders: int | None
    market_cap: float | None
    risk_flagged: bool

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _asset(row: dict) -> Asset:
    num = lambda k: (float(row[k]) if row.get(k) not in (None, "") else None)  # noqa: E731
    return Asset(
        chain=row.get("chain", ""), address=row.get("address", ""),
        symbol=row.get("symbol", ""), name=row.get("name", ""),
        decimals=int(row.get("decimals") or 18),
        price=num("price"), liquidity=num("liquidity"),
        volume24h=num("volume24h"), market_cap=num("marketCap"),
        holders=(int(row["holders"]) if row.get("holders") is not None else None),
        risk_flagged=bool(row.get("riskFlagged")),
    )


def search(query: str, chain: str | None = None, limit: int = 10) -> list:
    path = f"/search?query={urllib.parse.quote(query)}&limit={min(limit, 25)}"
    if chain:
        path += f"&chain={chain}"
    return [_asset(r) for r in _request("GET", path).get("assets", [])]


# --- the lookalike problem --------------------------------------------------

@dataclass
class Resolution:
    """Which contract a ticker should mean, and why."""
    ok: bool
    query: str
    chain: str
    chosen: Asset | None = None
    reason: str = ""
    impostors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "query": self.query, "chain": self.chain,
            "chosen": self.chosen.to_dict() if self.chosen else None,
            "reason": self.reason,
            "impostors": [
                {"symbol": a.symbol, "address": a.address,
                 "liquidity": a.liquidity, "holders": a.holders,
                 "why": w}
                for a, w in self.impostors
            ],
        }


# A ticker search returns squatters alongside the real asset. Searching AAPL on
# Robinhood Chain returns AAPLCAT; TSLA returns TSLAHOOD; SPY returns SPDR.
# Resolving a ticker by "first result" or by fuzzy match hands a follower's
# money to whichever one ranked highest.
MIN_LIQUIDITY_USD = 50_000
MIN_HOLDERS = 1_000


def resolve(ticker: str, chain: str, candidates: list | None = None) -> Resolution:
    """Resolve a ticker to one contract, or refuse and name the lookalikes.

    The rule is deliberately strict and boring: the symbol must match the
    request exactly, case-insensitively, and the asset must clear a liquidity
    and holder floor. Anything else is reported as an impostor rather than
    silently ranked below the winner.
    """
    want = ticker.strip().upper()
    rows = candidates if candidates is not None else search(ticker, chain, limit=25)
    res = Resolution(ok=False, query=want, chain=chain)

    exact, others = [], []
    for a in rows:
        if a.symbol.upper() == want:
            exact.append(a)
        else:
            others.append((a, "symbol does not match the requested ticker"))

    for a in exact:
        if a.risk_flagged:
            others.append((a, "exact symbol but flagged as risky by the venue"))
        elif (a.liquidity or 0) < MIN_LIQUIDITY_USD:
            others.append((a, f"exact symbol but only ${a.liquidity or 0:,.0f} of liquidity"))
        elif (a.holders or 0) < MIN_HOLDERS:
            others.append((a, f"exact symbol but only {a.holders or 0:,} holders"))

    viable = [a for a in exact if not a.risk_flagged
              and (a.liquidity or 0) >= MIN_LIQUIDITY_USD
              and (a.holders or 0) >= MIN_HOLDERS]

    res.impostors = others
    if not viable:
        res.reason = (
            f"no contract on {chain} matches the ticker {want} with at least "
            f"${MIN_LIQUIDITY_USD:,} liquidity and {MIN_HOLDERS:,} holders"
        )
        return res
    if len(viable) > 1:
        viable.sort(key=lambda a: -(a.liquidity or 0))
        res.impostors += [(a, "second contract with the same symbol")
                          for a in viable[1:]]
    best = viable[0]
    res.ok = True
    res.chosen = best
    res.reason = (
        f"exact symbol match with ${best.liquidity or 0:,.0f} liquidity and "
        f"{best.holders or 0:,} holders"
    )
    return res


# --- quotes -----------------------------------------------------------------

@dataclass
class Quote:
    ok: bool
    order_type: str
    quote_id: str | None = None
    spend: float | None = None
    receive: float | None = None
    receive_notional: float | None = None
    effective_price: float | None = None
    fee_notional: float | None = None
    price_impact: float | None = None
    needs_approval: bool = False
    typed_data: str | None = None
    error: str = ""
    details: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("raw", "typed_data")}
        d["has_typed_data"] = bool(self.typed_data)
        return d


def quote(*, chain: str, target: str, contra: str, side: str, qty: str,
          order_type: str = "market", funder: str | None = None,
          max_slippage: str | None = None,
          limit_notional_price: str | None = None,
          duration_seconds: int | None = None,
          twap_buckets: int | None = None,
          triggers: list | None = None,
          attached_bracket: dict | None = None) -> Quote:
    """Ask Flash what an order would do. Never signs, never sends."""
    body = {
        "targetChain": chain, "contraChain": chain,
        "targetAsset": target, "contraAsset": contra,
        "side": side, "qty": str(qty), "orderType": order_type,
    }
    if funder:
        body["funderAddress"] = funder
    if max_slippage:
        body["maxSlippage"] = str(max_slippage)
    if limit_notional_price:
        body["limitNotionalPrice"] = str(limit_notional_price)
    if duration_seconds:
        body["durationSeconds"] = int(duration_seconds)
    if twap_buckets:
        body["twapBucketCount"] = int(twap_buckets)
    if triggers:
        body["triggers"] = triggers
    if attached_bracket:
        body["attachedBracket"] = attached_bracket

    try:
        d = _request("POST", "/quote", body)
    except FlashError as e:
        return Quote(ok=False, order_type=order_type, error=str(e),
                     details=e.details)

    frm, to = d.get("from") or {}, d.get("to") or {}
    spend = float(frm["amount"]) if frm.get("amount") else None
    recv = float(to["amount"]) if to.get("amount") else None
    evm = d.get("evm") or {}
    return Quote(
        ok=True, order_type=d.get("orderType", order_type),
        quote_id=d.get("quoteId"), spend=spend, receive=recv,
        receive_notional=(float(to["notional"]) if to.get("notional") else None),
        effective_price=(spend / recv if spend and recv else None),
        fee_notional=(float((d.get("fees") or {}).get("estimatedFeeNotional"))
                      if (d.get("fees") or {}).get("estimatedFeeNotional") else None),
        price_impact=(float(d["estimatedPriceImpact"])
                      if d.get("estimatedPriceImpact") else None),
        needs_approval=bool(evm.get("approveTx")),
        typed_data=evm.get("orderTypedData") or None,
        raw=d,
    )


def probe_order_types(*, chain: str, target: str, contra: str,
                      funder: str, qty: str = "100") -> dict:
    """Ask the live API which order types it will actually accept right now.

    Capability is read from the venue rather than asserted from a doc, because
    `bracket` is in the published enum and is rejected at the endpoint.
    """
    out = {}
    probes = [
        ("market", {}),
        ("limit", {"limit_notional_price": "1"}),
        ("twap", {"duration_seconds": 1800, "twap_buckets": 6}),
        ("bracket", {}),
    ]
    for name, extra in probes:
        q = quote(chain=chain, target=target, contra=contra, side="buy",
                  qty=qty, order_type=name, funder=funder,
                  max_slippage="0.01", **extra)
        out[name] = {"accepted": q.ok,
                     "error": q.error or None,
                     "details": q.details or None}
    for name, side, extra in [
        ("stop-loss", "sell", {"triggers": [{"notionalPrice": "1",
                                             "triggerType": "lower"}]}),
        ("take-profit", "sell", {"triggers": [{"notionalPrice": "999999",
                                               "triggerType": "upper"}]}),
    ]:
        q = quote(chain=chain, target=target, contra=contra, side=side,
                  qty="0.001", order_type=name, funder=funder, **extra)
        out[name] = {"accepted": q.ok, "error": q.error or None,
                     "details": q.details or None}
    return out
