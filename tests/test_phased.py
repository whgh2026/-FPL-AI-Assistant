import unittest
from unittest import mock

import pulp

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

    def _pool_favouring_a_full_rebuild(self):
        """A weak current squad and a much stronger incoming pool, so a full
        rebuild is clearly worth more than any hit it would otherwise cost --
        used to force the planner to actually reach for a chip rather than
        merely being ABLE to represent one."""
        pool = []
        pid = [0]

        def entry(pos, team, price, xp):
            pid[0] += 1
            return {"id": pid[0], "name": f"P{pid[0]}", "team_id": team, "team": f"T{team}",
                    "position": pos, "price": price, "xp": xp, "status": "Available",
                    "on_yellow_card_tightrope": False, "minutes_floor": 1.0, "sell_price": price}

        current = ([entry("GK", 1, 4.0, 1.0), entry("GK", 2, 4.0, 1.0)]
                   + [entry("DEF", t, 4.0, 1.0) for t in range(1, 6)]
                   + [entry("MID", t, 4.5, 1.0) for t in range(1, 6)]
                   + [entry("FWD", t, 4.5, 1.0) for t in (1, 2, 3)])
        incoming = ([entry("GK", 7, 5.0, 6.0), entry("GK", 8, 4.5, 5.5)]
                    + [entry("DEF", t, 5.5, 7.0) for t in range(7, 12)]
                    + [entry("MID", t, 8.0, 9.0) for t in range(7, 12)]
                    + [entry("FWD", t, 9.0, 10.0) for t in (7, 8, 9)])
        return current + incoming, {p["id"] for p in current}

    def test_wildcard_or_free_hit_pays_no_hit_when_clearly_worth_it(self):
        """End-to-end: with only 1 free transfer banked, rebuilding a squad
        this weak against a pool this strong should use a chip rather than
        eat a huge hit -- and whichever chip it picks must be free."""
        saved_profile = fpl_tools.get_solver_profile()
        fpl_tools.set_solver_profile("deterministic")
        try:
            pool, current_ids = self._pool_favouring_a_full_rebuild()
            saa_mean = {p["id"]: [p["xp"]] * 4 for p in pool}
            schedule = fpl_tools._plan_transfers_multi_gw(
                pool, 100.0, 1, current_ids, saa_mean, event=10, n=4)
        finally:
            fpl_tools.set_solver_profile(saved_profile)
        chip_weeks = [s for s in schedule if s.get("chip")]
        self.assertTrue(chip_weeks, f"expected a chip to be used: {schedule}")
        for s in chip_weeks:
            self.assertIn(s["chip"], ("Wildcard", "Free Hit"))
            self.assertEqual(s["hits"], 0, f"a chip week must never charge a hit: {s}")

    def test_free_hit_squad_reverts_the_following_week(self):
        """Free Hit's squad change must not persist -- the persistent chain
        has to show the SAME squad the week after a Free Hit as it did going
        into it, since the real chip auto-reverts."""
        saved_profile = fpl_tools.get_solver_profile()
        fpl_tools.set_solver_profile("deterministic")
        try:
            pool, current_ids = self._pool_favouring_a_full_rebuild()
            saa_mean = {p["id"]: [p["xp"]] * 4 for p in pool}
            # Enough free transfers that the model has no incentive to use a
            # Wildcard just to fund routine moves, isolating Free Hit as the
            # chip a genuine "attack this one week only" scenario would reach
            # for -- though which chip it actually prefers isn't asserted
            # here, only that IF Free Hit fires, it reverts correctly.
            schedule = fpl_tools._plan_transfers_multi_gw(
                pool, 100.0, 5, current_ids, saa_mean, event=10, n=4)
        finally:
            fpl_tools.set_solver_profile(saved_profile)
        fh_weeks = [i for i, s in enumerate(schedule) if s.get("chip") == "Free Hit"]
        if not fh_weeks:
            self.skipTest("solver did not choose Free Hit in this fixture")
        i = fh_weeks[0]
        if i + 1 < len(schedule):
            following = schedule[i + 1]
            self.assertFalse(following["buys"] or following["sells"],
                             f"the week after Free Hit must show no changes "
                             f"to the persistent squad: {following}")
            self.assertIsNone(following["chip"])


