import unittest
from unittest import mock

import fpl_tools


def _element(pid, team, pos_id, ow=20.0, minutes=1000, starts=10):
    return {"id": pid, "element_type": pos_id, "team": team, "now_cost": 50,
            "first_name": "P", "second_name": str(pid), "status": "a",
            "chance_of_playing_next_round": None, "minutes": minutes,
            "starts": starts, "selected_by_percent": ow, "ep_next": 5.0,
            "points_per_game": 5.0}


class ScenarioTest(unittest.TestCase):

    def _scenarios(self, elements, S=200):
        bootstrap = {"elements": elements,
                     "teams": [{"id": t, "name": "T%d" % t, "played": 10} for t in range(1, 7)]}
        player_ids = [e["id"] for e in elements]
        with mock.patch.object(fpl_tools, "_get_bootstrap", return_value=bootstrap), \
             mock.patch.object(fpl_tools, "_player_xp", return_value=(5.0, "Available")), \
             mock.patch.object(fpl_tools, "_minute_distribution", return_value=(0.1, 0.1, 0.8)):
            return fpl_tools._generate_scenarios(player_ids, {}, event=10, risk="balanced", n=4, S=S, seed=7)

    def test_reproducible(self):
        els = [_element(1, 1, 3), _element(2, 1, 4), _element(3, 2, 3)]
        m1, _ = self._scenarios(els)
        m2, _ = self._scenarios(els)
        self.assertEqual(m1[1], m2[1])

    def test_saa_mean_near_base(self):
        els = [_element(1, 1, 3), _element(2, 1, 4), _element(3, 2, 3)]
        saa_mean, _ = self._scenarios(els)
        for pid, row in saa_mean.items():
            for v in row:
                self.assertAlmostEqual(v, 5.0, delta=0.5)

    def test_correlation_same_team(self):
        if not fpl_tools.HAS_NUMPY:
            self.skipTest("numpy required")
        els = [_element(1, 1, 3), _element(2, 1, 4), _element(3, 2, 3)]
        _, matrix = self._scenarios(els)
        import numpy as np
        a = np.asarray(matrix[1][0])
        b = np.asarray(matrix[2][0])
        c = np.asarray(matrix[3][0])
        same = float(np.corrcoef(a, b)[0, 1])
        diff = float(np.corrcoef(a, c)[0, 1])
        self.assertGreater(same, diff)

    def test_distribution_ordering(self):
        els = [_element(1, 1, 3), _element(2, 1, 4)]
        _, matrix = self._scenarios(els)
        dist = fpl_tools._scenario_distribution([1, 2], matrix, weights=[1.0, 0.85, 0.7, 0.55], n=4)
        self.assertLessEqual(dist["p5"], dist["p50"])
        self.assertLessEqual(dist["p50"], dist["p95"])


class PlannerTest(unittest.TestCase):

    def _pool(self):
        pool = []
        pid = 0

        def entry(pos, team, price=5.0):
            nonlocal pid
            pid += 1
            return {"id": pid, "name": "P%d" % pid, "team_id": team, "team": "T%d" % team,
                    "position": pos, "price": price, "xp": 5.0, "status": "Available",
                    "on_yellow_card_tightrope": False, "minutes_floor": 1.0, "sell_price": price}

        pool.append(entry("GK", 1, 4.5))
        pool.append(entry("GK", 2, 4.0))
        for t in range(1, 6):
            pool.append(entry("DEF", t, 4.5))
        for t in [3, 4, 5, 6, 1]:
            pool.append(entry("MID", t, 6.0))
        for t in [2, 3, 4]:
            pool.append(entry("FWD", t, 6.5))
        for t in [5, 6, 5, 6]:
            pool.append(entry("FWD", t, 7.0))
        return pool

    def test_planner_runs(self):
        pool = self._pool()
        current_ids = {p["id"] for p in pool[:15]}
        saa_mean = {p["id"]: [p["xp"]] * 4 for p in pool}
        schedule = fpl_tools._plan_transfers_multi_gw(pool, 100.0, 1, current_ids, saa_mean, event=10, n=4)
        self.assertIsInstance(schedule, list)
        self.assertEqual(len(schedule), 4)

    def test_planner_ft_bounded(self):
        pool = self._pool()
        current_ids = {p["id"] for p in pool[:15]}
        saa_mean = {p["id"]: [p["xp"]] * 4 for p in pool}
        schedule = fpl_tools._plan_transfers_multi_gw(pool, 100.0, 2, current_ids, saa_mean, event=10, n=4)
        for step in schedule:
            self.assertGreaterEqual(step["ft_after"], 0)
            self.assertLessEqual(step["ft_after"], 5)
            self.assertGreaterEqual(step["hits"], 0)


if __name__ == "__main__":
    unittest.main()
