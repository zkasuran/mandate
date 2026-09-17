# SPDX-License-Identifier: Apache-2.0
"""Offline tests for the issuer control-surface disclosure.

Bytecode fixtures are built from the real selectors, so a change to how a power
is detected fails here rather than silently dropping a disclosure a follower
was owed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import custody  # noqa: E402
from core.keccak import keccak256  # noqa: E402

NVDA = "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC"
AAPL = "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9"
BEACON = "0xe10b6f6b275de231345c20d14ab812db62151b00"
IMPL = "0xb35490d6f9163de4f80d88dc75c3516eb64c5ae2"


def code_with(signatures):
    """Bytecode that contains exactly these selectors."""
    return "0x" + "".join(keccak256(s.encode()).hex()[:8] for s in signatures)


class FakeChain:
    """Stands in for the chain so these tests never touch the network."""

    def __init__(self, storage, code):
        self.storage = storage          # (addr, slot) -> value
        self.code = code                # addr -> bytecode
        self.calls = {}                 # (addr, data) -> result

    def rpc(self, method, params, chain=None):
        addr, slot = params[0].lower(), params[1]
        return self.storage.get((addr, slot), "0x" + "0" * 64)

    def get_code(self, addr, chain=None):
        return self.code.get(addr.lower(), "0x")

    def try_call(self, to, data, chain=None):
        return self.calls.get((to.lower(), data))


class CustodyTestBase(unittest.TestCase):
    def install(self, fake):
        self._chain = custody.chain
        custody.chain = fake
        self.addCleanup(lambda: setattr(custody, "chain", self._chain))


class TestBeaconProxy(CustodyTestBase):
    def setUp(self):
        logic = code_with(["mint(address,uint256)", "burn(address,uint256)",
                           "pause()", "unpause()"])
        fake = FakeChain(
            storage={(NVDA.lower(), custody.BEACON_SLOT):
                     "0x" + "0" * 24 + BEACON[2:]},
            code={NVDA.lower(): "0x" + "60" * 283, IMPL.lower(): logic})
        fake.calls[(BEACON.lower(), custody.SEL_IMPLEMENTATION)] = \
            "0x" + "0" * 24 + IMPL[2:]
        self.install(fake)

    def test_detects_a_beacon_proxy(self):
        c = custody.capabilities("NVDA", NVDA)
        self.assertEqual(c.proxy, "beacon")
        self.assertEqual(c.beacon.lower(), BEACON.lower())
        self.assertEqual(c.implementation.lower(), IMPL.lower())
        self.assertTrue(c.upgradeable)

    def test_scans_the_implementation_not_the_proxy(self):
        # The proxy's own 283 bytes contain none of these. Scanning it instead
        # of the implementation would report a token with no powers at all.
        c = custody.capabilities("NVDA", NVDA)
        found = {p["function"] for p in c.powers}
        self.assertEqual(found, {"mint(address,uint256)", "burn(address,uint256)",
                                 "pause()", "unpause()"})

    def test_every_power_carries_what_it_means_for_a_follower(self):
        c = custody.capabilities("NVDA", NVDA)
        for p in c.powers:
            self.assertTrue(p["means"])
            self.assertTrue(p["selector"].startswith("0x"))

    def test_the_beacon_note_is_stated(self):
        c = custody.capabilities("NVDA", NVDA)
        self.assertTrue(any("beacon" in n for n in c.notes))


class TestDirectProxy(CustodyTestBase):
    def setUp(self):
        fake = FakeChain(
            storage={(NVDA.lower(), custody.IMPL_SLOT):
                     "0x" + "0" * 24 + IMPL[2:]},
            code={NVDA.lower(): "0x" + "60" * 100,
                  IMPL.lower(): code_with(["upgradeTo(address)"])})
        self.install(fake)

    def test_detects_an_eip1967_proxy(self):
        c = custody.capabilities("NVDA", NVDA)
        self.assertEqual(c.proxy, "eip1967")
        self.assertTrue(c.upgradeable)
        self.assertEqual([p["function"] for p in c.powers], ["upgradeTo(address)"])


class TestPlainToken(CustodyTestBase):
    def setUp(self):
        self.install(FakeChain(storage={},
                               code={NVDA.lower(): code_with(["decimals()"])}))

    def test_a_token_with_no_proxy_slot_is_not_upgradeable(self):
        c = custody.capabilities("NVDA", NVDA)
        self.assertEqual(c.proxy, "none")
        self.assertFalse(c.upgradeable)
        self.assertEqual(c.powers, [])

    def test_absence_of_powers_is_reported_rather_than_assumed(self):
        c = custody.capabilities("NVDA", NVDA)
        self.assertTrue(any("no proxy slot" in n for n in c.notes))


class Inst:
    def __init__(self, address):
        self.address = address
        self.verified = True


class TestSurvey(CustodyTestBase):
    def setUp(self):
        logic = code_with(["mint(address,uint256)", "pause()"])
        fake = FakeChain(
            storage={(NVDA.lower(), custody.BEACON_SLOT): "0x" + "0" * 24 + BEACON[2:],
                     (AAPL.lower(), custody.BEACON_SLOT): "0x" + "0" * 24 + BEACON[2:]},
            code={NVDA.lower(): "0x6060", AAPL.lower(): "0x6060", IMPL.lower(): logic})
        fake.calls[(BEACON.lower(), custody.SEL_IMPLEMENTATION)] = \
            "0x" + "0" * 24 + IMPL[2:]
        self.install(fake)
        self.universe = {"NVDA": Inst(NVDA), "AAPL": Inst(AAPL)}

    def test_a_shared_beacon_is_reported_as_concentration(self):
        # Two tokens behind one beacon is one party, not two. A per-token view
        # that omits this understates the risk.
        s = custody.survey(self.universe)
        self.assertEqual(s["distinct_beacons"], 1)
        self.assertEqual(len(s["concentration"]), 1)
        self.assertEqual(s["concentration"][0]["controls"], ["AAPL", "NVDA"])

    def test_every_instrument_is_listed_as_upgradeable(self):
        s = custody.survey(self.universe)
        self.assertEqual(sorted(s["upgradeable"]), ["AAPL", "NVDA"])

    def test_unverified_instruments_are_not_surveyed(self):
        inst = Inst(NVDA)
        inst.verified = False
        s = custody.survey({"NVDA": inst})
        self.assertEqual(s["instruments"], {})

    def test_a_single_instrument_is_not_concentration(self):
        s = custody.survey({"NVDA": Inst(NVDA)})
        self.assertEqual(s["concentration"], [])

    def test_the_disclosure_frames_this_as_information(self):
        s = custody.survey(self.universe)
        self.assertIn("not as an accusation", s["disclosure"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
