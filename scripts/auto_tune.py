"""
Wednesday Auto-Tuner: compare logged predictions against actual results and nudge
weights.json to close the systematic gap (damped to a 3% max shift per run).

Safety threshold: require >= 1000 rows before adjusting, to avoid overfitting on
a tiny sample.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEIGHTS_PATH = os.path.join(ROOT, "weights.json")
DEFAULT_WEIGHTS = {"global_xP_modifier": 1.0, "home_advantage": 1.0, "clean_sheet_confidence": 1.0}
MIN_ROWS = 1000
DAMPING = 0.03
MIN_MULTIPLIER = 0.5
MAX_MULTIPLIER = 2.0


def _connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(url)


def _load_weights() -> dict:
    try:
        with open(WEIGHTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: float(data.get(k, v)) for k, v in DEFAULT_WEIGHTS.items()}
    except Exception:
        pass
    return dict(DEFAULT_WEIGHTS)


def main() -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT predicted_xp, actual_points FROM fpl_predictions "
                "WHERE actual_points IS NOT NULL"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    n = len(rows)
    if n < MIN_ROWS:
        print(f"Skipping auto-tune: {n} rows < {MIN_ROWS} threshold (avoid overfitting).")
        return

    total_pred = sum(float(r[0]) for r in rows)
    total_actual = sum(float(r[1]) for r in rows)
    mae = sum(abs(float(r[0]) - float(r[1])) for r in rows) / n

    # bias > 0 => over-predicting (predicted too high); bias < 0 => under-predicting.
    bias = (total_pred - total_actual) / total_actual if total_actual else 0.0
    adjustment = max(-DAMPING, min(DAMPING, -bias))

    weights = _load_weights()
    old = weights["global_xP_modifier"]
    new = max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, round(old * (1.0 + adjustment), 4)))
    weights["global_xP_modifier"] = new

    with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(weights, f, indent=2)

    print(
        f"Auto-tune: n={n}, MAE={mae:.3f}, bias={bias:+.3%}, "
        f"global_xP_modifier {old:.4f} -> {new:.4f} (adjustment {adjustment:+.4f}, damped to 3%)."
    )


if __name__ == "__main__":
    main()
