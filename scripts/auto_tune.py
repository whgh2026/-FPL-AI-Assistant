"""
Wednesday Auto-Calibration: compare logged predictions against actual results and
coordinate-descend on weights.json to close the systematic RMSE / Poisson-deviance
gap (damped to a 5% max shift per parameter per run).

Safety threshold: require >= 1000 rows before adjusting, to avoid overfitting on
a tiny sample.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fpl_tools
from db import ensure_calibration_columns, get_prediction_history

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEIGHTS_PATH = os.path.join(ROOT, "weights.json")
MIN_ROWS = 1000
DAMPING = 0.05


def main() -> None:
    ensure_calibration_columns()
    rows = get_prediction_history()

    n = len(rows)
    if n < MIN_ROWS:
        print(f"Skipping calibration: {n} rows < {MIN_ROWS} threshold (avoid overfitting).")
        return

    weights = fpl_tools._load_weights()
    before = fpl_tools.evaluate_calibration(rows, weights)
    new_weights = fpl_tools.calibrate_weights(rows, weights, damping=DAMPING)
    after = fpl_tools.evaluate_calibration(rows, new_weights)

    with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(new_weights, f, indent=2)

    changed = {k: f"{weights.get(k)} -> {new_weights.get(k)}" for k in new_weights
               if round(weights.get(k, 0.0), 6) != round(new_weights.get(k, 0.0), 6)}
    print(
        f"Calibration: n={n}, RMSE {before['rmse']:.4f} -> {after['rmse']:.4f}, "
        f"deviance {before['deviance']:.4f} -> {after['deviance']:.4f}. "
        f"Tuned: {changed or 'none'}."
    )


if __name__ == "__main__":
    main()
