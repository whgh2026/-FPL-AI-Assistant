"""Tier 2 gate: the multi-GW planner's opening cash and solver honesty.

`_plan_transfers_multi_gw` takes two distinct money figures: `budget` (total
purchasing power -- bank plus the sell value of the entire current squad) and
`bank_cash` (real liquid cash). When the caller omitted the second, bank[0]
was seeded with the FIRST:

    prob += bank[0] == (budget if bank_cash is None else float(bank_cash))

The flow constraint then credits sell_value[t] again on every sale, so every
held player's equity was counted twice and the roadmap opened with roughly
`2 x squad_value - real_bank` to spend. On a ~100m squad with 0.3m in the
bank that is a phantom war chest two orders of magnitude too large, and the
plan built on it is unexecutable.
"""

import unittest
from unittest import mock

import fpl_tools
from tests import harness


def _pool():
    pool, pid = [], 0

    def entry(pos, team, price=5.0):
        nonlocal pid
        pid += 1
        return {"id": pid, "name": "P%d" % pid, "team_id": team, "team": "T%d" % team,
                "position": pos, "price": price, "xp": 5.0, "status": "Available",
                "on_yellow_card_tightrope": False, "minutes_floor": 1.0,
                "sell_price": price}

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


def _plan(bank_cash, budget_delta=0.3, **over):
    pool = _pool()
    current_ids = {p["id"] for p in pool[:15]}
    saa_mean = {p["id"]: [p["xp"]] * 4 for p in pool}
    total_sell = sum(p["price"] for p in pool[:15])
    kw = dict(event=10, n=4, bank_cash=bank_cash)
    kw.update(over)
    schedule = fpl_tools._plan_transfers_multi_gw(
        pool, total_sell + budget_delta, 1, current_ids, saa_mean, **kw)
    return schedule, total_sell


class BankInferenceTest(unittest.TestCase):

    def test_the_opening_bank_is_reconstructed_from_budget_minus_sell_equity(self):
        """The defect. With bank_cash omitted, bank[0] must be real money."""
        with harness.synthetic_world():
            _schedule, total_sell = _plan(bank_cash=None, budget_delta=0.3)
            diag = fpl_tools._LAST_PLAN_SOLVE
        self.assertTrue(diag["bank_inferred"])
        self.assertAlmostEqual(diag["opening_bank"], 0.3, places=6)
        self.assertLess(diag["opening_bank"], total_sell / 10.0,
                        "bank[0] is still carrying the whole squad's equity")

    def test_an_explicit_bank_cash_still_wins(self):
        with harness.synthetic_world():
            _schedule, _ts = _plan(bank_cash=1.7)
            diag = fpl_tools._LAST_PLAN_SOLVE
        self.assertFalse(diag["bank_inferred"])
        self.assertAlmostEqual(diag["opening_bank"], 1.7, places=6)
        self.assertFalse(diag["bank_clamped"])

    def test_a_negative_inference_is_clamped_rather_than_made_infeasible(self):
        """D-C. `bank` is declared lowBound=0, so constraining it to a negative
        value makes the whole model infeasible -- the planner returns [] and
        the roadmap silently vanishes with nothing to explain it. A small
        negative means rounding or a stale sell_price, not a real overdraft."""
        with harness.synthetic_world():
            schedule, _ts = _plan(bank_cash=None, budget_delta=-2.0)
            diag = fpl_tools._LAST_PLAN_SOLVE
        self.assertTrue(diag["bank_clamped"])
        self.assertEqual(diag["opening_bank"], 0.0)
        self.assertTrue(schedule, "clamping produced an infeasible model")

    def test_the_opening_bank_actually_constrains_the_plan(self):
        """D-A. A bare `bank[0] == ...` builds an LpConstraint and discards it,
        leaving bank[0] bounded only by lowBound=0 -- looser than the bug being
        fixed, and silent. If the constraint is really attached, a squad with
        0.0 opening cash and an all-equal-xP pool has nothing to gain by
        churning, so it cannot end a week richer than it started."""
        with harness.synthetic_world():
            schedule, _ts = _plan(bank_cash=0.0)
            diag = fpl_tools._LAST_PLAN_SOLVE
        self.assertEqual(diag["opening_bank"], 0.0)
        for week in schedule:
            bank_after = week.get("bank_after")
            if bank_after is not None:
                self.assertLess(
                    bank_after, 5.0,
                    "bank[0] is unconstrained: the plan conjured spendable cash")


