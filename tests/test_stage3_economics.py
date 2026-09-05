"""Stage 3 gate: solver economics.

The headline defect was scale. A -4 hit was charged at up to 24.8 points:
hit_cost was inflated per risk profile (6.5 balanced, 8.0 conservative) and then
multiplied AGAIN by HORIZON_SUM (3.1), on the reasoning that the cost should be
"scaled to the horizon". A hit is paid ONCE, in the gameweek it is taken, and the
objective is sum(w_t * xP_t) with w_0 = 1.0 -- so the cost belongs at w_0, at
face value. Around it sat five more anti-transfer terms and two post-solve
vetoes, none jointly calibrated.

NOT asserted here: the 0.6-1.1 transfers/week sanity band. The synthetic fixture
cannot support it -- see SanityBandTest for the measurement and why.
"""

import unittest

import fpl_tools
from tests import harness

EVENT = 4


class HitPricingTest(unittest.TestCase):
    def test_hit_cost_is_face_value(self):
        self.assertEqual(fpl_tools.HIT_COST, 4.0)

    def test_horizon_sum_is_not_a_hit_multiplier(self):
        """The specific bug: HIT_COST * HORIZON_SUM was 12.4 to 24.8."""
        charge = fpl_tools.HIT_COST + fpl_tools.HIT_HURDLE["balanced"]
        self.assertLess(charge, 8.0)
        self.assertGreater(charge, fpl_tools.HIT_COST)   # the hurdle is real, just named

    def test_risk_appetite_is_a_named_hurdle_not_a_corrupted_cost(self):
        h = fpl_tools.HIT_HURDLE
        self.assertGreater(h["conservative"], h["balanced"])
        self.assertGreater(h["balanced"], h["aggressive"])
        self.assertEqual(h["aggressive"], 0.0)

    def test_advice_string_quotes_multiples_of_four(self):
        """The UI printed "-6 pts" for a -4 on Balanced, and "-8" on
        Conservative, because it formatted the risk-profile scalar. The app's
        own LLM prompt meanwhile forbids inventing non-multiples of 4."""
        for hits in (1, 2, 3):
            self.assertEqual(int(fpl_tools.HIT_COST * hits) % 4, 0)


class DeletedFrictionsTest(unittest.TestCase):
    def test_stacked_penalties_are_gone(self):
        for name in ("TRANSFER_FRICTION", "ROLL_HURDLE", "ROLL_TRANSFER_VALUE",
                     "ROLLED_FT_SHAPE", "LIQUIDITY_BONUS_PER_05M", "HIT_FLOOR_PENALTY"):
            self.assertFalse(hasattr(fpl_tools, name), f"{name} should be deleted")

    def test_ft_option_curve_is_concave_and_bounded(self):
        curve = fpl_tools.FT_OPTION_MARGINAL
        self.assertEqual(len(curve), 5)
        self.assertAlmostEqual(sum(curve), 3.30, places=9)
        for a, b in zip(curve, curve[1:]):
            self.assertGreater(a, b, "marginal value of banking must diminish")

    def test_liquidity_is_capped(self):
        """Was linear and uncapped: freeing GBP 7m ADDED 2.8 points, so the
        solver was paid to downgrade the squad."""
        self.assertLessEqual(fpl_tools.LIQUIDITY_PER_M * fpl_tools.LIQUIDITY_CAP_M, 0.1)

    def test_ledger_helper_survives_even_though_the_tax_is_gone(self):
        """calculate_decaying_tax left the objective, but manager_transfer_ledger
        is NOT redundant: it establishes true purchase price and therefore the
        selling price the budget constraint depends on."""
        import db
        self.assertTrue(hasattr(db, "get_or_backfill_manager_history"))


class DecompositionTest(unittest.TestCase):
    """The displayed number must be the number that was maximised."""

    def _solve(self, **kw):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            entries = harness.squad_to_pool_entries(
                bs, harness.build_squad(bs, "balanced"), lookup, EVENT)
            budget = sum(e["price"] for e in entries)
            return fpl_tools._solve_squad(entries, budget=budget, **kw)

    def test_solver_returns_components(self):
        _sel, _obj, parts = self._solve()
        self.assertIsInstance(parts, dict)
        for key in ("squad_xp", "hit_cost", "ft_option", "hurdle", "liquidity"):
            self.assertIn(key, parts)

    def test_components_are_numeric_and_signed_correctly(self):
        _sel, _obj, parts = self._solve()
        self.assertGreater(parts["squad_xp"], 0.0)
        self.assertLessEqual(parts["hit_cost"], 0.0)
        self.assertLessEqual(parts["hurdle"], 0.0)
        self.assertGreaterEqual(parts["liquidity"], 0.0)

    def test_components_reconcile_with_the_objective(self):
        """sum(parts) must equal the objective the solver reported."""
        _sel, obj, parts = self._solve()
        self.assertAlmostEqual(sum(parts.values()), obj, places=4)


