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
            for name, _desc in harness.SQUAD_SPECS:
                with self.subTest(squad=name):
                    squad = harness.build_squad(bs, name)
                    entries = harness.squad_to_pool_entries(bs, squad, lookup, EVENT)
                    budget = sum(e["price"] for e in entries)
                    selected, _obj = fpl_tools._solve_squad(entries, budget=budget)
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
            for name, _desc in harness.SQUAD_SPECS:
                with self.subTest(squad=name):
                    entries = harness.squad_to_pool_entries(
                        bs, harness.build_squad(bs, name), lookup, EVENT)
                    by_id = {e["id"]: e for e in entries}
                    budget = sum(e["price"] for e in entries)
                    selected, _ = fpl_tools._solve_squad(entries, budget=budget)

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
        """Bench Boost skips the starter binaries, so the squad rules still bind.

        Under bench_boost the XI is not modelled at all (every player scores), so
        there is no formation to check -- but the 15-man composition must hold,
        and no stale XI may be left behind in the diagnostics.
        """
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            entries = harness.squad_to_pool_entries(
                bs, harness.build_squad(bs, "balanced"), lookup, EVENT)
            by_id = {e["id"]: e for e in entries}
            budget = sum(e["price"] for e in entries)
            selected, _ = fpl_tools._solve_squad(entries, budget=budget, bench_boost=True)
            chosen = [by_id[pid] for pid in selected]
            self.assertEqual(_formation(chosen), {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3})
            self.assertEqual(fpl_tools._LAST_SOLVE["xi"], [])


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
                selected, obj = fpl_tools._solve_squad(entries, budget=budget)
                runs.append((sorted(selected), round(obj, 6)))
            self.assertEqual(runs[0], runs[1])
            self.assertEqual(runs[1], runs[2])

    def test_unknown_profile_raises(self):
        """A typo in CI must not silently fall back to the fast profile."""
        with self.assertRaises(ValueError):
            fpl_tools.set_solver_profile("determinstic")   # deliberate typo


if __name__ == "__main__":
    unittest.main()
