# SPDX-License-Identifier: Apache-2.0
"""Offline tests for protective exits.

The venue calls are not exercised here. What is tested is the part that decides
what to protect and how much of the position is covered, because that is where
a silent hole leaves a follower unprotected while a summary says otherwise.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import exits, spec  # noqa: E402

NVDA = "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC"
META = "0xc0D6457C16Cc70d6790Dd43521C899C87ce02f35"


class Inst:
    def __init__(self, address, verified=True):
        self.address = address
        self.verified = verified


UNIVERSE = {"NVDA": Inst(NVDA), "META": Inst(META)}


def mandate(stop=800, take=1500, legs=(("NVDA", NVDA, 10_000),)):
    m = spec.Mandate(
        name="T", strategist="0x" + "11" * 20, thesis="t",
        legs=tuple(spec.Leg(s, a, w) for s, a, w in legs),
        execution=spec.Execution(kind="twap", slices=2, interval_seconds=900,
                                 max_slippage_bps=50, min_depth_multiple=3,
                                 require_market_hours=False),
        risk=spec.Risk(stop_loss_bps=stop, take_profit_bps=take),
        fee_bps=100, chain_id=4663)
    m.validate()
    return m


HOLD = {"NVDA": {"quantity": 10.0, "cost_basis": 200.0}}


class TestDerive(unittest.TestCase):
    def test_both_exits_derived_from_the_basis(self):
        p = exits.derive(mandate(), HOLD, UNIVERSE, "robinhood")
        self.assertEqual(len(p.exits), 2)
        stop = next(e for e in p.exits if e.kind == "stop_loss")
        take = next(e for e in p.exits if e.kind == "take_profit")
        self.assertAlmostEqual(stop.trigger_price, 184.0)     # 200 * (1 - 0.08)
        self.assertAlmostEqual(take.trigger_price, 230.0)     # 200 * (1 + 0.15)
        self.assertEqual(stop.reference_price, 200.0)

    def test_triggers_use_basis_not_a_live_mark(self):
        # A mark is deliberately never passed in. If a future change starts
        # deriving from one, this holds the line: the stop a follower agreed to
        # is the one measured from what they actually paid.
        p = exits.derive(mandate(), {"NVDA": {"quantity": 1.0, "cost_basis": 100.0}},
                         UNIVERSE, "robinhood")
        self.assertTrue(all(e.reference_price == 100.0 for e in p.exits))

    def test_exit_sells_the_whole_position(self):
        p = exits.derive(mandate(), {"NVDA": {"quantity": 7.25, "cost_basis": 50.0}},
                         UNIVERSE, "robinhood")
        self.assertTrue(all(e.quantity == 7.25 for e in p.exits))

    def test_no_position_means_no_exit(self):
        p = exits.derive(mandate(), {"NVDA": {"quantity": 0.0, "cost_basis": 0.0}},
                         UNIVERSE, "robinhood")
        self.assertEqual(p.exits, [])

    def test_unverified_leg_is_refused_and_reported(self):
        u = {"NVDA": Inst(NVDA, verified=False)}
        p = exits.derive(mandate(), HOLD, u, "robinhood")
        self.assertEqual(p.exits, [])
        self.assertTrue(any("unverified" in w for w in p.warnings))

    def test_zero_basis_cannot_produce_a_trigger(self):
        p = exits.derive(mandate(), {"NVDA": {"quantity": 5.0, "cost_basis": 0.0}},
                         UNIVERSE, "robinhood")
        self.assertEqual(p.exits, [])
        self.assertTrue(any("cost basis" in w for w in p.warnings))

    def test_a_mandate_with_no_exits_says_so(self):
        p = exits.derive(mandate(stop=None, take=None), HOLD, UNIVERSE, "robinhood")
        self.assertEqual(p.exits, [])
        self.assertTrue(any("no stop and no take-profit" in w for w in p.warnings))

    def test_take_profit_only(self):
        p = exits.derive(mandate(stop=None), HOLD, UNIVERSE, "robinhood")
        self.assertEqual([e.kind for e in p.exits], ["take_profit"])


class TestCoverage(unittest.TestCase):
    def test_full_coverage(self):
        p = exits.derive(mandate(), HOLD, UNIVERSE, "robinhood")
        for e in p.exits:
            e.placed = True
        c = exits.coverage(p, HOLD)
        self.assertTrue(c["fully_protected"])
        self.assertEqual(c["coverage_pct"], 100.0)

    def test_an_unplaced_stop_is_not_coverage(self):
        # The order was derived and then rejected by the venue. Counting it
        # would report a protected position that has no stop behind it.
        p = exits.derive(mandate(), HOLD, UNIVERSE, "robinhood")
        for e in p.exits:
            e.placed = False
        c = exits.coverage(p, HOLD)
        self.assertFalse(c["fully_protected"])
        self.assertEqual(c["legs_with_no_stop"], ["NVDA"])

    def test_a_take_profit_alone_does_not_protect(self):
        p = exits.derive(mandate(stop=None), HOLD, UNIVERSE, "robinhood")
        for e in p.exits:
            e.placed = True
        c = exits.coverage(p, HOLD)
        self.assertFalse(c["fully_protected"])

    def test_partial_coverage_is_reported(self):
        m = mandate(legs=(("NVDA", NVDA, 5_000), ("META", META, 5_000)))
        hold = {"NVDA": {"quantity": 1.0, "cost_basis": 200.0},
                "META": {"quantity": 1.0, "cost_basis": 600.0}}
        p = exits.derive(m, hold, UNIVERSE, "robinhood")
        for e in p.exits:
            e.placed = e.ticker == "NVDA"
        c = exits.coverage(p, hold)
        self.assertEqual(c["legs_with_a_live_stop"], ["NVDA"])
        self.assertEqual(c["legs_with_no_stop"], ["META"])
        self.assertAlmostEqual(c["coverage_pct"], 50.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
