"""Stage 8 gate: the self-correction loop can actually correct something.

Five defects, all of which made the loop look like it was working while doing
nothing (or the wrong thing):

  C14  damping so small a weight moved 0.5% per run, against 38 runs a season
  C15  dc_sensitivity written as a literal 0.0, so one of the four tuned
       parameters had an identically zero gradient and could never move
  C16  a surrogate that did not match production, over-attributing a unit
       change in a penalty by up to 2x
  C17  no floor on the deviance term, so one bad row outweighed hundreds
  C20  a weights cache with no TTL and no mtime check, so a re-tune never
       reached a running app

The through-line: every one of them is silent. Nothing errors, the job prints a
cheerful line, and the weights do not move -- or move for the wrong reason.
"""

import importlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fpl_tools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def W(**over):
    w = {"global_xP_modifier": 1.0, "autosub_ref": 1.8,
         "rotation_convexity": 0.4, "dixon_coles_decay": 0.03}
    w.update(over)
    return w


class SurrogateMatchesProductionTest(unittest.TestCase):
    """C16. If these diverge, coordinate descent optimises a model the app
    does not run."""

    def test_cameo_penalty_is_clamped_like_production(self):
        """Production is `p_cameo * max(0, autosub_ref - xp_cameo)`. Once the
        cameo projection clears autosub_ref the penalty is zero -- and so is its
        gradient. The old surrogate charged `autosub_ref * cameo_mass`
        regardless, inventing a gradient where production has none."""
        row = {"base_pts": 5.0, "cameo_mass": 0.5, "rotation_variance": 0.0,
               "dc_sensitivity": 0.0, "xp_cameo": 3.0, "ep_w": 0.0, "ep_term": 0.0}
        # autosub_ref 1.8 < xp_cameo 3.0 -> clamped to zero.
        self.assertAlmostEqual(fpl_tools.reproject(row, W()), 5.0, places=6)
        # Still clamped when autosub_ref rises to 2.5.
        self.assertAlmostEqual(fpl_tools.reproject(row, W(autosub_ref=2.5)), 5.0, places=6)
        # Only once it exceeds xp_cameo does the penalty engage.
        self.assertAlmostEqual(
            fpl_tools.reproject(row, W(autosub_ref=4.0)), 5.0 - 0.5 * 1.0, places=6)

    def test_penalty_is_scaled_by_one_minus_the_blend_weight(self):
        """Penalties are applied BEFORE the ep_next blend, so a unit change
        moves the final projection by (1 - ep_w), not by 1.0."""
        row = {"raw_total": 5.0, "cameo_mass": 1.0, "rotation_variance": 0.0,
               "dc_sensitivity": 0.0, "xp_cameo": 0.0, "ep_w": 0.5, "ep_term": 1.0}
        lo = fpl_tools.reproject(row, W(autosub_ref=1.0))
        hi = fpl_tools.reproject(row, W(autosub_ref=2.0))
        # 1.0 more penalty, halved by the blend.
        self.assertAlmostEqual(lo - hi, 0.5, places=6)

    def test_reproject_matches_player_xp_raw_on_a_real_player(self):
        """The one that matters: run the real projection, then rebuild it from
        the stored features and require the same number."""
        from tests import harness
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            lookup = fpl_tools._build_fixture_lookup(bootstrap)
            gw = fpl_tools._next_gameweek(bootstrap)
            weights = fpl_tools._load_weights()

            checked = 0
            gmod = weights["global_xP_modifier"]
            for e in bootstrap["elements"][:120]:
                # Against the UNROUNDED projection: _player_xp rounds to 2dp for
                # display, and comparing to that would only prove the surrogate
                # agrees to within a penny of a point.
                raw, _ = fpl_tools._player_xp_raw(e, lookup, event=gw)
                live = max(raw, 0.0) * gmod
                if live <= 0:
                    continue
                row = dict(fpl_tools.calibration_features(e, lookup, event=gw),
                           dc_sensitivity=0.0)
                rebuilt = fpl_tools.reproject(row, weights)
                self.assertAlmostEqual(
                    rebuilt, live, places=6,
                    msg=f"surrogate {rebuilt:.6f} != production {live:.6f} "
                        f"for element {e['id']}")
                checked += 1
            self.assertGreater(checked, 20, "too few live players to be a real check")

    def test_legacy_rows_without_the_new_columns_still_reproject(self):
        """Rows written before xp_cameo/ep_w existed must not be dropped."""
        row = {"base_pts": 5.0, "cameo_mass": 0.5, "rotation_variance": 0.2,
               "dc_sensitivity": 0.0}
        got = fpl_tools.reproject(row, W())
        self.assertAlmostEqual(got, 5.0 - 1.8 * 0.5 - 0.4 * 0.2, places=6)


