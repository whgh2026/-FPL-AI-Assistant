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
import db
from db import ensure_calibration_columns, get_prediction_history

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEIGHTS_PATH = os.path.join(ROOT, "weights.json")

# Raised from 1000. Rows now carry a model_version and only the current one is
# fitted, so the archive restarts from zero after Stage 4. With the
# active-player filter added in Stage 0 that is ~280 rows per gameweek, so 5000
# is roughly 18 gameweeks -- NOT the ~7 that the unfiltered count would suggest.
# The UI must derive its "recalibrating at N" copy from the live row count
# rather than hardcoding a gameweek, because this figure moves whenever the
# filter or the version boundary does.
MIN_ROWS = 5000
DAMPING = 0.05

# Stage 8 repairs the calibration loop itself: the damping caps movement at
# +/-0.5% per run (~53 runs to move a weight 30%, against a 38-gameweek season),
# dc_sensitivity is written as a literal 0.0 so dixon_coles_decay's gradient is
# identically zero, the surrogate does not match production, and a negative
# prediction blows up the Poisson deviance term. Running coordinate descent
# before those are fixed drifts the weights on noise, so the job refuses to
# write unless explicitly enabled.
ENABLED = os.environ.get("FPL_AUTOTUNE_ENABLED", "0") == "1"


def main() -> None:
    ensure_calibration_columns()
    rows = get_prediction_history(model_version=fpl_tools.MODEL_VERSION)

    n = len(rows)
    print(f"Calibration rows for {fpl_tools.MODEL_VERSION}: {n} (threshold {MIN_ROWS})")

    if not ENABLED:
        print("Auto-tune is disabled pending the Stage 8 calibration repairs "
              "(damping, dc_sensitivity gradient, surrogate mismatch, loss floor). "
              "Set FPL_AUTOTUNE_ENABLED=1 to override.")
        return

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
