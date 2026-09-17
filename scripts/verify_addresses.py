# SPDX-License-Identifier: Apache-2.0
"""Read every address this project uses off Base, live, and refuse the bad ones.

Run this before trusting anything the repo says about an address. It exists
because a documented example address turned out to carry no code on Base, and
a published address that nobody re-checked is the cheapest kind of wrong.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import chain, tokens, uniswap, wallet  # noqa: E402

INFRA = {
    "Uniswap v3 factory": uniswap.V3_FACTORY,
    "Uniswap v4 PoolManager": uniswap.V4_POOL_MANAGER,
    "Uniswap SwapRouter02": wallet.SWAP_ROUTER_02,
    "USDC": uniswap.USDC,
    "WETH": uniswap.WETH,
}

# Selectors this project derives with its own keccak, checked against the
# values the ecosystem publishes. A wrong selector builds calldata that reverts.
SELECTORS = {
    "exactInputSingle": (wallet.EXACT_INPUT_SINGLE, "0x04e45aaf"),
    "approve": (wallet.APPROVE, "0x095ea7b3"),
}


def main() -> int:
    failures = []
    print(f"Base block {chain.block_number():,}\n")

    print("infrastructure")
    for name, addr in INFRA.items():
        ok = chain.has_code(addr)
        print(f"  {'OK  ' if ok else 'FAIL'} {name:26} {addr}")
        if not ok:
            failures.append(f"{name} has no code at {addr}")

    print("\nselectors")
    for name, (got, expect) in SELECTORS.items():
        ok = got == expect
        print(f"  {'OK  ' if ok else 'FAIL'} {name:26} {got} (expected {expect})")
        if not ok:
            failures.append(f"{name} selector is {got}, expected {expect}")

    print("\ntokenized equities")
    for sym, t in tokens.verified().items():
        if not t.get("ok"):
            print(f"  FAIL {sym:26} {t.get('error') or '; '.join(t.get('notes', []))}")
            failures.append(f"{sym}: {t.get('error') or t.get('notes')}")
            continue
        print(f"  OK   {sym:26} {t['address']}  {t['name']} "
              f"({t['decimals']}dp, {t['supply_human']:,.2f} outstanding)")
        for n in t.get("notes", []):
            print(f"       note: {n}")

    print()
    if failures:
        print(f"{len(failures)} FAILURES")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("every address and selector verified against live Base state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
