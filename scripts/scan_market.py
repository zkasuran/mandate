# SPDX-License-Identifier: Apache-2.0
"""Live market scan: every tokenized equity, every pool, classified.

This is the measurement the product rests on. Re-run it and the numbers move,
because it reads the chain rather than a cached list.
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import chain, tokens, depth, uniswap  # noqa: E402

MARK = {"live": "LIVE   ", "thin": "THIN   ", "dead": "DEAD   ", "trapped": "TRAPPED"}


def main() -> int:
    blk = chain.block_number()
    print(f"Base block {blk:,}\n")

    registry = tokens.verified()
    out = {"block": blk, "tokens": {}}
    counts = {"live": 0, "thin": 0, "dead": 0, "trapped": 0}

    for sym, t in registry.items():
        if not t.get("ok"):
            print(f"{sym}: REJECTED  {t.get('error') or t.get('notes')}")
            continue
        print(f"{sym}  {t['name']}  dec={t['decimals']}  "
              f"supply={t['supply_human']:,.2f}")
        reports = depth.analyse_token(t["address"], t["decimals"])
        rows = []
        for r in sorted(reports, key=lambda x: (x.quote, x.fee)):
            q = "USDC" if r.quote.lower() == uniswap.USDC.lower() else "WETH"
            counts[r.status] += 1
            print(f"   {MARK[r.status]} fee={r.fee:>5} /{q}  px={r.price:>12,.2f}  "
                  f"tick={r.tick:>8}  depth=${r.quote_usd_held:>14,.2f}")
            for why in r.reasons:
                print(f"           ! {why}")
            rows.append(r.to_dict())
        out["tokens"][sym] = {"meta": t, "pools": rows}
        print()

    total = sum(counts.values())
    print(f"TOTALS  pools={total}  " +
          "  ".join(f"{k}={v}" for k, v in counts.items()))
    unusable = counts["dead"] + counts["trapped"]
    if total:
        print(f"unusable (dead or trapped): {unusable}/{total} "
              f"= {100 * unusable / total:.0f}%")
    out["totals"] = dict(counts, pools=total, unusable=unusable)

    os.makedirs("data", exist_ok=True)
    with open("data/market-scan.json", "w") as fh:
        json.dump(out, fh, indent=1)
    print("\nwrote data/market-scan.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
