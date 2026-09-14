"""Tier 1.1 gate: Free Hit is generated, routed and priced as a ONE-WEEK chip.

Free Hit lasts exactly one gameweek. The engine already solved a dedicated
one-week squad for it (`fh_selected`, scored on `xp_gw` rather than the
four-gameweek horizon) -- but never derived that squad's MOVES, so
`suggest_transfers_for_custom_squad` returned no Free Hit transfer list at all
and Step 3 fell back to `wildcard_transfers`: a four-gameweek horizon rebuild
applied to a chip that reverts after ninety minutes.

The second half of this file pins the solve CONSOLIDATION. Two
argument-for-argument identical unlimited solves used to run per click -- one
feeding `wildcard_transfers`, one feeding `chip_scores["Wildcard"]` -- so the
moves shown to a manager and the score gating them came from separate MILP
runs and were free to drift apart.
"""

import unittest

import fpl_tools
from tests import harness

EVENT = 4


def _solve(bs, chips, **over):
    squad = harness.squad_as_manager_input(bs, harness.build_squad(bs, "balanced"))
    kw = dict(bank=2.0, free_transfers=1, eval_chips=chips, event=EVENT,
              risk="balanced", holding_map=None, current_gw=EVENT, allow_hits=False)
    kw.update(over)
    return fpl_tools.suggest_transfers_for_custom_squad(squad, **kw)


class FreeHitRoutingTest(unittest.TestCase):

    def test_the_return_contract_carries_a_freehit_transfers_key(self):
        """The key Step 3 routes a confirmed Free Hit to. Its absence is what
        forced the fallback onto `wildcard_transfers` in the first place."""
        with harness.synthetic_world("interactive") as (bs, _fx):
            res = _solve(bs, ["Free Hit"])
        self.assertIn("freehit_transfers", res)
        self.assertIsInstance(res["freehit_transfers"], list)

    def test_freehit_moves_are_priced_in_one_week_units(self):
        """The actual units bug, pinned at its source. fh_pool rewrites each
        entry's "xp" to its one-week `xp_gw`; reading the moves back out of the
        HORIZON pool_by_id would report a four-gameweek xp_gain on a one-week
        chip -- exactly the ~3x inflation that made Free Hit outrank Bench
        Boost and Triple Captain before it was given its own solve."""
        with harness.synthetic_world("interactive") as (bs, _fx):
            res = _solve(bs, ["Free Hit"])
        moves = res["freehit_transfers"]
        if not moves:
            self.skipTest("solver held the squad; no Free Hit moves to price")
        for m in moves:
            for side in ("in", "out"):
                p = m[side]
                if "xp_gw" in p:
                    self.assertAlmostEqual(
                        p["xp"], p["xp_gw"], places=6,
                        msg=f"{side} player carries horizon xp, not the one-week figure")

    def test_a_wildcard_only_evaluation_emits_no_freehit_transfers(self):
        with harness.synthetic_world("interactive") as (bs, _fx):
            res = _solve(bs, ["Wildcard"])
        self.assertEqual(res["freehit_transfers"], [])

    def test_a_freehit_only_evaluation_emits_no_wildcard_transfers(self):
        """Consequence of the consolidation, stated so it cannot regress by
        accident: the deleted "Universe B" solve ran whenever EITHER chip was
        under evaluation, so a Free-Hit-only call used to populate
        `wildcard_transfers` with a squad no Wildcard had been scored for."""
        with harness.synthetic_world("interactive") as (bs, _fx):
            res = _solve(bs, ["Free Hit"])
        self.assertEqual(res["wildcard_transfers"], [])

    def test_both_keys_are_independent_when_both_chips_are_evaluated(self):
        """The headline contract: the two chips are solved separately and must
        not be aliases of one another."""
        with harness.synthetic_world("interactive") as (bs, _fx):
            res = _solve(bs, ["Wildcard", "Free Hit"])
        self.assertIsNot(res["freehit_transfers"], res["wildcard_transfers"],
                         "the two chips are sharing one move list object")


class SolveConsolidationTest(unittest.TestCase):
    """Source-level, because the defect is a duplicated CALL -- invisible in
    the return value, which is why it survived. Same technique as
    test_stage8_calibration.py::AutoTuneGuardTest's write-ordering check."""

    def _src(self):
        import inspect
        return inspect.getsource(fpl_tools.suggest_transfers_for_custom_squad)

    def test_only_one_unlimited_wildcard_solve_remains(self):
        src = self._src()
        self.assertNotIn("unl_selected", src,
                         "the duplicate Universe B unlimited solve is still here")
        self.assertNotIn("unl_moves", src)

    def test_the_wildcard_solve_does_not_inherit_bench_boost(self):
        """Scoring all 15 at full weight is a Bench Boost assumption. Folding
        it into the Wildcard solve made chip_scores["Wildcard"] rise purely
        because Bench Boost was under evaluation in the same call, so the two
        chips could not be ranked against each other."""
        src = self._src()
        self.assertNotIn('bench_boost=("Bench Boost" in eval_chips)', src,
                         "a chip solve still couples its bench weighting to Bench Boost")


if __name__ == "__main__":
    unittest.main()
