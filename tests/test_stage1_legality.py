"""Stage 1 gate: the MIP may only value a legal starting XI.

Before Stage 1, `_solve_squad` constrained the starter binaries only with
`sum(start) == 11` and `sum(start over GKs) == 1`. The optimum of that program is
exactly "best GK + best 10 outfield by xP", so any squad whose top ten outfield
assets skew to one position produced an illegal shape -- and the squad was then
purchased to serve an XI the manager could never field.

Two of the twenty fixture squads are built to trigger it (1-5-5-0 and 1-2-5-3).
`test_traps_are_still_traps` asserts those remain live, so this file cannot
quietly stop testing anything if the fixture is regenerated.
"""

import unittest
from unittest import mock

import fpl_tools
from tests import harness

EVENT = 4
BOUNDS = {"DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}


def _formation(entries):
    return {p: sum(1 for e in entries if e["position"] == p) for p in ("GK", "DEF", "MID", "FWD")}


def _greedy_xi(entries):
    """The unconstrained MIP's optimum, computed in closed form."""
    gk = max((e for e in entries if e["position"] == "GK"), key=lambda e: e["xp"])
    out = sorted((e for e in entries if e["position"] != "GK"), key=lambda e: -e["xp"])[:10]
    return [gk] + out


def _is_legal(form):
    if form["GK"] != 1 or sum(form.values()) != 11:
        return False
    return all(lo <= form[p] <= hi for p, (lo, hi) in BOUNDS.items())


class FormationLegalityTest(unittest.TestCase):
    def test_traps_are_still_traps(self):
        """The two trap squads must have an ILLEGAL unconstrained optimum.

        If this fails, the fixture has drifted and the legality gate below is
        no longer proving anything.
        """
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            expected = {"zero_fwd_trap": "FWD", "two_def_trap": "DEF"}
            for name, breached in expected.items():
                entries = harness.squad_to_pool_entries(
                    bs, harness.build_squad(bs, name), lookup, EVENT)
                form = _formation(_greedy_xi(entries))
                lo, hi = BOUNDS[breached]
                self.assertFalse(
                    lo <= form[breached] <= hi,
                    f"{name}: expected an illegal {breached} count, got {form}")

    def test_all_squads_solve_to_a_legal_xi(self):
        """Every fixture squad must yield a legal XI from the MIP itself."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            for name, _desc in harness.active_squads():
                with self.subTest(squad=name):
                    squad = harness.build_squad(bs, name)
                    entries = harness.squad_to_pool_entries(bs, squad, lookup, EVENT)
                    budget = sum(e["price"] for e in entries)
                    selected, _obj, _parts = fpl_tools._solve_squad(entries, budget=budget)
                    self.assertIsNotNone(selected, f"{name}: solver returned no squad")

                    form = fpl_tools._LAST_SOLVE["formation"]
                    self.assertIsNotNone(form, f"{name}: no XI recorded")
                    self.assertTrue(
                        _is_legal(form),
                        f"{name}: illegal XI 1-{form['DEF']}-{form['MID']}-{form['FWD']}")

    def test_squad_composition_and_club_limit(self):
        """The 2/5/5/3 split and the 3-per-club cap hold on every solve."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            for name, _desc in harness.active_squads():
                with self.subTest(squad=name):
                    entries = harness.squad_to_pool_entries(
                        bs, harness.build_squad(bs, name), lookup, EVENT)
                    by_id = {e["id"]: e for e in entries}
                    budget = sum(e["price"] for e in entries)
                    selected, _, _parts = fpl_tools._solve_squad(entries, budget=budget)

                    chosen = [by_id[pid] for pid in selected]
                    self.assertEqual(_formation(chosen),
                                     {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3})
                    clubs = {}
                    for e in chosen:
                        clubs[e["team_id"]] = clubs.get(e["team_id"], 0) + 1
                    self.assertLessEqual(max(clubs.values()), 3)
                    self.assertLessEqual(round(sum(e["price"] for e in chosen), 1),
                                         round(budget, 1) + 1e-6)

    def test_bench_boost_path_also_legal(self):
        """Bench Boost models the XI too, so BOTH sets of rules bind.

        RE-BASELINED. This test used to assert `_LAST_SOLVE["xi"] == []`,
        which codified the defect rather than a requirement: bench_boost set
        `start = None` to save 15+1+3+3 binaries, and the captaincy binary
        was then linked to x[] alone -- so the armband could be assigned to a
        BENCH player, which is exactly the week that must never happen. The
        empty xi was the visible symptom of the missing constraint, and
        asserting on it made the symptom a contract.

        The eleven is now chosen under Bench Boost as well (scored at
        BB_START_TIEBREAK so it cannot change which fifteen get bought), so
        the assertions become the real ones: a legal fifteen, a legal eleven,
        and an armband inside it.
        """
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            entries = harness.squad_to_pool_entries(
                bs, harness.build_squad(bs, "balanced"), lookup, EVENT)
            by_id = {e["id"]: e for e in entries}
            budget = sum(e["price"] for e in entries)
            selected, _, _parts = fpl_tools._solve_squad(entries, budget=budget, bench_boost=True)
            chosen = [by_id[pid] for pid in selected]
            self.assertEqual(_formation(chosen), {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3})

            xi = fpl_tools._LAST_SOLVE["xi"]
            self.assertEqual(len(xi), 11, "Bench Boost no longer models the XI")
            form = fpl_tools._LAST_SOLVE["formation"]
            self.assertEqual(form["GK"], 1)
            for pos, lo, hi in (("DEF", 3, 5), ("MID", 2, 5), ("FWD", 1, 3)):
                self.assertTrue(lo <= form[pos] <= hi,
                                f"illegal Bench Boost formation: {form}")
            self.assertTrue(set(xi).issubset(set(selected)),
                            "a starter was not among the selected fifteen")


class SolverProfileTest(unittest.TestCase):
    def test_deterministic_profile_proves_optimality(self):
        """The test profile must prove optimality, never return an incumbent."""
        with harness.synthetic_world("deterministic") as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            entries = harness.squad_to_pool_entries(
                bs, harness.build_squad(bs, "balanced"), lookup, EVENT)
            fpl_tools._solve_squad(entries, budget=sum(e["price"] for e in entries))
            self.assertTrue(fpl_tools._LAST_SOLVE["proven_optimal"],
                            f"status was {fpl_tools._LAST_SOLVE['status']}")

    def test_repeated_solves_are_identical(self):
        """Same input, same output -- the contract golden files depend on."""
        with harness.synthetic_world("deterministic") as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            entries = harness.squad_to_pool_entries(
                bs, harness.build_squad(bs, "premium_heavy"), lookup, EVENT)
            budget = sum(e["price"] for e in entries)
            runs = []
            for _ in range(3):
                selected, obj, _parts = fpl_tools._solve_squad(entries, budget=budget)
                runs.append((sorted(selected), round(obj, 6)))
            self.assertEqual(runs[0], runs[1])
            self.assertEqual(runs[1], runs[2])

    def test_unknown_profile_raises(self):
        """A typo in CI must not silently fall back to the fast profile."""
        with self.assertRaises(ValueError):
            fpl_tools.set_solver_profile("determinstic")   # deliberate typo


class SolverTimeoutHonestyTest(unittest.TestCase):
    """PuLP's LpStatus does not distinguish a proven bound from a solve CBC's
    own gap/time-limit termination called "optimal enough" -- both come back
    as the literal string "Optimal". _solve_squad's own elapsed-time check is
    what actually tells them apart, so it is tested by lying to the clock
    rather than by trying to make CBC itself run for a real 0.8s or 60s: the
    real solve on this fixture is fast and genuinely reaches CBC's own
    Optimal status either way -- only time.monotonic's two readings (t0, then
    the post-solve elapsed check) are faked."""

    def _entries(self, bs):
        lookup = fpl_tools._build_fixture_lookup(bs)
        return harness.squad_to_pool_entries(
            bs, harness.build_squad(bs, "balanced"), lookup, EVENT)

    def _solve_with_fake_elapsed(self, bs, profile, elapsed_fraction):
        entries = self._entries(bs)
        budget = sum(e["price"] for e in entries)
        limit = fpl_tools.SOLVER_PROFILES[profile]["timeLimit"]
        fake_times = iter([1000.0, 1000.0 + limit * elapsed_fraction])
        with mock.patch("fpl_tools.time.monotonic", side_effect=lambda: next(fake_times)):
            return fpl_tools._solve_squad(entries, budget=budget)

    def test_a_falsely_reported_optimal_is_not_recorded_as_proven(self):
        with harness.synthetic_world("interactive") as (bs, _fx):
            selected, _obj, _parts = self._solve_with_fake_elapsed(bs, "interactive", 0.97)
        self.assertEqual(fpl_tools._LAST_SOLVE["status"], "Optimal",
                         "the fixture must actually reach CBC's own Optimal status -- "
                         "otherwise this isn't exercising the false-optimal case at all")
        self.assertTrue(fpl_tools._LAST_SOLVE["timed_out"])
        self.assertFalse(fpl_tools._LAST_SOLVE["proven_optimal"],
                         "a solve that ran out its wall-clock budget must never be recorded "
                         "as proven, even when CBC's own status string says Optimal")
        # Still usable interactively -- falls through to the same structural
        # validation "Not Solved" already relied on, rather than being
        # rejected outright.
        self.assertIsNotNone(selected)

    def test_the_same_falsely_reported_optimal_is_rejected_under_deterministic(self):
        """Tests must fail loudly rather than accept a squad that would vary
        with machine load -- the whole reason the deterministic profile
        exists. The interactive-only fallback must not leak into it just
        because the status string happens to say Optimal."""
        with harness.synthetic_world("deterministic") as (bs, _fx):
            selected, _obj, _parts = self._solve_with_fake_elapsed(bs, "deterministic", 0.97)
        self.assertTrue(fpl_tools._LAST_SOLVE["timed_out"])
        self.assertFalse(fpl_tools._LAST_SOLVE["proven_optimal"])
        self.assertIsNone(selected, "the deterministic profile must reject an unproven "
                                    "incumbent even when CBC's status says Optimal")

    def test_a_fast_genuine_solve_is_not_flagged_as_timed_out(self):
        """Baseline: the honesty check must not cry wolf on the overwhelming
        common case of a solve that actually finished quickly."""
        with harness.synthetic_world("interactive") as (bs, _fx):
            entries = self._entries(bs)
            fpl_tools._solve_squad(entries, budget=sum(e["price"] for e in entries))
        self.assertFalse(fpl_tools._LAST_SOLVE["timed_out"])
        self.assertTrue(fpl_tools._LAST_SOLVE["proven_optimal"])


if __name__ == "__main__":
    unittest.main()
