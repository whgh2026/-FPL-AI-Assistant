"""The calibration read path: column order in, dict keys out.

`get_prediction_history` builds its SELECT as one string and unpacks the result
by positional index in a second place. Nothing ties the two together, so adding
a column to the front of the SELECT silently re-points every key -- which is
exactly what happened when `player_id, gameweek` were prepended so
`auto_tune._split` could stratify on them. Every field shifted two columns
left, `predicted_xp` came back holding a player id, and the tuner fitted that
against a gameweek number. Nothing raised.

These tests pin the MAPPING rather than any value, so the next column added to
the SELECT fails here instead of in production.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


# One distinct, non-zero, non-None value per column, in SELECT order. Non-zero
# matters: several keys are read through `or 0.0`, which would mask a shift
# that happened to land on another zero.
COLUMNS = [
    ("player_id",          101),
    ("gameweek",           7),
    ("predicted_xp",       5.5),
    ("actual_points",      9.0),
    ("base_pts",           4.25),
    ("cameo_mass",         0.3),
    ("rotation_variance",  0.45),
    ("dc_sensitivity",     -1.75),
    ("raw_total",          6.5),
    ("xp_cameo",           1.25),
    ("ep_w",               0.8),
    ("ep_term",            2.5),
]


def _fake_db(row):
    class FakeCursor:
        def __init__(self):
            self.sql = None
            self.params = None

        def execute(self, sql, params=None):
            self.sql = sql
            self.params = params

        def fetchall(self):
            return [row]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    cursor = FakeCursor()

    class FakeConn:
        def cursor(self):
            return cursor

        def close(self):
            pass

    return FakeConn(), cursor


class RowMappingTest(unittest.TestCase):

    def setUp(self):
        self._saved = db.get_db_connection

    def tearDown(self):
        db.get_db_connection = self._saved

    def _one_row(self, row):
        conn, cursor = _fake_db(row)
        db.get_db_connection = lambda: conn
        rows = db.get_prediction_history("v-test")
        self.assertEqual(len(rows), 1, "the stubbed row did not survive the read")
        return rows[0], cursor

    def test_every_key_holds_its_own_column(self):
        """The whole defect in one assertion: a two-column shift puts a player
        id in predicted_xp and a gameweek in actual_points."""
        out, _ = self._one_row(tuple(v for _, v in COLUMNS))
        for name, value in COLUMNS:
            self.assertEqual(out[name], value,
                             f"{name} does not hold its own column -- the "
                             f"SELECT and the unpacking have drifted apart")

    def test_the_select_order_matches_the_unpacking_order(self):
        """Guards the other direction: the dict is right only because the SQL
        lists the columns in this order. Reordering the SELECT without
        reordering the indices reintroduces the shift."""
        _, cursor = self._one_row(tuple(v for _, v in COLUMNS))
        select = cursor.sql.split(" FROM ")[0].replace("SELECT ", "")
        listed = [c.strip() for c in select.split(",")]
        self.assertEqual(listed, [name for name, _ in COLUMNS])

    def test_player_id_and_gameweek_are_actually_emitted(self):
        """auto_tune._split stratifies on these. Selecting them without
        emitting them made its sort key (0, 0) for every row, and a stable
        sort on a constant key reorders nothing -- the split looked
        deterministic because it was doing nothing."""
        out, _ = self._one_row(tuple(v for _, v in COLUMNS))
        self.assertIn("player_id", out)
        self.assertIn("gameweek", out)
        self.assertEqual(out["player_id"], 101)
        self.assertEqual(out["gameweek"], 7)

    def test_a_null_base_pts_falls_back_to_predicted_xp(self):
        """Backfilled rows carry no base_pts. The fallback has to reach the
        predicted_xp COLUMN, not whatever index it used to sit at."""
        row = list(v for _, v in COLUMNS)
        row[4] = None                      # base_pts
        out, _ = self._one_row(tuple(row))
        self.assertEqual(out["base_pts"], 5.5)

    def test_raw_total_and_xp_cameo_stay_none_rather_than_zero(self):
        """reproject() distinguishes "no clamp input recorded" from "the cameo
        projection really was zero"; coercing to 0.0 makes the clamp bind on
        every legacy row."""
        row = list(v for _, v in COLUMNS)
        row[8] = None                      # raw_total
        row[9] = None                      # xp_cameo
        out, _ = self._one_row(tuple(row))
        self.assertIsNone(out["raw_total"])
        self.assertIsNone(out["xp_cameo"])


if __name__ == "__main__":
    unittest.main()
