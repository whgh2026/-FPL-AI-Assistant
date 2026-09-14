"""Tier 1.2 gate: the armband stays inside the eleven on a Bench Boost week.

Bench Boost used to set `start = None` to save 15+1+3+3 binaries. The
captaincy linkage was then written as a fallback:

    if start is not None:  prob += captain[pid] <= start[pid]
    else:                  prob += captain[pid] <= x[pid]

so on precisely the week the chip is active, the armband was free to land on
any SELECTED player -- bench included. That is not a corner case under Bench
Boost: the solver picks the highest-xP fifteen and, with no starter binaries,
nothing forces the doubled player into the eleven that gets fielded.

The eleven is now chosen under Bench Boost too, scored only at
BB_START_TIEBREAK so it cannot change which fifteen get bought.
"""

import unittest

import fpl_tools
from tests import harness

EVENT = 4


def _solve(bs, **kw):
    lookup = fpl_tools._build_fixture_lookup(bs)
    entries = harness.squad_to_pool_entries(
        bs, harness.build_squad(bs, "balanced"), lookup, EVENT)
    budget = sum(e["price"] for e in entries)
    selected, obj, parts = fpl_tools._solve_squad(entries, budget=budget, **kw)
    return entries, selected, obj, parts


class BenchBoostCaptainTest(unittest.TestCase):

    def test_the_captain_is_inside_the_xi_under_bench_boost(self):
        """The defect itself."""
        with harness.synthetic_world() as (bs, _fx):
            _entries, selected, _obj, _parts = _solve(bs, bench_boost=True)
            cap = fpl_tools._LAST_SOLVE["captain"]
            xi = fpl_tools._LAST_SOLVE["xi"]
        self.assertIsNotNone(cap, "no armband was assigned at all")
        self.assertEqual(len(xi), 11)
        self.assertIn(cap, xi,
                      "the armband was assigned to a player outside the XI")
        self.assertIn(cap, selected)

    def test_the_bench_boost_xi_is_a_legal_formation(self):
        with harness.synthetic_world() as (bs, _fx):
            _solve(bs, bench_boost=True)
            form = fpl_tools._LAST_SOLVE["formation"]
        self.assertEqual(form["GK"], 1)
        for pos, lo, hi in (("DEF", 3, 5), ("MID", 2, 5), ("FWD", 1, 3)):
            self.assertTrue(lo <= form[pos] <= hi, f"illegal formation {form}")

    def test_the_captain_is_in_the_xi_on_the_standard_path_too(self):
        """Guard the branch that was already correct, so the restructure
        cannot quietly regress it while fixing the other one."""
        with harness.synthetic_world() as (bs, _fx):
            _entries, _selected, _obj, _parts = _solve(bs, bench_boost=False)
            cap = fpl_tools._LAST_SOLVE["captain"]
            xi = fpl_tools._LAST_SOLVE["xi"]
        self.assertIn(cap, xi)

    def test_bench_boost_still_scores_every_player_at_full_weight(self):
        """The chip's whole point. If the restructure had accidentally left the
        bench-decay reconstruction switched on, Bench Boost would score its
        bench at 30%/3.5%/5% and the chip would be worth almost nothing."""
        with harness.synthetic_world() as (bs, _fx):
            entries, selected, obj, _parts = _solve(bs, bench_boost=True)
            by_id = {e["id"]: e for e in entries}
            full_fifteen = sum(by_id[pid]["xp"] for pid in selected)
            best_eleven = sum(sorted((by_id[pid]["xp"] for pid in selected),
                                     reverse=True)[:11])
        self.assertGreater(
            full_fifteen, best_eleven,
            "fixture has no bench value; test cannot discriminate")
        # The objective carries squad xp + captaincy + tie-break + other terms,
        # so pin the discriminating fact: all fifteen are in, not just eleven.
        self.assertGreaterEqual(obj, full_fifteen - 1e-6,
                                "Bench Boost is not scoring all fifteen")

    def test_the_tiebreak_is_too_small_to_change_the_squad(self):
        """BB_START_TIEBREAK exists only to give branch-and-bound a gradient
        across otherwise-identical elevens. If it were large enough to move
        the objective materially it would start selecting the fifteen."""
        self.assertLess(fpl_tools.BB_START_TIEBREAK, fpl_tools.BENCH_DEAD_WEIGHT / 100.0)
        self.assertGreater(fpl_tools.BB_START_TIEBREAK, 0.0)


if __name__ == "__main__":
    unittest.main()
