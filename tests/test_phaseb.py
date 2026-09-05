import math
import random
import unittest
from unittest import mock

import fpl_tools


def _pois(lam):
    l = math.exp(-lam)
    k = 0
    while True:
        k += 1
        if random.random() < l:
            return k - 1


class MinuteDistributionTest(unittest.TestCase):

    def test_nailed_starter_is_full_minutes(self):
        p = {"element_type": 3, "team": 1, "starts": 10, "minutes": 900,
             "chance_of_playing_next_round": None}
        with mock.patch.object(fpl_tools, "_team_played_map", return_value={1: 10}):
            p0, p_cameo, p_full = fpl_tools._minute_distribution(p, "a")
        self.assertGreater(p_full, 0.9)
        self.assertLess(p_cameo, 0.1)

    def test_rotation_player_has_absence_mass(self):
        p = {"element_type": 3, "team": 1, "starts": 5, "minutes": 450,
             "chance_of_playing_next_round": None}
        with mock.patch.object(fpl_tools, "_team_played_map", return_value={1: 10}):
            p0, p_cameo, p_full = fpl_tools._minute_distribution(p, "a")
        self.assertGreater(p0, 0.3)
        self.assertLess(p_full, 0.7)

    def test_injured_is_full_absence(self):
        p = {"element_type": 3, "team": 1, "starts": 5, "minutes": 450}
        self.assertEqual(fpl_tools._minute_distribution(p, "i"), (1.0, 0.0, 0.0))

    def test_zero_minutes_inferred_as_starter_when_starts_missing(self):
        # Robustness: a 1000-minute player with no `starts` field must not be
        # misread as a 100% cameo sub (regression guard for the test mock).
        p = {"element_type": 3, "team": 1, "minutes": 1000}
        with mock.patch.object(fpl_tools, "_team_played_map", return_value={1: 0}):
            p0, p_cameo, p_full = fpl_tools._minute_distribution(p, "a")
        self.assertGreater(p_full, 0.9)


class DixonColesTest(unittest.TestCase):

    def _fit(self):
        random.seed(7)
        teams = [1, 2, 3, 4]
        att = {1: 0.5, 2: 0.3, 3: -0.3, 4: -0.5}
        dfn = {1: 0.3, 2: -0.4, 3: 0.3, 4: -0.4}
        gamma = 0.3
        fixtures = []
        for _ in range(60):
            for h in teams:
                for a in teams:
                    if h == a:
                        continue
                    lh = math.exp(att[h] - dfn[a] + gamma)
                    la = math.exp(att[a] - dfn[h])
                    fixtures.append({
                        "team_h": h, "team_a": a,
                        "team_h_score": min(9, _pois(lh)),
                        "team_a_score": min(9, _pois(la)),
                        "kickoff_time": "2026-08-15T15:00:00Z",
                    })
        return fpl_tools._fit_dixon_coles(fixtures, decay=0.0, tau=0.1, iterations=500, lr=0.1)

    def test_recovers_attack_ordering(self):
        ratings, _gamma, _rho = self._fit()
        self.assertGreater(ratings[1]["att"], ratings[4]["att"])

    def test_recovers_defence_ordering(self):
        ratings, _gamma, _rho = self._fit()
        # Strong defence (team 2) has a lower `def` than weak defence (team 3).
        self.assertLess(ratings[2]["def"], ratings[3]["def"])

    def test_home_advantage_positive(self):
        _ratings, gamma, _rho = self._fit()
        self.assertGreater(gamma, 0.0)


class StructuralHealthTest(unittest.TestCase):

    def _squad(self, bench_prices=None):
        pl = lambda pid, pos, price, xp, status="Available": {
            "player_id": pid, "position": pos, "price": price, "xp": xp,
            "status": status, "name": "P%d" % pid,
        }
        squad = [pl(1, "GK", 4.5, 4.0), pl(2, "GK", 4.0, 3.0)]
        for i in range(5):
            squad.append(pl(3 + i, "DEF", 5.0, 4.0))
        for i in range(5):
            squad.append(pl(8 + i, "MID", 6.5, 5.0))
        squad.append(pl(13, "FWD", 10.0, 7.0))
        squad.append(pl(14, "FWD", 6.0, 4.5))
        squad.append(pl(15, "FWD", 5.5, 4.0))
        return squad

    def test_returns_four_checks(self):
        checks = fpl_tools._squad_structural_health(self._squad(), 0.5)
        self.assertEqual(len(checks), 4)

    def test_flags_stranded_bench_capital(self):
        checks = fpl_tools._squad_structural_health(self._squad(), 0.5)
        stranded = next(c for c in checks if c["label"] == "Stranded bench capital")
        # This squad's bench exceeds £15m (expensive MID/FWD bench).
        self.assertFalse(stranded["ok"])

    def test_formation_optionality_flexible(self):
        checks = fpl_tools._squad_structural_health(self._squad(), 2.0)
        opt = next(c for c in checks if c["label"] == "Formation optionality")
        self.assertTrue(opt["ok"])


class BenchWeightsTest(unittest.TestCase):

    def test_convex_ordering(self):
        self.assertGreater(fpl_tools.BENCH_B1_WEIGHT, fpl_tools.BENCH_DEAD_WEIGHT)
        self.assertGreater(fpl_tools.BENCH_DEAD_WEIGHT, 0.0)
        self.assertAlmostEqual(fpl_tools.BENCH_GK_WEIGHT, 0.05)


if __name__ == "__main__":
    unittest.main()
