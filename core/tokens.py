# SPDX-License-Identifier: Apache-2.0
"""The tokenized-equity registry, verified against Base at load time.

Every entry here is a candidate until the chain confirms it. `verified()`
reads name, symbol, decimals and supply straight off each contract and drops
anything that does not answer. A documentation example address that carried no
code on Base is exactly why this file trusts nothing it was told.
"""
from __future__ import annotations

from . import chain

# B20 tokenized equities on Base. Addresses carry the issuer's 0xb20000 vanity
# prefix. Symbols below are the expected values, checked against the contract.
CANDIDATES = {
    "AAPLc": "0xb200000000000000000000C2e324d24d7eEcd1fb",
    "GOOGLc": "0xb2000000000000000000002D0BA3164cc74f58B7",
    "METAc": "0xb2000000000000000000008bC8786B856E61707C",
    "NVDAc": "0xb20000000000000000000078ee7ce2fE4908108C",
}

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"

B20_PREFIX = "0xb20000000000000000000"


def verified(candidates: dict | None = None) -> dict:
    """Read each candidate off the chain. Only confirmed tokens come back.

    A mismatch between the expected symbol and the contract's own symbol is
    reported rather than swallowed, because that is the shape of a bad copy or
    a swapped address.
    """
    out = {}
    for expected_symbol, addr in (candidates or CANDIDATES).items():
        info = chain.erc20(addr)
        if not info["code"]:
            out[expected_symbol] = {"address": addr, "ok": False,
                                    "error": "no code at this address on Base"}
            continue
        ok = True
        notes = []
        if info["symbol"] != expected_symbol:
            ok = False
            notes.append(f"contract reports symbol {info['symbol']!r}, "
                         f"registry expected {expected_symbol!r}")
        if info["decimals"] is None:
            ok = False
            notes.append("decimals() did not answer")
        if not addr.lower().startswith(B20_PREFIX):
            notes.append("address does not carry the B20 vanity prefix")
        out[expected_symbol] = {
            "address": addr,
            "ok": ok,
            "symbol": info["symbol"],
            "name": info["name"],
            "decimals": info["decimals"],
            "total_supply": info["totalSupply"],
            "supply_human": (info["totalSupply"] / 10 ** info["decimals"]
                             if info["decimals"] else None),
            "notes": notes,
        }
    return out
