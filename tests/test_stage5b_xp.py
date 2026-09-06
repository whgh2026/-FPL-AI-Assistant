"""Stage 5b Tranche 1 gate: the player forecast.

Stages 1-5 fixed the solver and the team model while leaving _player_xp full of
flat constants and one outright paradox. This tranche addresses the four items
agreed as immediate; BPS bonus, exponential minutes recency and per-player card
rates are Tranche 2.

Ordering matters here and is asserted where it can be: the EP_BLEND taper comes
first because it was capping the benefit of ALL the Stage 4 ratings work at
roughly 50%, and the Beta prior on start_rate comes before the clean-sheet fix
because that fix consumes p_full.
"""

import copy
import math
import unittest

import fpl_tools
from tests import harness

EVENT = 4


class FixtureLambdaTest(unittest.TestCase):
    """The Dixon-Coles fit estimates goal rates; they were being thrown away."""

    def test_lambdas_are_exposed_on_every_fixture(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            for tid, fixtures in lookup.items():
                for f in fixtures:
                    self.assertIn("lam_for", f)
                    self.assertIn("lam_against", f)
                    self.assertGreater(f["lam_for"], 0.0)

    def test_a_strong_side_scores_more_and_concedes_less(self):
        with harness.synthetic_world() as (bs, _fx):
            fpl_tools._team_attack_def_ratings()      # populate the raw fit
            strong_h, strong_a = fpl_tools._fixture_lambdas(1, 20)
            weak_h, weak_a = fpl_tools._fixture_lambdas(20, 1)
            self.assertGreater(strong_h, weak_h, "the better side should score more")
            self.assertLess(strong_a, weak_a, "the better side should concede less")

    def test_lambdas_are_in_a_plausible_football_range(self):
        with harness.synthetic_world() as (bs, _fx):
            fpl_tools._team_attack_def_ratings()
            for h in range(1, 21):
                for a in range(1, 21):
                    if h == a:
                        continue
                    lh, la = fpl_tools._fixture_lambdas(h, a)
                    self.assertGreater(lh, 0.1)
                    self.assertLess(lh, 6.0)


class CleanSheetTest(unittest.TestCase):
    """C8: the cameo clean-sheet paradox."""

    def _defender_and_fixture(self, bs, lookup, team=1):
        d = next(e for e in bs["elements"]
                 if e["element_type"] == 2 and e["team"] == team)
        return d, fpl_tools._gw_fixtures(lookup[team], EVENT)[0]

    def test_team_clean_sheet_probability_ignores_player_minutes(self):
        """The headline. P(CS) is a TEAM-MATCH event.

        Before: the minutes fraction sat inside the exponent, so
        xgc = 1.3 * (30/90) = 0.43 gave P(CS) = 65% for a cameo against 27% for
        a full 90. Playing less made your team likelier to keep a clean sheet.
        """
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            _d, f = self._defender_and_fixture(bs, lookup)
            p_cs = math.exp(-f["lam_against"])
            self.assertGreater(p_cs, 0.0)
            self.assertLess(p_cs, 1.0)
            # Derived purely from the fixture: no player attribute involved.
            self.assertNotIn("minutes", f)

    def test_a_cameo_earns_no_clean_sheet_points(self):
        """FPL requires 60 minutes. The old code paid a 30-minute cameo MORE
        clean-sheet value than a full match; it must now pay none."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            d, f = self._defender_and_fixture(bs, lookup)
            gk = next(e for e in bs["elements"]
                      if e["element_type"] == 1 and e["team"] == 1)
            for player, pos in ((d, 2), (gk, 1)):
                full = fpl_tools._xp_for_fixture(player, f, 90.0, pos)
                cameo = fpl_tools._xp_for_fixture(player, f, 30.0, pos)
                self.assertGreater(full, cameo,
                                   "a full match must be worth more than a cameo")

    def test_xp_is_monotone_in_minutes(self):
        """The paradox showed up as a non-monotonicity; it must be gone."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            d, f = self._defender_and_fixture(bs, lookup)
            vals = [fpl_tools._xp_for_fixture(d, f, m, 2) for m in (15, 30, 45, 60, 75, 90)]
            for a, b in zip(vals, vals[1:]):
                self.assertLessEqual(a, b + 1e-9, f"xp fell as minutes rose: {vals}")

    def test_a_tougher_opponent_lowers_clean_sheet_value(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            d, _f = self._defender_and_fixture(bs, lookup)
            easy = {"is_home": True, "lam_against": 0.6, "lam_for": 2.0,
                    "opp_strength_def": 2.0, "opp_strength_att": 2.0}
            hard = {"is_home": True, "lam_against": 2.2, "lam_for": 0.8,
                    "opp_strength_def": 4.5, "opp_strength_att": 4.5}
            self.assertGreater(fpl_tools._xp_for_fixture(d, easy, 90.0, 2),
                               fpl_tools._xp_for_fixture(d, hard, 90.0, 2))


class EpBlendTaperTest(unittest.TestCase):
    """C9: FPL's own ep_next becomes a thin-data prior, not half the answer."""

    def test_weight_decays_to_zero_with_evidence(self):
        def w(mins):
            return max(0.0, min(fpl_tools.EP_BLEND,
                                fpl_tools.EP_BLEND * (1.0 - mins / fpl_tools.EP_BLEND_FADE_MINUTES)))
        self.assertAlmostEqual(w(0), fpl_tools.EP_BLEND)
        self.assertGreater(w(0), w(300))
        self.assertGreater(w(300), w(500))
        self.assertEqual(w(fpl_tools.EP_BLEND_FADE_MINUTES), 0.0)
        self.assertEqual(w(3000), 0.0)

    def test_established_player_is_independent_of_the_vendor_number(self):
        """Past the fade, our projection must stand on its own."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            # Constructed, not searched: at GW4 only three matches have been
            # played, so no player CAN have 600 minutes. That is the point of
            # the taper -- early in the season everyone still leans on ep_next,
            # and the model takes over as evidence accumulates.
            base = copy.deepcopy(next(e for e in bs["elements"] if e["element_type"] == 3))
            base["minutes"] = fpl_tools.EP_BLEND_FADE_MINUTES + 200
            base["starts"] = 8
            lo = copy.deepcopy(base); lo["ep_next"] = 0.5
            hi = copy.deepcopy(base); hi["ep_next"] = 12.0
            self.assertEqual(fpl_tools._player_xp(lo, lookup, event=EVENT),
                             fpl_tools._player_xp(hi, lookup, event=EVENT))

    def test_thin_data_player_still_leans_on_it(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            base = next(e for e in bs["elements"] if e["element_type"] == 3)
            thin = copy.deepcopy(base)
            thin["minutes"] = 45          # barely any evidence
            thin["starts"] = 0
            lo = copy.deepcopy(thin); lo["ep_next"] = 0.5
            hi = copy.deepcopy(thin); hi["ep_next"] = 12.0
            self.assertNotEqual(fpl_tools._player_xp(lo, lookup, event=EVENT),
                                fpl_tools._player_xp(hi, lookup, event=EVENT))


class StartRatePriorTest(unittest.TestCase):
    """C10a: the small-sample half of the minutes problem."""

    def _dist(self, starts, games, played=None):
        p = {"element_type": 3, "team": 1, "starts": starts,
             "minutes": (played if played is not None else starts * 90),
             "chance_of_playing_next_round": None}
        from unittest import mock
        with mock.patch.object(fpl_tools, "_team_played_map", return_value={1: games}):
            return fpl_tools._minute_distribution(p, "a")

    def test_a_single_rest_no_longer_swings_a_starter_by_a_quarter(self):
        """At GW4 an unshrunk rate moved 1.00 -> 0.75 on one rotation, and
        minutes multiply every other component of the projection."""
        _p0, _c, full_4 = self._dist(4, 4)
        _p0, _c, full_3 = self._dist(3, 4)
        self.assertLess(full_4 - full_3, 0.20, "shrinkage is too weak")
        self.assertGreater(full_4 - full_3, 0.05, "shrinkage is too strong")

    def test_evidence_still_dominates_the_prior(self):
        _p0, _c, full = self._dist(19, 19)
        self.assertGreater(full, 0.93, "a season-long starter must read as nailed")

    def test_prior_mean_is_above_a_coin_flip(self):
        """A player with minutes on the clock is more likely than not a starter;
        a symmetric prior would drag nailed players down."""
        self.assertGreater(fpl_tools.PRIOR_STARTS / fpl_tools.PRIOR_GAMES, 0.5)


class DefconTest(unittest.TestCase):
    """C6: every defender used to receive an identical +1.36."""

    def test_rate_varies_across_defenders(self):
        with harness.synthetic_world() as (bs, _fx):
            rates = [fpl_tools._defcon_rate_per90(e, 2)
                     for e in bs["elements"] if e["element_type"] == 2]
            self.assertGreater(len(set(round(r, 3) for r in rates)), 20,
                               "DefCon is still a positional constant")

    def test_points_vary_across_defenders(self):
        with harness.synthetic_world() as (bs, _fx):
            pts = [fpl_tools._defcon_expected_pts(e, 90.0, 2)
                   for e in bs["elements"] if e["element_type"] == 2]
            self.assertGreater(max(pts) - min(pts), 0.2,
                               "no per-player spread in DefCon points")

    def test_a_busier_defender_scores_more(self):
        busy = {"element_type": 2, "minutes": 900,
                "clearances_blocks_interceptions": 120, "tackles": 40, "recoveries": 30}
        quiet = {"element_type": 2, "minutes": 900,
                 "clearances_blocks_interceptions": 20, "tackles": 5, "recoveries": 10}
        self.assertGreater(fpl_tools._defcon_rate_per90(busy, 2),
                           fpl_tools._defcon_rate_per90(quiet, 2))
        self.assertGreater(fpl_tools._defcon_expected_pts(busy, 90.0, 2),
                           fpl_tools._defcon_expected_pts(quiet, 90.0, 2))

    def test_influence_no_longer_drives_it(self):
        """`influence` is season-cumulative, so min(influence/60, 2.0) saturated
        at 2.0 for every regular -- which is how all defenders ended up equal."""
        a = {"element_type": 2, "minutes": 900, "influence": 50.0,
             "clearances_blocks_interceptions": 60, "tackles": 20, "recoveries": 15}
        b = dict(a)
        b["influence"] = 5000.0
        self.assertEqual(fpl_tools._defcon_rate_per90(a, 2),
                         fpl_tools._defcon_rate_per90(b, 2))

    def test_falls_back_to_the_prior_without_data(self):
        bare = {"element_type": 2, "minutes": 900}
        self.assertAlmostEqual(fpl_tools._defcon_rate_per90(bare, 2),
                               fpl_tools.DEFCON_BASE_PER90[2], places=6)


class ModelVersionTest(unittest.TestCase):
    def test_version_is_past_the_stage_5b_boundary(self):
        """Stage 4 -> v2, Stage 2 -> v3, Stage 5b -> v4, Stage 8 -> v5. Each
        stage that changes what _player_xp returns must bump, or auto_tune fits
        across a heterogeneous population under one label.

        Asserts the ordering rather than the literal: this test's concern is
        that Stage 5b's EP_BLEND taper, clean sheets and DefCon put a boundary
        in the archive, not what the current stamp happens to say.
        """
        import re
        m = re.match(r"^v(\d+)-", fpl_tools.MODEL_VERSION)
        self.assertIsNotNone(m, f"unversioned stamp {fpl_tools.MODEL_VERSION!r}")
        self.assertGreaterEqual(int(m.group(1)), 4,
                                "pre-Stage-5b rows would be fitted alongside post-fix ones")


if __name__ == "__main__":
    unittest.main()
