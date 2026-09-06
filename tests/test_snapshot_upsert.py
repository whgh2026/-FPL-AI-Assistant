"""Stage 8 infra gate: snapshot_xp.py writes via a bulk UPSERT, never the
DELETE-then-INSERT it replaces.

The DELETE used to destroy actual_points too: ingest_actuals.py backfills that
column days after the Friday snapshot, so a manual re-run of this script (or a
MODEL_VERSION bump for a gameweek already snapshotted) silently wiped out
results that had already been checked. These tests mock the database layer --
no live Postgres is available in CI or this sandbox -- and assert the write
path's shape: one execute_batch call, an ON CONFLICT targeting
(player_id, gameweek, model_version), page_size=1000, and no DELETE anywhere.
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
import snapshot_xp
from tests import harness


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
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class SnapshotUpsertTest(unittest.TestCase):
    def _run_main_with_fakes(self):
        cur = _FakeCursor()
        conn = _FakeConn(cur)
        batch_mock = mock.MagicMock()

        def _fake_execute_batch(cursor, sql, rows, page_size=100):
            cur.batch_calls.append((sql, list(rows), page_size))
            batch_mock(cursor, sql, rows, page_size=page_size)

        with harness.synthetic_world() as (_bs, _fx), \
             mock.patch.object(snapshot_xp, "_connect", return_value=conn), \
             mock.patch.object(db, "run_migrations", return_value=True), \
             mock.patch.object(db, "ensure_calibration_columns", return_value=True), \
             mock.patch.object(db, "ensure_predictions_upsert_key", return_value=True), \
             mock.patch("psycopg2.extras.execute_batch", side_effect=_fake_execute_batch):
            snapshot_xp.main()
        return conn, cur

    def test_writes_via_execute_batch_not_executemany_loop(self):
        conn, cur = self._run_main_with_fakes()
        self.assertEqual(len(cur.batch_calls), 1, "expected exactly one execute_batch call")
        self.assertTrue(conn.committed)
        self.assertTrue(conn.closed)

    def test_upsert_targets_the_three_column_conflict_key(self):
        _conn, cur = self._run_main_with_fakes()
        sql, rows, page_size = cur.batch_calls[0]
        self.assertIn("ON CONFLICT (player_id, gameweek, model_version)", sql)
        self.assertIn("DO UPDATE SET", sql)
        self.assertEqual(page_size, 1000)
        self.assertTrue(rows, "expected at least one row from the synthetic fixture")

    def test_actual_points_is_never_overwritten_by_the_upsert(self):
        """The whole point: a re-snapshot must not clobber a result that
        ingest_actuals.py already checked in."""
        _conn, cur = self._run_main_with_fakes()
        sql, _rows, _page_size = cur.batch_calls[0]
        self.assertNotIn("actual_points", sql)

    def test_no_delete_statement_survives_anywhere_in_the_write_path(self):
        _conn, cur = self._run_main_with_fakes()
        for sql, _params in cur.executed:
            self.assertNotIn("DELETE", sql.upper())
        for sql, _rows, _page_size in cur.batch_calls:
            self.assertNotIn("DELETE", sql.upper())

    def test_runs_the_upsert_key_migration_alongside_the_others(self):
        with harness.synthetic_world() as (_bs, _fx), \
             mock.patch.object(snapshot_xp, "_connect", return_value=_FakeConn(_FakeCursor())), \
             mock.patch.object(db, "run_migrations", return_value=True) as run_mig, \
             mock.patch.object(db, "ensure_calibration_columns", return_value=True) as ensure_cal, \
             mock.patch.object(db, "ensure_predictions_upsert_key", return_value=True) as ensure_key, \
             mock.patch("psycopg2.extras.execute_batch"):
            snapshot_xp.main()
        run_mig.assert_called_once()
        ensure_cal.assert_called_once()
        ensure_key.assert_called_once()


if __name__ == "__main__":
    unittest.main()
