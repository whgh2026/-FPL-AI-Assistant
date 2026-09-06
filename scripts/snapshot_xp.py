"""
Friday Snapshot: log the upcoming gameweek's 1-GW xP forecast for all active players.

Writes are an UPSERT keyed on (player_id, gameweek, model_version) -- see
db.ensure_predictions_upsert_key() -- rather than a DELETE-then-INSERT for the
whole gameweek, specifically so a re-run never destroys actual_points that
ingest_actuals.py already backfilled into an existing row.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fpl_tools
import db


# Forward-difference step for the dixon_coles_decay derivative. Large enough
# that the perturbed fit is distinguishable from the base one, small enough to
# stay in the linear regime the surrogate assumes.
DC_PROBE_H = 0.01


def _connect():
    # Imported here, not at module scope: the projection logic in this file is
    # worth testing without a database driver present, and a top-level import
    # made the whole module unimportable in any environment without psycopg2.
    import psycopg2
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    # Strips Supabase's `pgbouncer=true` (which psycopg2 rejects outright as
    # an unrecognised DSN parameter) and ensures sslmode=require. See
    # db.prepare_database_url's docstring for why routing through the actual
    # pooler host/port is a DATABASE_URL secret change, not something this
    # can do for you.
    return psycopg2.connect(db.prepare_database_url(url), connect_timeout=10)


def _lookup_at_weights(weights, bootstrap):
    """Rebuild the fixture lookup with a different set of weights in force.

    The Dixon-Coles fit is time-weighted by dixon_coles_decay and cached, so a
    perturbed decay needs both the weight swap and a cache clear -- otherwise
    this silently returns the base ratings and the derivative comes out as
    exactly 0.0, which is the very bug it exists to fix.
    """
    orig_cache = fpl_tools._WEIGHTS_CACHE
    fpl_tools._WEIGHTS_CACHE = weights
    try:
        fpl_tools._clear_rating_caches()
        return fpl_tools._build_fixture_lookup(bootstrap)
    finally:
        fpl_tools._WEIGHTS_CACHE = orig_cache
        fpl_tools._clear_rating_caches()


def main() -> None:
    # Schema first. This script writes base_pts/cameo_mass/rotation_variance/
    # dc_sensitivity/minutes_floor, but ensure_calibration_columns() was only
    # ever called from auto_tune.py -- so on a fresh database the Friday
    # snapshot failed with UndefinedColumn, and the columns only appeared the
    # following Wednesday. The migration belongs with the writer.
    db.run_migrations()
    db.ensure_calibration_columns()
    db.ensure_predictions_upsert_key()

    bootstrap = fpl_tools._get_bootstrap()
    fixture_lookup = fpl_tools._build_fixture_lookup(bootstrap)
    teams_by_id = {t["id"]: t.get("short_name", t.get("name", "?")) for t in bootstrap.get("teams", [])}
    gw = fpl_tools._next_gameweek(bootstrap)

    # Neutral-parameter weights for the base projection (before tunable penalties).
    neutral = dict(fpl_tools._load_weights())
    neutral.update({"global_xP_modifier": 1.0, "autosub_ref": 0.0,
                    "rotation_convexity": 0.0, "dixon_coles_decay": fpl_tools.DIXON_COLES_DECAY_DEFAULT})
    _orig_cache = fpl_tools._WEIGHTS_CACHE

    # dc_sensitivity used to be written as a literal 0.0 for every row. The
    # calibration surrogate multiplies it by (decay - default_decay), so the
    # gradient of dixon_coles_decay was identically zero and that parameter --
    # one of the four the tuner is supposed to fit -- could never move, in
    # either direction, no matter what the results said.
    #
    # It is a derivative, so measure it: re-project at a perturbed decay and
    # take the forward difference. The decay changes the time-weighting of the
    # Dixon-Coles fit, which is shared across all players, so the perturbed
    # ratings are computed ONCE here rather than per player.
    dc_probe = dict(neutral)
    dc_probe["dixon_coles_decay"] = fpl_tools.DIXON_COLES_DECAY_DEFAULT + DC_PROBE_H
    perturbed_lookup = _lookup_at_weights(dc_probe, bootstrap)

    rows = []
    for e in bootstrap.get("elements", []):
        pos = fpl_tools.POS_MAP.get(e.get("element_type"))
        if not pos:
            continue
        xp, _note = fpl_tools._player_xp(e, fixture_lookup, event=gw)
        minutes_floor = fpl_tools._expected_playing_fraction(e, e.get("status", "a"))

        # Every feature the surrogate needs, read out of the production path so
        # it cannot drift from it. raw_total is pre-penalty and pre-blend, which
        # base_pts is not -- base_pts is _player_xp at neutral weights, already
        # blended, so rebuilding from it and blending again double-counts
        # ep_next. base_pts is still written for continuity with older rows.
        fpl_tools._WEIGHTS_CACHE = neutral
        try:
            base_xp, _ = fpl_tools._player_xp(e, fixture_lookup, event=gw)
            feats = fpl_tools.calibration_features(e, fixture_lookup, event=gw)
            probe = fpl_tools.calibration_features(e, perturbed_lookup, event=gw)
        finally:
            fpl_tools._WEIGHTS_CACHE = _orig_cache
        dc_sensitivity = (probe["raw_total"] - feats["raw_total"]) / DC_PROBE_H

        rows.append((e["id"], gw, e.get("web_name", "?"), pos, teams_by_id.get(e["team"], "?"),
                     float(xp), float(base_xp), feats["cameo_mass"],
                     feats["rotation_variance"], dc_sensitivity, minutes_floor,
                     fpl_tools.MODEL_VERSION, feats["raw_total"],
                     feats["xp_cameo"], feats["ep_w"], feats["ep_term"]))

    conn = _connect()
    try:
        with conn.cursor() as cur:
            # UPSERT keyed on (player_id, gameweek, model_version) rather than
            # DELETE-then-INSERT for the whole gameweek. The delete used to
            # destroy actual_points too: ingest_actuals.py backfills that
            # column days after the snapshot, so a manual re-run of this
            # script (or a MODEL_VERSION bump for a gameweek already
            # snapshotted) wiped out results that had already been checked,
            # with no way to recover them. An UPSERT only ever touches the
            # PREDICTION columns -- actual_points, once set, survives.
            #
            # execute_batch (not executemany) batches multiple parameter sets
            # per network round trip: ~700 single-row INSERTs became one
            # query per commit here already, at the DB layer this is the
            # remaining win.
            from psycopg2.extras import execute_batch
            execute_batch(
                cur,
                "INSERT INTO fpl_predictions "
                "(player_id, gameweek, player_name, position, team, predicted_xp, "
                "base_pts, cameo_mass, rotation_variance, dc_sensitivity, minutes_floor, "
                "model_version, raw_total, xp_cameo, ep_w, ep_term) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (player_id, gameweek, model_version) DO UPDATE SET "
                "player_name = EXCLUDED.player_name, position = EXCLUDED.position, "
                "team = EXCLUDED.team, predicted_xp = EXCLUDED.predicted_xp, "
                "base_pts = EXCLUDED.base_pts, cameo_mass = EXCLUDED.cameo_mass, "
                "rotation_variance = EXCLUDED.rotation_variance, "
                "dc_sensitivity = EXCLUDED.dc_sensitivity, "
                "minutes_floor = EXCLUDED.minutes_floor, raw_total = EXCLUDED.raw_total, "
                "xp_cameo = EXCLUDED.xp_cameo, ep_w = EXCLUDED.ep_w, ep_term = EXCLUDED.ep_term",
                rows,
                page_size=1000,
            )
        conn.commit()
    finally:
        conn.close()

    print(f"Snapshot complete: {len(rows)} player forecasts written for GW {gw}.")


if __name__ == "__main__":
    main()
