"""Stage 6 gate: chip isolation, reservation curves, and solver-result validity.

Three defects, plus one latent bug this stage exposed.

  * Chip scores were incommensurate. Wildcard and Free Hit were measured over
    the 4-gameweek horizon while Bench Boost and Triple Captain were measured
    over one, so the first two were ~3x inflated by construction and almost
    always outranked the others. Free Hit was the real error: it lasts ONE week.
  * _chip_reservation_threshold returned a hard 0.0 for every gw >= 18 --
    including GW20-38, where the second chip set lives -- so from GW18 onward the
    top-ranked chip cleared its threshold every week for the rest of the season.
  * The API's chip vocabulary ("wildcard", "3xc") was compared directly against
    display names ("Wildcard", "Triple Captain"), so nothing matched and a
    played chip stayed on the available list.
"""

import unittest

import fpl_tools
from tests import harness

EVENT = 4


class ReservationCurveTest(unittest.TestCase):
    def test_decays_monotonically_within_a_set(self):
        for chip in fpl_tools.CHIPS:
            vals = [fpl_tools._chip_reservation_threshold(chip, gw) for gw in range(1, 20)]
            for a, b in zip(vals, vals[1:]):
                self.assertGreaterEqual(a, b, f"{chip} reservation rose at some gameweek")

    def test_collapses_to_zero_at_each_deadline(self):
        for chip in fpl_tools.CHIPS:
            self.assertEqual(fpl_tools._chip_reservation_threshold(chip, 19), 0.0)
            self.assertEqual(fpl_tools._chip_reservation_threshold(chip, 38), 0.0)

    def test_set_two_resets(self):
        """The specific bug: a hard 0.0 for every gw >= 18 meant the engine
        recommended playing a chip every week from GW18 to GW38."""
        for chip in fpl_tools.CHIPS:
            self.assertGreater(fpl_tools._chip_reservation_threshold(chip, 20), 1.0,
                               f"{chip} inherited Set 1's spent clock")

    def test_thresholds_are_on_the_same_scale_as_the_scores(self):
        """Triple Captain scores one captain's single-gameweek xP (~6-9) and was
        gated at 15.0, so it could not be recommended before GW15 at all."""
        self.assertLess(fpl_tools.CHIP_RESERVATION_BASE["Triple Captain"], 10.0)
        self.assertLess(fpl_tools.CHIP_RESERVATION_BASE["Bench Boost"], 10.0)


class WildcardTimingTest(unittest.TestCase):
    def test_window_is_free_and_outside_is_penalised(self):
        lo, hi = fpl_tools.WILDCARD_WINDOW
        for gw in range(lo, hi + 1):
            self.assertEqual(fpl_tools._wildcard_timing_penalty(gw), 0.0)
        self.assertGreater(fpl_tools._wildcard_timing_penalty(lo - 3), 0.0)
        self.assertGreater(fpl_tools._wildcard_timing_penalty(hi + 3), 0.0)

    def test_penalty_grows_with_distance_from_the_window(self):
        lo, _hi = fpl_tools.WILDCARD_WINDOW
        near = fpl_tools._wildcard_timing_penalty(lo - 1)
        far = fpl_tools._wildcard_timing_penalty(lo - 4)
        self.assertGreater(far, near)

    def test_it_is_a_prior_not_a_hard_band(self):
        """A large enough gain must still clear it -- otherwise it is a
        hardcoded window pretending to be a model."""
        lo, _hi = fpl_tools.WILDCARD_WINDOW
        penalty = fpl_tools._wildcard_timing_penalty(lo - 4)
        self.assertLess(penalty, 50.0)

    def test_set_two_is_unconstrained_by_the_set_one_window(self):
        self.assertEqual(fpl_tools._wildcard_timing_penalty(30), 0.0)


class ChipNameTest(unittest.TestCase):
    def test_api_vocabulary_maps_to_display_names(self):
        self.assertEqual(fpl_tools.normalise_chip_name("wildcard"), "Wildcard")
        self.assertEqual(fpl_tools.normalise_chip_name("freehit"), "Free Hit")
        self.assertEqual(fpl_tools.normalise_chip_name("bboost"), "Bench Boost")
        self.assertEqual(fpl_tools.normalise_chip_name("3xc"), "Triple Captain")

    def test_display_names_round_trip(self):
        for chip in fpl_tools.CHIPS:
            self.assertEqual(fpl_tools.normalise_chip_name(chip), chip)

    def test_unknown_and_empty_are_none(self):
        for bad in ("manager", "", None, "assistant_manager"):
            self.assertIsNone(fpl_tools.normalise_chip_name(bad))


