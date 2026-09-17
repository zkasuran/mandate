# SPDX-License-Identifier: Apache-2.0
"""Offline tests for the book, the bond and ticker resolution.

No network. The venue's index is fed in as fixtures taken from real responses,
so the lookalike cases under test are the ones that actually exist on Robinhood
Chain rather than invented ones.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import bond, book, flash  # noqa: E402

NVDA = "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC"
AAPL = "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9"


def asset(symbol, address, liquidity, holders, risk=False, price=100.0):
    return flash.Asset(chain="robinhood", address=address, symbol=symbol,
                       name=symbol, decimals=18, price=price,
                       liquidity=liquidity, volume24h=0.0, holders=holders,
                       market_cap=None, risk_flagged=risk)


class TestTickerResolution(unittest.TestCase):
    """Every fixture below is a contract that really exists on this chain."""

    def test_picks_the_exact_symbol_over_a_deep_lookalike(self):
        # NVDAx3L is a 3x leveraged token holding $407,605. A resolver that
        # ranks by liquidity alone, else fuzzy-matches, hands over leverage the
        # follower never asked for.
        rows = [asset("NVDAx3L", "0xf51f", 407_605, 513),
                asset("NVDA", NVDA, 2_655_568, 166_218)]
        r = flash.resolve("NVDA", "robinhood", rows)
        self.assertTrue(r.ok)
        self.assertEqual(r.chosen.address, NVDA)
        self.assertTrue(any(a.symbol == "NVDAx3L" for a, _ in r.impostors))

    def test_rejects_a_search_poisoning_symbol(self):
        # A real token whose symbol is a comma-joined list of thousands of
        # tickers, built to match every ticker search run against it.
        poison = "AAAP,AACG,AACI,AAPL,AAPU,ABNB,ABTC"
        rows = [asset(poison, "0x9403", 68, 92),
                asset("AAPL", AAPL, 336_314, 79_239)]
        r = flash.resolve("AAPL", "robinhood", rows)
        self.assertTrue(r.ok)
        self.assertEqual(r.chosen.address, AAPL)

    def test_refuses_when_only_lookalikes_exist(self):
        rows = [asset("AAPLCAT", "0x73a9", 86_717, 1_127),
                asset("AAPLHOOD", "0xA713", 5_722, 74)]
        r = flash.resolve("AAPL", "robinhood", rows)
        self.assertFalse(r.ok)
        self.assertIsNone(r.chosen)
        self.assertIn("no contract", r.reason)

    def test_exact_symbol_under_the_liquidity_floor_is_refused(self):
        rows = [asset("AAPL", AAPL, 1_000, 79_239)]
        r = flash.resolve("AAPL", "robinhood", rows)
        self.assertFalse(r.ok)

    def test_exact_symbol_under_the_holder_floor_is_refused(self):
        rows = [asset("AAPL", AAPL, 900_000, 12)]
        r = flash.resolve("AAPL", "robinhood", rows)
        self.assertFalse(r.ok)

    def test_risk_flagged_is_refused_even_on_an_exact_match(self):
        rows = [asset("AAPL", AAPL, 900_000, 79_239, risk=True)]
        r = flash.resolve("AAPL", "robinhood", rows)
        self.assertFalse(r.ok)

    def test_case_insensitive_ticker(self):
        rows = [asset("NVDA", NVDA, 2_655_568, 166_218)]
        self.assertTrue(flash.resolve("nvda", "robinhood", rows).ok)


class TestBondMath(unittest.TestCase):
    def test_floor_applies_to_small_sleeves(self):
        self.assertEqual(bond.required_bond(1_000), bond.BOND_FLOOR_USD)

    def test_percentage_applies_above_the_floor(self):
        self.assertEqual(bond.required_bond(100_000), 2_000.0)

    def test_insufficient_bond_is_flagged(self):
        b = bond.bond_for("mnd_x", "0xabc", 100.0, 100_000)
        self.assertFalse(b.sufficient)
        self.assertEqual(b.to_dict()["shortfall_usd"], 1_900.0)


def _mandate_record(**over):
    rules = {
        "schema": "mandate/v1", "version": 1, "chain_id": 4663,
        "trigger": "schedule", "cadence_seconds": 86400, "fee_bps": 100,
        "legs": [{"symbol": "NVDA", "token": NVDA.lower(), "weight_bps": 10000}],
        "execution": {"kind": "twap", "slices": 4, "interval_seconds": 900,
                      "max_slippage_bps": 50, "min_depth_multiple": 3,
                      "require_market_hours": True},
        "risk": {"stop_loss_bps": 800, "take_profit_bps": 1500,
                 "max_position_bps": 10000, "max_daily_orders": 12},
    }
    rules.update(over)
    return {"mandate_id": "mnd_test", "name": "T", "strategist": "0xabc",
            "thesis": "t", "rules": rules}


def _fill(**over):
    f = {"fill_id": "f1", "follower": "0xf1", "ticker": "NVDA",
         "address": NVDA, "spend_usd": 1_000.0, "slippage_bps": 10,
         "mark_certified": True, "depth_cover": 100.0}
    f.update(over)
    return f


class _Verified:
    verified = True


class TestComplianceAudit(unittest.TestCase):
    UNIVERSE = {"NVDA": _Verified()}

    def test_a_compliant_fill_is_clean(self):
        a = bond.audit(_mandate_record(), [_fill()], self.UNIVERSE)
        self.assertTrue(a["clean"])
        self.assertEqual(a["slash_bps"], 0)

    def test_trading_an_instrument_outside_the_mandate(self):
        a = bond.audit(_mandate_record(), [_fill(ticker="TSLA")], self.UNIVERSE)
        self.assertFalse(a["clean"])
        self.assertEqual(a["violations"][0]["kind"], "wrong_instrument")

    def test_breaking_the_published_slippage_ceiling(self):
        a = bond.audit(_mandate_record(), [_fill(slippage_bps=420)], self.UNIVERSE)
        self.assertEqual(a["violations"][0]["kind"], "slippage_exceeded")
        # harm is the overshoot on the traded notional, not the whole clip
        self.assertAlmostEqual(a["violations"][0]["harmed_usd"], 1000 * 370 / 10000)

    def test_trading_out_of_hours_on_a_market_hours_mandate(self):
        a = bond.audit(_mandate_record(), [_fill(mark_certified=False)],
                       self.UNIVERSE)
        self.assertEqual(a["violations"][0]["kind"], "traded_out_of_hours")

    def test_out_of_hours_is_fine_when_the_mandate_allows_it(self):
        rec = _mandate_record()
        rec["rules"]["execution"]["require_market_hours"] = False
        a = bond.audit(rec, [_fill(mark_certified=False)], self.UNIVERSE)
        self.assertTrue(a["clean"])

    def test_ignoring_the_published_depth_cover(self):
        a = bond.audit(_mandate_record(), [_fill(depth_cover=1.2)], self.UNIVERSE)
        self.assertEqual(a["violations"][0]["kind"], "depth_cover_ignored")

    def test_unverified_instrument_is_a_violation(self):
        a = bond.audit(_mandate_record(), [_fill()], {})
        self.assertEqual(a["violations"][0]["kind"], "unverified_instrument")

    def test_slash_is_capped_at_the_whole_bond(self):
        fills = [_fill(fill_id=f"f{i}", ticker="TSLA") for i in range(5)]
        a = bond.audit(_mandate_record(), fills, self.UNIVERSE)
        self.assertEqual(a["slash_bps"], bond.MAX_SLASH_BPS)


class TestSettlement(unittest.TestCase):
    def test_clean_audit_leaves_the_bond_alone(self):
        b = bond.bond_for("mnd_test", "0xabc", 500.0, 5_000)
        s = bond.settle(b, {"slash_bps": 0, "violations": []})
        self.assertEqual(s["slashed_usd"], 0.0)

    def test_payouts_go_to_the_harmed_in_proportion(self):
        b = bond.bond_for("mnd_test", "0xabc", 1_000.0, 5_000)
        result = {"slash_bps": 5_000, "violations": [
            {"follower": "0xaaa", "harmed_usd": 300.0},
            {"follower": "0xbbb", "harmed_usd": 100.0},
        ]}
        s = bond.settle(b, result)
        self.assertEqual(s["slashed_usd"], 500.0)
        self.assertAlmostEqual(s["payouts"][0]["payout_usd"], 375.0)
        self.assertAlmostEqual(s["payouts"][1]["payout_usd"], 125.0)
        self.assertAlmostEqual(sum(p["payout_usd"] for p in s["payouts"]), 500.0)


class TestBook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = book.STORE
        book.STORE = os.path.join(self.tmp, "book.json")

    def tearDown(self):
        book.STORE = self._old

    def _publish(self):
        book.put_mandate({"mandate_id": "mnd_a", "name": "A",
                          "strategist": "0xs", "thesis": "t",
                          "rules": _mandate_record()["rules"]})

    def test_identical_mandate_is_not_stored_twice(self):
        self._publish()
        self._publish()
        self.assertEqual(len(book.read()["mandates"]), 1)

    def test_subscribe_then_no_duplicate_active_subscription(self):
        self._publish()
        a = book.subscribe("mnd_a", "0xF1", 1_000)
        self.assertTrue(a["created"])
        b = book.subscribe("mnd_a", "0xf1", 2_000)
        self.assertFalse(b["created"])

    def test_subscribing_to_an_unknown_mandate_fails(self):
        with self.assertRaises(KeyError):
            book.subscribe("mnd_missing", "0xf1", 100)

    def test_negative_sleeve_refused(self):
        self._publish()
        with self.assertRaises(ValueError):
            book.subscribe("mnd_a", "0xf1", -5)

    def test_portfolio_replays_fills(self):
        self._publish()
        sub = book.subscribe("mnd_a", "0xf1", 1_000)["subscription"]
        book.record_fills([
            {"fill_id": "f1", "subscription_id": sub["subscription_id"],
             "mandate_id": "mnd_a", "follower": "0xf1", "ticker": "NVDA",
             "address": NVDA, "chain": "robinhood", "side": "buy",
             "spend_usd": 400.0, "quantity": 2.0, "price": 200.0,
             "venue": "flash", "order_type": "twap", "quote_id": None,
             "broadcast": False, "tx_hash": None, "mark_certified": True,
             "fee_usd": 4.0, "block": 1},
            {"fill_id": "f2", "subscription_id": sub["subscription_id"],
             "mandate_id": "mnd_a", "follower": "0xf1", "ticker": "NVDA",
             "address": NVDA, "chain": "robinhood", "side": "buy",
             "spend_usd": 600.0, "quantity": 2.0, "price": 300.0,
             "venue": "flash", "order_type": "twap", "quote_id": None,
             "broadcast": False, "tx_hash": None, "mark_certified": True,
             "fee_usd": 6.0, "block": 2},
        ])
        p = book.portfolio("0xf1", {"NVDA": 300.0})
        h = p["holdings"][0]
        self.assertEqual(h["quantity"], 4.0)
        self.assertAlmostEqual(h["cost_basis"], 250.0)     # weighted average
        self.assertAlmostEqual(h["unrealised_pnl"], 200.0)  # (300-250)*4
        self.assertAlmostEqual(p["deployed_usd"], 1_000.0)
        self.assertAlmostEqual(p["fees_accrued_usd"], 10.0)

    def test_selling_realises_against_the_average_basis(self):
        self._publish()
        sub = book.subscribe("mnd_a", "0xf2", 1_000)["subscription"]
        common = {"subscription_id": sub["subscription_id"], "mandate_id": "mnd_a",
                  "follower": "0xf2", "ticker": "NVDA", "address": NVDA,
                  "chain": "robinhood", "venue": "flash", "order_type": "market",
                  "quote_id": None, "broadcast": False, "tx_hash": None,
                  "mark_certified": True, "fee_usd": 0.0}
        book.record_fills([
            dict(common, fill_id="b1", side="buy", spend_usd=200.0,
                 quantity=1.0, price=200.0, block=1),
            dict(common, fill_id="s1", side="sell", spend_usd=250.0,
                 quantity=1.0, price=250.0, block=2),
        ])
        p = book.portfolio("0xf2", {"NVDA": 250.0})
        self.assertEqual(p["holdings"][0]["quantity"], 0.0)
        self.assertAlmostEqual(p["holdings"][0]["realised_pnl"], 50.0)

    def test_cancelling_a_subscription(self):
        self._publish()
        sub = book.subscribe("mnd_a", "0xf3", 500)["subscription"]
        book.set_status(sub["subscription_id"], "cancelled")
        self.assertEqual(len(book.subscriptions(follower="0xf3",
                                                active_only=True)), 0)

    def test_bad_status_refused(self):
        with self.assertRaises(ValueError):
            book.set_status("sub_x", "deleted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