class DevianceFloorTest(unittest.TestCase):
    """C17."""

    def test_one_bad_row_no_longer_dominates_the_metric(self):
        """At max(pred, 1e-6) a single zero prediction against a 12-point haul
        contributed 2*(0 + 12*13.8) = +331 to a per-row metric of order 1."""
        good = [{"base_pts": 4.0, "cameo_mass": 0.0, "rotation_variance": 0.0,
                 "dc_sensitivity": 0.0, "actual_points": 4.0} for _ in range(99)]
        bad = [{"base_pts": -5.0, "cameo_mass": 0.0, "rotation_variance": 0.0,
                "dc_sensitivity": 0.0, "actual_points": 12.0}]
        m = fpl_tools.evaluate_calibration(good + bad, W())
        per_row_old = 2.0 * (0.0 - 12.0 * math.log(1e-6)) / 100.0
        self.assertLess(m["deviance"], per_row_old / 10.0,
                        f"deviance {m['deviance']:.2f} still dominated by one row")

    def test_a_negative_projection_cannot_produce_a_negative_prediction(self):
        row = {"base_pts": -5.0, "cameo_mass": 0.0, "rotation_variance": 0.0,
               "dc_sensitivity": 0.0}
        self.assertGreaterEqual(fpl_tools.reproject(row, W()), 0.0)

    def test_deviance_is_finite_for_every_plausible_input(self):
        for base in (-10.0, -0.1, 0.0, 0.05, 1.0, 20.0):
            for actual in (0.0, 1.0, 25.0):
                rows = [{"base_pts": base, "cameo_mass": 0.0,
                         "rotation_variance": 0.0, "dc_sensitivity": 0.0,
                         "actual_points": actual}]
                d = fpl_tools.evaluate_calibration(rows, W())["deviance"]
                self.assertTrue(math.isfinite(d), f"base={base} actual={actual} -> {d}")


class DecayGradientTest(unittest.TestCase):
    """C15. dixon_coles_decay is listed as tuned; it could not move."""

    def test_a_zero_sensitivity_leaves_the_decay_gradient_flat(self):
        """The defect, stated directly: with dc_sensitivity = 0.0 the metric is
        identical at every decay, so descent has nothing to descend."""
        rows = [{"base_pts": 4.0, "cameo_mass": 0.0, "rotation_variance": 0.0,
                 "dc_sensitivity": 0.0, "actual_points": 5.0} for _ in range(20)]
        a = fpl_tools.evaluate_calibration(rows, W(dixon_coles_decay=0.01))
        b = fpl_tools.evaluate_calibration(rows, W(dixon_coles_decay=0.14))
        self.assertEqual(a["rmse"], b["rmse"])

    def test_a_measured_sensitivity_makes_the_decay_move(self):
        rows = [{"base_pts": 4.0, "cameo_mass": 0.0, "rotation_variance": 0.0,
                 "dc_sensitivity": 8.0, "actual_points": 4.4} for _ in range(20)]
        new = fpl_tools.calibrate_weights(rows, W())
        self.assertNotAlmostEqual(new["dixon_coles_decay"], 0.03, places=6)

    def test_snapshot_measures_a_real_nonzero_derivative(self):
        """Behavioural, not a source-string match: perturb the decay, refit, and
        require the projections to actually move for most players. A literal
        0.0 -- the bug -- fails this; so does a probe that forgets to clear the
        rating cache, which silently returns the base fit."""
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import snapshot_xp
        from tests import harness

        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            lookup = fpl_tools._build_fixture_lookup(bootstrap)
            gw = fpl_tools._next_gameweek(bootstrap)

            neutral = dict(fpl_tools._load_weights())
            neutral.update({"global_xP_modifier": 1.0, "autosub_ref": 0.0,
                            "rotation_convexity": 0.0,
                            "dixon_coles_decay": fpl_tools.DIXON_COLES_DECAY_DEFAULT})
            probe_w = dict(neutral)
            probe_w["dixon_coles_decay"] = (fpl_tools.DIXON_COLES_DECAY_DEFAULT
                                            + snapshot_xp.DC_PROBE_H)
            perturbed = snapshot_xp._lookup_at_weights(probe_w, bootstrap)

            orig = fpl_tools._WEIGHTS_CACHE
            live, nonzero = 0, 0
            for e in bootstrap["elements"][:200]:
                fpl_tools._WEIGHTS_CACHE = neutral
                try:
                    a = fpl_tools.calibration_features(e, lookup, event=gw)
                    b = fpl_tools.calibration_features(e, perturbed, event=gw)
                finally:
                    fpl_tools._WEIGHTS_CACHE = orig
                if a["raw_total"] <= 0:
                    continue                      # blank, injured, no minutes
                live += 1
                if abs(b["raw_total"] - a["raw_total"]) / snapshot_xp.DC_PROBE_H > 1e-9:
                    nonzero += 1

            self.assertGreater(live, 50, "too few projectable players to judge")
            self.assertGreater(
                nonzero, 0.9 * live,
                f"dixon_coles_decay moved the projection for only {nonzero}/{live} "
                "players -- its gradient is still effectively zero")

    def test_team_ratings_are_not_quantised(self):
        """How the gradient was being eaten.

        The band ratings were round(_, 2). Attacker xP runs through
        def_adj = 3.0 / opp_def, so quantising opp_def at 0.01 quantised every
        attacking projection -- and forwards, who carry no clean-sheet or
        conceded term, had NO path left through which opponent strength could
        reach them at sub-0.01 resolution. Perturbing the decay moved the raw
        fit for all 20 clubs and the rounded band for 2.
        """
        from tests import harness
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            fpl_tools._build_fixture_lookup(bootstrap)
            ratings = fpl_tools._team_attack_def_ratings()
            self.assertGreaterEqual(len(ratings), 10)
            vals = [v for r in ratings.values() for v in r.values()]
            unrounded = [v for v in vals if abs(v - round(v, 2)) > 1e-9]
            self.assertGreater(
                len(unrounded), 0.5 * len(vals),
                "team ratings still look rounded to 2dp -- the attack path is "
                "quantised and forwards lose all sub-0.01 fixture resolution")

    def test_clearing_the_rating_cache_is_what_makes_the_probe_real(self):
        """The fit is cached for 300s, so a caller that swaps the decay without
        clearing gets the OLD ratings and measures exactly 0.0 -- the same
        symptom as the bug being fixed."""
        from tests import harness
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            fpl_tools._build_fixture_lookup(bootstrap)
            self.assertIsNotNone(fpl_tools._TEAM_RATINGS_CACHE)
            fpl_tools._clear_rating_caches()
            self.assertIsNone(fpl_tools._TEAM_RATINGS_CACHE)
            self.assertEqual(fpl_tools._DC_RAW, {})


