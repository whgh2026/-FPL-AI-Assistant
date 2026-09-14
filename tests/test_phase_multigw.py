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


if __name__ == "__main__":
    unittest.main()
