"""
Tuesday Ingest: backfill actual_points for finished gameweeks whose predictions
still have NULL actuals, using the live FPL per-player stats endpoint.

Expected table schema (fpl_predictions):
  player_id INT, gameweek INT, player_name TEXT, position TEXT, team TEXT,
  predicted_xp DOUBLE PRECISION, actual_points DOUBLE PRECISION
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import psycopg2
import fpl_tools


def _connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(url)


def main() -> None:
    bootstrap = fpl_tools._get_bootstrap()
    finished_gws = {int(e["id"]) for e in bootstrap.get("events", []) if e.get("finished")}

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT gameweek FROM fpl_predictions "
                "WHERE actual_points IS NULL ORDER BY gameweek"
            )
            pending = [int(r[0]) for r in cur.fetchall()]

        updated = 0
        for gw in pending:
            if gw not in finished_gws:
                continue
            live = requests.get(f"{fpl_tools.BASE_URL}/event/{gw}/live/", timeout=30).json()
            element_points = {}
            for elem in live.get("elements", []):
                element_points[elem.get("id")] = elem.get("stats", {}).get("total_points", 0)

            with conn.cursor() as cur:
                for pid, pts in element_points.items():
                    cur.execute(
                        "UPDATE fpl_predictions SET actual_points = %s "
                        "WHERE player_id = %s AND gameweek = %s AND actual_points IS NULL",
                        (pts, pid, gw),
                    )
                    updated += cur.rowcount
        conn.commit()
    finally:
        conn.close()

    print(f"Ingest complete: {updated} actual_points rows updated.")


if __name__ == "__main__":
    main()
