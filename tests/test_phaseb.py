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
        ratings, _gamma, _rho, _mu = self._fit()
        self.assertGreater(ratings[1]["att"], ratings[4]["att"])

    def test_recovers_defence_ordering(self):
        ratings, _gamma, _rho, _mu = self._fit()
        # A HIGHER `def` is a BETTER defence: the generator above uses
        # lh = exp(att[h] - dfn[a]), so a large dfn[a] suppresses the goals
        # scored against team a. Team 3 (dfn = +0.3) is therefore the strong
        # defence and team 2 (dfn = -0.4) the weak one.
        #
        # The previous comment here had those roles the wrong way round. The
        # assertion was still correct, so the test passed -- but that same
        # misreading is what produced `dc_def = 3.0 - def*scale` in
        # _team_attack_def_ratings, which boosted attackers against elite
        # defences for the whole season. Naming the roles explicitly so the
        # convention cannot be misread again.
        self.assertLess(ratings[2]["def"], ratings[3]["def"])

    def test_home_advantage_positive(self):
        _ratings, gamma, _rho, _mu = self._fit()
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
        stranded = next(c for c in checks if c["key"] == "bench_capital")
        # This squad's bench exceeds £15m (expensive MID/FWD bench).
        self.assertFalse(stranded["ok"])

    def test_formation_optionality_flexible(self):
        checks = fpl_tools._squad_structural_health(self._squad(), 2.0)
        opt = next(c for c in checks if c["key"] == "formation_optionality")
        self.assertTrue(opt["ok"])


class CaptaincyOverrideTest(unittest.TestCase):
    """_select_captaincy: the armband defaults to the best MID/FWD, with a
    margin-gated exception when a GK/DEF's own projection clears it by
    enough that the extra clean-sheet variance no longer hides a genuinely
    bigger return. See CAPTAINCY_OVERRIDE_MARGIN's own comment for the full
    reasoning; these tests are its behavioural half.
    """

    _ATTACKER_XP = 5.0

    def _squad(self, gk_xp, def_xps, mid_xps, fwd_xps):
        """A supply-exact 1-3-4-3 squad (11 players -> the only formation
        VALID_FORMATIONS can build from this supply), so the resulting XI is
        unambiguous regardless of the xP values under test, with no bench."""
        rows = ([("GK", gk_xp)] + [("DEF", x) for x in def_xps]
                + [("MID", x) for x in mid_xps] + [("FWD", x) for x in fwd_xps])
        return [{"player_id": i + 1, "position": pos, "xp": xp, "name": "P%d" % (i + 1)}
                for i, (pos, xp) in enumerate(rows)]

    def test_normal_case_is_byte_for_byte_unchanged(self):
        """The highest-xP player in the XI is already a MID/FWD -- the
        override must never even be consulted, and (C)/(VC) must match
        exactly what the pre-existing plain max()/runner-up formula gave."""
        squad = self._squad(3.0, [4.0, 3.3, 1.8], [7.0, 4.6, 2.9, 2.2], [5.0, 3.6, 1.5])
        result = fpl_tools.select_starting_xi(squad)
        self.assertEqual(result["captain"]["player_id"], 5)       # MID, xp 7.0 - outright best
        self.assertEqual(result["vice_captain"]["player_id"], 9)  # FWD, xp 5.0 - plain runner-up

    def test_override_does_not_fire_below_the_margin(self):
        """The DEF is the single highest-xP player in the XI, but only
        (margin - 0.5) clear of the best attacker -- below
        CAPTAINCY_OVERRIDE_MARGIN, so the armband must still default to the
        attacker, and the vice must still be the plain runner-up (the DEF)."""
        def_xp = self._ATTACKER_XP + fpl_tools.CAPTAINCY_OVERRIDE_MARGIN - 0.5
        squad = self._squad(3.0, [def_xp, 3.3, 1.8],
                             [self._ATTACKER_XP, 4.0, 2.9, 2.2], [4.5, 3.6, 1.5])
        result = fpl_tools.select_starting_xi(squad)
        self.assertEqual(result["captain"]["player_id"], 5)   # MID, best attacker
        self.assertEqual(result["vice_captain"]["player_id"], 2)  # plain runner-up (the DEF)

    def test_override_fires_at_exactly_the_margin(self):
        """A gap of exactly CAPTAINCY_OVERRIDE_MARGIN must fire (>=, not >)."""
        def_xp = self._ATTACKER_XP + fpl_tools.CAPTAINCY_OVERRIDE_MARGIN
        squad = self._squad(3.0, [def_xp, 3.3, 1.8],
                             [self._ATTACKER_XP, 4.0, 2.9, 2.2], [4.5, 3.6, 1.5])
        result = fpl_tools.select_starting_xi(squad)
        self.assertEqual(result["captain"]["player_id"], 2)

    def test_override_fires_comfortably_above_the_margin(self):
        def_xp = self._ATTACKER_XP + fpl_tools.CAPTAINCY_OVERRIDE_MARGIN + 3.0
        squad = self._squad(3.0, [def_xp, 3.3, 1.8],
                             [self._ATTACKER_XP, 4.0, 2.9, 2.2], [4.5, 3.6, 1.5])
        result = fpl_tools.select_starting_xi(squad)
        self.assertEqual(result["captain"]["player_id"], 2)

    def test_vice_anchors_to_the_best_attacker_when_the_override_fires(self):
        """Regression guard for the reported failure mode: captain and vice
        both being high-variance defensive assets from the same back line
        leaves the whole armband exposed to one goal conceded. A SECOND
        defender out-projects the best attacker and would win the naive
        runner-up race -- the vice must anchor to the attacker instead."""
        def_xp_captain = self._ATTACKER_XP + fpl_tools.CAPTAINCY_OVERRIDE_MARGIN + 3.0
        def_xp_trap = self._ATTACKER_XP + 1.0   # 2nd-highest overall, but still a DEF
        squad = self._squad(3.0, [def_xp_captain, def_xp_trap, 1.8],
                             [self._ATTACKER_XP, 4.0, 2.9, 2.2], [4.5, 3.6, 1.5])
        result = fpl_tools.select_starting_xi(squad)
        self.assertEqual(result["captain"]["player_id"], 2)
        self.assertEqual(result["vice_captain"]["player_id"], 5)
        self.assertNotEqual(result["vice_captain"]["player_id"], 3,
                             "vice must not be the naive runner-up (another DEF)")


