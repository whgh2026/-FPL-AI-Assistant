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

    def test_predictions_upsert_key_covers_model_version(self):
        """snapshot_xp.py's UPSERT targets (player_id, gameweek,
        model_version) -- (player_id, gameweek) alone would reject it with a
        genuine unique-violation on the OLD constraint, quite apart from the
        ON CONFLICT clause not matching any index."""
        preds = next(s for s in db._SCHEMA if "fpl_predictions (" in s)
        self.assertIn("UNIQUE (player_id, gameweek, model_version)", preds)


class PredictionsUpsertMigrationTest(unittest.TestCase):
    """ensure_predictions_upsert_key migrates an existing database (created
    before this) off the two-column UNIQUE and onto the three-column one the
    UPSERT in snapshot_xp.py depends on."""

    def test_drops_the_old_two_column_constraint_by_its_deterministic_name(self):
        """Postgres names a UNIQUE(...) declared inline in CREATE TABLE
        `<table>_<col>_<col>_key` when no explicit name is given -- this is
        what makes `DROP CONSTRAINT IF EXISTS` targeted rather than a guess."""
        import inspect
        src = inspect.getsource(db.ensure_predictions_upsert_key)
        self.assertIn("fpl_predictions_player_id_gameweek_key", src)
        self.assertIn("IF EXISTS", src)

    def test_creates_the_three_column_unique_index_idempotently(self):
        import inspect
        src = inspect.getsource(db.ensure_predictions_upsert_key)
        self.assertIn("player_id, gameweek, model_version", src)
        self.assertIn("IF NOT EXISTS", src)

    def test_returns_false_without_a_database(self):
        self.assertIs(db.ensure_predictions_upsert_key(), False)


class DatabaseUrlNormalisationTest(unittest.TestCase):
    """prepare_database_url: Supabase's own dashboard-provided pooler
    connection string includes `pgbouncer=true`, which psycopg2 rejects
    outright at DSN-parse time (before any network call) as an unrecognised
    URI query parameter -- a straight copy-paste into DATABASE_URL would
    otherwise break every connection this app makes."""

    def test_strips_pgbouncer_query_parameter(self):
        url = "postgresql://u:p@aws-0-eu-west-2.pooler.supabase.com:6543/postgres?pgbouncer=true"
        out = db.prepare_database_url(url)
        self.assertNotIn("pgbouncer", out)

    def test_adds_sslmode_require_when_missing(self):
        url = "postgresql://u:p@db.example.supabase.co:5432/postgres"
        out = db.prepare_database_url(url)
        self.assertIn("sslmode=require", out)

    def test_does_not_override_an_explicit_sslmode(self):
        url = "postgresql://u:p@host:5432/postgres?sslmode=verify-full"
        out = db.prepare_database_url(url)
        self.assertIn("sslmode=verify-full", out)
        self.assertNotIn("sslmode=require", out)

    def test_preserves_other_query_parameters(self):
        url = "postgresql://u:p@host:6543/postgres?pgbouncer=true&connect_timeout=5"
        out = db.prepare_database_url(url)
        self.assertIn("connect_timeout=5", out)

    def test_falsy_input_passes_through(self):
        self.assertIsNone(db.prepare_database_url(None))
        self.assertEqual(db.prepare_database_url(""), "")

    def test_malformed_input_never_raises(self):
        """urlsplit itself is lenient enough that almost nothing reaches the
        except branch -- this pins the actual contract (never raises) rather
        than one specific return value for unparseable input."""
        for garbage in ("not a url at all", 12345, object()):
            try:
                db.prepare_database_url(garbage)
            except Exception as exc:
                self.fail(f"prepare_database_url raised on {garbage!r}: {exc}")

    @unittest.skipUnless(db._PSYCOPG2, "psycopg2 not installed")
    def test_psycopg2_accepts_the_normalised_dsn_syntactically(self):
        """The regression this exists to prevent: handed pgbouncer=true
        verbatim, psycopg2 raises before attempting any network connection
        at all. An unreachable host after normalisation must fail on
        CONNECTION, never on DSN parsing."""
        import psycopg2
        url = "postgresql://u:p@127.0.0.1:1/db?pgbouncer=true"
        with self.assertRaises(psycopg2.OperationalError):
            psycopg2.connect(db.prepare_database_url(url), connect_timeout=1)


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