class PlanIncumbentTest(unittest.TestCase):
    """`if LpStatus[prob.status] != "Optimal": return []` discarded a
    perfectly usable time-limited incumbent. Under the interactive profile
    (timeLimit=0.8s) a six-week plan over a ~40-player pool routinely runs the
    clock out, so the roadmap vanished on exactly the solves it was most
    needed for -- the same failure _solve_squad was fixed for in Stage 6.

    Invariant I-7 is the boundary: an unproven incumbent is acceptable
    interactively and never under `deterministic`, which must fail loudly
    rather than return a schedule that varies with machine load.
    """

    def _timed_out(self):
        """Force the elapsed-time check to read as a timeout without actually
        waiting. Two readings: t0, then one far past any profile's limit."""
        return mock.patch.object(fpl_tools.time, "monotonic",
                                 side_effect=[0.0, 1e6])

    def test_a_proven_solve_is_reported_as_proven(self):
        with harness.synthetic_world("deterministic"):
            schedule, _ts = _plan(bank_cash=0.3)
            diag = fpl_tools._LAST_PLAN_SOLVE
        self.assertTrue(schedule)
        self.assertTrue(diag["proven_optimal"])
        self.assertFalse(diag["timed_out"])
        self.assertEqual(diag["status"], "Optimal")

    def test_the_deterministic_profile_rejects_an_unproven_incumbent(self):
        """I-7. A schedule that depends on how loaded the runner was is not a
        result a golden-file suite can compare against."""
        with harness.synthetic_world("deterministic"):
            with self._timed_out():
                schedule, _ts = _plan(bank_cash=0.3)
            diag = fpl_tools._LAST_PLAN_SOLVE
        self.assertTrue(diag["timed_out"])
        self.assertFalse(diag["proven_optimal"])
        self.assertEqual(schedule, [], "deterministic accepted an unproven plan")

    def test_the_interactive_profile_accepts_a_valid_incumbent(self):
        """The recovery itself: same forced timeout, opposite verdict."""
        with harness.synthetic_world("interactive"):
            with self._timed_out():
                schedule, _ts = _plan(bank_cash=0.3)
            diag = fpl_tools._LAST_PLAN_SOLVE
        self.assertTrue(diag["timed_out"])
        self.assertFalse(diag["proven_optimal"])
        self.assertTrue(schedule,
                        "a usable time-limited incumbent was thrown away")

    def test_the_diagnostics_say_which_it_was(self):
        """The UI must be able to distinguish "optimal" from "best plan found
        in the time available" -- it cannot claim the first for the second."""
        with harness.synthetic_world("interactive"):
            _plan(bank_cash=0.3)
            diag = fpl_tools._LAST_PLAN_SOLVE
        for key in ("status", "proven_optimal", "timed_out", "elapsed",
                    "profile", "objective"):
            self.assertIn(key, diag)


class PlanValidatorTest(unittest.TestCase):

    def test_a_structurally_broken_week_is_rejected(self):
        """_is_valid_plan is the gate an incumbent has to pass. An infeasible
        solve leaves stale relaxation values behind that can look plausible --
        _solve_squad once observed 15 selected players with a TEN-man XI -- so
        the schedule is validated, not just its status."""
        class _V:
            def __init__(self, v):
                self.varValue = v

        by_id = {i: {"position": p, "team_id": 1}
                 for i, p in enumerate(["GK"] * 2 + ["DEF"] * 5
                                       + ["MID"] * 5 + ["FWD"] * 3)}
        ids = list(by_id)
        # Only fourteen held in week 0 -> not a legal squad.
        x = {i: {0: _V(1.0 if i < 14 else 0.0)} for i in ids}
        y = {i: {0: _V(0.0)} for i in ids}
        ok = fpl_tools._is_valid_plan(ids, by_id, x, y, {0: _V(0.0)},
                                      {0: _V(0.5)}, 1)
        self.assertFalse(ok)


