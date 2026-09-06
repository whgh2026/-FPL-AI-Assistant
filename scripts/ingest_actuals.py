"""
Tuesday Ingest: backfill actual_points for finished gameweeks whose predictions
still have NULL actuals, using the live FPL per-player stats endpoint.

Three defects fixed here:

1. Whole-gameweek rollback. conn.commit() sat outside the per-gameweek loop but
   inside try/finally: conn.close(). A requests failure on gameweek n+1 closed
   the connection uncommitted, discarding gameweek n's ingested actuals with no
   way to recover until the next scheduled run. Now each gameweek commits on its
   own, and a failure on one leaves the others intact.

2. Non-players dominating the training set. Every element in the live feed was
   written, including the ~450 per gameweek who did not appear. The calibration
   fit was then majority (base ~= 0.2, actual = 0) rows, dragging
   global_xP_modifier down for reasons unrelated to whether the model prices
   real starters correctly. Only players who actually featured are ingested.

3. ~700 round-trips per gameweek, one UPDATE per player, replaced with a single
   batched statement.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import psycopg2

import fpl_tools
import db


def _connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(url, connect_timeout=10)


def _fetch_played(gw):
    """{player_id: total_points} for players who actually featured in `gw`.

    Minutes > 0 is the filter: a player who did not appear carries no
    information about projection accuracy, only about the minutes model, which
    is evaluated separately.
    """
    live = requests.get(f"{fpl_tools.BASE_URL}/event/{gw}/live/", timeout=30).json()
    played = {}
    for elem in live.get("elements", []):
        stats = elem.get("stats", {}) or {}
        if (stats.get("minutes") or 0) > 0:
            played[elem.get("id")] = stats.get("total_points", 0)
    return played


def main() -> None:
    db.run_migrations()

    bootstrap = fpl_tools._get_bootstrap()
    finished_gws = {int(e["id"]) for e in bootstrap.get("events", []) if e.get("finished")}

    conn = _connect()
    updated = 0
    failures = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT gameweek FROM fpl_predictions "
                "WHERE actual_points IS NULL ORDER BY gameweek"
            )
            pending = [int(r[0]) for r in cur.fetchall()]

        for gw in pending:
            if gw not in finished_gws:
                continue
            # Each gameweek is its own transaction: one bad fetch must not
            # discard gameweeks already ingested in this run.
            try:
                played = _fetch_played(gw)
                if not played:
                    continue
                # One statement per gameweek: unnest two parallel arrays into a
                # join source, rather than ~700 individual UPDATE round-trips.
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE fpl_predictions AS p SET actual_points = v.pts "
                        "FROM (SELECT unnest(%s::int[]) AS pid, "
                        "             unnest(%s::double precision[]) AS pts) AS v "
                        "WHERE p.player_id = v.pid AND p.gameweek = %s "
                        "AND p.actual_points IS NULL",
                        (list(played.keys()), [float(v) for v in played.values()], gw),
                    )
                    rows = cur.rowcount
                conn.commit()
                updated += rows
                print(f"  GW{gw}: {rows} rows from {len(played)} players who featured")
            except Exception as exc:
                conn.rollback()
                failures.append((gw, str(exc)[:200]))
                print(f"  GW{gw}: FAILED ({type(exc).__name__}) - other gameweeks unaffected")
    finally:
        conn.close()

    print(f"Ingest complete: {updated} actual_points rows updated.")
    if failures:
        for gw, msg in failures:
            print(f"  failed GW{gw}: {msg}", file=sys.stderr)
        # Non-zero exit so a scheduled run surfaces the failure instead of
        # reporting success while silently skipping gameweeks.
        sys.exit(1)


if __name__ == "__main__":
    main()