class FreeTransferStateMachineTest(unittest.TestCase):
    """Direct tests of _ft_state_machine_constraints, the retained
    free-transfer state machine shared by _plan_transfers_multi_gw. ft_t/u_t/
    chip_t are FIXED here rather than left to the horizon optimiser, which
    can't reliably be dictated into reaching one specific week's state --
    this tests the formula itself, deterministically."""

    def _solve(self, ft_t, u_t, chip_t):
        prob = pulp.LpProblem("ft_state_machine_test", pulp.LpMaximize)
        ft_next = pulp.LpVariable("ft_next", lowBound=0, upBound=5, cat="Integer")
        hits_t = pulp.LpVariable("hits_t", lowBound=0, cat="Integer")
        fpl_tools._ft_state_machine_constraints(prob, ft_t, ft_next, u_t, chip_t, hits_t)
        # The constraints are upper-bound-only by design (see the function's
        # docstring): they need the SAME "pull ft_next up, push hits_t down"
        # preference the real horizon objective supplies (a hit costs 4.0
        # there; nothing rewards a lower ft) to settle at a unique point --
        # with no preference at all they are merely upper bounds, and CBC is
        # free to return anything feasible below them, e.g. ft_next=0.
        prob += ft_next - 1000 * hits_t
        prob.solve(pulp.PULP_CBC_CMD(msg=0))
        self.assertEqual(pulp.LpStatus[prob.status], "Optimal", f"infeasible for ft_t={ft_t}, u_t={u_t}, chip_t={chip_t}")
        return int(round(ft_next.varValue)), int(round(hits_t.varValue))

    def test_normal_week_no_hit_carries_forward(self):
        """3 banked, 2 used, no chip -> 3-2+1 = 2 next week, no hit."""
        ft_next, hits = self._solve(ft_t=3, u_t=2, chip_t=0)
        self.assertEqual((ft_next, hits), (2, 0))

    def test_normal_week_bank_increments_when_transfers_are_saved(self):
        """3 banked, 0 used -> 4 next week, no hit."""
        ft_next, hits = self._solve(ft_t=3, u_t=0, chip_t=0)
        self.assertEqual((ft_next, hits), (4, 0))

    def test_normal_week_caps_at_five(self):
        ft_next, hits = self._solve(ft_t=5, u_t=0, chip_t=0)
        self.assertEqual((ft_next, hits), (5, 0))

    def test_normal_week_hit_is_exactly_transfers_over_budget_and_resets_bank_to_one(self):
        """1 banked, 3 used, no chip -> exactly 2 hits (u - ft), and the bank
        resets to the floor of 1 rather than going negative."""
        ft_next, hits = self._solve(ft_t=1, u_t=3, chip_t=0)
        self.assertEqual((ft_next, hits), (1, 2))

    def test_wildcard_u15_preserves_and_increments_banked_inventory(self):
        """The exact case in the request: fts 3 -> 4 after a Wildcard (u=15),
        with hits == 0, exactly as if zero transfers had been made."""
        ft_next, hits = self._solve(ft_t=3, u_t=15, chip_t=1)
        self.assertEqual((ft_next, hits), (4, 0))

    def test_free_hit_u11_preserves_and_increments_banked_inventory(self):
        ft_next, hits = self._solve(ft_t=3, u_t=11, chip_t=1)
        self.assertEqual((ft_next, hits), (4, 0))

    def test_chip_still_caps_the_bank_at_five(self):
        ft_next, hits = self._solve(ft_t=5, u_t=15, chip_t=1)
        self.assertEqual((ft_next, hits), (5, 0))


if __name__ == "__main__":
    unittest.main()
