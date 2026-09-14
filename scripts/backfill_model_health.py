"""
Historical Clean Backfill: retroactively project every ARCHIVED gameweek under
the current model version and write it straight into `fpl_predictions`,
paired with its real result.

Why this exists: `docs/FORENSIC_AUDIT_REPORT.md` section 8 notes that the
calibration archive restarts empty on every `MODEL_VERSION` bump, and at
~280 filtered rows per gameweek against `MIN_ROWS = 5000` the auto-tuner does
not fire again for roughly 18 real gameweeks. This script closes that gap in
one run by replaying the gameweeks the app has already archived (via
`scripts/archive_bootstrap.py`) through today's projection code, rather than
waiting for the season to produce fresh rows one week at a time.

No leakage, by construction: every gameweek is read STRICTLY via
`db.load_bootstrap_snapshot(gw)` -- the bootstrap as the app actually saw it
at that gameweek's kickoff -- and a gameweek with no archived snapshot is
SKIPPED, never patched over with today's live bootstrap. Today's player
prices, cumulative season stats and statuses are not what the model saw at
gameweek `gw`; treating "missing snapshot" as "use live data instead" would
backfill fabricated history that LOOKS like a clean per-gameweek row but
silently mixes in information from every gameweek since.

The one live call in the whole script is `fpl_tools._get_all_fixtures()`,
fetched ONCE and threaded through every gameweek via `_build_fixture_lookup`'s
`fixtures_override`. That is deliberate and does not leak: it is fetching the
unfiltered fixture SCHEDULE, not any player's stats, and `_build_fixture_lookup`
defaults to `_get_fixtures()` (`/fixtures/?future=1`), which excludes finished
fixtures -- so building a lookup for a gameweek that has already been played
would otherwise come back empty for that gameweek entirely.

Team ratings are point-in-time too, via `as_of_event=gw`: the Dixon-Coles fit
behind each gameweek's projection sees only fixtures from gameweeks strictly
before it, reproducing what the live Friday snapshot could see standing in
front of that gameweek. This previously did NOT hold -- ratings came from the
shared live cache, fitted on every fixture finished today -- so a row
backfilled for GW5 was projected by a model that already knew how GW5 and
every later gameweek turned out.

What this still measures is "how the CURRENT model code projects that
gameweek", not "how the model actually deployed that week projected it": the
weights and the projection logic are today's. That is the intended meaning for
a calibration archive -- fitting today's weights needs today's model evaluated
on honest point-in-time inputs -- and it is a versioning question, not a
leakage one, which MODEL_VERSION already stamps on every row.

    python scripts/backfill_model_health.py                # every archived GW
    python scripts/backfill_model_health.py --from 5 --to 12
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fpl_tools
import db
import ingest_actuals

MAX_GAMEWEEKS = 38


def _connect():
    # Imported here, not at module scope: this module's projection logic
    # (_project_gameweek) is worth testing without a database driver present,
    # same reasoning as snapshot_xp.py's _connect().
    import psycopg2
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(db.prepare_database_url(url), connect_timeout=10)


def _project_gameweek(gw, all_fixtures):
    """Rows for `fpl_predictions` for one historical gameweek, or None if it
    cannot be built honestly.

    Returns None (never a fabricated bootstrap) when `gw` has no archived
    snapshot -- the no-leakage guarantee this whole script exists for. Every
    call below is passed the ARCHIVED `bootstrap` explicitly, so nothing here
    reaches for `fpl_tools._get_bootstrap()` (today's live data).
    """
    bootstrap = db.load_bootstrap_snapshot(gw)
    if not bootstrap:
        return None

    teams_by_id = {t["id"]: t.get("short_name", t.get("name", "?"))
                   for t in bootstrap.get("teams", [])}
    # as_of_event=gw is what closes the retro-projection leak. Without it the
    # Dixon-Coles ratings came from the shared live cache -- fitted on every
    # fixture finished TODAY -- so a row backfilled for GW5 was projected by a
    # model that already knew how GW5, and every gameweek after it, turned
    # out. Those rows feed auto_tune through db.get_prediction_history, which
    # cannot tell a retro-projection from a genuine Friday snapshot, so the
    # leakage would have landed directly in the fitted weights.
    fixture_lookup = fpl_tools._build_fixture_lookup(
        bootstrap, fixtures_override=all_fixtures, as_of_event=gw)
    played = ingest_actuals._fetch_played(gw)

    rows = []
    for e in bootstrap.get("elements", []):
        pos = fpl_tools.POS_MAP.get(e.get("element_type"))
        if not pos:
            continue
        xp, _note = fpl_tools._player_xp(e, fixture_lookup, event=gw)
        feats = fpl_tools.calibration_features(e, fixture_lookup, event=gw)
        minutes_floor = fpl_tools._expected_playing_fraction(e, e.get("status", "a"))
        rows.append((
            e["id"], gw, e.get("web_name", "?"), pos, teams_by_id.get(e.get("team"), "?"),
            float(xp), played.get(e["id"]),
            feats["cameo_mass"], feats["rotation_variance"], minutes_floor,
            fpl_tools.MODEL_VERSION, feats["raw_total"], feats["xp_cameo"],
            feats["ep_w"], feats["ep_term"], fpl_tools.ARITH_FINGERPRINT,
        ))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="gw_from", type=int, default=1)
    ap.add_argument("--to", dest="gw_to", type=int, default=MAX_GAMEWEEKS)
    args = ap.parse_args(argv)

    db.run_migrations()
    db.ensure_calibration_columns()
    db.ensure_predictions_upsert_key()

    all_fixtures = fpl_tools._get_all_fixtures()

    conn = _connect()
    written, skipped = 0, []
    try:
        for gw in range(args.gw_from, args.gw_to + 1):
            rows = _project_gameweek(gw, all_fixtures)
            if rows is None:
                skipped.append(gw)
                continue
            if not rows:
                continue
            with conn.cursor() as cur:
                from psycopg2.extras import execute_batch
                execute_batch(
                    cur,
                    "INSERT INTO fpl_predictions "
                    "(player_id, gameweek, player_name, position, team, predicted_xp, "
                    "actual_points, cameo_mass, rotation_variance, minutes_floor, "
                    "model_version, raw_total, xp_cameo, ep_w, ep_term, "
                    "arith_fingerprint) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (player_id, gameweek, model_version) DO UPDATE SET "
                    "player_name = EXCLUDED.player_name, position = EXCLUDED.position, "
                    "team = EXCLUDED.team, predicted_xp = EXCLUDED.predicted_xp, "
                    # COALESCE, not a plain overwrite: a re-run must never
                    # destroy an actual_points value a previous run (or
                    # ingest_actuals.py) already checked in, even though a
                    # historical result cannot actually change.
                    "actual_points = COALESCE(fpl_predictions.actual_points, EXCLUDED.actual_points), "
                    "cameo_mass = EXCLUDED.cameo_mass, rotation_variance = EXCLUDED.rotation_variance, "
                    "minutes_floor = EXCLUDED.minutes_floor, raw_total = EXCLUDED.raw_total, "
                    "xp_cameo = EXCLUDED.xp_cameo, ep_w = EXCLUDED.ep_w, "
                    "ep_term = EXCLUDED.ep_term, "
                    "arith_fingerprint = EXCLUDED.arith_fingerprint",
                    rows,
                    page_size=1000,
                )
            conn.commit()
            written += len(rows)
            print(f"  GW{gw}: {len(rows)} rows backfilled under {fpl_tools.MODEL_VERSION}")
    finally:
        conn.close()

    print(f"Backfill complete: {written} rows written.")
    if skipped:
        print(f"  skipped (no archived snapshot): {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
