#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Mandate command line. One entry point for the whole pipeline.

    ./mandate.py verify                 every address and selector, off chain
    ./mandate.py universe               resolve tickers, show refused lookalikes
    ./mandate.py ghosts                 classify Uniswap v3 pools on Base
    ./mandate.py publish  --name .. --legs NVDA:40,META:30,SPY:30
    ./mandate.py mandates               what has been published
    ./mandate.py subscribe --mandate .. --sleeve 5000
    ./mandate.py run      --mandate .. --bond 300
    ./mandate.py portfolio
    ./mandate.py board                  the leaderboard
    ./mandate.py audit    --mandate ..  fills against the hashed rules
    ./mandate.py tape     --symbol ..   a track record rebuilt from chain logs
    ./mandate.py token                  what $MANDATE is for

Everything reads live state. Nothing in here can move funds: the venue returns
a payload to be signed, then no key is held anywhere in this repo.
"""
from __future__ import annotations

import argparse
import json
import sys

from core import (basis, bond, book, chains, exits, flash, spec, universe)
from agent.executor import Executor

FOLLOWER = "0xDB6c6340342e71A63cD11Ebac2185204b7777777"


def usd(v, d=2):
    return "-" if v is None else f"${v:,.{d}f}"


def rule(title=""):
    print(f"\n{title}\n" + "=" * max(len(title), 60) if title else "=" * 60)


# --- commands ---------------------------------------------------------------

def cmd_verify(args):
    from scripts import verify_addresses  # noqa
    return verify_addresses.main()


def cmd_universe(args):
    u = universe.build(args.tickers or universe.TICKERS, args.chain)
    rule(f"UNIVERSE on {chains.get(args.chain).name}")
    print(f"{'TICKER':8}{'STATE':10}{'PRICE':>10}{'LIQUIDITY':>14}"
          f"{'24H VOLUME':>16}{'HOLDERS':>10}  NAME")
    for t, i in sorted(u.items(), key=lambda kv: -(kv[1].liquidity or 0)):
        print(f"{t:8}{'verified' if i.verified else 'REFUSED':10}"
              f"{usd(i.price):>10}{usd(i.liquidity, 0):>14}"
              f"{usd(i.volume24h, 0):>16}{(f'{i.holders:,}' if i.holders else '-'):>10}"
              f"  {i.name[:34]}")
        for n in i.notes:
            print(f"        ! {n}")
    rep = universe.impostor_report(u)
    rule(f"REFUSED LOOKALIKES: {rep['count']} holding "
         f"{usd(rep['total_liquidity'], 0)} between them")
    for r in rep["rows"][:args.limit]:
        print(f"  {r['ticker']:6} <- {r['symbol'][:34]:34} "
              f"{usd(r.get('liquidity'), 0):>12}  holders="
              f"{(r.get('holders') or 0):>8,}")
    if args.json:
        print(json.dumps({k: v.to_dict() for k, v in u.items()}, indent=1))
    return 0


def cmd_ghosts(args):
    from core import depth, tokens, uniswap
    rule("UNISWAP V3 POOLS ON BASE")
    counts = {"live": 0, "thin": 0, "dead": 0, "trapped": 0}
    eth = uniswap.eth_usd_price()
    for sym, t in tokens.verified().items():
        if not t.get("ok"):
            continue
        print(f"\n{sym}  {t['name']}")
        for r in sorted(depth.analyse_token(t["address"], t["decimals"], eth_usd=eth),
                        key=lambda x: (x.quote, x.fee)):
            counts[r.status] += 1
            q = "USDC" if r.quote.lower() == uniswap.USDC.lower() else "WETH"
            px = (f"{r.price:.3e}" if r.price > 1e12 else usd(r.price))
            print(f"   {r.status.upper():8} fee={r.fee:>5} /{q}  {px:>16}  "
                  f"depth={usd(r.quote_usd_held):>14}  tick={r.tick:>8}")
            for why in r.reasons:
                print(f"            ! {why}")
    total = sum(counts.values())
    bad = counts["dead"] + counts["trapped"]
    rule(f"{total} pools: " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"unusable (dead or trapped): {bad}/{total} = {100 * bad / total:.0f}%")
    return 0


def _parse_legs(text, u):
    legs, total = [], 0
    for part in text.split(","):
        sym, _, pct = part.partition(":")
        sym = sym.strip().upper()
        w = int(round(float(pct or 0) * 100))
        inst = u.get(sym)
        if not inst or not inst.verified:
            raise SystemExit(f"{sym} did not verify, refusing to build a mandate on it")
        legs.append(spec.Leg(sym, inst.address, w))
        total += w
    if total != 10_000:
        raise SystemExit(f"weights sum to {total / 100:.1f}%, they must sum to 100%")
    return tuple(legs)


def cmd_publish(args):
    u = universe.build(chain_key=args.chain)
    legs = _parse_legs(args.legs, u)
    m = spec.Mandate(
        name=args.name, strategist=args.strategist, thesis=args.thesis, legs=legs,
        execution=spec.Execution(kind=args.kind, slices=args.slices,
                                 interval_seconds=args.interval,
                                 max_slippage_bps=args.slippage,
                                 min_depth_multiple=args.cover,
                                 require_market_hours=args.market_hours),
        risk=spec.Risk(stop_loss_bps=args.stop, take_profit_bps=args.take),
        fee_bps=args.fee, chain_id=chains.get(args.chain).chain_id)
    m.validate()
    res = book.put_mandate(m.to_dict())
    rule("PUBLISHED" if res["stored"] else "ALREADY PUBLISHED")
    print(m.signing_message())
    rule("CANONICAL RULES, the exact bytes the id was taken over")
    print(m.canonical().decode())
    return 0


def cmd_mandates(args):
    rows = book.read()["mandates"]
    rule(f"{len(rows)} PUBLISHED MANDATES")
    for r in rows:
        ex, rk = r["rules"]["execution"], r["rules"]["risk"]
        print(f"\n{r['name']}\n  {r['mandate_id']}")
        print(f"  {r['thesis']}")
        print("  " + "  ".join(f"{l['symbol']} {l['weight_bps'] / 100:.0f}%"
                               for l in r["rules"]["legs"]))
        print(f"  {ex['kind']} x{ex['slices']} | slip {ex['max_slippage_bps']}bps | "
              f"cover {ex['min_depth_multiple']}x | "
              f"{'market hours only' if ex['require_market_hours'] else '24/7'} | "
              f"stop {rk['stop_loss_bps']}bps | fee {r['rules']['fee_bps']}bps")
    return 0


def cmd_subscribe(args):
    res = book.subscribe(args.mandate, args.follower, args.sleeve, args.wallet)
    s = res["subscription"]
    rule("SUBSCRIBED" if res["created"] else "ALREADY SUBSCRIBED")
    print(f"  {s['subscription_id']}  sleeve {usd(s['sleeve_usd'])}  "
          f"wallet {s['wallet_kind']}")
    committed = sum(x["sleeve_usd"] for x in
                    book.subscriptions(args.mandate, active_only=True))
    print(f"  strategist must now bond {usd(bond.required_bond(committed))} "
          f"against {usd(committed)} of committed follower money")
    return 0


def cmd_run(args):
    subs = book.subscriptions(args.mandate, follower=args.follower, active_only=True)
    if not subs:
        raise SystemExit("no active subscription for that mandate and follower")
    u = universe.build(chain_key=args.chain)
    r = Executor(u, args.chain).run(subs[0], book.get_mandate(args.mandate),
                                    posted_bond_usd=args.bond)
    rule("AGENT RUN")
    for st in r.steps:
        print(f"  [{'PASS' if st.ok else 'STOP'}] {st.name:13} {st.detail}")
    if r.fills:
        rule(f"{len(r.fills)} ENTRIES")
        for f in r.fills:
            print(f"  {f['ticker']:5} {usd(f['spend_usd']):>10} -> {f['quantity']:>12.6f} "
                  f"@ {usd(f['price']):>10}  slip={f['slippage_bps']}bps  "
                  f"cover={f['depth_cover']:,.0f}x  {f['order_type']}")
    for x in r.refusals:
        print(f"  REFUSED {x['ticker']} {usd(x['spend_usd'])}: {x['reason']}")
    ep = r.exits.get("plan", {})
    if ep.get("exits"):
        rule(f"{len([e for e in ep['exits'] if e['placed']])}/{len(ep['exits'])} "
             "PROTECTIVE EXITS RESTING")
        for e in ep["exits"]:
            print(f"  {e['kind']:12} {e['ticker']:5} qty={e['quantity']:>10.4f} "
                  f"trigger={usd(e['trigger_price']):>10}  placed={e['placed']}")
        print(f"  coverage: {r.exits['coverage']['coverage_pct']:.0f}% of legs carry a live stop")
    for w in r.warnings:
        print(f"  warn: {w}")
    if args.json:
        print(json.dumps(r.to_dict(), indent=1, default=str))
    return 0


def cmd_portfolio(args):
    u = universe.build(chain_key=args.chain)
    marks = {t: i.price for t, i in universe.tradeable(u).items()}
    p = book.portfolio(args.follower, marks, basis.session_state().open)
    rule(f"PORTFOLIO {args.follower}")
    print(f"  committed {usd(p['committed_usd'])}   deployed {usd(p['deployed_usd'])}"
          f"   fees accrued {usd(p['fees_accrued_usd'])}   fills {p['fill_count']}")
    for h in p["holdings"]:
        print(f"  {h['ticker']:5} qty={h['quantity']:>12.6f}  basis={usd(h['cost_basis']):>10}"
              f"  mark={usd(h['mark']):>10}  value={usd(h['market_value']):>12}"
              f"  pnl={usd(h['total_pnl']):>10}")
    print(f"  market value {usd(p['market_value'])}   unrealised "
          f"{usd(p['unrealised_pnl'])}   realised {usd(p['realised_pnl'])}")
    print(f"  marks certified: {p['marks_certified']}")
    print(f"  {p['basis']}")
    return 0


def cmd_board(args):
    u = universe.build(chain_key=args.chain)
    marks = {t: i.price for t, i in universe.tradeable(u).items()}
    rule("LEADERBOARD")
    for r in book.leaderboard(marks):
        print(f"  {r['name']:22} followers={r['followers']:<3} "
              f"volume={usd(r['volume_usd']):>12} fees={usd(r['strategist_fees_usd']):>9} "
              f"pnl={usd(r['total_pnl']):>10} fills={r['fills']}")
        print(f"     {r['mandate_id']}")
    print("\n  ranked on followed volume and recomputed outcome. "
          "no strategist supplies a number to this table.")
    return 0


def cmd_audit(args):
    m = book.get_mandate(args.mandate)
    if not m:
        raise SystemExit("unknown mandate")
    u = universe.build(chain_key=args.chain)
    fills = book.fills(mandate_id=args.mandate)
    a = bond.audit(m, fills, u)
    rule("COMPLIANCE AUDIT against the rules hashed into the id")
    print(f"  fills checked {a['fills_checked']}   clean={a['clean']}   "
          f"slash={a['slash_bps'] / 100:.0f}% of bond")
    for v in a["violations"]:
        print(f"  BREACH {v['kind']:22} harm={usd(v['harmed_usd']):>10}  {v['detail']}")
    print(f"  {a['basis']}")
    if args.bond:
        b = bond.bond_for(args.mandate, m["strategist"], args.bond,
                          sum(s["sleeve_usd"] for s in
                              book.subscriptions(args.mandate, active_only=True)))
        st = bond.settle(b, a)
        print(f"\n  slashed {usd(st['slashed_usd'])} of {usd(b.posted_usd)}")
        for p in st["payouts"]:
            print(f"    {p['follower']} receives {usd(p['payout_usd'])} "
                  f"({p['share'] * 100:.1f}% of harm)")
        print(f"  {st['note']}")
    return 0


def cmd_tape(args):
    """Rebuild a track record from raw Uniswap Swap logs on Base.

    This is the claim made concrete: performance derived from public logs by
    anyone, at a block height, with no cooperation from the party being
    measured. It reads Base because that is where an auditable AMM sits.
    """
    from core import depth, ledger, tokens, uniswap
    reg = tokens.verified()
    meta = reg.get(args.symbol)
    if not meta or not meta.get("ok"):
        raise SystemExit(f"unknown symbol {args.symbol}, known: "
                         f"{', '.join(k for k, v in reg.items() if v.get('ok'))}")
    usable = [r for r in depth.analyse_token(meta["address"], meta["decimals"])
              if r.status in ("live", "thin")
              and r.quote.lower() == uniswap.USDC.lower()]
    if not usable:
        raise SystemExit("no live USDC pool to read fills from")
    pool = max(usable, key=lambda r: r.quote_usd_held)
    fills = ledger.pool_activity(pool.pool, args.symbol, meta["address"],
                                 meta["decimals"], lookback=args.blocks)
    session = basis.session_state()
    rec = ledger.track_record(fills, args.symbol, meta["address"],
                              mark=pool.price, mark_certified=session.open)
    rule(f"TAPE {args.symbol} over the last {args.blocks:,} blocks")
    print(f"  pool {pool.pool}  ({pool.status}, depth {usd(pool.quote_usd_held)})")
    print(f"  fills {rec['fill_count']}   bought {usd(rec['quote_spent'])}   "
          f"sold {usd(rec['quote_received'])}")
    print(f"  net position {rec['quantity']:.6f} at basis {usd(rec['cost_basis'])}, "
          f"mark {usd(rec['mark'])} (certified={rec['mark_certified']})")
    print(f"  realised {usd(rec['realised_pnl'])}   unrealised {usd(rec['unrealised_pnl'])}")
    for f in rec["fills"][-args.show:]:
        print(f"    blk {f['block']:,}  {f['side']:4} {f['base_amount']:>12.6f}  "
              f"{usd(f['quote_amount']):>12}  @ {usd(f['price']):>10}  {f['tx'][:18]}")
    print(f"\n  {rec['coverage']}")
    return 0


def cmd_token(args):
    s = bond.spec()
    rule(f"${s['symbol']} on {s['chain']}, launched through {s['launch']}")
    for r in s["roles"]:
        print(f"\n  {r['role']}")
        print(f"    {r['why']}")
    print("\n  deliberately not:")
    for x in s["deliberately_not"]:
        print(f"    - {x}")
    print("\n  penalties, in bps of the bond:")
    for k, v in s["penalties_bps"].items():
        print(f"    {k:24} {v / 100:>5.0f}%")
    return 0


# --- wiring -----------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="mandate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chain", default="robinhood", choices=sorted(chains.CHAINS))
    ap.add_argument("--json", action="store_true", help="also dump raw json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("verify").set_defaults(fn=cmd_verify)

    p = sub.add_parser("universe")
    p.add_argument("tickers", nargs="*")
    p.add_argument("--limit", type=int, default=12)
    p.set_defaults(fn=cmd_universe)

    sub.add_parser("ghosts").set_defaults(fn=cmd_ghosts)

    p = sub.add_parser("publish")
    p.add_argument("--name", required=True)
    p.add_argument("--legs", required=True, help="NVDA:40,META:30,SPY:30")
    p.add_argument("--thesis", default="Published from the command line.")
    p.add_argument("--strategist", default=FOLLOWER)
    p.add_argument("--kind", default="twap", choices=sorted(spec.ORDER_KINDS))
    p.add_argument("--slices", type=int, default=4)
    p.add_argument("--interval", type=int, default=900)
    p.add_argument("--slippage", type=int, default=50)
    p.add_argument("--cover", type=int, default=3)
    p.add_argument("--stop", type=int, default=800)
    p.add_argument("--take", type=int, default=1500)
    p.add_argument("--fee", type=int, default=100)
    p.add_argument("--market-hours", action="store_true",
                   help="refuse to trade outside the underlying session")
    p.set_defaults(fn=cmd_publish)

    sub.add_parser("mandates").set_defaults(fn=cmd_mandates)

    p = sub.add_parser("subscribe")
    p.add_argument("--mandate", required=True)
    p.add_argument("--sleeve", type=float, required=True)
    p.add_argument("--follower", default=FOLLOWER)
    p.add_argument("--wallet", default="dynamic-delegated")
    p.set_defaults(fn=cmd_subscribe)

    p = sub.add_parser("run")
    p.add_argument("--mandate", required=True)
    p.add_argument("--bond", type=float, default=0.0)
    p.add_argument("--follower", default=FOLLOWER)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("portfolio")
    p.add_argument("--follower", default=FOLLOWER)
    p.set_defaults(fn=cmd_portfolio)

    sub.add_parser("board").set_defaults(fn=cmd_board)

    p = sub.add_parser("audit")
    p.add_argument("--mandate", required=True)
    p.add_argument("--bond", type=float, default=0.0,
                   help="also settle a slash against this posted bond")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("tape")
    p.add_argument("--symbol", default="AAPLc",
                   help="a Base B20 symbol, which is where an auditable AMM sits")
    p.add_argument("--blocks", type=int, default=5_000)
    p.add_argument("--show", type=int, default=10)
    p.set_defaults(fn=cmd_tape)

    sub.add_parser("token").set_defaults(fn=cmd_token)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except flash.FlashError as e:
        print(f"venue refused: {e}", file=sys.stderr)
        return 2
    except spec.MandateError as e:
        print(f"invalid mandate: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
