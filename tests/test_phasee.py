import math
import unittest

import fpl_tools


def _weights():
    return {"global_xP_modifier": 1.0, "autosub_ref": 1.8,
            "rotation_convexity": 0.4, "dixon_coles_decay": 0.03}


class CalibrationTest(unittest.TestCase):

    def test_rmse_zero(self):
        rows = [{"base_pts": 4.0, "cameo_mass": 0.0, "rotation_variance": 0.0,
                 "dc_sensitivity": 0.0, "actual_points": 4.0}]
        r = fpl_tools.evaluate_calibration(rows, _weights())
        self.assertEqual(r["rmse"], 0.0)
        self.assertEqual(r["n"], 1)

    def test_poisson_deviance(self):
        rows = [{"base_pts": 5.0, "cameo_mass": 0.0, "rotation_variance": 0.0,
                 "dc_sensitivity": 0.0, "actual_points": 3.0}]
        r = fpl_tools.evaluate_calibration(rows, _weights())
        expected = 2.0 * (5.0 - 3.0 * math.log(5.0))
        self.assertAlmostEqual(r["deviance"], expected, places=3)

    def test_reprojection(self):
        rows = [{"base_pts": 6.0, "cameo_mass": 0.2, "rotation_variance": 0.3,
                 "dc_sensitivity": 1.0, "actual_points": 5.0}]
        w = {"global_xP_modifier": 1.2, "autosub_ref": 2.0,
             "rotation_convexity": 0.5, "dixon_coles_decay": 0.04}
        pred = 1.2 * (6.0 - 2.0 * 0.2 - 0.5 * 0.3 + (0.04 - 0.03) * 1.0)
        r = fpl_tools.evaluate_calibration(rows, w)
        self.assertAlmostEqual(r["rmse"], abs(pred - 5.0), places=3)

    def test_calibrate_converges(self):
        """At the PRODUCTION damping, not damping=1.0.

        This test used to pass damping=1.0 -- twenty times the shipped 0.05 --
        so the setting that actually ran was never exercised, and the fact that
        it moved a weight by only 0.5% per run went unnoticed for a season.
        """
        base = 4.0
        rows = [{"base_pts": base, "cameo_mass": 0.0, "rotation_variance": 0.0,
                 "dc_sensitivity": 0.0, "actual_points": 1.4 * base} for _ in range(50)]
        new = fpl_tools.calibrate_weights(rows, _weights())
        self.assertGreater(new["global_xP_modifier"], 1.0)

    def test_default_damping_reaches_a_target_within_a_season(self):
        """The C14 defect, as a number. The probe is +/-10%, so one run moves a
        weight by damping*10%. At 0.05 that is 0.5% per run and 1.005^n = 1.30
        needs n = 53 weekly runs against a 38-gameweek season -- weights.json
        could not meaningfully move within a season whatever the data said."""
        per_run = fpl_tools.CALIBRATION_DAMPING * 0.10
        runs = math.log(1.30) / math.log(1.0 + per_run)
        self.assertLess(runs, 20,
                        f"{runs:.0f} runs to move a weight 30%, against a 38-week season")


class CvarTest(unittest.TestCase):

    def test_rockafellar_uryasev_formula(self):
        # Lower alpha-tail mean of [1,2,3,4,5] with alpha=0.4 = mean of worst 2 = 1.5.
        pts = [1.0, 2.0, 3.0, 4.0, 5.0]
        alpha = 0.4
        K = len(pts)
        best = -1e9
        for i in range(1001):
            zeta = i / 200.0
            shortfall = sum(max(0.0, zeta - p) for p in pts)
            best = max(best, zeta - (1.0 / (alpha * K)) * shortfall)
        self.assertAlmostEqual(best, 1.5, places=2)

    def test_stress_pooling(self):
        if not fpl_tools.HAS_NUMPY:
            self.skipTest("numpy required")
        import numpy as np
        # Shape is (S, n): three SCENARIOS down the rows, one gameweek across.
        # This test previously wrote [np.array([10.0, 5.0, 8.0])] -- shape (1, 3)
        # -- i.e. one scenario over three gameweeks, encoding the transposed
        # layout the consumers were buggily assuming. The intent (three
        # scenarios, pick the worst two) is unchanged; only the orientation is
        # corrected, so the expected values are identical.
        matrix = {
            1: np.array([[10.0], [5.0], [8.0]]),
            2: np.array([[3.0], [2.0], [4.0]]),
        }
        out = fpl_tools._select_stress_scenarios(matrix, K=2)
        # Aggregate totals per scenario: [13, 7, 12] -> lowest two are
        # scenario 1 (7.0) then scenario 2 (12.0).
        self.assertEqual(out[1], [5.0, 8.0])

    def test_blocker_solves_with_cvar(self):
        pool = []
        pid = 0

        def entry(pos, team, price=5.0):
            nonlocal pid
            pid += 1
            return {"id": pid, "name": "P%d" % pid, "team_id": team, "team": "T%d" % team,
                    "position": pos, "price": price, "xp": 5.0, "status": "Available",
                    "on_yellow_card_tightrope": False, "minutes_floor": 1.0, "sell_price": price}

        pool.append(entry("GK", 1, 4.5)); pool.append(entry("GK", 2, 4.0))
        for t in range(1, 6):
            pool.append(entry("DEF", t, 4.5))
        for t in [3, 4, 5, 6, 1]:
            pool.append(entry("MID", t, 6.0))
        for t in [2, 3, 4]:
            pool.append(entry("FWD", t, 6.5))

        current_ids = set(range(1, 16))
        eo_map = {pid: {"eo": 20.0} for pid in current_ids}
        cvar = {pid: [5.0] * 10 for pid in current_ids}  # 10 stress scenarios
        sel, _, _parts = fpl_tools._solve_squad(
            pool, budget=100.0, must_include_ids=current_ids,
            hit_config={"free_transfers": 1, "hit_cost": 0.0, "max_transfers": 1, "ft_friction": 0.0},
            eo_map=eo_map, mode="blocker", phase=2, cvar_scenarios=cvar,
        )
        self.assertIsNotNone(sel)
        self.assertEqual(len(sel), 15)


class H2HTest(unittest.TestCase):

    def test_compute_h2h(self):
        live = {
            1: {"total_points": 10, "minutes": 90, "played": True, "bps": 30, "bonus": 3},
            2: {"total_points": 5, "minutes": 60, "played": True, "bps": 20, "bonus": 0},
            3: {"total_points": 8, "minutes": 90, "played": True, "bps": 40, "bonus": 2},
            4: {"total_points": 2, "minutes": 0, "played": False, "bps": 0, "bonus": 0},
        }
        my = [{"player_id": 1, "name": "A", "multiplier": 2, "is_captain": True},
              {"player_id": 2, "name": "B", "multiplier": 1, "is_captain": False}]
        rival = [{"player_id": 3, "name": "C", "multiplier": 2, "is_captain": True},
                 {"player_id": 4, "name": "D", "multiplier": 1, "is_captain": False}]
        h = fpl_tools.compute_h2h(my, rival, live)
        self.assertEqual(h["my_total"], 25.0)
        self.assertEqual(h["rival_total"], 18.0)
        self.assertEqual(h["margin"], 7.0)
        # Player B is still playing (60 minutes) -> in_progress.
        self.assertTrue(h["my_rows"][1]["in_progress"])


if __name__ == "__main__":
    unittest.main()
