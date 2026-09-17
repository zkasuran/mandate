# SPDX-License-Identifier: Apache-2.0
"""Verifiable track records, reconstructed from onchain fills.

This is the part that makes the product's claim real. A copy-trading
leaderboard normally shows numbers its own operator computed from its own
database. Here every fill is a Uniswap v3 `Swap` log, so the record is derived
from public data and anyone can recompute it from the same blocks.

Accounting rules, stated so a reader can check them rather than trust them:

  * cost basis is weighted average, the convention a retail statement uses
  * a sell realises P&L against the average basis at that moment
  * unrealised P&L marks the remaining position at the pool's current price
  * fees paid to the AMM are already inside the fill price, so they are not
    subtracted twice
  * a mark taken while the underlying equity market is shut is carried as
    uncertified, with the record saying so rather than quietly booking it

What this does not do: it cannot see trades routed through an aggregator that
settles somewhere other than the pools it watches. Coverage is stated on every
record instead of being implied.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from . import chain, uniswap
from .keccak import event_topic

SWAP_TOPIC = event_topic(
    "Swap(address,address,int256,int256,uint160,uint128,int24)")

MAX_RANGE = 500          # public RPCs cap eth_getLogs spans


def _to_signed(word: str) -> int:
    v = int(word, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


@dataclass
class Fill:
    block: int
    tx: str
    pool: str
    symbol: str
    side: str                 # buy | sell  (from the trader's point of view)
    base_amount: float        # signed positive, whole tokens
    quote_amount: float       # whole USDC
    price: float
    log_index: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Position:
    symbol: str
    token: str
    quantity: float = 0.0
    cost_basis: float = 0.0          # average price paid on the open quantity
    realised_pnl: float = 0.0
    quote_spent: float = 0.0
    quote_received: float = 0.0
    fills: list = field(default_factory=list)

    def apply(self, fill: Fill) -> None:
        if fill.side == "buy":
            total_cost = self.cost_basis * self.quantity + fill.quote_amount
            self.quantity += fill.base_amount
            self.cost_basis = total_cost / self.quantity if self.quantity else 0.0
            self.quote_spent += fill.quote_amount
        else:
            sold = min(fill.base_amount, self.quantity)
            self.realised_pnl += (fill.price - self.cost_basis) * sold
            self.quantity -= sold
            self.quote_received += fill.quote_amount
            if self.quantity <= 1e-12:
                self.quantity = 0.0
                self.cost_basis = 0.0
        self.fills.append(fill)

    def summary(self, mark: float | None, certified: bool | None) -> dict:
        unrealised = ((mark - self.cost_basis) * self.quantity
                      if mark and self.quantity else 0.0)
        invested = self.cost_basis * self.quantity
        return {
            "symbol": self.symbol,
            "token": self.token,
            "quantity": self.quantity,
            "cost_basis": self.cost_basis,
            "mark": mark,
            "mark_certified": certified,
            "realised_pnl": self.realised_pnl,
            "unrealised_pnl": unrealised,
            "total_pnl": self.realised_pnl + unrealised,
            "return_pct": ((unrealised / invested * 100) if invested else None),
            "quote_spent": self.quote_spent,
            "quote_received": self.quote_received,
            "fill_count": len(self.fills),
        }


def fetch_swaps(pool: str, from_block: int, to_block: int,
                trader: str | None = None) -> list[dict]:
    """Swap logs for a pool, optionally narrowed to one recipient.

    `recipient` is topic2 on the v3 Swap event, which is the address that
    received the output of the swap. For a router-mediated trade that is the
    router, so a caller passing `trader` should expect to miss those. The
    caller is told this rather than being given a silently short list.
    """
    logs: list[dict] = []
    start = from_block
    topics: list = [SWAP_TOPIC]
    if trader:
        topics = [SWAP_TOPIC, None, "0x" + chain.enc_addr(trader)]

    while start <= to_block:
        end = min(start + MAX_RANGE - 1, to_block)
        try:
            batch = chain.rpc("eth_getLogs", [{
                "address": pool,
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "topics": topics,
            }])
            logs.extend(batch)
        except chain.RpcError:
            # A provider that refuses this span is a provider problem, not an
            # empty result. Halve and retry rather than reporting no trades.
            if end > start:
                mid = (start + end) // 2
                logs.extend(fetch_swaps(pool, start, mid, trader))
                logs.extend(fetch_swaps(pool, mid + 1, end, trader))
            start = end + 1
            continue
        start = end + 1
    return logs


def decode_swap(log: dict, base_is_0: bool, base_dec: int,
                quote_dec: int, symbol: str) -> Fill | None:
    """Turn a raw Swap log into a fill from the trader's point of view."""
    body = log["data"][2:]
    amount0 = _to_signed(body[0:64])
    amount1 = _to_signed(body[64:128])
    base_raw, quote_raw = (amount0, amount1) if base_is_0 else (amount1, amount0)

    # Pool-signed: negative means the pool paid it out, so the trader received it.
    base_amt = abs(base_raw) / 10 ** base_dec
    quote_amt = abs(quote_raw) / 10 ** quote_dec
    if base_amt == 0 or quote_amt == 0:
        return None
    side = "buy" if base_raw < 0 else "sell"

    return Fill(
        block=int(log["blockNumber"], 16),
        tx=log["transactionHash"],
        pool=log["address"],
        symbol=symbol,
        side=side,
        base_amount=base_amt,
        quote_amount=quote_amt,
        price=quote_amt / base_amt,
        log_index=int(log["logIndex"], 16),
    )


def pool_activity(pool: str, symbol: str, base: str, base_dec: int,
                  lookback: int = 5_000, quote_dec: int = 6) -> list[Fill]:
    """Every fill in a pool over the last `lookback` blocks."""
    head = chain.block_number()
    state = uniswap.pool_state(pool)
    if not state:
        return []
    base_is_0 = (state["token0"] or "").lower() == base.lower()
    logs = fetch_swaps(pool, max(0, head - lookback), head)
    fills = [decode_swap(l, base_is_0, base_dec, quote_dec, symbol) for l in logs]
    return sorted([f for f in fills if f], key=lambda f: (f.block, f.log_index))


def track_record(fills: list[Fill], symbol: str, token: str,
                 mark: float | None = None,
                 mark_certified: bool | None = None) -> dict:
    """Replay fills in order into a position and report it."""
    pos = Position(symbol=symbol, token=token)
    for f in sorted(fills, key=lambda f: (f.block, f.log_index)):
        pos.apply(f)
    out = pos.summary(mark, mark_certified)
    out["fills"] = [f.to_dict() for f in pos.fills]
    out["coverage"] = (
        "computed from Uniswap v3 Swap logs on the pools this registry knows. "
        "Trades settled elsewhere are not counted."
    )
    return out
