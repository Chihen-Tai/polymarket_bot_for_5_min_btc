import unittest


class TestStructuredHedgeSizing(unittest.TestCase):
    def test_low_cash_policy_can_block_entry(self):
        from core.hedge_logic import plan_structured_hedge_entry

        plan = plan_structured_hedge_entry(
            cash_balance_usd=1.80,
            primary_order_usd=1.00,
            hedge_ratio=1.0,
            reserve_usd=0.50,
            min_order_usd=0.50,
            low_cash_policy="skip_entry",
        )

        self.assertFalse(plan.entry_allowed)
        self.assertFalse(plan.should_place_hedge)
        self.assertEqual(plan.reason, "entry_blocked_low_cash_for_hedge")

    def test_low_cash_policy_can_allow_primary_only(self):
        from core.hedge_logic import plan_structured_hedge_entry

        plan = plan_structured_hedge_entry(
            cash_balance_usd=1.80,
            primary_order_usd=1.00,
            hedge_ratio=1.0,
            reserve_usd=0.50,
            min_order_usd=0.50,
            low_cash_policy="primary_only",
        )

        self.assertTrue(plan.entry_allowed)
        self.assertFalse(plan.should_place_hedge)
        self.assertEqual(plan.reason, "hedge_disabled_low_cash_policy")

    def test_cash_equal_to_primary_plus_hedge_keeps_full_hedge(self):
        from core.hedge_logic import finalize_structured_hedge_after_fill

        decision = finalize_structured_hedge_after_fill(
            cash_balance_usd=3.00,
            primary_fill_cost_usd=1.00,
            planned_hedge_usd=1.00,
            reserve_usd=0.50,
            min_order_usd=0.50,
            primary_filled=True,
        )

        self.assertTrue(decision.should_place_hedge)
        self.assertAlmostEqual(decision.hedge_size_usd, 1.00)
        self.assertEqual(decision.reason, "hedge_planned")

    def test_cash_well_above_requirements_keeps_full_hedge(self):
        from core.hedge_logic import finalize_structured_hedge_after_fill

        decision = finalize_structured_hedge_after_fill(
            cash_balance_usd=10.00,
            primary_fill_cost_usd=1.00,
            planned_hedge_usd=1.50,
            reserve_usd=0.50,
            min_order_usd=0.50,
            primary_filled=True,
        )

        self.assertTrue(decision.should_place_hedge)
        self.assertAlmostEqual(decision.hedge_size_usd, 1.50)
        self.assertEqual(decision.reason, "hedge_planned")

    def test_hedge_waits_for_primary_fill(self):
        from core.hedge_logic import finalize_structured_hedge_after_fill

        decision = finalize_structured_hedge_after_fill(
            cash_balance_usd=3.00,
            primary_fill_cost_usd=0.00,
            planned_hedge_usd=1.00,
            reserve_usd=0.50,
            min_order_usd=0.50,
            primary_filled=False,
        )

        self.assertFalse(decision.should_place_hedge)
        self.assertEqual(decision.hedge_size_usd, 0.0)
        self.assertEqual(decision.reason, "hedge_waiting_for_primary_fill")

    def test_partial_primary_fill_caps_hedge_to_available_cash(self):
        from core.hedge_logic import finalize_structured_hedge_after_fill

        decision = finalize_structured_hedge_after_fill(
            cash_balance_usd=2.00,
            primary_fill_cost_usd=0.75,
            planned_hedge_usd=1.00,
            reserve_usd=0.50,
            min_order_usd=0.50,
            primary_filled=True,
        )

        self.assertTrue(decision.should_place_hedge)
        self.assertAlmostEqual(decision.hedge_size_usd, 0.75)
        self.assertEqual(decision.reason, "hedge_capped_to_available_cash")

    def test_hedge_skips_when_remaining_cash_is_below_min_order(self):
        from core.hedge_logic import finalize_structured_hedge_after_fill

        decision = finalize_structured_hedge_after_fill(
            cash_balance_usd=1.60,
            primary_fill_cost_usd=0.90,
            planned_hedge_usd=1.00,
            reserve_usd=0.50,
            min_order_usd=0.50,
            primary_filled=True,
        )

        self.assertFalse(decision.should_place_hedge)
        self.assertEqual(decision.hedge_size_usd, 0.0)
        self.assertEqual(decision.reason, "hedge_skipped_insufficient_capital")


if __name__ == "__main__":
    unittest.main()
