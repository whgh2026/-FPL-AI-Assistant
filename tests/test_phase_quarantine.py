"""Tier 3 gate: the calibrator fits a genuinely homogeneous population.

MODEL_VERSION is a hand-maintained label, so it can lag the code it
describes. Between a change to the projection arithmetic landing and the
version string being bumped there is a window in which snapshot_xp.py writes
rows carrying the NEW arithmetic under the OLD label -- and the calibrator
selects on the label alone, so it fits them as though they were homogeneous
with everything else.

ARITH_FINGERPRINT closes that window mechanically: the hash is derived from
the constants that actually decide what _player_xp_raw returns, so changing
one changes the hash and nobody has to remember to bump anything.

The v8 quarantine clause is the other half -- and it is the one place in this
whole remediation where a SQL precedence mistake would have deleted live data.
"""

import unittest

import db
import fpl_tools


class ArithFingerprintTest(unittest.TestCase):

    def test_the_fingerprint_is_a_stable_short_hash(self):
        fp = fpl_tools.ARITH_FINGERPRINT
        self.assertIsInstance(fp, str)
        self.assertEqual(len(fp), 12)
        self.assertEqual(fp, fpl_tools._arith_fingerprint(),
                         "the fingerprint is not deterministic within a process")

    def test_changing_a_projection_constant_changes_the_fingerprint(self):
        """The whole point: an arithmetic change must invalidate old rows
        whether or not anyone remembered to bump MODEL_VERSION."""
        before = fpl_tools._arith_fingerprint()
        saved = fpl_tools.BONUS_CONVEXITY
        fpl_tools.BONUS_CONVEXITY = saved + 0.1
        try:
            after = fpl_tools._arith_fingerprint()
        finally:
            fpl_tools.BONUS_CONVEXITY = saved
        self.assertNotEqual(before, after)
        self.assertEqual(before, fpl_tools._arith_fingerprint(),
                         "restoring the constant did not restore the hash")

    def test_the_model_version_is_part_of_the_hash(self):
        before = fpl_tools._arith_fingerprint()
        saved = fpl_tools.MODEL_VERSION
        fpl_tools.MODEL_VERSION = saved + "-x"
        try:
            self.assertNotEqual(before, fpl_tools._arith_fingerprint())
        finally:
            fpl_tools.MODEL_VERSION = saved


class QuarantineClauseTest(unittest.TestCase):
    """D-B. The spec's own quarantine SQL was

        WHERE model_version = 'v8-tau-correction'
          AND dc_sensitivity IS NULL OR dc_sensitivity = 0.0
          AND created_at < '...'

    AND binds tighter than OR, so that parses as

        (model_version = 'v8' AND dc_sensitivity IS NULL)
        OR (dc_sensitivity = 0.0 AND created_at < '...')

    -- the second disjunct carries NO version filter and would have DELETED
    v9 rows. This project uses a filter, never a DELETE, and the clause is
    written as a single negated conjunction so it can only ever exclude the
    rows it names.
    """

    def test_the_clause_is_fully_parenthesised(self):
        clause = db._V8_QUARANTINE
        self.assertTrue(clause.startswith("NOT ("), clause)
        self.assertTrue(clause.rstrip().endswith(")"), clause)
        self.assertIn("model_version = 'v8-tau-correction'", clause)
        self.assertIn("base_pts IS NULL", clause)

    def test_the_clause_contains_no_bare_or(self):
        """An OR anywhere in this clause reintroduces the precedence trap."""
        self.assertNotIn(" OR ", db._V8_QUARANTINE.upper())

    def test_nothing_in_the_project_deletes_prediction_rows(self):
        """The archive is the only record of what the model predicted before
        it knew the answer. It is never reconstructible."""
        import os
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run(["grep", "-rn", "DELETE FROM fpl_predictions",
                            "--include=*.py", "."],
                           cwd=root, capture_output=True, text=True)
        hits = [ln for ln in r.stdout.splitlines() if "tests/" not in ln]
        self.assertEqual(hits, [], f"a destructive delete survives: {hits}")

    def test_every_calibration_reader_applies_the_clause(self):
        """All three readers, or the quarantine leaks through whichever one
        was forgotten."""
        import inspect
        for fn in (db.get_prediction_history, db.count_checked_predictions,
                   db.prediction_accuracy_by_gw):
            self.assertIn("_V8_QUARANTINE", inspect.getsource(fn),
                          f"{fn.__name__} does not apply the quarantine")


class FingerprintColumnTest(unittest.TestCase):

    def test_the_column_is_declared_nullable(self):
        """Legacy rows carry NULL and stay readable. Their arithmetic is not
        reproducible against today's fpl_tools, but dropping them is the
        calibrator's decision to make, not the schema's."""
        import inspect
        src = inspect.getsource(db.ensure_calibration_columns)
        self.assertIn('("arith_fingerprint", "TEXT")', src)
        self.assertNotIn("arith_fingerprint\", \"TEXT NOT NULL", src)

    def test_both_writers_stamp_it(self):
        import io
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("snapshot_xp.py", "backfill_model_health.py"):
            src = io.open(os.path.join(root, "scripts", name), encoding="utf-8").read()
            self.assertIn("ARITH_FINGERPRINT", src, f"{name} does not stamp the hash")
            self.assertIn("arith_fingerprint", src)


if __name__ == "__main__":
    unittest.main()