class ObjectiveAlignmentTest(unittest.TestCase):
    """The weekly score used to be sum(xp * x) over all fifteen at 1.0x, so a
    selected player contributed their full xP whether they could be fielded or
    not. The planner was therefore indifferent between a premium starter and a
    premium BENCH player, and would happily spend on fodder it could never
    start. The score is now the same trilinear shape _solve_squad uses for a
    single gameweek, projected onto the T-week grid.
    """

    def _src(self):
        import inspect
        return inspect.getsource(fpl_tools._plan_transfers_multi_gw)

    def test_the_weekly_score_is_no_longer_flat_across_all_fifteen(self):
        src = self._src()
        self.assertNotIn("pts_x = pulp.lpSum(x[pid][t] * saa_xp[pid][t] for pid in ids)", src,
                         "the persistent squad is still scored flat at 1.0x")
        self.assertNotIn("pts_y = pulp.lpSum(y[pid][t] * saa_xp[pid][t] for pid in ids)", src,
                         "the Free Hit squad is still scored flat at 1.0x")

    def test_both_branches_carry_the_xi_and_captain_shape(self):
        """D-D. The spec's own rewrite left the Free Hit branch at a flat 1.0x
        for all fifteen while giving the persistent branch XI + weighted bench
        + captaincy. That scores a chip week several points above an identical
        non-chip week purely from bench accounting, biasing the planner toward
        BURNING the chip -- the same defect, reintroduced on the other side."""
        src = self._src()
        for name in ("start_x", "cap_x", "b1_x", "start_y", "cap_y"):
            self.assertIn(name, src, f"{name} binaries are missing")
        self.assertIn("BENCH_B1_WEIGHT", src)
        self.assertIn("BENCH_GK_WEIGHT", src)

    def test_a_free_hit_is_not_played_for_phantom_bench_points(self):
        """Behavioural half of D-D. With a pool where the current squad is
        already the best available, a Free Hit can buy nothing -- so it must
        not be played. Under the flat objective the chip's squad scored all
        fifteen at 1.0x while the persistent squad scored XI + decayed bench,
        so firing the chip conjured the bench difference out of nothing and
        the planner took it."""
        with harness.synthetic_world("deterministic"):
            pool = _pool()
            # Every player identical, so no transfer and no chip can improve
            # anything. Any chip week in the output is phantom value.
            for p in pool:
                p["xp"] = 5.0
            current_ids = {p["id"] for p in pool[:15]}
            saa_mean = {p["id"]: [5.0] * 4 for p in pool}
            total_sell = sum(p["price"] for p in pool[:15])
            schedule = fpl_tools._plan_transfers_multi_gw(
                pool, total_sell + 0.5, 1, current_ids, saa_mean,
                event=10, n=4, bank_cash=0.5)
        # Free Hit specifically. A Wildcard on this fixture is a genuine
        # no-op the solver is INDIFFERENT to -- with every player identical it
        # costs nothing and gains nothing, so CBC may set wc[t]=1 arbitrarily.
        # That is degeneracy in the Wildcard modelling, not phantom value, and
        # it is not what D-D is about. Free Hit is different: under the flat
        # objective it produced a positive, fictitious gain.
        fh_weeks = [w["gw"] for w in schedule if w.get("chip") == "Free Hit"]
        self.assertEqual(fh_weeks, [],
                         f"Free Hit was played for phantom bench points: {schedule}")

    def test_the_big_m_cannot_bind_on_a_realistic_week(self):
        """F-2. _PLAN_PTS_BIGM was 1000.0 against a realistic weekly total
        under 100 -- slack that is pure weakness in the LP relaxation. It must
        stay above any achievable weekly score and well below the old value."""
        self.assertGreater(fpl_tools._PLAN_PTS_BIGM, 100.0)
        self.assertLess(fpl_tools._PLAN_PTS_BIGM, 1000.0)


if __name__ == "__main__":
    unittest.main()


