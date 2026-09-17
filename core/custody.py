# SPDX-License-Identifier: Apache-2.0
"""What the issuer can still do to a position after a follower takes it.

Depth and ticker verification answer "can I trade this". Neither answers the
question a follower should ask before committing money for a month: once I hold
this token, what can somebody else do to it without asking me?

For the tokenized equities on Robinhood Chain the answer is substantial. All
of it was read off the chain rather than taken from a doc:

  * every instrument in the universe is a **beacon proxy**, 283 bytes of
    forwarding logic in front of shared code
  * all eight point at the **same beacon**, so one upgrade changes the behaviour
    of every tokenized equity on the chain at once
  * the implementation behind that beacon carries `mint`, `burn`, `pause` and
    `unpause`

None of that is an accusation. A regulated issuer needs exactly these powers.
A token that could not be paused or reissued would be the surprising thing.
What is wrong is a system that takes a follower's money for a month without
telling them the powers exist.

So this module reports them. `capabilities()` reads the proxy pattern, resolves
the beacon, then scans the implementation's own bytecode for the selectors that
matter. It is a disclosure, not a verdict: nothing here blocks a trade, because
refusing to trade a regulated equity for being pausable would refuse all of them.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field

from . import chain
from .keccak import keccak256

# EIP-1967 storage slots.
IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"

SEL_IMPLEMENTATION = "0x5c60da1b"          # implementation()

# Powers worth telling a follower about, with what each one means for them.
POWERS = {
    "mint(address,uint256)":
        "the issuer can create new units, so supply is not fixed",
    "burn(address,uint256)":
        "the issuer can destroy units",
    "pause()":
        "transfers can be halted, which would strand an open position",
    "unpause()":
        "the halt is reversible by the same party",
    "blacklist(address)":
        "an address can be denied transfers",
    "freeze(address)":
        "an address's balance can be frozen",
    "seize(address,address,uint256)":
        "a balance can be taken from one address and given to another",
    "upgradeTo(address)":
        "the logic behind this token can be replaced",
    "upgradeToAndCall(address,bytes)":
        "the logic can be replaced and called in the same transaction",
}


@dataclass
class Custody:
    ticker: str
    address: str
    chain_key: str
    proxy: str                       # beacon | eip1967 | none
    beacon: str | None = None
    implementation: str | None = None
    admin: str | None = None
    proxy_bytes: int = 0
    logic_bytes: int = 0
    powers: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def upgradeable(self) -> bool:
        return self.proxy != "none"


def _slot(addr: str, slot: str, chain_key: str) -> str | None:
    raw = chain.rpc("eth_getStorageAt", [addr, slot, "latest"], chain=chain_key)
    return ("0x" + raw[-40:]) if raw and int(raw, 16) else None


def capabilities(ticker: str, address: str, chain_key: str = "robinhood") -> Custody:
    """Read the control surface of one token, off the chain."""
    code = chain.get_code(address, chain=chain_key)
    out = Custody(ticker=ticker, address=address, chain_key=chain_key,
                  proxy="none", proxy_bytes=(len(code) - 2) // 2)

    beacon = _slot(address, BEACON_SLOT, chain_key)
    direct = _slot(address, IMPL_SLOT, chain_key)
    out.admin = _slot(address, ADMIN_SLOT, chain_key)

    if beacon:
        out.proxy = "beacon"
        out.beacon = beacon
        raw = chain.try_call(beacon, SEL_IMPLEMENTATION, chain=chain_key)
        out.implementation = ("0x" + raw[-40:]) if raw else None
        out.notes.append(
            "a beacon proxy: the logic is chosen by the beacon, not by this "
            "token, so whoever controls the beacon controls this token")
    elif direct:
        out.proxy = "eip1967"
        out.implementation = direct
        out.notes.append("an upgradeable proxy: the logic can be replaced")
    else:
        out.notes.append("no proxy slot set, the code at this address is the code")

    target = out.implementation or address
    logic = chain.get_code(target, chain=chain_key)
    out.logic_bytes = (len(logic) - 2) // 2
    body = logic[2:]

    for sig, meaning in POWERS.items():
        sel = keccak256(sig.encode()).hex()[:8]
        if sel in body:
            out.powers.append({"function": sig, "selector": "0x" + sel,
                               "means": meaning})
    return out


def survey(universe: dict, chain_key: str = "robinhood") -> dict:
    """The control surface across a whole universe, plus the concentration.

    The per-token view understates the risk. What matters is how many distinct
    parties there are: eight tokens behind one beacon is one party, not eight.
    """
    rows = {}
    for t, inst in universe.items():
        if not getattr(inst, "verified", False):
            continue
        rows[t] = capabilities(t, inst.address, chain_key)

    beacons: dict = {}
    impls: dict = {}
    for t, c in rows.items():
        if c.beacon:
            beacons.setdefault(c.beacon, []).append(t)
        if c.implementation:
            impls.setdefault(c.implementation, []).append(t)

    concentration = []
    for b, tickers in beacons.items():
        if len(tickers) > 1:
            concentration.append({
                "beacon": b,
                "controls": sorted(tickers),
                "note": (f"one upgrade here changes the behaviour of "
                         f"{len(tickers)} instruments at once"),
            })

    every_power = sorted({p["function"] for c in rows.values() for p in c.powers})
    return {
        "chain": chain_key,
        "instruments": {t: c.to_dict() for t, c in rows.items()},
        "upgradeable": sorted(t for t, c in rows.items() if c.upgradeable),
        "distinct_beacons": len(beacons),
        "distinct_implementations": len(impls),
        "concentration": concentration,
        "powers_present": every_power,
        "disclosure": (
            "These are powers a regulated issuer normally needs. They are "
            "listed so a follower knows they exist before committing money, "
            "not as an accusation. Nothing here blocks a trade."
        ),
    }
