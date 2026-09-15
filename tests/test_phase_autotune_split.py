"""The tuner's train/holdout split, and the population it fits.

Two defects met here. `_split` sorted on `gameweek`/`player_id` keys that
`get_prediction_history` selected but never emitted, so the sort key was
(0, 0) for every row and a stable sort reordered nothing -- the "deterministic
split" was deterministic only in the sense that doing nothing is repeatable.
And `main()` selected on MODEL_VERSION alone, which is the hand-maintained
label the arithmetic fingerprint exists to backstop.
"""
import ast
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import auto_tune
import fpl_tools


def _rows(counts):
    """{gameweek: n_players} -> rows, player ids deliberately not contiguous
    across gameweeks so a global stride would visibly drift."""
    out = []
    for gw, n in counts.items():
        for i in range(n):
            out.append({"gameweek": gw, "player_id": gw * 1000 + i,
                        "predicted_xp": 1.0 + i, "actual_points": 2.0})
    return out


class StratifiedSplitTest(unittest.TestCase):

    def test_every_gameweek_contributes_its_own_share(self):
        """The property a global stride cannot guarantee: with uneven player
        counts the phase carries over, and a gameweek can be over- or
        under-sampled purely because of how many rows the previous one had."""
        step = int(1 / auto_tune.HOLDOUT_FRACTION)
        counts = {5: 17, 6: 23, 7: 11, 8: 30}
        train, holdout = auto_tune._split(_rows(counts))
        for gw, n in counts.items():
            got = len([r for r in holdout if r["gameweek"] == gw])
            self.assertEqual(got, -(-n // step),     # ceil(n / step)
                             f"GW{gw} contributed {got} holdout rows, not "
                             f"{-(-n // step)} -- the stride drifted")

    def test_no_row_is_in_both_halves_and_none_is_lost(self):
        rows = _rows({5: 17, 6: 23, 7: 11})
        train, holdout = auto_tune._split(rows)
        ids_t = {r["player_id"] for r in train}
        ids_h = {r["player_id"] for r in holdout}
        self.assertEqual(ids_t & ids_h, set())
        self.assertEqual(len(ids_t | ids_h), len(rows))

    def test_the_split_is_reproducible_and_input_order_independent(self):
        """A tuner that reaches a different verdict on a re-run is not
        measuring anything. Shuffling the input must not move the split --
        which is what the sort is for, and what it could not do while the
        keys were missing."""
        import random
        rows = _rows({5: 17, 6: 23, 7: 11})
        a_train, a_hold = auto_tune._split(list(rows))
        shuffled = list(rows)
        random.Random(11).shuffle(shuffled)
        b_train, b_hold = auto_tune._split(shuffled)
        self.assertEqual([r["player_id"] for r in a_hold],
                         [r["player_id"] for r in b_hold])
        self.assertEqual([r["player_id"] for r in a_train],
                         [r["player_id"] for r in b_train])

    def test_rows_without_the_keys_still_split_rather_than_raising(self):
        """Legacy callers and hand-built rows land in one bucket keyed 0.
        Degraded, but it must not crash the Wednesday job."""
        rows = [{"predicted_xp": 1.0, "actual_points": 2.0} for _ in range(8)]
        train, holdout = auto_tune._split(rows)
        self.assertEqual(len(train) + len(holdout), 8)


class FingerprintSelectionTest(unittest.TestCase):

    def test_main_passes_the_fingerprint_to_get_prediction_history(self):
        """Parsed rather than grepped: a comment mentioning the argument reads
        identically to a substring search."""
        src = io.open(os.path.join(ROOT, "scripts", "auto_tune.py"),
                      encoding="utf-8").read()
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "get_prediction_history"]
        self.assertTrue(calls, "auto_tune no longer calls get_prediction_history")
        for call in calls:
            kwargs = {k.arg for k in call.keywords}
            self.assertIn("model_version", kwargs)
            self.assertIn("arith_fingerprint", kwargs,
                          "the tuner still fits on the hand-maintained label "
                          "alone, which is the window the fingerprint closes")

    def test_the_fingerprint_it_passes_is_the_live_one(self):
        self.assertEqual(fpl_tools.ARITH_FINGERPRINT,
                         fpl_tools._arith_fingerprint())


if __name__ == "__main__":
    unittest.main()