class WeightsCacheTest(unittest.TestCase):
    """C20. The weekly commit never reached the running app."""

    def setUp(self):
        self.path = fpl_tools._weights_path()
        with open(self.path, encoding="utf-8") as fh:
            self.original = fh.read()
        fpl_tools._WEIGHTS_CACHE = None
        fpl_tools._WEIGHTS_STAMP = (0.0, 0.0)

    def tearDown(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(self.original)
        fpl_tools._WEIGHTS_CACHE = None
        fpl_tools._WEIGHTS_STAMP = (0.0, 0.0)

    def test_a_rewritten_weights_file_is_picked_up(self):
        first = fpl_tools._load_weights()["global_xP_modifier"]
        data = json.loads(self.original)
        data["global_xP_modifier"] = first + 0.17
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        # Move the mtime forward: same-second writes can compare equal.
        future = time.time() + 10
        os.utime(self.path, (future, future))
        self.assertAlmostEqual(
            fpl_tools._load_weights()["global_xP_modifier"], first + 0.17, places=6,
            msg="the app is still serving the weights it read at boot")

    def test_an_untouched_file_is_served_from_cache(self):
        """The check has to be an mtime comparison, not a re-read: this runs on
        every projection for every player."""
        fpl_tools._load_weights()
        cached = fpl_tools._WEIGHTS_CACHE
        self.assertIs(fpl_tools._load_weights(), cached)

    def test_the_cache_has_a_bounded_lifetime(self):
        self.assertLessEqual(fpl_tools.WEIGHTS_TTL_SECONDS, 900)
        fpl_tools._load_weights()
        mtime, _ = fpl_tools._WEIGHTS_STAMP
        fpl_tools._WEIGHTS_STAMP = (mtime, time.time() - fpl_tools.WEIGHTS_TTL_SECONDS - 1)
        before = fpl_tools._WEIGHTS_CACHE
        self.assertIsNot(fpl_tools._load_weights(), before,
                         "an expired cache was reused")


class AutoTuneGuardTest(unittest.TestCase):
    """The job writes weights.json into the repo, so what it refuses to do
    matters as much as what it does."""

    def _mod(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import auto_tune
        return importlib.reload(auto_tune)

    def test_split_is_deterministic_and_disjoint(self):
        at = self._mod()
        rows = [{"i": i} for i in range(100)]
        train, holdout = at._split(rows)
        self.assertEqual((train, holdout), at._split(rows), "split is not reproducible")
        self.assertEqual(len(train) + len(holdout), len(rows))
        ids = {id(r) for r in train}
        self.assertFalse(ids & {id(r) for r in holdout}, "train and holdout overlap")

    def test_holdout_is_a_meaningful_fraction(self):
        at = self._mod()
        _train, holdout = at._split([{"i": i} for i in range(1000)])
        self.assertGreater(len(holdout), 100)

    def test_damping_tracks_the_engine_constant(self):
        at = self._mod()
        self.assertEqual(at.DAMPING, fpl_tools.CALIBRATION_DAMPING)

    def test_the_job_refuses_to_write_when_the_holdout_worsens(self):
        src = open(os.path.join(ROOT, "scripts", "auto_tune.py"), encoding="utf-8").read()
        reject = src.index("REJECTED")
        write = src.index('open(WEIGHTS_PATH, "w"')
        self.assertLess(reject, write,
                        "weights.json is written before the holdout check")
        self.assertIn("return", src[reject:write])


class ScriptsStillCompileTest(unittest.TestCase):
    def test_every_script_parses(self):
        d = os.path.join(ROOT, "scripts")
        for name in sorted(os.listdir(d)):
            if name.endswith(".py"):
                r = subprocess.run([sys.executable, "-m", "py_compile",
                                    os.path.join(d, name)],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