class SelectStartingXIBenchOrderTest(unittest.TestCase):
    """select_starting_xi's bench order (GK-first, then outfield descending
    by xP) backs _squad_structural_health's bench-cost and dead-weight
    checks and is rendered directly as Bench Slots 1-4 in the Final Lineup,
    but nothing asserted on it directly until now -- StructuralHealthTest
    only ever checks the aggregate bench COST, never slot order or identity.
    """

    def _squad(self):
        pl = lambda pid, pos, xp: {"player_id": pid, "position": pos, "xp": xp,
                                    "name": "P%d" % pid}
        return [
            pl(1, "GK", 5.0), pl(2, "GK", 2.0),
            pl(3, "DEF", 6.5), pl(4, "DEF", 6.0), pl(5, "DEF", 5.5),
            pl(6, "DEF", 3.0), pl(7, "DEF", 1.5),
            pl(8, "MID", 7.0), pl(9, "MID", 6.8), pl(10, "MID", 6.2),
            pl(11, "MID", 4.0), pl(12, "MID", 2.5),
            pl(13, "FWD", 8.0), pl(14, "FWD", 5.0), pl(15, "FWD", 1.0),
        ]

    def test_reserve_gk_leads_the_bench_even_when_an_outfielder_projects_higher(self):
        """MID pid=12 (xp 2.5) out-projects GK pid=2 (xp 2.0) but must still
        sit in Slot 2, not Slot 1 -- Slot 1 is reserved for the only player
        who can legally replace the starting keeper."""
        result = fpl_tools.select_starting_xi(self._squad())
        self.assertEqual(result["formation"], (4, 4, 2))
        bench_ids = [p["player_id"] for p in result["bench"]]
        self.assertEqual(bench_ids, [2, 12, 7, 15])
        self.assertEqual(result["bench"][0]["position"], "GK")

    def test_outfield_bench_is_strictly_descending_by_xp(self):
        result = fpl_tools.select_starting_xi(self._squad())
        outfield_xp = [p["xp"] for p in result["bench"][1:]]
        self.assertEqual(outfield_xp, sorted(outfield_xp, reverse=True))


class BenchWeightsTest(unittest.TestCase):

    def test_convex_ordering(self):
        self.assertGreater(fpl_tools.BENCH_B1_WEIGHT, fpl_tools.BENCH_DEAD_WEIGHT)
        self.assertGreater(fpl_tools.BENCH_DEAD_WEIGHT, 0.0)
        self.assertAlmostEqual(fpl_tools.BENCH_GK_WEIGHT, 0.05)


if __name__ == "__main__":
    unittest.main()
