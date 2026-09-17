# SPDX-License-Identifier: Apache-2.0
"""Offline tests. No network, so they run anywhere and never flake on an RPC.

The live-chain checks live in scripts/verify_addresses.py, which is a gate
rather than a test: it is allowed to fail when the network is down.
"""
import datetime as dt
import sys
import os
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import basis, depth, fees, orders, spec  # noqa: E402
from core.keccak import event_topic, keccak256  # noqa: E402

NY = ZoneInfo("America/New_York")
TOK_A = "0xb200000000000000000000C2e324d24d7eEcd1fb"
TOK_B = "0xb20000000000000000000078ee7ce2fE4908108C"


class TestKeccak(unittest.TestCase):
    def test_published_vectors(self):
        self.assertEqual(
            keccak256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(
            keccak256(b"abc").hex(),
            "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")

    def test_known_event_topics(self):
        self.assertEqual(
            event_topic("Transfer(address,address,uint256)"),
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
        self.assertEqual(
            event_topic("Swap(address,address,int256,int256,uint160,uint128,int24)"),
            "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67")

    def test_a_wrong_signature_gives_a_different_topic(self):
        # Guards against a hash function that returns something constant.
        self.assertNotEqual(event_topic("Swap(address)"),
                            event_topic("Swap(address,address)"))


def _mandate(**kw):
    defaults = dict(
        name="Test", strategist="0x" + "11" * 20, thesis="A test strategy.",
        legs=(spec.Leg("AAPLc", TOK_A, 10_000),))
    defaults.update(kw)
    return spec.Mandate(**defaults)


class TestMandateIdentity(unittest.TestCase):
    def test_id_is_stable_across_construction_order(self):
        a = _mandate(legs=(spec.Leg("AAPLc", TOK_A, 6_000),
                           spec.Leg("NVDAc", TOK_B, 4_000)))
        b = _mandate(legs=(spec.Leg("NVDAc", TOK_B, 4_000),
                           spec.Leg("AAPLc", TOK_A, 6_000)))
        self.assertEqual(a.mandate_id(), b.mandate_id())

    def test_id_changes_when_a_rule_changes(self):
        a = _mandate()
        b = _mandate(fee_bps=200)
        self.assertNotEqual(a.mandate_id(), b.mandate_id())

    def test_display_text_does_not_change_the_id(self):
        a = _mandate(name="One", thesis="First wording.")
        b = _mandate(name="Two", thesis="Completely different wording.")
        self.assertEqual(a.mandate_id(), b.mandate_id())

    def test_round_trip_through_load(self):
        m = _mandate()
        self.assertEqual(spec.load(m.to_dict()).mandate_id(), m.mandate_id())

    def test_tampered_record_is_rejected(self):
        m = _mandate()
        payload = m.to_dict()
        payload["rules"]["fee_bps"] = 2_000          # silently sweeten the fee
        with self.assertRaises(spec.MandateError):
            spec.load(payload)

    def test_signing_message_commits_to_the_id(self):
        m = _mandate()
        self.assertIn(m.mandate_id(), m.signing_message())


class TestMandateValidation(unittest.TestCase):
    def test_weights_must_sum_to_full(self):
        with self.assertRaises(spec.MandateError):
            _mandate(legs=(spec.Leg("AAPLc", TOK_A, 5_000),)).validate()

    def test_duplicate_legs_rejected(self):
        with self.assertRaises(spec.MandateError):
            _mandate(legs=(spec.Leg("AAPLc", TOK_A, 5_000),
                           spec.Leg("AAPLc", TOK_A, 5_000))).validate()

    def test_floats_are_refused(self):
        with self.assertRaises(spec.MandateError):
            spec._reject_floats({"slippage": 0.5})

    def test_fee_ceiling(self):
        with self.assertRaises(spec.MandateError):
            _mandate(fee_bps=5_000).validate()

    def test_empty_thesis_rejected(self):
        with self.assertRaises(spec.MandateError):
            _mandate(thesis="  ").validate()


class TestSplitNotional(unittest.TestCase):
    def test_no_dust_is_lost(self):
        for total in (1, 7, 100, 999_999, 1_000_000_001):
            for slices in (1, 3, 4, 7):
                parts = orders.split_notional(total, slices)
                self.assertEqual(sum(parts), total, (total, slices))
                self.assertEqual(len(parts), slices)
                self.assertLessEqual(max(parts) - min(parts), 1)

    def test_rejects_zero_slices(self):
        with self.assertRaises(ValueError):
            orders.split_notional(100, 0)


class TestDepthAwareSlicing(unittest.TestCase):
    def test_widens_when_venue_is_too_shallow(self):
        # $10,000 sleeve, $6,000 of depth, 3x cover => $2,000 per clip => 5 clips
        slices, note = orders.depth_aware_slices(10_000_000_000, 6_000.0, 2, 3)
        self.assertEqual(slices, 5)
        self.assertIn("widened", note)

    def test_leaves_a_sufficient_plan_alone(self):
        slices, note = orders.depth_aware_slices(1_000_000, 900_000.0, 2, 3)
        self.assertEqual(slices, 2)
        self.assertIsNone(note)


class TestMarketHours(unittest.TestCase):
    def test_open_midweek_midday(self):
        st = basis.session_state(dt.datetime(2026, 9, 17, 11, 0, tzinfo=NY))
        self.assertTrue(st.open)

    def test_closed_at_the_weekend(self):
        st = basis.session_state(dt.datetime(2026, 9, 19, 12, 0, tzinfo=NY))
        self.assertFalse(st.open)
        self.assertEqual(st.reason, "weekend")

    def test_closed_on_a_holiday(self):
        st = basis.session_state(dt.datetime(2026, 12, 25, 12, 0, tzinfo=NY))
        self.assertFalse(st.open)
        self.assertEqual(st.reason, "exchange holiday")

    def test_half_day_closes_early(self):
        self.assertFalse(
            basis.session_state(dt.datetime(2026, 11, 27, 14, 0, tzinfo=NY)).open)
        self.assertTrue(
            basis.session_state(dt.datetime(2026, 11, 27, 12, 0, tzinfo=NY)).open)

    def test_before_the_bell_is_closed(self):
        st = basis.session_state(dt.datetime(2026, 9, 17, 9, 0, tzinfo=NY))
        self.assertFalse(st.open)

    def test_guard_blocks_when_required(self):
        allowed, why = basis.guard(
            True, dt.datetime(2026, 9, 19, 12, 0, tzinfo=NY))
        self.assertFalse(allowed)
        self.assertIn("weekend", why)

    def test_guard_passes_when_not_required(self):
        allowed, _ = basis.guard(
            False, dt.datetime(2026, 9, 19, 12, 0, tzinfo=NY))
        self.assertTrue(allowed)

    def test_mark_is_uncertified_out_of_hours(self):
        c = basis.certify_mark(100.0, dt.datetime(2026, 9, 19, 12, 0, tzinfo=NY))
        self.assertFalse(c["certified"])
        self.assertEqual(c["price"], 100.0)


def _report(status="live", price=100.0, liquidity=10**12,
            base_balance=10**12, quote_usd=50_000.0, fee=3000, tick=-1000):
    return depth.PoolReport(
        pool="0x" + "22" * 20, fee=fee, base=TOK_A, quote=depth.uniswap.USDC,
        tick=tick, liquidity=liquidity, price=price,
        base_balance=base_balance, quote_balance=int(quote_usd * 1e6),
        base_decimals=8, quote_decimals=6, quote_usd_held=quote_usd,
        status=status, reasons=[])


class TestDepthGuards(unittest.TestCase):
    def test_dead_pool_is_refused_not_quoted(self):
        r = _report(status="dead")
        out = depth.quote_buy(r, {"token0": TOK_A, "liquidity": 0,
                                  "sqrtPriceX96": 1 << 96}, 100_000_000)
        self.assertFalse(out["usable"])
        self.assertIn("dead", out["refused"])

    def test_trapped_pool_is_refused(self):
        r = _report(status="trapped")
        out = depth.quote_buy(r, {"token0": TOK_A, "liquidity": 0,
                                  "sqrtPriceX96": 1 << 96}, 100_000_000)
        self.assertFalse(out["usable"])

    def test_fill_beyond_pool_balance_is_refused(self):
        r = _report(base_balance=1)          # pool holds essentially nothing
        state = {"token0": TOK_A, "liquidity": 10 ** 18, "sqrtPriceX96": 1 << 96}
        out = depth.quote_buy(r, state, 1_000_000_000)
        self.assertTrue(out["exceeds_in_range"])
        self.assertFalse(out["usable"])

    def test_outlier_pool_is_marked_trapped(self):
        reports = [_report(price=100.0), _report(price=101.0), _report(price=500.0)]
        depth.flag_outliers(reports)
        self.assertEqual(reports[2].status, "trapped")
        self.assertEqual(reports[0].status, "live")

    def test_extreme_tick_classifies_as_trapped(self):
        state = {"pool": "0x" + "33" * 20, "sqrtPriceX96": 4295128740,
                 "tick": -887272, "liquidity": 0,
                 "token0": TOK_A, "token1": depth.uniswap.USDC,
                 "base": TOK_A, "quote": depth.uniswap.USDC, "fee": 10000}
        original = depth.uniswap.reserves
        depth.uniswap.reserves = lambda *a, **k: (0, 1_019_010_000)
        try:
            r = depth.classify(state, 8, 6, 1.0)
        finally:
            depth.uniswap.reserves = original
        self.assertEqual(r.status, "trapped")
        self.assertTrue(any("extreme" in x for x in r.reasons))


class TestFees(unittest.TestCase):
    def test_split_adds_up(self):
        s = fees.fee_on_volume(1_000_000_000, 100)      # $1,000 at 1%
        self.assertEqual(s.gross_usdc, 10_000_000)
        self.assertEqual(s.strategist_usdc + s.protocol_usdc, s.gross_usdc)
        self.assertEqual(s.protocol_usdc, 1_000_000)

    def test_zero_fee_is_allowed(self):
        self.assertEqual(fees.fee_on_volume(1_000_000, 0).gross_usdc, 0)

    def test_fee_above_ceiling_is_refused(self):
        with self.assertRaises(ValueError):
            fees.fee_on_volume(1_000_000, fees.MAX_FEE_BPS + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
