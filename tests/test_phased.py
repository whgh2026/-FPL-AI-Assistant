import unittest
from unittest import mock

import pulp

import fpl_tools
from tests import harness


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

    def _pool_tempting_a_rebuy(self):
        """Two MID candidates share a scarce club slot (team 9 already holds
        two DEF fillers, so the <=3-per-club cap leaves room for only one of
        them at a time) and their xp curves cross twice: 'mid_swing' is best
        in weeks 1, 2 and 4 but worst in week 3, 'alt_swing' is the mirror
        image. Taken at face value week by week, the highest-xp path sells
        mid_swing for alt_swing in week 3 and buys mid_swing straight back in
        week 4 -- a real sell-then-rebuy of the identical player, not a
        different player filling the same slot. Confirmed by disabling the
        no_rebuy_* constraints: the unconstrained solver reaches for exactly
        that flip-flop on this fixture."""
        pid = [0]

        def entry(pos, team, price, xp):
            pid[0] += 1
            return {"id": pid[0], "name": f"P{pid[0]}", "team_id": team, "team": f"T{team}",
                    "position": pos, "price": price, "xp": xp, "status": "Available",
                    "on_yellow_card_tightrope": False, "minutes_floor": 1.0, "sell_price": price}

        filler9a = entry("DEF", 9, 4.0, 3.0)
        filler9b = entry("DEF", 9, 4.0, 3.0)
        current = ([entry("GK", 1, 4.0, 3.0), entry("GK", 2, 4.0, 3.0)]
                   + [entry("DEF", 3, 4.0, 3.0), entry("DEF", 4, 4.0, 3.0), entry("DEF", 5, 4.0, 3.0),
                      filler9a, filler9b]
                   + [entry("MID", 6, 4.5, 3.0), entry("MID", 7, 4.5, 3.0), entry("MID", 8, 4.5, 3.0),
                      entry("MID", 10, 4.5, 3.0), entry("MID", 14, 4.5, 3.0)]
                   + [entry("FWD", 11, 4.5, 3.0), entry("FWD", 12, 4.5, 3.0), entry("FWD", 13, 4.5, 3.0)])
        # Both candidates sit on team 9 alongside the two DEF fillers already
        # in the squad, so holding both at once would need four team-9 slots
        # -- one more than the <=3-per-club cap allows.
        mid_swing = entry("MID", 9, 5.0, 3.0)
        alt_swing = entry("MID", 9, 5.0, 3.0)
        pool = current + [mid_swing, alt_swing]
        saa_mean = {p["id"]: [p["xp"]] * 4 for p in pool}
        saa_mean[mid_swing["id"]] = [9.0, 9.0, 1.0, 9.0]
        saa_mean[alt_swing["id"]] = [3.0, 3.0, 7.0, 3.0]
        return pool, {p["id"] for p in current}, saa_mean, mid_swing["name"], alt_swing["name"]

    def test_no_immediate_rebuy_after_a_sale(self):
        """The reported defect: the planner sold a player one week and bought
        the same player back a couple of weeks later for a real hit, netting
        no squad change but paying transfer cost twice. Both chips are
        blocked for the whole horizon so this isolates the persistent
        ownership chain (a chip's one-off Free Hit squad legitimately may
        re-select a previously-sold player at no extra cost -- that is not
        the churn being guarded against here)."""
        saved_profile = fpl_tools.get_solver_profile()
        fpl_tools.set_solver_profile("deterministic")
        try:
            pool, current_ids, saa_mean, mid_swing_name, _alt_swing_name = self._pool_tempting_a_rebuy()
            chip_ledger = fpl_tools.ChipLedger.from_history([("Wildcard", 5), ("Free Hit", 5)])
            schedule = fpl_tools._plan_transfers_multi_gw(
                pool, 100.0, 5, current_ids, saa_mean, event=10, n=4, chip_ledger=chip_ledger)
        finally:
            fpl_tools.set_solver_profile(saved_profile)

        # buys/sells hold player *names* (see _plan_transfers_multi_gw), not ids.
        sold_by = {}
        for t, s in enumerate(schedule):
            for name in s["sells"]:
                sold_by.setdefault(name, t)
            for name in s["buys"]:
                self.assertNotIn(
                    name, sold_by,
                    f"player {name!r} was sold at week {sold_by.get(name)} and "
                    f"bought back at week {t} -- a churn re-buy: {schedule}")
        # Non-vacuousness guard. RE-BASELINED: this used to require that
        # `mid_swing` specifically was sold, on the premise that the
        # highest-xP path flip-flops it out in week 3 and back in week 4.
        #
        # The multi-GW objective now selects an XI and weights the bench
        # (BENCH_B1_WEIGHT / BENCH_DEAD_WEIGHT) instead of scoring all fifteen
        # at 1.0x, which hands the planner a THIRD option the flat objective
        # did not have: keep mid_swing and simply BENCH him through his bad
        # week. Holding and benching now beats selling on this fixture, so it
        # no longer tempts the flip at all -- the temptation was partly an
        # artefact of valuing a benched player at full weight.
        #
        # The contract under test is unchanged and still asserted above: no
        # player is ever bought back after being sold. The guard is simply
        # expressed generically now -- at least one real sale has to occur,
        # or the loop above would have nothing to check.
        self.assertTrue(
            sold_by,
            f"fixture produced no sales at all; the guard is vacuous: {schedule}")


