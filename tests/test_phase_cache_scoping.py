"""Tier 3 gate: one process can hold several point-in-time fits at once.

The ratings caches were single slots keyed on NOTHING -- a `time.time()` guard
and a value. A backfill replaying GW3 and then GW6 in the same process had the
second replay served the first's fit for 300 seconds, so a replay's answer
depended on what had been replayed before it. `_fixture_calendar` had the same
shape with a worse blast radius: the Free Hit emergency check calls it with a
different `start_event` every gameweek.
"""

import time
import unittest

import fpl_tools
from tests import harness


def _fixtures(n_gw=6):
    rows = []
    for gw in range(1, n_gw + 1):
        for h, a in ((1, 2), (3, 4)):
            rows.append({"team_h": h, "team_a": a, "event": gw, "finished": True,
                         "team_h_score": (gw % 4), "team_a_score": (h % 3),
                         "kickoff_time": "2026-08-%02dT15:00:00Z" % (gw + 1)})
    return rows


class ScopedRatingsCacheTest(unittest.TestCase):

    def setUp(self):
        fpl_tools._clear_rating_caches()

    tearDown = setUp

    def test_two_as_of_windows_do_not_clobber_each_other(self):
        """The defect. Both fits must remain independently reachable."""
        with harness.synthetic_world() as (bs, _fx):
            fx = _fixtures()
            at3 = fpl_tools._team_attack_def_ratings(as_of_event=3, bootstrap=bs, fixtures=fx)
            at6 = fpl_tools._team_attack_def_ratings(as_of_event=6, bootstrap=bs, fixtures=fx)
            # Re-request the FIRST window: a single-slot cache would now hand
            # back the GW6 fit.
            again3 = fpl_tools._team_attack_def_ratings(as_of_event=3, bootstrap=bs, fixtures=fx)
        moved = any(abs(at3[t]["att"] - at6[t]["att"]) > 1e-9 for t in at3)
        self.assertTrue(moved, "fixture cannot discriminate the two windows")
        for t in at3:
            self.assertAlmostEqual(at3[t]["att"], again3[t]["att"], places=12,
                                   msg="the GW3 window was overwritten by GW6")

    def test_the_raw_fit_is_keyed_with_the_banded_ratings(self):
        """_fixture_lambdas reads the RAW fit, and the raw fit sets the
        clean-sheet probability. Keying only the banded ratings would mean a
        cache HIT returns early without re-deriving _DC_RAW, so the lambdas
        would silently come from whichever window was fitted last."""
        with harness.synthetic_world() as (bs, _fx):
            fx = _fixtures()
            fpl_tools._team_attack_def_ratings(as_of_event=3, bootstrap=bs, fixtures=fx)
            lam3 = fpl_tools._fixture_lambdas(1, 2, as_of_event=3)
            fpl_tools._team_attack_def_ratings(as_of_event=6, bootstrap=bs, fixtures=fx)
            lam3_again = fpl_tools._fixture_lambdas(1, 2, as_of_event=3)
            lam6 = fpl_tools._fixture_lambdas(1, 2, as_of_event=6)
        self.assertEqual(lam3, lam3_again,
                         "the GW3 lambdas changed when GW6 was fitted")
        self.assertNotEqual(lam3, lam6,
                            "fixture cannot discriminate the two windows")

    def test_clearing_with_no_argument_clears_every_window(self):
        with harness.synthetic_world() as (bs, _fx):
            fx = _fixtures()
            for gw in (2, 4, 6):
                fpl_tools._team_attack_def_ratings(as_of_event=gw, bootstrap=bs, fixtures=fx)
            self.assertEqual(len(fpl_tools._TEAM_RATINGS_CACHE), 3)
            fpl_tools._clear_rating_caches()
            self.assertEqual(fpl_tools._TEAM_RATINGS_CACHE, {})
            self.assertEqual(fpl_tools._DC_RAW, {})

    def test_a_targeted_clear_leaves_the_other_windows_intact(self):
        with harness.synthetic_world() as (bs, _fx):
            fx = _fixtures()
            for gw in (2, 4):
                fpl_tools._team_attack_def_ratings(as_of_event=gw, bootstrap=bs, fixtures=fx)
            fpl_tools._clear_rating_caches(as_of_event=2)
            keys = set(fpl_tools._TEAM_RATINGS_CACHE)
        self.assertEqual(len(keys), 1)
        self.assertEqual(next(iter(keys))[0], 4)

    def test_the_epoch_is_part_of_every_key(self):
        """A module-local nonce, so a reimport or a fork cannot read entries
        written by a previous incarnation."""
        with harness.synthetic_world() as (bs, _fx):
            fpl_tools._team_attack_def_ratings(
                as_of_event=3, bootstrap=bs, fixtures=_fixtures())
            keys = list(fpl_tools._TEAM_RATINGS_CACHE)
        self.assertEqual(keys[0], (3, fpl_tools._CACHE_EPOCH))


class CalendarKeyTest(unittest.TestCase):

    def setUp(self):
        fpl_tools._CALENDAR_CACHE.clear()
        fpl_tools._CALENDAR_CACHE_TS.clear()

    tearDown = setUp

    def test_one_start_event_does_not_leak_into_another(self):
        with harness.synthetic_world() as (bs, fx):
            lookup = fpl_tools._build_fixture_lookup(bs, fixtures_override=fx)
            a = fpl_tools._fixture_calendar(start_event=1, n=12)
            b = fpl_tools._fixture_calendar(start_event=10, n=12)
            keys = set(fpl_tools._CALENDAR_CACHE)
        self.assertEqual(keys, {(1, 12), (10, 12)},
                         "the calendar is still held in a single slot")
        self.assertNotEqual(sorted(a), sorted(b),
                            "fixture cannot discriminate the two windows")

    def test_one_horizon_length_does_not_leak_into_another(self):
        with harness.synthetic_world() as (bs, fx):
            fpl_tools._fixture_calendar(start_event=1, n=12)
            fpl_tools._fixture_calendar(start_event=1, n=38)
            keys = set(fpl_tools._CALENDAR_CACHE)
        self.assertEqual(keys, {(1, 12), (1, 38)})

    def test_an_explicitly_supplied_lookup_bypasses_the_cache(self):
        """D-H. The alternative -- keying on id(fixture_lookup) -- is unsafe:
        CPython reuses id() values after garbage collection, so a freed
        lookup's id can be reassigned to a different object and produce a
        false HIT with a silently wrong calendar."""
        with harness.synthetic_world() as (bs, fx):
            lookup = fpl_tools._build_fixture_lookup(bs, fixtures_override=fx)
            fpl_tools._fixture_calendar(lookup, start_event=1, n=12)
            self.assertEqual(fpl_tools._CALENDAR_CACHE, {},
                             "an explicit lookup was cached anyway")


if __name__ == "__main__":
    unittest.main()