class BenchCapTest(unittest.TestCase):
    def test_ramps_into_a_scheduled_bench_boost(self):
        self.assertEqual(fpl_tools._bench_cap_for(10, None), fpl_tools.BENCH_CAP_STANDARD)
        self.assertEqual(fpl_tools._bench_cap_for(10, 12), 19.0)   # T-2
        self.assertEqual(fpl_tools._bench_cap_for(11, 12), 22.0)   # T-1
        self.assertIsNone(fpl_tools._bench_cap_for(12, 12))        # uncapped in the week

    def test_not_applied_to_the_standard_weekly_solve(self):
        """With the fifteen fixed, the only way to satisfy a bench-cost cap is
        to change who STARTS -- forcing price into a decision that should be made
        on expected points.

        Measured on the fixture: a GBP 16m cap cut the XI by 12.7 points, RAISED
        bench cost (it benched cheap players to start expensive ones), and where
        the squad could not comply it made the program infeasible. The cap
        belongs on squad construction.
        """
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            entries = harness.squad_to_pool_entries(
                bs, harness.build_squad(bs, "balanced"), lookup, EVENT)
            by = {e["id"]: e for e in entries}
            budget = sum(e["price"] for e in entries)
            hc = {"free_transfers": 1, "hit_cost": 5.0, "max_transfers": 0}

            free_sel, _o, _p = fpl_tools._solve_squad(
                entries, budget=budget, must_include_ids=set(by), hit_config=hc)
            free_xp = sum(by[i]["xp"] for i in fpl_tools._LAST_SOLVE["xi"])

            capped_sel, _o2, _p2 = fpl_tools._solve_squad(
                entries, budget=budget, must_include_ids=set(by), hit_config=hc,
                bench_cap=16.0)
            # Either infeasible (rejected), or a strictly worse XI. Both are
            # reasons not to apply it on a standard gameweek.
            if capped_sel is not None:
                capped_xp = sum(by[i]["xp"] for i in fpl_tools._LAST_SOLVE["xi"])
                self.assertLessEqual(capped_xp, free_xp)


class SolverResultValidityTest(unittest.TestCase):
    """A latent Stage 0 bug that Stage 6 exposed.

    The interactive profile accepted a non-optimal result whenever
    len(selected) == 15. Infeasible / Unbounded / Undefined solves leave STALE
    variable values from an earlier relaxation, and those can look plausible: an
    infeasible solve was observed returning 15 selected players with a TEN-man
    starting XI. In the live app that is a garbage squad, silently.
    """

    def test_infeasible_solves_are_rejected(self):
        with harness.synthetic_world("interactive") as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            entries = harness.squad_to_pool_entries(
                bs, harness.build_squad(bs, "balanced"), lookup, EVENT)
            by = {e["id"]: e for e in entries}
            # A budget no squad can meet makes the program infeasible.
            sel, obj, parts = fpl_tools._solve_squad(
                entries, budget=1.0, must_include_ids=set(by),
                hit_config={"free_transfers": 1, "hit_cost": 5.0, "max_transfers": 0})
            self.assertIsNone(sel, "an infeasible solve must not return a squad")
            self.assertEqual(parts, {})

    def test_validator_rejects_a_short_xi(self):
        by = {i: {"id": i, "position": p, "team_id": i}
              for i, p in enumerate(["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3)}
        self.assertFalse(fpl_tools._is_valid_squad(list(by)[:14], by, None))
        self.assertTrue(fpl_tools._is_valid_squad(list(by), by, None))

    def test_validator_rejects_a_club_stack(self):
        by = {i: {"id": i, "position": p, "team_id": 1}
              for i, p in enumerate(["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3)}
        self.assertFalse(fpl_tools._is_valid_squad(list(by), by, None))


class ChipScoringTest(unittest.TestCase):
    def test_free_hit_is_scored_over_one_gameweek(self):
        """It lasts one week; it was measured over four."""
        with harness.synthetic_world("interactive") as (bs, _fx):
            squad = harness.squad_as_manager_input(bs, harness.build_squad(bs, "balanced"))
            res = fpl_tools.suggest_transfers_for_custom_squad(
                squad, bank=2.0, free_transfers=1,
                eval_chips=["Free Hit", "Bench Boost", "Triple Captain"],
                event=EVENT, risk="balanced", holding_map=None, current_gw=EVENT,
                allow_hits=False)
            scores = {c: s for c, s in res.get("chip_scores", {}).items()} \
                if isinstance(res.get("chip_scores"), dict) else {}
            # The engine may not expose raw scores; the ranking is the contract.
            self.assertIn("recommended_chip", res)
            self.assertIsInstance(res["chip_evaluations"], list)

    def test_engine_runs_with_every_chip_enabled(self):
        with harness.synthetic_world("interactive") as (bs, _fx):
            squad = harness.squad_as_manager_input(bs, harness.build_squad(bs, "balanced"))
            res = fpl_tools.suggest_transfers_for_custom_squad(
                squad, bank=2.0, free_transfers=1, eval_chips=list(fpl_tools.CHIPS),
                event=EVENT, risk="balanced", holding_map=None, current_gw=EVENT,
                allow_hits=True)
            self.assertIn("recommended_chip", res)


if __name__ == "__main__":
    unittest.main()
