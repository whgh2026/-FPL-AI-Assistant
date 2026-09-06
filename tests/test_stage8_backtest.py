"""Stage 8d gate: the backtester scores honestly, and the DB layer degrades.

The backtester is the first thing in this rebuild that can say whether the model
is actually GOOD, rather than whether a particular line of it was wrong. That
makes its arithmetic worth testing carefully -- a scorer that flatters the model
is worse than no scorer, because it would end the argument in the wrong place.
"""

import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import db
import fpl_tools
import backtest
from tests import harness


class ScoringTest(unittest.TestCase):
    def test_perfect_prediction_scores_zero_error(self):
        s = backtest._score([(3.0, 3.0), (5.0, 5.0), (1.0, 1.0)])
        self.assertAlmostEqual(s["rmse"], 0.0)
        self.assertAlmostEqual(s["bias"], 0.0)
        self.assertAlmostEqual(s["rank_corr"], 1.0)

    def test_bias_signs_the_direction(self):
        """Positive means we projected MORE than they scored."""
        self.assertGreater(backtest._score([(5.0, 3.0)] * 4)["bias"], 0)
        self.assertLess(backtest._score([(1.0, 3.0)] * 4)["bias"], 0)

    def test_rank_correlation_is_blind_to_a_constant_offset(self):
        """The point of tracking it. A transfer is a ranking decision, so being
        uniformly half a point high costs nothing if the ORDER is right."""
        truth = [(1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0), (9.0, 9.0)]
        shifted = [(p + 5.0, a) for p, a in truth]
        self.assertAlmostEqual(backtest._score(shifted)["rank_corr"], 1.0)
        self.assertGreater(backtest._score(shifted)["rmse"], 4.0)

    def test_inverted_ranking_scores_negative(self):
        s = backtest._score([(1.0, 5.0), (2.0, 4.0), (4.0, 2.0), (5.0, 1.0)])
        self.assertLess(s["rank_corr"], -0.9)

    def test_ties_do_not_break_the_correlation(self):
        """A model that projects the same value for many players -- which is
        exactly what the flat bonus and card constants used to produce -- must
        not yield a divide-by-zero or a spurious correlation."""
        s = backtest._score([(2.0, 1.0), (2.0, 5.0), (2.0, 3.0), (2.0, 9.0)])
        self.assertIsNone(s["rank_corr"])

    def test_too_few_points_returns_no_correlation(self):
        self.assertIsNone(backtest._rank_correlation([(1.0, 2.0), (2.0, 1.0)]))

    def test_empty_input(self):
        self.assertIsNone(backtest._score([]))

    def test_rmse_matches_the_definition(self):
        pairs = [(1.0, 2.0), (3.0, 5.0), (4.0, 4.0)]
        want = math.sqrt((1 + 4 + 0) / 3)
        self.assertAlmostEqual(backtest._score(pairs)["rmse"], want)


class ReplayTest(unittest.TestCase):
    def test_replays_an_archived_gameweek_against_real_actuals(self):
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            gw = fpl_tools._next_gameweek(bootstrap)
            # Actuals injected directly: the point is the scoring path, not the
            # live endpoint.
            actuals = {int(e["id"]): float(i % 13)
                       for i, e in enumerate(bootstrap["elements"])}
            r = backtest.replay_gameweek(gw, snapshot=bootstrap, actuals=actuals)
            self.assertIsNotNone(r, "a full bootstrap failed to replay")
            self.assertEqual(r["gameweek"], gw)
            self.assertGreater(r["model"]["n"], backtest.MIN_ROWS_PER_GW)

    def test_scores_the_ep_next_baseline_alongside(self):
        """The comparison that matters. ep_next is free, needs no modelling and
        ships in the same payload, so a model that cannot beat it is not earning
        its complexity."""
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            gw = fpl_tools._next_gameweek(bootstrap)
            actuals = {int(e["id"]): float(i % 13)
                       for i, e in enumerate(bootstrap["elements"])}
            r = backtest.replay_gameweek(gw, snapshot=bootstrap, actuals=actuals)
            self.assertIsNotNone(r["ep_next"], "no baseline was scored")
            self.assertGreater(r["ep_next"]["n"], 0)

    def test_a_missing_snapshot_returns_none_rather_than_guessing(self):
        with harness.synthetic_world():
            self.assertIsNone(backtest.replay_gameweek(7, snapshot=None, actuals={1: 2.0}))

    def test_no_actuals_returns_none(self):
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            self.assertIsNone(backtest.replay_gameweek(4, snapshot=bootstrap, actuals={}))

    def test_a_thin_gameweek_is_refused(self):
        """Below MIN_ROWS_PER_GW the statistics say nothing, so reporting them
        would be worse than reporting nothing."""
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            gw = fpl_tools._next_gameweek(bootstrap)
            few = {int(bootstrap["elements"][i]["id"]): 3.0 for i in range(3)}
            self.assertIsNone(backtest.replay_gameweek(gw, snapshot=bootstrap, actuals=few))

    def test_only_players_who_featured_are_scored(self):
        """C18's filter, in the backtester. ~60% of a bootstrap did not play,
        all scoring zero against a near-zero projection; including them measures
        how well the model predicts ABSENCE."""
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            gw = fpl_tools._next_gameweek(bootstrap)
            subset = {int(e["id"]): 4.0 for e in bootstrap["elements"][:80]}
            r = backtest.replay_gameweek(gw, snapshot=bootstrap, actuals=subset)
            self.assertLessEqual(r["model"]["n"], 80,
                                 "players with no actual result were scored anyway")

    def test_cli_reports_cleanly_when_there_is_nothing_archived(self):
        """A fresh deployment has no archive. That must read as 'nothing to
        replay yet', not as a crash or as a zero score."""
        saved = db.load_bootstrap_snapshot
        db.load_bootstrap_snapshot = lambda gw, captured_date=None: None
        try:
            self.assertEqual(backtest.main(["--from", "1", "--to", "3"]), 1)
        finally:
            db.load_bootstrap_snapshot = saved


