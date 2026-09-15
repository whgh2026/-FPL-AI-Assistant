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
DAMPING = fpl_tools.CALIBRATION_DAMPING

# The four repairs this gate was originally waiting on have landed (damping
# raised to 0.25 and the production value now under test, dc_sensitivity
# measured by forward difference instead of written as a literal 0.0, the
# surrogate matched to _player_xp_raw including the cameo clamp and the
# ep_next blend, and the deviance term floored at PRED_FLOOR) -- but this job
# writes weights.json straight into the repo and auto-commits it (see
# .github/workflows/fpl_logger.yml), so it defaults OFF regardless: a
# misconfigured env, a bad rerun, or a future caller that invokes main()
# without checking ENABLED first must never be one missing flag away from
# silently rewriting production weights. Requires an explicit
# FPL_AUTOTUNE_ENABLED=1 to run.
ENABLED = os.environ.get("FPL_AUTOTUNE_ENABLED", "0") == "1"

HOLDOUT_FRACTION = 0.25


def _split(rows):
    """Deterministic train/holdout split.

    Strided rather than random so a rerun on the same archive reaches the same
    verdict -- a tuner that writes different weights each time it is run is not
    measuring anything. Striding by player order also avoids splitting on
    gameweek, which would put a whole week's shared fixture conditions entirely
    on one side.
    """
    step = int(1 / HOLDOUT_FRACTION)
    # Stratify WITHIN each gameweek. A global stride over a globally-sorted
    # list carries its phase across gameweek boundaries, so an uneven player
    # count in one week shifts which slice of the next week lands in holdout --
    # and since rows are ordered by player_id, that slice is correlated with
    # position and price. Restarting the stride per gameweek gives every week
    # the same share at the same phase.
    #
    # This also depends on get_prediction_history actually EMITTING player_id
    # and gameweek. It did not until recently: the keys were absent, .get()
    # returned 0 for every row, and a stable sort on a constant key is a no-op
    # -- the split looked deterministic because it was doing nothing at all.
    by_gw = {}
    for r in rows:
        by_gw.setdefault(r.get("gameweek", 0), []).append(r)
    train, holdout = [], []
    for gw in sorted(by_gw):
        gw_rows = sorted(by_gw[gw], key=lambda r: r.get("player_id", 0))
        for i, r in enumerate(gw_rows):
            (holdout if i % step == 0 else train).append(r)
    return train, holdout


def _blended(metrics):
    return metrics["rmse"] + 0.3 * metrics["deviance"]


def main() -> None:
    ensure_calibration_columns()
    # Filter on the arithmetic fingerprint as well as the label. MODEL_VERSION
    # is hand-maintained and can lag the code it describes, so rows written in
    # that window carry the NEW arithmetic under the OLD label -- fitting
    # across them mixes two different functions under one name, which is
    # exactly what _arith_fingerprint() exists to prevent. Rows written before
    # the column existed carry NULL and are correctly excluded.
    rows = get_prediction_history(
        model_version=fpl_tools.MODEL_VERSION,
        arith_fingerprint=fpl_tools.ARITH_FINGERPRINT,
    )

    n = len(rows)
    print(f"Calibration rows for {fpl_tools.MODEL_VERSION} "
          f"[{fpl_tools.ARITH_FINGERPRINT}]: {n} (threshold {MIN_ROWS})")

    if not ENABLED:
        print("Auto-tune is disabled (FPL_AUTOTUNE_ENABLED=0). No weights written.")
        return

    if n < MIN_ROWS:
        print(f"Skipping calibration: {n} rows < {MIN_ROWS} threshold (avoid overfitting).")
        return

    weights = fpl_tools._load_weights()
    train, holdout = _split(rows)

    # Fit on train only. Fitting on everything and reporting the improvement on
    # the same rows measures how well coordinate descent can memorise an
    # archive, which it can always do, and says nothing about next Saturday.
    new_weights = fpl_tools.calibrate_weights(train, weights, damping=DAMPING)

    before_h = fpl_tools.evaluate_calibration(holdout, weights)
    after_h = fpl_tools.evaluate_calibration(holdout, new_weights)
    before_t = fpl_tools.evaluate_calibration(train, weights)
    after_t = fpl_tools.evaluate_calibration(train, new_weights)

    changed = {k: f"{weights.get(k)} -> {new_weights.get(k)}" for k in new_weights
               if round(weights.get(k, 0.0), 6) != round(new_weights.get(k, 0.0), 6)}

    print(f"Train    (n={after_t['n']}): RMSE {before_t['rmse']:.4f} -> {after_t['rmse']:.4f}, "
          f"deviance {before_t['deviance']:.4f} -> {after_t['deviance']:.4f}")
    print(f"Held out (n={after_h['n']}): RMSE {before_h['rmse']:.4f} -> {after_h['rmse']:.4f}, "
          f"deviance {before_h['deviance']:.4f} -> {after_h['deviance']:.4f}")

    if _blended(after_h) > _blended(before_h):
        # The old job wrote unconditionally. Damped coordinate descent picks each
        # coordinate's step against the metric as it stood at that coordinate, so
        # the combined damped move is not guaranteed to be an improvement even on
        # the training rows -- and the point of the holdout is to catch the case
        # where it is an improvement on train and a regression everywhere else.
        print(f"REJECTED: held-out metric worsened "
              f"({_blended(before_h):.4f} -> {_blended(after_h):.4f}). "
              f"weights.json unchanged. Proposed: {changed or 'none'}.")
        return

    with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(new_weights, f, indent=2)

    print(f"Accepted: held-out metric {_blended(before_h):.4f} -> {_blended(after_h):.4f}. "
          f"Tuned: {changed or 'none'}.")


if __name__ == "__main__":
    main()
