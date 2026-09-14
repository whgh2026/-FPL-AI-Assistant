"""Tier 3 gate: a replayed gameweek sees only what was knowable at the time.

`_team_attack_def_ratings()` fitted Dixon-Coles against every fixture the API
reported as finished -- i.e. the season to date, TODAY. Replaying GW6 therefore
scored it with attack/defence ratings tuned on GW1..GW38, so the backtester
flattered the very model it exists to audit, and every row
`scripts/backfill_model_health.py` wrote carried the same edge into the
calibration archive, indistinguishable from a genuine Friday snapshot.

The boundary is STRICTLY BEFORE. A projection for gameweek N is made before N
kicks off, so N's own results are not knowable; admitting them would narrow the
leak from thirty-eight gameweeks to one rather than close it.
"""

import unittest

import fpl_tools
from tests import harness


class AsOfEventFilterTest(unittest.TestCase):

    def setUp(self):
        fpl_tools._clear_rating_caches()

    tearDown = setUp

    def _fixtures(self):
        """Finished results in GW1-3, plus an unplayed GW4."""
        rows = []
        for gw in (1, 2, 3):
            for h, a in ((1, 2), (3, 4)):
                rows.append({"team_h": h, "team_a": a, "event": gw,
                             "finished": True, "team_h_score": 3, "team_a_score": 0,
                             "kickoff_time": "2026-08-%02dT15:00:00Z" % (gw + 1)})
        rows.append({"team_h": 1, "team_a": 3, "event": 4, "finished": False,
                     "team_h_score": None, "team_a_score": None,
                     "kickoff_time": "2026-09-01T15:00:00Z"})
        return rows

    def test_a_later_gameweeks_result_cannot_move_an_earlier_fit(self):
        """The defect, stated directly: perturb GW3 and the GW3 fit must not
        notice, because GW3 had not been played when GW3 was projected."""
        with harness.synthetic_world() as (bs, _fx):
            base = self._fixtures()
            r1 = fpl_tools._team_attack_def_ratings(
                as_of_event=3, bootstrap=bs, fixtures=base)
            att1 = {t: v["att"] for t, v in r1.items()}

            fpl_tools._clear_rating_caches()
            skewed = [dict(f) for f in base]
            for f in skewed:
                if f["event"] == 3:
                    f["team_h_score"], f["team_a_score"] = 0, 9
            r2 = fpl_tools._team_attack_def_ratings(
                as_of_event=3, bootstrap=bs, fixtures=skewed)
            att2 = {t: v["att"] for t, v in r2.items()}

        for t in att1:
            self.assertAlmostEqual(att1[t], att2[t], places=9,
                                   msg=f"team {t}'s GW3 fit moved on a GW3 result")

    def test_the_same_result_does_move_a_later_fit(self):
        """Non-vacuousness: the GW3 result must be visible to a GW4 fit, or
        the test above would pass simply because nothing is ever read."""
        with harness.synthetic_world() as (bs, _fx):
            base = self._fixtures()
            r1 = fpl_tools._team_attack_def_ratings(
                as_of_event=4, bootstrap=bs, fixtures=base)
            fpl_tools._clear_rating_caches()
            skewed = [dict(f) for f in base]
            for f in skewed:
                if f["event"] == 3:
                    f["team_h_score"], f["team_a_score"] = 0, 9
            r2 = fpl_tools._team_attack_def_ratings(
                as_of_event=4, bootstrap=bs, fixtures=skewed)
        moved = any(abs(r1[t]["att"] - r2[t]["att"]) > 1e-6 for t in r1)
        self.assertTrue(moved, "a GW3 result is invisible even to a GW4 fit")

    def test_the_boundary_is_strictly_before_not_inclusive(self):
        """`<=` plus as_of_event=N would admit N's own results. Fitting at N
        must equal fitting at N with N's fixtures deleted entirely."""
        with harness.synthetic_world() as (bs, _fx):
            base = self._fixtures()
            at3 = fpl_tools._team_attack_def_ratings(
                as_of_event=3, bootstrap=bs, fixtures=base)
            fpl_tools._clear_rating_caches()
            without3 = [f for f in base if f["event"] != 3]
            at3_removed = fpl_tools._team_attack_def_ratings(
                as_of_event=3, bootstrap=bs, fixtures=without3)
        for t in at3:
            self.assertAlmostEqual(at3[t]["att"], at3_removed[t]["att"], places=9,
                                   msg="gameweek N's own results are in N's fit")

    def test_the_default_path_is_unchanged(self):
        """as_of_event=None must behave exactly as before -- this is the live
        app's path and nothing about it should move."""
        with harness.synthetic_world() as (bs, fx):
            a = fpl_tools._team_attack_def_ratings()
            fpl_tools._clear_rating_caches()
            b = fpl_tools._team_attack_def_ratings(as_of_event=None)
        self.assertEqual(set(a), set(b))
        for t in a:
            self.assertAlmostEqual(a[t]["att"], b[t]["att"], places=9)


class LiveOddsIsolationTest(unittest.TestCase):

    def test_a_historical_lookup_does_not_read_live_market_prices(self):
        """Today's bookmaker prices are an opinion about matches that have
        already been played -- leakage of a purer kind than the ratings fit."""
        calls = []
        # Patched INSIDE the context: synthetic_world installs its own
        # _fetch_market_win_probs stub on entry, so patching before would be
        # silently overwritten and the test would pass for the wrong reason.
        with harness.synthetic_world() as (bs, fx):
            saved = fpl_tools._fetch_market_win_probs
            fpl_tools._fetch_market_win_probs = lambda bootstrap=None: (
                calls.append(1) or {})
            try:
                fpl_tools._build_fixture_lookup(
                    bs, fixtures_override=fx, as_of_event=5)
                self.assertEqual(calls, [], "a replay fetched live odds")
                fpl_tools._build_fixture_lookup(bs, fixtures_override=fx)
                self.assertEqual(len(calls), 1,
                                 "the live path stopped fetching odds")
            finally:
                fpl_tools._fetch_market_win_probs = saved


if __name__ == "__main__":
    unittest.main()