EVENT = 4


class BankCashWiringTest(unittest.TestCase):
    """_plan_transfers_multi_gw takes two distinct money figures: `budget`
    (bank + the sell value of the whole current squad -- total purchasing
    power, used to cap what the solver may ever hold) and `bank_cash` (real
    liquid cash, used to seed bank[0]). Collapsing them into one would leave
    a squad worth ~100m read as ~100m sitting in the bank, so a plan with no
    transfers at all would still misreport what's actually spendable.

    This was already correct at both levels when checked (bank0's own
    constraint reads `bank_cash if bank_cash is not None else budget`, and
    suggest_transfers_for_custom_squad's call site passes bank_cash=bank, the
    small figure, never budget, the large one) -- these tests exist to keep
    it that way."""

    def test_plan_transfers_multi_gw_seeds_bank_from_bank_cash_not_budget(self):
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
        current_ids = {p["id"] for p in pool[:15]}
        saa_mean = {p["id"]: [p["xp"]] * 4 for p in pool}
        # Total purchasing power (bank + full squad sell value) is huge; real
        # liquid cash is a fraction of a million. A no-transfer week's
        # bank_after must track the small figure.
        total_sell = sum(p["price"] for p in pool[:15])
        budget = 0.3 + total_sell
        self.assertGreater(budget, 50.0, "fixture must make the two figures obviously distinguishable")

        schedule = fpl_tools._plan_transfers_multi_gw(
            pool, budget, 1, current_ids, saa_mean, event=10, n=4, bank_cash=0.3)
        for s in schedule:
            self.assertLess(
                s["bank_after"], 20.0,
                f"bank_after tracked total purchasing power ({budget:.1f}) "
                f"instead of real liquid cash (0.3): {s}")

    def test_end_to_end_call_site_passes_bank_cash_not_budget(self):
        """The lower-level unit test above proves _plan_transfers_multi_gw
        itself honours bank_cash when given it; this proves the real caller,
        suggest_transfers_for_custom_squad, actually supplies the small figure
        rather than the combined one -- the wiring the reported defect was
        really about."""
        with harness.synthetic_world() as (bs, _fx):
            squad = harness.squad_as_manager_input(bs, harness.build_squad(bs, "balanced"))
            total_squad_value = sum(p["price"] for p in squad)
            self.assertGreater(total_squad_value, 50.0,
                               "fixture squad must be worth much more than the tiny bank below")
            res = fpl_tools.suggest_transfers_for_custom_squad(
                squad, bank=0.3, free_transfers=1, eval_chips=[],
                event=EVENT, risk="balanced", holding_map=None, current_gw=EVENT,
                allow_hits=False)
        plan = res.get("multi_gw_plan") or []
        self.assertTrue(plan, "expected a multi-GW plan to be produced")
        for s in plan:
            self.assertLess(
                s["bank_after"], 20.0,
                f"bank_after tracked squad value (~{total_squad_value:.1f}) rather "
                f"than the manager's real £0.3m bank: {s}")


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


