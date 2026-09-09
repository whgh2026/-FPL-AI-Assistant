"""Historical Clean Backfill gate: scripts/backfill_model_health.py must never
substitute today's live bootstrap for a historical gameweek it has no
archived snapshot for.

The whole point of the script is that a gameweek's projection is built from
data the app actually saw AT THAT GAMEWEEK. A live-bootstrap fallback would
still produce a plausible-looking row -- today's prices, today's cumulative
season stats, today's statuses -- for a gameweek none of that applied to, and
the mistake is invisible downstream: the calibration fit just sees one more
row, quietly contaminated with information from every gameweek since.

Mirrors tests/test_snapshot_upsert.py's shape: a fake cursor/connection with
no real database behind it, and assertions on what the write path was asked
to run.
"""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import fpl_tools
import db
import ingest_actuals
import backfill_model_health as backfill
from tests import harness

GW = 4   # inside the synthetic fixture's event range (1-12) with real fixtures


class _FakeCursor:
    """Records what it was asked to run; no real database behind it."""

    def __init__(self):
        self.executed = []          # [(sql, params), ...] from plain execute()
        self.batch_calls = []       # [(sql, rows, page_size), ...] from execute_batch

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class NoLeakageTest(unittest.TestCase):
    """The core guarantee: a missing archive means "skip", never "substitute
    the live bootstrap and carry on"."""

    def test_missing_snapshot_is_skipped_not_faked(self):
        with mock.patch.object(db, "load_bootstrap_snapshot", return_value=None), \
             mock.patch.object(fpl_tools, "_get_bootstrap",
                                side_effect=AssertionError(
                                    "must not fall back to the live bootstrap")):
            rows = backfill._project_gameweek(GW, all_fixtures=[])
        self.assertIsNone(rows)

    def test_a_falsy_snapshot_is_also_skipped(self):
        """An archived row that decompresses to an empty dict must be treated
        the same as "missing", not iterated as a squad of zero players."""
        with mock.patch.object(db, "load_bootstrap_snapshot", return_value={}), \
             mock.patch.object(fpl_tools, "_get_bootstrap",
                                side_effect=AssertionError(
                                    "must not fall back to the live bootstrap")):
            rows = backfill._project_gameweek(GW, all_fixtures=[])
        self.assertIsNone(rows)

    def test_archived_snapshot_is_used_to_build_real_rows(self):
        """With a real archived snapshot for `gw`, projection must actually
        run and produce rows stamped for that gameweek and model version."""
        bootstrap, fixtures = harness.load_synthetic()
        with harness.synthetic_world() as (_bs, _fx), \
             mock.patch.object(db, "load_bootstrap_snapshot", return_value=bootstrap), \
             mock.patch.object(ingest_actuals, "_fetch_played", return_value={}):
            rows = backfill._project_gameweek(GW, all_fixtures=fixtures)
        self.assertTrue(rows, "expected rows from the synthetic snapshot")
        for row in rows:
            self.assertEqual(row[1], GW)                        # gameweek
            self.assertEqual(row[10], fpl_tools.MODEL_VERSION)   # model_version


class BackfillWriteTest(unittest.TestCase):
    def _run_main_with_fakes(self, snapshot_by_gw):
        cur = _FakeCursor()
        conn = _FakeConn(cur)

        def _fake_load_snapshot(gw, captured_date=None):
            return snapshot_by_gw.get(gw)

        def _fake_execute_batch(cursor, sql, rows, page_size=100):
            cur.batch_calls.append((sql, list(rows), page_size))

        with harness.synthetic_world() as (_bs, _fx), \
             mock.patch.object(backfill, "_connect", return_value=conn), \
             mock.patch.object(db, "run_migrations", return_value=True), \
             mock.patch.object(db, "ensure_calibration_columns", return_value=True), \
             mock.patch.object(db, "ensure_predictions_upsert_key", return_value=True), \
             mock.patch.object(db, "load_bootstrap_snapshot", side_effect=_fake_load_snapshot), \
             mock.patch.object(ingest_actuals, "_fetch_played", return_value={}), \
             mock.patch("psycopg2.extras.execute_batch", side_effect=_fake_execute_batch):
            backfill.main(["--from", "1", "--to", "2"])
        return conn, cur

    def test_no_archived_gameweeks_writes_nothing(self):
        conn, cur = self._run_main_with_fakes({1: None, 2: None})
        self.assertEqual(cur.batch_calls, [])
        self.assertEqual(conn.commits, 0)
        self.assertTrue(conn.closed)

    def test_one_archived_gameweek_writes_one_stamped_batch(self):
        bootstrap, _fixtures = harness.load_synthetic()
        _conn, cur = self._run_main_with_fakes({1: bootstrap, 2: None})
        self.assertEqual(len(cur.batch_calls), 1,
                          "gw2 has no snapshot and must not be written")
        sql, rows, page_size = cur.batch_calls[0]
        self.assertIn("ON CONFLICT (player_id, gameweek, model_version)", sql)
        self.assertIn("DO UPDATE SET", sql)
        self.assertEqual(page_size, 1000)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row[1], 1)
            self.assertEqual(row[10], fpl_tools.MODEL_VERSION)

    def test_actual_points_upsert_never_clobbers_a_checked_in_result(self):
        """A re-run (or a later ingest_actuals.py pass) must never destroy an
        actual_points value already sitting in the row -- COALESCE, not a
        plain overwrite, the same discipline snapshot_xp.py uses for the
        predicted-only columns."""
        bootstrap, _fixtures = harness.load_synthetic()
        _conn, cur = self._run_main_with_fakes({1: bootstrap, 2: None})
        sql, _rows, _page_size = cur.batch_calls[0]
        self.assertIn("actual_points = COALESCE(fpl_predictions.actual_points", sql)

    def test_no_delete_statement_survives_anywhere_in_the_write_path(self):
        bootstrap, _fixtures = harness.load_synthetic()
        _conn, cur = self._run_main_with_fakes({1: bootstrap, 2: None})
        for sql, _params in cur.executed:
            self.assertNotIn("DELETE", sql.upper())


if __name__ == "__main__":
    unittest.main()
