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

import io
import os
import re
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


def _insert_columns():
    """Column names from backfill's own INSERT, in order.

    Indexing the row tuple by hand is how get_prediction_history came to read
    every field two columns to the left: a column was added to one half of the
    statement and the positional indices in the other half were not moved with
    it. Deriving the positions from the SQL means a future column addition
    fails here, loudly, instead of shifting an assertion onto its neighbour.
    """
    src = io.open(os.path.join(ROOT, "scripts", "backfill_model_health.py"),
                  encoding="utf-8").read()
    m = re.search(r'INSERT INTO fpl_predictions "\s*\n(.*?)VALUES \((.*?)\) ',
                  src, re.S)
    assert m, "could not find the INSERT statement"
    joined = "".join(re.findall(r'"([^"]*)"', m.group(1)))
    names = [c.strip() for c in joined.strip().lstrip("(").rstrip(") ").split(",")
             if c.strip()]
    placeholders = m.group(2).count("%s")
    assert len(names) == placeholders, (
        f"{len(names)} columns but {placeholders} placeholders -- "
        f"the INSERT is malformed")
    return {name: i for i, name in enumerate(names)}


COL = _insert_columns()


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
            self.assertEqual(row[COL["gameweek"]], GW)
            self.assertEqual(row[COL["model_version"]], fpl_tools.MODEL_VERSION)


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
            self.assertEqual(row[COL["gameweek"]], 1)
            self.assertEqual(row[COL["model_version"]], fpl_tools.MODEL_VERSION)

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


class DcSensitivityTest(unittest.TestCase):
    """dc_sensitivity was in neither the row tuple nor the INSERT, so every
    backfilled row left the column NULL. reproject() multiplies it by
    (decay - default_decay), so NULL reads as a zero gradient and
    dixon_coles_decay -- one of the four parameters the tuner fits -- could
    never move. The backfill is what refills the archive after a
    MODEL_VERSION bump, so that was the whole archive."""

    def _rows(self):
        bootstrap, fixtures = harness.load_synthetic()
        with harness.synthetic_world() as (_bs, _fx), \
             mock.patch.object(db, "load_bootstrap_snapshot", return_value=bootstrap), \
             mock.patch.object(ingest_actuals, "_fetch_played", return_value={}):
            return backfill._project_gameweek(GW, all_fixtures=fixtures)

    def test_the_column_is_written_at_all(self):
        self.assertIn("dc_sensitivity", COL,
                      "the INSERT still does not carry dc_sensitivity")

    def test_some_player_has_a_non_zero_gradient(self):
        """A per-player forward difference that comes back identically zero
        for everyone means the perturbed lookup was served from the cache --
        the exact failure _lookup_at_weights' cache clear exists to prevent."""
        rows = self._rows()
        self.assertTrue(rows)
        vals = [r[COL["dc_sensitivity"]] for r in rows]
        self.assertTrue(any(abs(v) > 1e-9 for v in vals),
                        "every dc_sensitivity came back 0.0 -- the probe is "
                        "measuring the base fit against itself")

    def test_the_probe_step_matches_the_other_writer(self):
        """Both writers feed this column into the same surrogate, so a
        different step makes the two populations incomparable."""
        import snapshot_xp
        self.assertEqual(backfill.DC_PROBE_H, snapshot_xp.DC_PROBE_H)

    def test_the_probe_is_point_in_time_like_the_base_projection(self):
        """as_of_event on both sides of the difference. Without it the probe
        is fitted on every fixture finished today while the base is fitted on
        fixtures strictly before gw, and the difference measures the leakage
        rather than the decay."""
        import inspect
        src = inspect.getsource(backfill._lookup_at_weights)
        self.assertIn("as_of_event=as_of_event", src)
        caller = inspect.getsource(backfill._project_gameweek)
        self.assertIn("_lookup_at_weights(dc_probe, bootstrap, all_fixtures, gw)", caller)

    def test_the_weights_cache_is_always_restored(self):
        """The probe swaps _WEIGHTS_CACHE for neutral weights. Leaking that
        into the rest of the process would silently re-scale every subsequent
        projection."""
        before = fpl_tools._WEIGHTS_CACHE
        self._rows()
        self.assertIs(fpl_tools._WEIGHTS_CACHE, before)
