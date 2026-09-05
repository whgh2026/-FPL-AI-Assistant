"""
Friday Snapshot: log the upcoming gameweek's 1-GW xP forecast for all active players.

Expected table schema (fpl_predictions):
  player_id INT, gameweek INT, player_name TEXT, position TEXT, team TEXT,
  predicted_xp DOUBLE PRECISION, actual_points DOUBLE PRECISION
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
import fpl_tools
import db


def _connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(url, connect_timeout=10)


def main() -> None:
    # Schema first. This script writes base_pts/cameo_mass/rotation_variance/
    # dc_sensitivity/minutes_floor, but ensure_calibration_columns() was only
    # ever called from auto_tune.py -- so on a fresh database the Friday
    # snapshot failed with UndefinedColumn, and the columns only appeared the
    # following Wednesday. The migration belongs with the writer.
    db.run_migrations()
    db.ensure_calibration_columns()

    bootstrap = fpl_tools._get_bootstrap()
    fixture_lookup = fpl_tools._build_fixture_lookup(bootstrap)
    teams_by_id = {t["id"]: t.get("short_name", t.get("name", "?")) for t in bootstrap.get("teams", [])}
    gw = fpl_tools._next_gameweek(bootstrap)

    # Neutral-parameter weights for the base projection (before tunable penalties).
    neutral = dict(fpl_tools._load_weights())
    neutral.update({"global_xP_modifier": 1.0, "autosub_ref": 0.0,
                    "rotation_convexity": 0.0, "dixon_coles_decay": fpl_tools.DIXON_COLES_DECAY_DEFAULT})
    _orig_cache = fpl_tools._WEIGHTS_CACHE

    rows = []
    for e in bootstrap.get("elements", []):
        pos = fpl_tools.POS_MAP.get(e.get("element_type"))
        if not pos:
            continue
        xp, _note = fpl_tools._player_xp(e, fixture_lookup, event=gw, risk="balanced")
        p0, pc, pf = fpl_tools._minute_distribution(e, e.get("status", "a"))
        cameo_mass = pc
        rotation_variance = p0 * (1.0 - p0) + pc * (1.0 - pc)
        minutes_floor = fpl_tools._expected_playing_fraction(e, e.get("status", "a"))
        # Base projection at neutral parameters (independent of the tunable weights).
        fpl_tools._WEIGHTS_CACHE = neutral
        try:
            base_xp, _ = fpl_tools._player_xp(e, fixture_lookup, event=gw, risk="balanced")
        finally:
            fpl_tools._WEIGHTS_CACHE = _orig_cache
        rows.append((e["id"], gw, e.get("web_name", "?"), pos, teams_by_id.get(e["team"], "?"),
                     float(xp), float(base_xp), cameo_mass, rotation_variance, 0.0, minutes_floor))

    conn = _connect()
    try:
        with conn.cursor() as cur:
            # Safely clear any existing predictions for this specific gameweek,
            # then insert the fresh projection (with calibration features).
            cur.execute("DELETE FROM fpl_predictions WHERE gameweek = %s", (gw,))
            cur.executemany(
                "INSERT INTO fpl_predictions "
                "(player_id, gameweek, player_name, position, team, predicted_xp, "
                "base_pts, cameo_mass, rotation_variance, dc_sensitivity, minutes_floor) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )
        conn.commit()
    finally:
        conn.close()

    print(f"Snapshot complete: {len(rows)} player forecasts written for GW {gw}.")


if __name__ == "__main__":
    main()