class DatabaseResilienceTest(unittest.TestCase):
    def test_connection_failures_are_explained_not_just_reported(self):
        """get_db_connection returns None on every failure, which is the right
        shape -- but it discarded the REASON, so a page could only ever say
        'unavailable'. A missing DATABASE_URL and a refused connection need
        different responses, and one is a five-second fix."""
        saved = os.environ.pop("DATABASE_URL", None)
        try:
            db._POOL = None
            self.assertIsNone(db.get_db_connection())
            kind, _msg = db.last_db_error()
            self.assertIn(kind, ("config", "driver"))
        finally:
            if saved is not None:
                os.environ["DATABASE_URL"] = saved

    def test_a_connect_timeout_is_configured(self):
        """There was none, so an unreachable database did not fail -- it HUNG,
        and took the page render with it for as long as the OS would wait."""
        self.assertGreater(db.CONNECT_TIMEOUT_SECONDS, 0)
        self.assertLessEqual(db.CONNECT_TIMEOUT_SECONDS, 30)

    def test_pooled_connection_returns_itself_rather_than_closing(self):
        """All thirteen callers do conn.close() in a finally block, which
        against a pool would destroy the connection instead of returning it."""
        returned = []

        class FakePool:
            def putconn(self, conn, close=False):
                returned.append((conn, close))

        class FakeConn:
            def __init__(self):
                self.rolled_back = False

            def rollback(self):
                self.rolled_back = True

        raw = FakeConn()
        wrapped = db._PooledConnection(raw, FakePool())
        wrapped.close()
        self.assertEqual(returned, [(raw, False)])

    def test_closing_rolls_back_first(self):
        """More important than the pooling. Several callers catch an exception
        and return a default inside a try/finally that closes the connection,
        leaving the transaction aborted -- handed to the next caller unchanged,
        that connection fails every subsequent statement with 'current
        transaction is aborted' and one bad query poisons the pool for the life
        of the process."""
        class FakePool:
            def putconn(self, conn, close=False):
                pass

        class FakeConn:
            def __init__(self):
                self.rolled_back = False

            def rollback(self):
                self.rolled_back = True

        raw = FakeConn()
        db._PooledConnection(raw, FakePool()).close()
        self.assertTrue(raw.rolled_back)

    def test_an_unrollbackable_connection_is_discarded_not_reused(self):
        returned = []

        class FakePool:
            def putconn(self, conn, close=False):
                returned.append(close)

        class BrokenConn:
            def rollback(self):
                raise RuntimeError("connection already dead")

        db._PooledConnection(BrokenConn(), FakePool()).close()
        self.assertEqual(returned, [True], "a broken connection went back into the pool")

    def test_double_close_is_safe(self):
        calls = []

        class FakePool:
            def putconn(self, conn, close=False):
                calls.append(close)

        class FakeConn:
            def rollback(self):
                pass

        c = db._PooledConnection(FakeConn(), FakePool())
        c.close()
        c.close()
        self.assertEqual(len(calls), 1, "a second close returned it to the pool twice")

    def test_the_ui_names_the_failure_kind(self):
        src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
        self.assertIn("last_db_error", src,
                      "the UI still reports an unexplained 'unavailable'")
        for kind in ("config", "driver", "connect"):
            self.assertIn(f'"{kind}"', src)


if __name__ == "__main__":
    unittest.main()