class TerminalDeadWeightTest(unittest.TestCase):
    """The planner's terminal penalty classified a player as dead by testing
    membership of _NON_PLAYING_NOTES or xp <= 0.1. _pool_entry writes the
    DISPLAY note into "status", and a doubt is rendered as f"{chance}% Chance"
    -- not in that tuple, and not zero-xP either, because doubt is priced into
    p_full/p_cameo rather than zeroing the projection. So the planner valued a
    player three-quarters likely to be unavailable at full terminal equity.
    """

    def test_the_fixed_vocabulary_still_counts(self):
        for note in fpl_tools._NON_PLAYING_NOTES:
            with self.subTest(note=note):
                self.assertTrue(
                    fpl_tools._is_terminally_dead({"status": note, "xp": 4.0}))

    def test_a_deep_doubt_flag_counts(self):
        """The defect in one assertion: "25% Chance" is a real note
        _pool_entry emits, carries a healthy projection, and was classified
        alive."""
        self.assertTrue(
            fpl_tools._is_terminally_dead({"status": "25% Chance", "xp": 4.0}))

    def test_a_shallow_doubt_flag_does_not(self):
        """A 75%-chance player is a live asset. Treating every doubt as dead
        would have the planner churn out of anyone carrying a knock."""
        for note in ("50% Chance", "75% Chance", "Doubtful", "Available"):
            with self.subTest(note=note):
                self.assertFalse(
                    fpl_tools._is_terminally_dead({"status": note, "xp": 4.0}))

    def test_the_boundary_is_inclusive(self):
        self.assertEqual(fpl_tools.DOUBT_DEAD_MAX_CHANCE, 25.0)
        self.assertTrue(fpl_tools._is_terminally_dead(
            {"status": f"{int(fpl_tools.DOUBT_DEAD_MAX_CHANCE)}% Chance", "xp": 4.0}))
        self.assertFalse(fpl_tools._is_terminally_dead(
            {"status": f"{int(fpl_tools.DOUBT_DEAD_MAX_CHANCE) + 1}% Chance", "xp": 4.0}))

    def test_the_zero_xp_fallback_survives(self):
        self.assertTrue(
            fpl_tools._is_terminally_dead({"status": "Available", "xp": 0.05}))

    def test_a_missing_or_non_string_note_does_not_raise(self):
        for entry in ({"xp": 4.0}, {"status": None, "xp": 4.0},
                      {"status": 42, "xp": 4.0}):
            with self.subTest(entry=entry):
                self.assertFalse(fpl_tools._is_terminally_dead(entry))

    def test_the_planner_sheds_a_deep_doubt_by_the_terminal_week(self):
        """End to end: the same squad, the same everything, one held player
        flagged. At 25% the planner moves them on; at 75% it keeps them. Run as
        a pair, because a single run cannot distinguish "the penalty fired"
        from "the planner churned anyway".

        term_dead is raised for the test. At its production 0.5 the penalty is
        real but smaller than the transfer friction a swap costs (HURDLE_BASE
        is 0.8 before sigma), so on a fixture where every player projects an
        identical 5.0 the planner correctly declines to pay 0.8 to avoid 0.5
        and the flag never gets to decide anything. The magnitude is a tunable
        weight; what this test is about is whether the CLASSIFICATION reaches
        the objective at all, which is where the defect was.
        """
        def terminal_holds(note):
            pool = _pool()
            current_ids = {p["id"] for p in pool[:15]}
            flagged = pool[10]["id"]              # a held MID
            self.assertIn(flagged, current_ids)
            pool[10]["status"] = note
            # _pool()'s only unowned players are forwards, and the squad needs
            # exactly five midfielders -- so without a spare MID in the market
            # the flagged player is structurally unsellable and the plan comes
            # out identical whatever the flag says. The penalty has to be given
            # a legal move before it can be observed at all.
            spare = dict(pool[10])
            spare.update({"id": 99, "name": "P99", "team_id": 2, "team": "T2",
                          "status": "Available"})
            pool.append(spare)
            saa_mean = {p["id"]: [p["xp"]] * 4 for p in pool}
            total_sell = sum(p["price"] for p in pool[:15])
            loud = dict(fpl_tools._load_weights())
            loud["term_dead"] = 20.0
            with harness.synthetic_world(), \
                 mock.patch.object(fpl_tools, "_load_weights", return_value=loud):
                schedule = fpl_tools._plan_transfers_multi_gw(
                    pool, total_sell + 5.0, 5, current_ids, saa_mean,
                    event=10, n=4, bank_cash=5.0)
            self.assertTrue(schedule, f"no schedule produced for {note}")
            sold = {name.split()[0] for s in schedule for name in s["sells"]}
            return f"P{flagged}" not in sold, flagged

        shed_at_25, pid = terminal_holds("25% Chance")
        shed_at_75, _ = terminal_holds("75% Chance")
        self.assertFalse(shed_at_25,
                         f"P{pid} at 25% chance was still held at the terminal "
                         f"week -- the doubt flag is not reaching the penalty")
        self.assertTrue(shed_at_75,
                        f"P{pid} at 75% chance was sold -- the penalty is "
                        f"firing on live assets, not just dead ones")