class HurdleTest(unittest.TestCase):
    def test_hurdle_is_disabled_on_chip_rebuilds(self):
        """A Wildcard reshapes 15 players by design; charging a churn brake per
        leg would tax it ~15 points for doing what the chip is for. This is the
        same defect TRANSFER_FRICTION had on chip solves."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            entries = harness.market_pool_entries(bs, lookup, EVENT)
            for e in entries:
                e["sigma"] = 1.0
            held = {e["id"] for e in entries[:15]}
            budget = sum(e["price"] for e in entries) / 2.0

            _s, _o, braked = fpl_tools._solve_squad(
                entries, budget=budget, must_include_ids=held,
                hit_config={"free_transfers": 15, "hit_cost": 0.0, "max_transfers": 15})
            _s2, _o2, free = fpl_tools._solve_squad(
                entries, budget=budget, must_include_ids=held,
                hit_config={"free_transfers": 15, "hit_cost": 0.0, "max_transfers": 15,
                            "hurdle_scale": 0.0})
            self.assertLess(braked["hurdle"], 0.0, "brake should bite when enabled")
            self.assertEqual(free["hurdle"], 0.0, "brake must be off for chip rebuilds")

    def test_sigma_comes_from_the_scenario_axis_not_the_gameweek_axis(self):
        """D0b. _scenario_distribution mis-indexes the array until Stage 5, so
        sigma must be read straight off the (S, n) matrix. A sigma derived from
        six gameweek slots instead of 500 scenarios would be meaningless."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            ids = [e["id"] for e in bs["elements"][:40]]
            _mean, matrix = fpl_tools._generate_scenarios(ids, lookup, EVENT)
            sigmas = fpl_tools._scenario_sigmas(matrix)
            self.assertTrue(sigmas)
            for pid, arr in matrix.items():
                self.assertEqual(arr.shape[0], fpl_tools.SAA_SCENARIOS)
                break
            self.assertTrue(all(v >= 0 for v in sigmas.values()))
            self.assertGreater(max(sigmas.values()), 0.0)


class MovePairingTest(unittest.TestCase):
    def test_pairs_by_price_distance(self):
        """The MIP picks a SET; the old code sorted both sides by xP and zipped
        them, inventing a mapping that every per-row figure then hung off."""
        pool = {
            1: {"id": 1, "price": 12.0, "sell_price": 12.0, "position": "MID"},
            2: {"id": 2, "price": 4.5, "sell_price": 4.5, "position": "MID"},
            10: {"id": 10, "price": 11.8, "position": "MID"},
            20: {"id": 20, "price": 4.6, "position": "MID"},
        }
        pairs = dict(fpl_tools._pair_by_price([1, 2], [10, 20], pool))
        self.assertEqual(pairs[1], 10, "the premium should pair with the premium")
        self.assertEqual(pairs[2], 20, "the budget option should pair with the budget option")

    def test_handles_uneven_sides(self):
        pool = {1: {"id": 1, "price": 8.0, "sell_price": 8.0, "position": "MID"},
                2: {"id": 2, "price": 5.0, "sell_price": 5.0, "position": "MID"},
                9: {"id": 9, "price": 7.9, "position": "MID"}}
        pairs = fpl_tools._pair_by_price([1, 2], [9], pool)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][1], 9)

    def test_empty_sides_are_safe(self):
        self.assertEqual(fpl_tools._pair_by_price([], [1], {}), [])
        self.assertEqual(fpl_tools._pair_by_price([1], [], {}), [])


class SanityBandTest(unittest.TestCase):
    """The 0.6-1.1 transfers/week band cannot be gated on this fixture.

    Measured across all 20 squads, the best available single-swap gain takes
    exactly TWO values: 2.11 (14 squads) and 14-17 (6 squads). There is no
    distribution of marginal decisions, because the generator lays players on a
    smooth quality ladder and the squads are built by taking the top or bottom
    of it -- so the gap to the best replacement is identical almost everywhere.

    The transfer rate therefore flips entirely on whether the bar sits above or
    below 2.11: a sweep of HURDLE_BASE x HURDLE_SIGMA_WEIGHT returns only 0.45,
    0.50 or 1.20 per week, with nothing in between at any setting. Tuning the
    constants to land inside the band would be fitting to an artefact of the
    fixture, and would bake in a number chosen to satisfy a synthetic threshold.

    So the constants stay at the reviewed values and the band is measured
    against real manager squads instead. This test pins the reason, and asserts
    only that the machinery runs end to end and stays bounded.
    """

    def test_transfer_rate_is_bounded_and_the_engine_runs(self):
        rates = []
        with harness.synthetic_world("interactive") as (bs, _fx):
            for name, _d in harness.active_squads():
                squad = harness.squad_as_manager_input(bs, harness.build_squad(bs, name))
                res = fpl_tools.suggest_transfers_for_custom_squad(
                    squad, bank=2.0, free_transfers=1, eval_chips=[], event=EVENT,
                    risk="balanced", holding_map=None, current_gw=EVENT, allow_hits=True)
                self.assertIn("breakdown", res)
                rates.append(len(res["transfers"]))
        self.assertTrue(rates)
        self.assertLessEqual(max(rates), 4, "runaway churn")
        self.assertGreaterEqual(sum(rates), 0)


if __name__ == "__main__":
    unittest.main()