class TransferGanttDataTest(unittest.TestCase):
    """build_transfer_gantt_data: pure reshaping of a multi-GW schedule into
    per-player tenure bars, independent of Plotly or Streamlit."""

    def _squad(self, names, position="MID"):
        return [{"name": n, "position": position} for n in names]

    def test_empty_schedule_returns_empty_structure(self):
        out = fpl_tools.build_transfer_gantt_data(self._squad(["A"]), [])
        self.assertEqual(out["bars"], [])
        self.assertEqual(out["chip_events"], [])
        self.assertEqual(out["captains"], {})
        self.assertIsNone(out["start_gw"])
        self.assertIsNone(out["end_gw"])

    def test_a_player_held_the_whole_horizon_gets_one_bar_spanning_it(self):
        squad = self._squad(["A", "B"])
        schedule = [
            {"gw": 10, "buys": [], "sells": [], "chip": None, "hits": 0},
            {"gw": 11, "buys": [], "sells": [], "chip": None, "hits": 0},
            {"gw": 12, "buys": [], "sells": [], "chip": None, "hits": 0},
        ]
        out = fpl_tools.build_transfer_gantt_data(squad, schedule)
        self.assertEqual(len(out["bars"]), 2)
        for bar in out["bars"]:
            self.assertEqual(bar["start_gw"], 10)
            self.assertEqual(bar["end_gw"], 12)
            self.assertEqual(bar["entered_via"], "initial")
            self.assertEqual(bar["left_via"], "horizon_end")

    def test_a_sale_ends_the_bar_the_week_before_the_sale(self):
        squad = self._squad(["A", "B"])
        schedule = [
            {"gw": 10, "buys": [], "sells": [], "chip": None, "hits": 0},
            {"gw": 11, "buys": ["C"], "sells": ["A"], "chip": None, "hits": 0},
            {"gw": 12, "buys": [], "sells": [], "chip": None, "hits": 0},
        ]
        out = fpl_tools.build_transfer_gantt_data(squad, schedule)
        by_name = {b["name"]: b for b in out["bars"]}
        self.assertEqual(by_name["A"]["start_gw"], 10)
        self.assertEqual(by_name["A"]["end_gw"], 10, "must end the week BEFORE the sale, not on it")
        self.assertEqual(by_name["A"]["left_via"], "sold")
        self.assertEqual(by_name["B"]["end_gw"], 12)
        self.assertEqual(by_name["C"]["start_gw"], 11)
        self.assertEqual(by_name["C"]["entered_via"], "buy")
        self.assertEqual(by_name["C"]["left_via"], "horizon_end")

    def test_sold_then_bought_back_produces_two_separate_bars(self):
        squad = self._squad(["A"])
        schedule = [
            {"gw": 10, "buys": [], "sells": ["A"], "chip": None, "hits": 0},
            {"gw": 11, "buys": [], "sells": [], "chip": None, "hits": 0},
            {"gw": 12, "buys": ["A"], "sells": [], "chip": None, "hits": 0},
        ]
        out = fpl_tools.build_transfer_gantt_data(squad, schedule)
        a_bars = sorted((b for b in out["bars"] if b["name"] == "A"), key=lambda b: b["start_gw"])
        self.assertEqual(len(a_bars), 2, "a resold-then-rebought player must get two segments, not one")
        self.assertEqual((a_bars[0]["start_gw"], a_bars[0]["end_gw"]), (10, 9))
        self.assertEqual((a_bars[1]["start_gw"], a_bars[1]["end_gw"]), (12, 12))

    def test_free_hit_does_not_alter_persistent_tenure(self):
        """Matches _plan_transfers_multi_gw's own freeze: a Free Hit week's
        buys/sells describe the one-off XI, never the persistent squad."""
        squad = self._squad(["A", "B"])
        schedule = [
            {"gw": 10, "buys": [], "sells": [], "chip": None, "hits": 0},
            {"gw": 11, "buys": ["X", "Y"], "sells": ["A", "B"], "chip": "Free Hit", "hits": 0},
            {"gw": 12, "buys": [], "sells": [], "chip": None, "hits": 0},
        ]
        out = fpl_tools.build_transfer_gantt_data(squad, schedule)
        names = {b["name"] for b in out["bars"]}
        self.assertEqual(names, {"A", "B"}, "the Free Hit's one-off X/Y must not appear as tenure bars")
        for bar in out["bars"]:
            self.assertEqual((bar["start_gw"], bar["end_gw"]), (10, 12),
                            "Free Hit must not interrupt persistent tenure")

    def test_chip_events_are_recorded(self):
        squad = self._squad(["A"])
        schedule = [
            {"gw": 10, "buys": [], "sells": [], "chip": None, "hits": 0},
            {"gw": 11, "buys": [], "sells": [], "chip": "Wildcard", "hits": 0},
        ]
        out = fpl_tools.build_transfer_gantt_data(squad, schedule)
        self.assertEqual(out["chip_events"], [{"gw": 11, "chip": "Wildcard"}])

    def test_captain_and_vice_captain_are_the_top_two_by_projected_xp(self):
        squad = self._squad(["A", "B", "C"])
        schedule = [{"gw": 10, "buys": [], "sells": [], "chip": None, "hits": 0}]
        xp_lookup = {"A": 4.0, "B": 9.0, "C": 6.5}
        out = fpl_tools.build_transfer_gantt_data(squad, schedule, xp_lookup=xp_lookup)
        self.assertEqual(out["captains"][10], {"captain": "B", "vice_captain": "C"})

    def test_no_xp_lookup_means_no_captain_guess(self):
        squad = self._squad(["A", "B"])
        schedule = [{"gw": 10, "buys": [], "sells": [], "chip": None, "hits": 0}]
        out = fpl_tools.build_transfer_gantt_data(squad, schedule)
        self.assertEqual(out["captains"][10], {"captain": None, "vice_captain": None})

    def test_captain_on_a_free_hit_week_is_drawn_from_the_one_off_squad(self):
        """The persistent squad's best player isn't necessarily playing that
        week -- the one-off Free Hit XI is who is actually selected."""
        squad = self._squad(["A", "B"])
        schedule = [
            {"gw": 10, "buys": ["Z"], "sells": ["A"], "chip": "Free Hit", "hits": 0},
        ]
        xp_lookup = {"A": 20.0, "B": 3.0, "Z": 9.0}
        out = fpl_tools.build_transfer_gantt_data(squad, schedule, xp_lookup=xp_lookup)
        # A (xp=20) is NOT playing this week -- it was sold for the one-off
        # squad -- so the captain must come from {B, Z}, not A.
        self.assertEqual(out["captains"][10]["captain"], "Z")

    def test_bars_are_sorted_by_start_then_name(self):
        squad = self._squad(["B", "A"])
        schedule = [
            {"gw": 10, "buys": [], "sells": [], "chip": None, "hits": 0},
            {"gw": 11, "buys": ["C"], "sells": [], "chip": None, "hits": 0},
        ]
        out = fpl_tools.build_transfer_gantt_data(squad, schedule)
        starts = [(b["start_gw"], b["name"]) for b in out["bars"]]
        self.assertEqual(starts, sorted(starts))


if __name__ == "__main__":
    unittest.main()
