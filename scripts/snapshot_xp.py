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


def _connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(url)


def main() -> None:
    bootstrap = fpl_tools._get_bootstrap()
    fixture_lookup = fpl_tools._build_fixture_lookup(bootstrap)
    teams_by_id = {t["id"]: t.get("short_name", t.get("name", "?")) for t in bootstrap.get("teams", [])}
    gw = fpl_tools._next_gameweek(bootstrap)

    rows = []
    for e in bootstrap.get("elements", []):
        pos = fpl_tools.POS_MAP.get(e.get("element_type"))
        if not pos:
            continue
        xp, _note = fpl_tools._player_xp(e, fixture_lookup, event=gw, risk="balanced")
        rows.append((e["id"], gw, e.get("web_name", "?"), pos, teams_by_id.get(e["team"], "?"), float(xp)))

    conn = _connect()
    try:
        with conn.cursor() as cur:
            # Safely clear any existing predictions for this specific gameweek,
            # then insert the fresh projection.
            cur.execute("DELETE FROM fpl_predictions WHERE gameweek = %s", (gw,))
            cur.executemany(
                "INSERT INTO fpl_predictions "
                "(player_id, gameweek, player_name, position, team, predicted_xp) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                rows,
            )
        conn.commit()
    finally:
        conn.close()

    print(f"Snapshot complete: {len(rows)} player forecasts written for GW {gw}.")


if __name__ == "__main__":
    main()
