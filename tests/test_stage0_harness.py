"""Stage 0 gate: the golden-file harness itself must be trustworthy.

A regression harness that leaks state between runs, or silently stops covering a
new cache, is worse than none -- it produces green runs that mean nothing. These
tests check the harness before anything relies on it.
"""

import json
import os
import unittest

import fpl_tools
from tests import harness

EVENT = 4

# Module-level caches that `harness.reset_caches` deliberately does not touch,
# with the reason. Anything else cache-shaped must be reset between runs.
_EXEMPT = {
    "_ODDS_CACHE": "reset explicitly (dict with a different shape)",
    "_LAST_FETCH_TIME": "diagnostic only, never read for correctness",
    "_LAST_SOLVE": "per-solve diagnostics, overwritten on every call",
}


class HarnessIntegrityTest(unittest.TestCase):
    def test_cache_reset_list_is_complete(self):
        """Every module-level cache in fpl_tools must be reset between runs.

        Fails loudly when a new cache is introduced without harness support,
        rather than letting state leak into a later test.
        """
        found = {
            name for name in dir(fpl_tools)
            if name.startswith("_")
            and ("CACHE" in name or name in ("_LEAGUE_AVG", "_LEAGUE_AVG_TS",
                                             "_TEAM_PLAYED", "_TEAM_PLAYED_TS",
                                             "_TEAM_RATINGS_TS", "_LIVE_CACHE_TS",
                                             "_CALENDAR_CACHE_TS"))
        }
        uncovered = found - set(harness._CACHE_RESET) - set(_EXEMPT)
        self.assertEqual(
            uncovered, set(),
            f"new fpl_tools cache(s) not handled by harness.reset_caches: {sorted(uncovered)}")

    def test_fixture_matches_generator_output(self):
        """The committed JSON must be what build_bootstrap.py produces.

        Guards against someone hand-editing the fixture, which would make the
        golden files unreproducible.
        """
        bootstrap, fixtures = harness.load_synthetic()
        self.assertEqual(len(bootstrap["teams"]), 20)
        self.assertEqual(len(bootstrap["elements"]), 360)
        self.assertEqual(len(fixtures), 120)
        counts = {}
        for e in bootstrap["elements"]:
            counts[e["element_type"]] = counts.get(e["element_type"], 0) + 1
        self.assertEqual(counts, {1: 40, 2: 120, 3: 120, 4: 80})

    def test_fixture_preserves_the_fpl_strength_scale_split(self):
        """`strength` is 1-5; `strength_overall_*` is ~1000-1400.

        This split is the premise of the Stage 4 clamp bug: the ratings blend
        mixes the 1000-1400 field into a [1,5] scale. The fixture mirrors the
        real API so Stage 4 can be tested offline -- but the premise itself
        still needs confirming against a live bootstrap before Stage 4 ships,
        because the build environment cannot reach the FPL API.
        """
        bootstrap, _ = harness.load_synthetic()
        for t in bootstrap["teams"]:
            self.assertIn(t["strength"], (1, 2, 3, 4, 5))
            self.assertGreater(t["strength_overall_home"], 900)
            self.assertLess(t["strength_overall_home"], 1500)

    def test_fixture_contains_a_blank_and_a_double_gameweek(self):
        """Stage 5's BGW/DGW detectors need something to detect."""
        _bs, fixtures = harness.load_synthetic()
        per_team_gw = {}
        for f in fixtures:
            for tid in (f["team_h"], f["team_a"]):
                per_team_gw.setdefault(f["event"], {}).setdefault(tid, 0)
                per_team_gw[f["event"]][tid] += 1

        blanking = sum(1 for t in range(1, 21) if per_team_gw.get(9, {}).get(t, 0) == 0)
        doubling = sum(1 for t in range(1, 21) if per_team_gw.get(10, {}).get(t, 0) >= 2)
        self.assertGreaterEqual(blanking, 4, "fixture has no usable blank gameweek")
        self.assertGreaterEqual(doubling, 4, "fixture has no usable double gameweek")

    def test_synthetic_world_is_hermetic(self):
        """No network call may escape the patched world."""
        def _boom(*a, **k):
            raise AssertionError("network access attempted inside synthetic_world")

        saved = fpl_tools._cached_json
        fpl_tools._cached_json = _boom
        try:
            with harness.synthetic_world() as (bs, _fx):
                lookup = fpl_tools._build_fixture_lookup(bs)
                entries = harness.squad_to_pool_entries(
                    bs, harness.build_squad(bs, "balanced"), lookup, EVENT)
                self.assertEqual(len(entries), 15)
        finally:
            fpl_tools._cached_json = saved

    def test_world_restores_state_on_exit(self):
        """Patched accessors and the solver profile must be put back."""
        before = (fpl_tools._get_bootstrap, fpl_tools._get_fixtures,
                  fpl_tools.get_solver_profile())
        with harness.synthetic_world("deterministic"):
            self.assertEqual(fpl_tools.get_solver_profile(), "deterministic")
        after = (fpl_tools._get_bootstrap, fpl_tools._get_fixtures,
                 fpl_tools.get_solver_profile())
        self.assertEqual(before, after)

    def test_xp_is_reproducible_across_worlds(self):
        """Two independent worlds must produce identical projections."""
        runs = []
        for _ in range(2):
            with harness.synthetic_world() as (bs, _fx):
                lookup = fpl_tools._build_fixture_lookup(bs)
                entries = harness.squad_to_pool_entries(
                    bs, harness.build_squad(bs, "balanced"), lookup, EVENT)
                runs.append([(e["id"], e["xp"], e["xp_gw"]) for e in entries])
        self.assertEqual(runs[0], runs[1])

    def test_every_declared_squad_builds(self):
        """All 20 archetypes must produce a legal 2/5/5/3 squad."""
        with harness.synthetic_world() as (bs, _fx):
            for name, _desc in harness.SQUAD_SPECS:
                with self.subTest(squad=name):
                    squad = harness.build_squad(bs, name)
                    counts = {}
                    for e in squad:
                        counts[e["element_type"]] = counts.get(e["element_type"], 0) + 1
                    self.assertEqual(counts, {1: 2, 2: 5, 3: 5, 4: 3})
                    clubs = {}
                    for e in squad:
                        clubs[e["team"]] = clubs.get(e["team"], 0) + 1
                    self.assertLessEqual(max(clubs.values()), 3)
        self.assertEqual(len(harness.SQUAD_SPECS), 20)
        self.assertEqual(len(harness.SMOKE_SQUADS), 5)


if __name__ == "__main__":
    unittest.main()
