# SPDX-License-Identifier: Apache-2.0
"""The tradeable universe, resolved from a ticker and verified on the chain.

A follower says "NVDA". Turning that into a contract address is the single most
dangerous step in the whole system, because the venue's own search returns
squatters alongside the real asset:

  NVDA  ->  NVDAx3L, a 3x leveraged token holding $407,605 of liquidity
  AAPL  ->  AAPLCAT ($86,717, 1,127 holders), AAPLHOOD
  TSLA  ->  TSLAHOOD, TSLATEST, TITSLA
  SPY   ->  SPDR, SPY6900, and a token whose symbol is just "I"

and one token whose symbol is a comma-joined list of several thousand tickers,
so it matches every ticker search that has ever been run against it.

Resolution is therefore two independent steps that must agree:

  1. the venue's index gives a candidate, filtered by exact symbol, liquidity
     and holder count  (core/flash.resolve)
  2. the chain confirms that contract reports that exact symbol

A ticker that fails either step is refused. Refusing to trade a name is always
cheaper than trading the wrong one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import chain as rpc
from . import chains, flash

# The names this product will consider. Chosen for real depth and real holder
# counts, not for being interesting. Verified at load time, never trusted.
TICKERS = ("NVDA", "AAPL", "TSLA", "SPY", "GOOGL", "META", "MSFT", "QQQ")


@dataclass
class Instrument:
    ticker: str
    chain_key: str
    address: str
    symbol_onchain: str
    name: str
    decimals: int
    price: float | None
    liquidity: float | None
    volume24h: float | None
    holders: int | None
    verified: bool
    notes: list = field(default_factory=list)
    impostors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["explorer"] = (f"{chains.get(self.chain_key).explorer}"
                         f"/address/{self.address}")
        return d


def resolve_instrument(ticker: str, chain_key: str = "robinhood") -> Instrument | None:
    """Resolve one ticker, confirming the venue's answer against the chain."""
    res = flash.resolve(ticker, chain_key)
    impostors = [
        {"symbol": a.symbol[:60], "address": a.address,
         "liquidity": a.liquidity, "holders": a.holders, "why": w}
        for a, w in res.impostors
    ]
    if not res.ok or not res.chosen:
        return Instrument(
            ticker=ticker, chain_key=chain_key, address="", symbol_onchain="",
            name="", decimals=0, price=None, liquidity=None, volume24h=None,
            holders=None, verified=False,
            notes=[res.reason], impostors=impostors)

    a = res.chosen
    notes = []
    token = rpc.erc20(a.address, chain=chain_key)
    verified = True

    if not token.get("code"):
        verified = False
        notes.append("no contract code at the address the venue returned")
    elif (token.get("symbol") or "").upper() != ticker.upper():
        verified = False
        notes.append(
            f"chain reports symbol {token.get('symbol')!r}, "
            f"venue indexed it as {a.symbol!r}")
    if token.get("decimals") is not None and token["decimals"] != a.decimals:
        notes.append(
            f"decimals disagree: chain says {token['decimals']}, "
            f"venue says {a.decimals}")

    return Instrument(
        ticker=ticker.upper(), chain_key=chain_key, address=a.address,
        symbol_onchain=token.get("symbol") or "", name=token.get("name") or a.name,
        decimals=(token.get("decimals") if token.get("decimals") is not None
                  else a.decimals),
        price=a.price, liquidity=a.liquidity, volume24h=a.volume24h,
        holders=a.holders, verified=verified, notes=notes, impostors=impostors)


def build(tickers=TICKERS, chain_key: str = "robinhood") -> dict:
    """Resolve the whole universe. Unverified names are kept, flagged, unused."""
    out = {}
    for t in tickers:
        try:
            inst = resolve_instrument(t, chain_key)
        except flash.FlashError as e:
            inst = Instrument(ticker=t, chain_key=chain_key, address="",
                              symbol_onchain="", name="", decimals=0,
                              price=None, liquidity=None, volume24h=None,
                              holders=None, verified=False,
                              notes=[f"venue lookup failed: {e}"])
        if inst:
            out[t] = inst
    return out


def tradeable(universe: dict) -> dict:
    return {k: v for k, v in universe.items() if v.verified}


def impostor_report(universe: dict) -> dict:
    """Everything that tried to answer to a ticker it is not."""
    rows = []
    for t, inst in universe.items():
        for imp in inst.impostors:
            rows.append(dict(imp, ticker=t))
    rows.sort(key=lambda r: -(r.get("liquidity") or 0))
    return {
        "count": len(rows),
        "total_liquidity": sum(r.get("liquidity") or 0 for r in rows),
        "rows": rows,
    }
