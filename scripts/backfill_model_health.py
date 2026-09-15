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

`dc_sensitivity` is measured here, not written as a NULL. It is the derivative
of the projection with respect to `dixon_coles_decay`, taken as a forward
difference under neutral weights with both sides of the difference built
`as_of_event=gw`, exactly as `snapshot_xp.py` does it and with the same
`DC_PROBE_H`. Leaving it NULL made `reproject()` read a zero gradient, which
pins `dixon_coles_decay` -- and since this script is what refills the archive
after a MODEL_VERSION bump, it pinned it across the whole archive.

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

# Forward-difference step for the dixon_coles_decay derivative. Large enough
# that the perturbed fit is distinguishable from the base one, small enough to
# stay in the linear regime the surrogate assumes. Must match snapshot_xp.py's
# DC_PROBE_H: the two writers feed the same column into the same surrogate, so
# a different step would make the two populations incomparable.
DC_PROBE_H = 0.01


def _lookup_at_weights(weights, bootstrap, all_fixtures, as_of_event):
    """Rebuild the fixture lookup with a different set of weights in force.

    The Dixon-Coles fit is time-weighted by dixon_coles_decay and cached, so a
    perturbed decay needs both the weight swap and a cache clear -- otherwise
    this silently returns the base ratings and the derivative comes out as
    exactly 0.0, which is the very bug it exists to fix.

    `as_of_event` is threaded through so the perturbed lookup is point-in-time
    exactly like the base one, and cleared per-window rather than globally so
    the loop does not throw away every other gameweek's fit on each pass.
    Without it the probe would be fitted on every fixture finished TODAY while
    the base was fitted on fixtures strictly before `gw`, and the difference
    would measure the leakage rather than the decay.
    """
    orig_cache = fpl_tools._WEIGHTS_CACHE
    fpl_tools._WEIGHTS_CACHE = weights
    try:
        fpl_tools._clear_rating_caches(as_of_event)
        return fpl_tools._build_fixture_lookup(
            bootstrap, fixtures_override=all_fixtures, as_of_event=as_of_event)
    finally:
        fpl_tools._WEIGHTS_CACHE = orig_cache
        fpl_tools._clear_rating_caches(as_of_event)


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

    # dc_sensitivity was absent from this script's row tuple and INSERT
    # entirely, so every backfilled row left the column NULL. reproject()
    # multiplies it by (decay - default_decay), so a NULL reads as a zero
    # gradient and dixon_coles_decay -- one of the four parameters the tuner
    # fits -- could never move on backfilled data. Since the backfill is the
    # mechanism that refills the archive after a MODEL_VERSION bump, that is
    # the whole archive.
    #
    # It is a derivative, so measure it, the same way snapshot_xp.py does: two
    # projections either side of a perturbed decay, under NEUTRAL weights so
    # the surrogate sees the pre-penalty scale it was fitted against. The
    # perturbed ratings are shared across all players, so the lookup is built
    # once per gameweek rather than once per player.
    neutral = dict(fpl_tools._load_weights())
    neutral.update({"global_xP_modifier": 1.0, "autosub_ref": 0.0,
                    "rotation_convexity": 0.0,
                    "dixon_coles_decay": fpl_tools.DIXON_COLES_DECAY_DEFAULT})
    dc_probe = dict(neutral)
    dc_probe["dixon_coles_decay"] = fpl_tools.DIXON_COLES_DECAY_DEFAULT + DC_PROBE_H
    perturbed_lookup = _lookup_at_weights(dc_probe, bootstrap, all_fixtures, gw)
    _orig_cache = fpl_tools._WEIGHTS_CACHE

    rows = []
    for e in bootstrap.get("elements", []):
        pos = fpl_tools.POS_MAP.get(e.get("element_type"))
        if not pos:
            continue
        xp, _note = fpl_tools._player_xp(e, fixture_lookup, event=gw)
        minutes_floor = fpl_tools._expected_playing_fraction(e, e.get("status", "a"))
        fpl_tools._WEIGHTS_CACHE = neutral
        try:
            feats = fpl_tools.calibration_features(e, fixture_lookup, event=gw)
            probe = fpl_tools.calibration_features(e, perturbed_lookup, event=gw)
        finally:
            fpl_tools._WEIGHTS_CACHE = _orig_cache
        dc_sensitivity = (probe["raw_total"] - feats["raw_total"]) / DC_PROBE_H
        rows.append((
            e["id"], gw, e.get("web_name", "?"), pos, teams_by_id.get(e.get("team"), "?"),
            float(xp), played.get(e["id"]),
            feats["cameo_mass"], feats["rotation_variance"], dc_sensitivity,
            minutes_floor,
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
                    "actual_points, cameo_mass, rotation_variance, dc_sensitivity, "
                    "minutes_floor, "
                    "model_version, raw_total, xp_cameo, ep_w, ep_term, "
                    "arith_fingerprint) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (player_id, gameweek, model_version) DO UPDATE SET "
                    "player_name = EXCLUDED.player_name, position = EXCLUDED.position, "
                    "team = EXCLUDED.team, predicted_xp = EXCLUDED.predicted_xp, "
                    # COALESCE, not a plain overwrite: a re-run must never
                    # destroy an actual_points value a previous run (or
                    # ingest_actuals.py) already checked in, even though a
                    # historical result cannot actually change.
                    "actual_points = COALESCE(fpl_predictions.actual_points, EXCLUDED.actual_points), "
                    "cameo_mass = EXCLUDED.cameo_mass, rotation_variance = EXCLUDED.rotation_variance, "
                    "dc_sensitivity = EXCLUDED.dc_sensitivity, "
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
