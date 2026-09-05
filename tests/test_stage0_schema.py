"""Stage 0 gate: schema completeness and graceful degradation.

`fpl_predictions` and `manager_transfer_ledger` previously had no CREATE
statement anywhere in the repository. The snapshot job would fail on a fresh
database with UndefinedTable, and the ledger backfill's ON CONFLICT DO NOTHING
had no unique constraint to act on -- so a re-run duplicated every row instead
of no-oping.

These tests need no database: they assert the schema covers what the code reads
and writes, and that the writers degrade to False rather than raising when the
database is unreachable.
"""

import gzip
import json
import unittest

import db


class SchemaCompletenessTest(unittest.TestCase):
    def test_previously_missing_tables_are_created(self):
        sql = "\n".join(db._SCHEMA)
        for table in ("fpl_predictions", "manager_transfer_ledger", "bootstrap_snapshots"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql,
                          f"{table} has no CREATE statement")

    def test_every_statement_is_idempotent(self):
        """Migrations run on every boot; none may fail on a second pass."""
        for stmt in db._SCHEMA:
            head = " ".join(stmt.split())[:60]
            self.assertTrue("IF NOT EXISTS" in stmt,
                            f"non-idempotent migration: {head}")

    def test_ledger_has_the_unique_its_upsert_depends_on(self):
        """get_or_backfill_manager_history relies on ON CONFLICT DO NOTHING."""
        ledger = next(s for s in db._SCHEMA if "manager_transfer_ledger (" in s)
        self.assertIn("UNIQUE (manager_id, player_id, gameweek, direction)", ledger)

    def test_predictions_cover_every_column_the_code_touches(self):
        """Columns written by snapshot_xp / read by get_prediction_history."""
        preds = next(s for s in db._SCHEMA if "fpl_predictions (" in s)
        for col in ("player_id", "gameweek", "player_name", "position", "team",
                    "predicted_xp", "actual_points", "base_pts", "cameo_mass",
                    "rotation_variance", "dc_sensitivity", "minutes_floor"):
            self.assertIn(col, preds, f"fpl_predictions missing {col}")

    def test_snapshot_table_dedupes_per_day(self):
        """save_bootstrap_snapshot upserts on (gameweek, captured_date)."""
        snap = next(s for s in db._SCHEMA if "bootstrap_snapshots (" in s)
        self.assertIn("UNIQUE (gameweek, captured_date)", snap)


class GracefulDegradationTest(unittest.TestCase):
    """With no DATABASE_URL configured, writers return False; they never raise.

    This is current behaviour, not desired behaviour: Stage 8 replaces silent
    falsy returns with a structured signal, because today a dead database is
    indistinguishable from an empty one. These tests pin the contract so that
    change is a deliberate, visible break rather than an accident.
    """

    def test_run_migrations_returns_false_without_db(self):
        self.assertIs(db.run_migrations(), False)

    def test_snapshot_writer_returns_false_without_db(self):
        self.assertIs(db.save_bootstrap_snapshot({"elements": []}, 4), False)

    def test_snapshot_reader_returns_none_without_db(self):
        self.assertIsNone(db.load_bootstrap_snapshot(4))


class PayloadTest(unittest.TestCase):
    def test_gzip_round_trip_preserves_the_bootstrap(self):
        """The archive stores gzipped JSON; replay must recover it exactly."""
        bootstrap = {
            "elements": [{"id": 1, "web_name": "Test", "now_cost": 55}],
            "teams": [{"id": 1, "short_name": "ARS", "strength_overall_home": 1350}],
            "events": [{"id": 4, "is_next": True}],
        }
        raw = json.dumps(bootstrap, separators=(",", ":")).encode("utf-8")
        recovered = json.loads(gzip.decompress(gzip.compress(raw)).decode("utf-8"))
        self.assertEqual(recovered, bootstrap)

    def test_compression_is_worth_it(self):
        """Sanity-check the storage assumption behind nightly archiving."""
        bootstrap = {"elements": [
            {"id": i, "web_name": f"Player{i}", "now_cost": 40 + i % 100,
             "minutes": i * 7, "expected_goals_per_90": 0.31}
            for i in range(700)
        ]}
        raw = json.dumps(bootstrap, separators=(",", ":")).encode("utf-8")
        packed = gzip.compress(raw)
        self.assertLess(len(packed), len(raw) / 3,
                        "compression ratio worse than assumed for the storage estimate")


if __name__ == "__main__":
    unittest.main()
