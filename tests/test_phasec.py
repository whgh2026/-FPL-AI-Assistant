import unittest
from unittest import mock

import fpl_tools


def _entry(pid, pos, xp, team=1, price=5.0, sell=5.0):
    return {"id": pid, "name": "P%d" % pid, "team_id": team, "team": "T%d" % team,
            "position": pos, "price": price, "xp": xp, "status": "Available",
            "on_yellow_card_tightrope": False, "minutes_floor": 1.0, "sell_price": sell}


def _squad_pool():
    pool = [
        _entry(1, "GK", 5.0, 1, 4.5), _entry(2, "GK", 5.0, 2, 4.0),
        _entry(3, "DEF", 5.0, 1), _entry(4, "DEF", 5.0, 2),
        _entry(5, "DEF", 5.0, 3), _entry(6, "DEF", 5.0, 4),
        _entry(7, "DEF", 5.0, 5),
        _entry(8, "MID", 5.0, 3), _entry(9, "MID", 5.0, 4),
        _entry(10, "MID", 5.0, 5), _entry(11, "MID", 5.0, 6),
        _entry(12, "MID", 5.0, 1),
        _entry(13, "FWD", 5.0, 2), _entry(14, "FWD", 5.0, 3),
        _entry(15, "FWD", 5.0, 4),
        _entry(99, "FWD", 5.0, 5),  # incoming candidate
    ]
    return pool


def _eo_map_high_low():
    m = {pid: {"eo": 20.0} for pid in range(1, 16)}
    m[99] = {"eo": 5.0}  # low-EO differential
    return m


class EOTest(unittest.TestCase):

    def _elements(self):
        return [
            {"id": 1, "selected_by_percent": 50.0, "ep_next": 6.0, "minutes": 1000, "points_per_game": 6.0},
            {"id": 2, "selected_by_percent": 30.0, "ep_next": 5.0, "minutes": 1000, "points_per_game": 5.0},
            {"id": 3, "selected_by_percent": 10.0, "ep_next": 4.0, "minutes": 500, "points_per_game": 4.0},
        ]

    def test_captain_distribution_sums_to_100(self):
        dist = fpl_tools._captain_distribution(self._elements())
        self.assertAlmostEqual(sum(dist.values()), 100.0, places=1)
        self.assertEqual(max(dist, key=dist.get), 1)  # highest-ownership premium gets most captaincy

    def test_eo_composition(self):
        fpl_tools._EO_CACHE = None
        with mock.patch.object(fpl_tools, "_get_bootstrap", return_value={"elements": self._elements(), "teams": []}):
            eo_map = fpl_tools._eo_map()
        for pid, d in eo_map.items():
            self.assertAlmostEqual(d["eo"], d["ownership"] + d["captain"] + 2.0 * d["tc"], places=1)
            self.assertGreaterEqual(d["eo"], d["ownership"])

    def test_inverted_exposure_negative(self):
        # Owned + started + uncaptained at 140% EO => every point drops rank.
        self.assertLess(fpl_tools._rank_exposure(1, 140.0, 1.0), 0.0)

    def test_captained_exposure_positive(self):
        # Captaining the 140%-EO asset restores positive exposure.
        self.assertGreater(fpl_tools._rank_exposure(2, 140.0, 1.0), 0.0)

    def test_leveraged_short_negative(self):
        # Unowned at 80%+ EO is a leveraged short.
        self.assertLess(fpl_tools._rank_exposure(0, 80.0, 1.0), 0.0)


class StrategyModeTest(unittest.TestCase):

    def test_mode_mapping(self):
        self.assertEqual(fpl_tools._strategy_mode("conservative"), "blocker")
        self.assertEqual(fpl_tools._strategy_mode("rank protecting"), "blocker")
        self.assertEqual(fpl_tools._strategy_mode("aggressive"), "divergence")
        self.assertEqual(fpl_tools._strategy_mode("rank_chasing"), "divergence")
        self.assertEqual(fpl_tools._strategy_mode("balanced"), "ev")
        self.assertEqual(fpl_tools.PHASE2_START_GW, 26)

    def test_blocker_penalises_low_eo(self):
        pool = _squad_pool()
        sel, _ = fpl_tools._solve_squad(
            pool, budget=100.0, must_include_ids=set(range(1, 16)),
            hit_config={"free_transfers": 1, "hit_cost": 0.0, "max_transfers": 1, "ft_friction": 0.0},
            eo_map=_eo_map_high_low(), mode="blocker", phase=2,
        )
        self.assertNotIn(99, sel)  # low-EO incoming is penalised in blocker mode

    def test_divergence_rewards_low_eo(self):
        pool = _squad_pool()
        sel, _ = fpl_tools._solve_squad(
            pool, budget=100.0, must_include_ids=set(range(1, 16)),
            hit_config={"free_transfers": 1, "hit_cost": 0.0, "max_transfers": 1, "ft_friction": 0.0},
            eo_map=_eo_map_high_low(), mode="divergence", phase=2,
        )
        self.assertIn(99, sel)  # low-EO differential is rewarded in divergence mode

    def test_phase1_no_eo_terms(self):
        # In phase 1 the EO terms are inactive; the solver still solves cleanly.
        pool = _squad_pool()
        sel, _ = fpl_tools._solve_squad(
            pool, budget=100.0, must_include_ids=set(range(1, 16)),
            hit_config={"free_transfers": 1, "hit_cost": 0.0, "max_transfers": 1, "ft_friction": 0.0},
            eo_map=_eo_map_high_low(), mode="blocker", phase=1,
        )
        self.assertEqual(len(sel), 15)


class ChipSchedulingTest(unittest.TestCase):

    def test_reservation_declines(self):
        tc10 = fpl_tools._chip_reservation_threshold("Triple Captain", 10)
        tc17 = fpl_tools._chip_reservation_threshold("Triple Captain", 17)
        tc19 = fpl_tools._chip_reservation_threshold("Triple Captain", 19)
        self.assertGreater(tc10, tc17)
        self.assertGreater(tc17, tc19)

    def test_forced_exercise(self):
        self.assertEqual(fpl_tools._chip_reservation_threshold("Wildcard", 18), 0.0)
        self.assertEqual(fpl_tools._chip_reservation_threshold("Triple Captain", 19), 0.0)

    def test_inventory_expiry(self):
        inv1 = fpl_tools._chip_inventory(19)
        inv2 = fpl_tools._chip_inventory(20)
        self.assertEqual(set(inv1["set1"]), set(fpl_tools.CHIPS))
        self.assertEqual(inv2["set1"], [])
        self.assertEqual(set(inv2["set2"]), set(fpl_tools.CHIPS))


if __name__ == "__main__":
    unittest.main()
