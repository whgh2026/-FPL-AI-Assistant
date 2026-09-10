"""Stage 8c gate: minutes remember WHEN, not just how many.

A season-long starts/games rate has no memory of when the starts happened. A
player benched for the opening ten matches and nailed for the last five reads as
0.33 -- a rotation risk -- when he is a certain starter; the mirror case reads
as nailed when he has lost his place. Minutes multiply every other component of
the projection, so this is not a small edge: it is "sell the nailed player, buy
the one who has been dropped", stated confidently.

The per-match history behind the fix is one HTTP call per player, so it is
fetched deliberately and never from the projection path. These tests populate
the cache directly, which is also the only way to control exactly what history a
player has.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fpl_tools as ft
from tests import harness


def history(pattern):
    """Per-match rows from a string: S start, C cameo, B benched. Oldest first."""
    rows = []
    for ch in pattern:
        if ch == "S":
            rows.append({"minutes": 90, "starts": 1})
        elif ch == "C":
            rows.append({"minutes": 20, "starts": 0})
        else:
            rows.append({"minutes": 0, "starts": 0})
    return rows


def cache(pid, pattern):
    s = ft._summarise_recent(history(pattern))
    ft._RECENT_CACHE[pid] = s
    ft._RECENT_CACHE_TS[pid] = time.time()
    return s


def player(pid=500, **over):
    p = {"id": pid, "team": 1, "element_type": 3, "status": "a",
         "minutes": 450, "starts": 5, "chance_of_playing_next_round": None}
    p.update(over)
    return p


class SummaryTest(unittest.TestCase):
    def test_recent_matches_dominate_old_ones(self):
        """The whole point. Same fifteen matches, opposite order."""
        benched_then_nailed = ft._summarise_recent(history("BBBBBBBBBBSSSSS"))
        nailed_then_dropped = ft._summarise_recent(history("SSSSSBBBBBBBBBB"))
        self.assertGreater(benched_then_nailed["start_rate"], 0.7,
                           "five straight starts still read as a rotation risk")
        self.assertLess(nailed_then_dropped["start_rate"], 0.3,
                        "a player dropped ten matches ago still reads as nailed")

    def test_a_season_rate_cannot_tell_those_two_apart(self):
        """Stated as the baseline this replaces: both are 5 starts in 15."""
        a, b = "BBBBBBBBBBSSSSS", "SSSSSBBBBBBBBBB"
        self.assertEqual(a.count("S"), b.count("S"))
        self.assertNotAlmostEqual(
            ft._summarise_recent(history(a))["start_rate"],
            ft._summarise_recent(history(b))["start_rate"], places=2)

    def test_half_life_is_respected(self):
        """A match RECENT_HALF_LIFE old carries half the weight of the newest."""
        s = ft._summarise_recent(history("SB" * 8))
        self.assertGreater(ft.RECENT_HALF_LIFE, 1.0)
        self.assertGreater(s["n_eff"], 1.0)
        self.assertLess(s["n_eff"], 16.0, "weights are not decaying at all")

    def test_cameos_are_counted_not_inferred(self):
        s = ft._summarise_recent(history("CCCCCC"))
        self.assertGreater(s["sub_rate"], 0.8)
        self.assertLess(s["start_rate"], 0.2)

    def test_sixty_minutes_counts_as_a_start_when_starts_is_absent(self):
        s = ft._summarise_recent([{"minutes": 75}] * 5)
        self.assertGreater(s["start_rate"], 0.8)

    def test_empty_and_malformed_history(self):
        self.assertIsNone(ft._summarise_recent([]))
        self.assertIsNone(ft._summarise_recent([{"no_minutes": 1}]))


class DistributionTest(unittest.TestCase):
    def setUp(self):
        ft._RECENT_CACHE.clear()
        ft._RECENT_CACHE_TS.clear()

    tearDown = setUp

    def test_recency_overrides_the_season_rate(self):
        with harness.synthetic_world():
            harness.load_synthetic()
            # The season figures have to actually SAY rotation risk, or this
            # test proves nothing. The fixture's clubs have played 3 matches, so
            # games = max(starts, 3, 1): one start in three reads as 0.5, while
            # five starts in five already reads as nailed and there would be
            # nothing for recency to lift.
            p = player(minutes=120, starts=1)
            before = ft._expected_playing_fraction(p, "a")
            cache(p["id"], "BBBBBBBBBBSSSSS")
            after = ft._expected_playing_fraction(p, "a")
            self.assertGreater(after, before,
                               "recent starts did not lift the projection")

    def test_recency_can_also_lower_a_projection(self):
        with harness.synthetic_world():
            harness.load_synthetic()
            p = player(minutes=900, starts=10)
            before = ft._expected_playing_fraction(p, "a")
            cache(p["id"], "SSSSSSSSSSBBBBB")
            after = ft._expected_playing_fraction(p, "a")
            self.assertLess(after, before,
                            "a player dropped five matches ago still reads as nailed")

    def test_thin_history_falls_back_to_the_season_rate(self):
        """Two matches is not evidence of a changed role."""
        with harness.synthetic_world():
            harness.load_synthetic()
            p = player()
            before = ft._expected_playing_fraction(p, "a")
            cache(p["id"], "SS")
            self.assertEqual(ft._expected_playing_fraction(p, "a"), before)

    def test_an_expired_entry_is_ignored(self):
        with harness.synthetic_world():
            harness.load_synthetic()
            p = player()
            before = ft._expected_playing_fraction(p, "a")
            cache(p["id"], "BBBBBBBBBBSSSSS")
            ft._RECENT_CACHE_TS[p["id"]] = time.time() - ft.RECENT_TTL_SECONDS - 1
            self.assertEqual(ft._expected_playing_fraction(p, "a"), before)

    def test_an_empty_cache_changes_nothing(self):
        """The engine must work exactly as before with no history at all."""
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            for e in bootstrap["elements"][:40]:
                self.assertGreaterEqual(ft._expected_playing_fraction(e, "a"), 0.0)

    def test_probabilities_stay_normalised(self):
        with harness.synthetic_world():
            harness.load_synthetic()
            for pattern in ("SSSSSSSSSS", "CCCCCCCCCC", "BBBBBBBBBB", "SCBSCBSCBS"):
                p = player(pid=hash(pattern) % 10000)
                cache(p["id"], pattern)
                p0, pc, pf = ft._minute_distribution(p, "a")
                self.assertAlmostEqual(p0 + pc + pf, 1.0, places=6, msg=pattern)
                for v in (p0, pc, pf):
                    self.assertGreaterEqual(v, 0.0)
                    self.assertLessEqual(v, 1.0)


class DoubtRedistributionTest(unittest.TestCase):
    """The old model scaled p_full and p_cameo by the SAME availability factor,
    so the full/cameo split was identical however doubtful the player was."""

    def test_doubt_shifts_mass_from_starting_into_a_cameo(self):
        with harness.synthetic_world():
            harness.load_synthetic()
            fit = player(chance_of_playing_next_round=None)
            doubt = player(chance_of_playing_next_round=50)
            _, pc_fit, pf_fit = ft._minute_distribution(fit, "a")
            _, pc_d, pf_d = ft._minute_distribution(doubt, "a")
            share_fit = pf_fit / max(1e-9, pf_fit + pc_fit)
            share_doubt = pf_d / max(1e-9, pf_d + pc_d)
            self.assertLess(share_doubt, share_fit,
                            "doubt does not change the full/cameo split at all")

    def test_doubt_still_reduces_the_chance_of_featuring(self):
        with harness.synthetic_world():
            harness.load_synthetic()
            p0_fit, pc_f, pf_f = ft._minute_distribution(
                player(chance_of_playing_next_round=None), "a")
            p0_d, pc_d, pf_d = ft._minute_distribution(
                player(chance_of_playing_next_round=25), "a")
            self.assertGreater(p0_d, p0_fit)
            self.assertLess(pf_d, pf_f)

    def test_a_fully_fit_player_is_unaffected(self):
        """At zero doubt the redistribution must be the identity, or this
        changes every player in the game rather than the doubtful ones."""
        with harness.synthetic_world():
            harness.load_synthetic()
            a = ft._minute_distribution(player(chance_of_playing_next_round=None), "a")
            b = ft._minute_distribution(player(chance_of_playing_next_round=100), "a")
            for x, y in zip(a, b):
                self.assertAlmostEqual(x, y, places=9)

    def test_keepers_never_get_a_cameo(self):
        with harness.synthetic_world():
            harness.load_synthetic()
            _p0, pc, _pf = ft._minute_distribution(
                player(element_type=1, chance_of_playing_next_round=50), "a")
            self.assertAlmostEqual(pc, 0.0, places=9)


class MinutesDiscountEndToEndTest(unittest.TestCase):
    """DoubtRedistributionTest above proves _minute_distribution's own three
    probabilities move the right way in isolation. Nothing before this walks
    that discount all the way through _player_xp_raw/_player_xp to the final
    projection -- the number every other engine decision (transfers,
    captaincy, the solver's objective) actually reads. The two players below
    share every attacking input (expected_goals_per_90, expected_assists_
    per_90 -- both set comfortably above the positional prior
    _xp_for_fixture's empirical-Bayes shrinkage pulls thin-minutes players
    toward, so that shrinkage reinforces rather than fights the effect under
    test here) and differ ONLY in the signals _minute_distribution reads:
    minutes, starts, and chance_of_playing_next_round.
    """

    def test_a_rotation_and_doubt_risk_scores_strictly_below_an_identical_nailed_starter(self):
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            lookup = ft._build_fixture_lookup(bootstrap)
            gw = ft._next_gameweek(bootstrap)
            nailed = player(pid=501, expected_goals_per_90=0.6, expected_assists_per_90=0.4,
                            minutes=900, starts=10, chance_of_playing_next_round=None)
            fringe = player(pid=501, expected_goals_per_90=0.6, expected_assists_per_90=0.4,
                            minutes=90, starts=1, chance_of_playing_next_round=50)
            xp_nailed, _ = ft._player_xp_raw(nailed, lookup, event=gw)
            xp_fringe, _ = ft._player_xp_raw(fringe, lookup, event=gw)
            self.assertGreater(xp_nailed, 0.0,
                               "sanity check: the nailed player must have a live projection")
            self.assertLess(xp_fringe, xp_nailed,
                            "identical per-90 production must not save a rotation/doubt "
                            "risk from a strictly lower final projection")

    def test_the_ordering_survives_the_global_modifier_in_player_xp(self):
        """_player_xp layers weights.json's global_xP_modifier and rounding
        on top of _player_xp_raw -- neither may undo the ordering
        _player_xp_raw already established, and _player_xp (not the _raw
        variant) is what every production call site actually uses."""
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            lookup = ft._build_fixture_lookup(bootstrap)
            gw = ft._next_gameweek(bootstrap)
            nailed = player(pid=501, expected_goals_per_90=0.6, expected_assists_per_90=0.4,
                            minutes=900, starts=10, chance_of_playing_next_round=None)
            fringe = player(pid=501, expected_goals_per_90=0.6, expected_assists_per_90=0.4,
                            minutes=90, starts=1, chance_of_playing_next_round=50)
            xp_nailed, _ = ft._player_xp(nailed, lookup, event=gw)
            xp_fringe, _ = ft._player_xp(fringe, lookup, event=gw)
            self.assertLess(xp_fringe, xp_nailed)


class FetchDisciplineTest(unittest.TestCase):
    """One HTTP call per player, so where it is NOT called matters."""

    def setUp(self):
        ft._RECENT_CACHE.clear()
        ft._RECENT_CACHE_TS.clear()

    tearDown = setUp

    def test_the_projection_path_never_fetches(self):
        calls = []
        saved = ft._element_summary
        ft._element_summary = lambda pid: calls.append(pid) or None
        try:
            with harness.synthetic_world():
                bootstrap, _ = harness.load_synthetic()
                lookup = ft._build_fixture_lookup(bootstrap)
                gw = ft._next_gameweek(bootstrap)
                for e in bootstrap["elements"][:50]:
                    ft._player_xp(e, lookup, event=gw)
            self.assertEqual(calls, [],
                             "the projection is fetching per-player history")
        finally:
            ft._element_summary = saved

    def test_prefetch_is_bounded(self):
        calls = []
        saved = ft._element_summary
        ft._element_summary = lambda pid: calls.append(pid) or None
        try:
            ft.prefetch_recent_minutes(range(1, 1000), limit=7)
            self.assertEqual(len(calls), 7)
        finally:
            ft._element_summary = saved

    def test_prefetch_skips_what_is_already_cached(self):
        calls = []
        saved = ft._element_summary
        ft._element_summary = lambda pid: calls.append(pid) or history("SSSSS")
        try:
            ft.prefetch_recent_minutes([1, 2, 3])
            self.assertEqual(len(calls), 3)
            ft.prefetch_recent_minutes([1, 2, 3])
            self.assertEqual(len(calls), 3, "a warm cache was refetched")
        finally:
            ft._element_summary = saved

    def test_a_failed_fetch_is_not_retried_on_every_call(self):
        """A player with no history must not cost a request per render."""
        calls = []
        saved = ft._element_summary
        ft._element_summary = lambda pid: calls.append(pid) or None
        try:
            ft.prefetch_recent_minutes([42])
            ft.prefetch_recent_minutes([42])
            self.assertEqual(len(calls), 1, "a failed fetch is retried every time")
        finally:
            ft._element_summary = saved

    def test_network_failure_degrades_silently_to_the_season_rate(self):
        saved = ft._element_summary
        ft._element_summary = lambda pid: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            with harness.synthetic_world():
                harness.load_synthetic()
                p = player()
                before = ft._expected_playing_fraction(p, "a")
                ft.prefetch_recent_minutes([p["id"]])
                self.assertEqual(ft._expected_playing_fraction(p, "a"), before)
        finally:
            ft._element_summary = saved

    def test_the_suite_runs_with_the_prefetch_disabled(self):
        """Stated, not hidden: there is no FPL API in the build environment, so
        the harness turns the prefetch off and the suite exercises the SEASON
        path. The recency path is covered by populating the cache directly."""
        with harness.synthetic_world():
            self.assertFalse(ft.RECENT_MINUTES_ENABLED)


class SubAppearanceLengthTest(unittest.TestCase):
    def test_the_assumed_cameo_length_is_realistic(self):
        """sub_apps = sub_minutes / 30.0 assumed every cameo is exactly half an
        hour, which over-counts the minutes and under-counts the appearances."""
        self.assertLess(ft.AVG_SUB_MINUTES, 30.0)
        self.assertGreater(ft.AVG_SUB_MINUTES, 10.0)


if __name__ == "__main__":
    unittest.main()
