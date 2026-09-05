import gzip
import math
import os
import json

try:
    import streamlit as st
except ImportError:
    st = None

try:
    import psycopg2
    _PSYCOPG2 = True
except ImportError:
    psycopg2 = None
    _PSYCOPG2 = False

import requests

_CRISIS_STATUSES = ("i", "s", "u", "Injured", "Suspended", "Unavailable")


def calculate_decaying_tax(current_gw, purchase_gw, status="a"):
    """Decaying transfer tax for selling an asset before its hold matures.

    tax = 2.0 * exp(-0.7 * max(0, current_gw - purchase_gw)).

    Crisis sales (injured/suspended/unavailable) are zero-rated, and an unknown
    purchase gameweek yields 0.0 (no memory, no friction).
    """
    if status in _CRISIS_STATUSES:
        return 0.0
    if purchase_gw is None:
        return 0.0
    try:
        held = max(0, int(current_gw) - int(purchase_gw))
    except (TypeError, ValueError):
        return 0.0
    return round(2.0 * math.exp(-0.7 * held), 2)


def get_db_connection():
    """Return a psycopg2 connection from DATABASE_URL, or None on failure."""
    if not _PSYCOPG2:
        return None
    try:
        url = None
        if st is not None:
            try:
                url = st.secrets.get("DATABASE_URL")
            except Exception:
                url = None
        if not url:
            url = os.getenv("DATABASE_URL")
        if not url:
            return None
        return psycopg2.connect(url)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# Every table the application reads or writes, created in one place.
#
# `fpl_predictions` and `manager_transfer_ledger` previously had no CREATE
# statement anywhere in the repository: the calibration snapshot would fail on a
# fresh database with UndefinedTable, and the ledger's ON CONFLICT DO NOTHING
# had no unique constraint to act on, so re-running the backfill duplicated rows
# instead of no-oping.
#
# The other tables are also created lazily inside their writers. That is left in
# place for now (removing the per-insert DDL is Stage 8) -- these statements are
# idempotent, so running both is harmless.
_SCHEMA = (
    # Calibration loop: pre-deadline predictions, joined to realised points.
    """
    CREATE TABLE IF NOT EXISTS fpl_predictions (
        id BIGSERIAL PRIMARY KEY,
        player_id INTEGER NOT NULL,
        gameweek INTEGER NOT NULL,
        player_name TEXT,
        position TEXT,
        team TEXT,
        predicted_xp DOUBLE PRECISION,
        actual_points DOUBLE PRECISION,
        base_pts DOUBLE PRECISION,
        cameo_mass DOUBLE PRECISION,
        rotation_variance DOUBLE PRECISION,
        dc_sensitivity DOUBLE PRECISION,
        minutes_floor DOUBLE PRECISION,
        model_version TEXT DEFAULT 'v1',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (player_id, gameweek))
    """,
    "CREATE INDEX IF NOT EXISTS fpl_predictions_gw_idx ON fpl_predictions (gameweek)",
    # Ledger: establishes true purchase price, and therefore selling price, per
    # manager. The UNIQUE is what makes the backfill's ON CONFLICT idempotent.
    """
    CREATE TABLE IF NOT EXISTS manager_transfer_ledger (
        id BIGSERIAL PRIMARY KEY,
        manager_id BIGINT NOT NULL,
        player_id INTEGER NOT NULL,
        gameweek INTEGER NOT NULL,
        direction TEXT NOT NULL,
        purchase_price DOUBLE PRECISION,
        selling_price DOUBLE PRECISION,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (manager_id, player_id, gameweek, direction))
    """,
    "CREATE INDEX IF NOT EXISTS manager_ledger_mgr_idx ON manager_transfer_ledger (manager_id)",
    # Archive of the raw FPL bootstrap, so the Stage 8 backtester can replay a
    # gameweek against the data the model actually saw at the time. Payload is
    # gzipped JSON: raw is ~3MB, compressed ~250KB.
    """
    CREATE TABLE IF NOT EXISTS bootstrap_snapshots (
        id BIGSERIAL PRIMARY KEY,
        gameweek INTEGER NOT NULL,
        captured_date DATE NOT NULL DEFAULT CURRENT_DATE,
        captured_at TIMESTAMPTZ DEFAULT NOW(),
        payload BYTEA NOT NULL,
        UNIQUE (gameweek, captured_date))
    """,
)


def run_migrations():
    """Create every table the app needs. Idempotent; safe to call on each boot.

    Returns True on success, False if the database is unreachable or the DDL
    failed -- callers that care should surface that rather than assume success.
    """
    conn = get_db_connection()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            for stmt in _SCHEMA:
                cur.execute(stmt)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def _reconstruct_holdings(rows):
    """[(player_id, gameweek, direction), ...] -> {player_id: purchase_gameweek}."""
    last_in = {}
    last_out = {}
    for player_id, gameweek, direction in rows:
        gw = int(gameweek)
        if direction == "in":
            last_in[player_id] = max(last_in.get(player_id, 0), gw)
        else:
            last_out[player_id] = max(last_out.get(player_id, 0), gw)
    return {pid: gw for pid, gw in last_in.items() if gw > last_out.get(pid, 0)}


def get_or_backfill_manager_history(manager_id, current_gw):
    """Return {player_id: purchase_gameweek} for a manager's active holdings."""
    try:
        conn = get_db_connection()
        if conn is None:
            return {}
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT player_id, gameweek, direction "
                "FROM manager_transfer_ledger WHERE manager_id = %s "
                "ORDER BY gameweek ASC",
                (int(manager_id),),
            )
            rows = cur.fetchall()

            if not rows and int(current_gw) > 1:
                resp = requests.get(
                    "https://fantasy.premierleague.com/api/entry/" + str(manager_id) + "/transfers/",
                    timeout=10,
                )
                resp.raise_for_status()
                transfers = resp.json()
                insert_rows = []
                for t in transfers:
                    ev = t.get("event")
                    if ev is None:
                        continue
                    elem_in = t.get("element_in")
                    elem_out = t.get("element_out")
                    if elem_in:
                        insert_rows.append((int(manager_id), int(elem_in), int(ev), "in"))
                    if elem_out:
                        insert_rows.append((int(manager_id), int(elem_out), int(ev), "out"))
                if insert_rows:
                    cur.executemany(
                        "INSERT INTO manager_transfer_ledger "
                        "(manager_id, player_id, gameweek, direction) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        insert_rows,
                    )
                    conn.commit()
                    cur.execute(
                        "SELECT player_id, gameweek, direction "
                        "FROM manager_transfer_ledger WHERE manager_id = %s "
                        "ORDER BY gameweek ASC",
                        (int(manager_id),),
                    )
                    rows = cur.fetchall()

            return _reconstruct_holdings(rows)
        finally:
            conn.close()
    except Exception:
        return {}


def log_decision(manager_id, gameweek, action, delta_xp, hits=0, chip=None, transfers=None):
    """Persist a transfer decision and its expected xP delta for audit / ΔxP capture.

    The `decision_log` table is created idempotently on first use, so no separate
    migration step is required.
    """
    try:
        conn = get_db_connection()
        if conn is None:
            return False
        try:
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS decision_log ("
                "id BIGSERIAL PRIMARY KEY,"
                "manager_id TEXT NOT NULL,"
                "gameweek INTEGER NOT NULL,"
                "action TEXT NOT NULL,"
                "delta_xp DOUBLE PRECISION,"
                "hits INTEGER DEFAULT 0,"
                "chip TEXT,"
                "transfers TEXT,"
                "created_at TIMESTAMPTZ DEFAULT NOW())"
            )
            cur.execute(
                "INSERT INTO decision_log "
                "(manager_id, gameweek, action, delta_xp, hits, chip, transfers) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (str(manager_id), int(gameweek), action, delta_xp, int(hits or 0), chip, transfers),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def save_team_ratings(ratings, gameweek=None):
    """Persist a Dixon-Coles team-ratings snapshot for audit / warm-start."""
    try:
        conn = get_db_connection()
        if conn is None:
            return False
        try:
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS team_strength_ratings ("
                "id BIGSERIAL PRIMARY KEY,"
                "team_id INTEGER NOT NULL,"
                "gameweek INTEGER,"
                "att DOUBLE PRECISION,"
                "def DOUBLE PRECISION,"
                "created_at TIMESTAMPTZ DEFAULT NOW())"
            )
            rows = []
            for tid, r in (ratings or {}).items():
                rows.append((int(tid), gameweek, float(r.get("att", 0.0)), float(r.get("def", 0.0))))
            if rows:
                cur.executemany(
                    "INSERT INTO team_strength_ratings (team_id, gameweek, att, def) "
                    "VALUES (%s, %s, %s, %s)",
                    rows,
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def log_squad_health(manager_id, gameweek, checks):
    """Log the structural-health flags as a decision-log row (action='health')."""
    try:
        summary = " | ".join(f"{'OK' if c.get('ok') else 'FLAG'}:{c.get('label')}" for c in (checks or []))
        return log_decision(manager_id, gameweek, "health", 0.0, hits=0, chip=None, transfers=summary)
    except Exception:
        return False


def save_chip_play(manager_id, gameweek, chip):
    """Record a chip activation in the local ledger (idempotent per manager/GW/chip)."""
    try:
        conn = get_db_connection()
        if conn is None:
            return False
        try:
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS chip_plays ("
                "id BIGSERIAL PRIMARY KEY,"
                "manager_id TEXT NOT NULL,"
                "gameweek INTEGER NOT NULL,"
                "chip TEXT NOT NULL,"
                "created_at TIMESTAMPTZ DEFAULT NOW(),"
                "UNIQUE (manager_id, gameweek, chip))"
            )
            cur.execute(
                "INSERT INTO chip_plays (manager_id, gameweek, chip) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (str(manager_id), int(gameweek), chip),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def ensure_calibration_columns():
    """Add Phase E feature-tracking columns to fpl_predictions (idempotent)."""
    cols = [
        ("minutes_floor", "DOUBLE PRECISION"),
        ("cameo_mass", "DOUBLE PRECISION"),
        ("rotation_variance", "DOUBLE PRECISION"),
        ("dc_sensitivity", "DOUBLE PRECISION"),
        ("base_pts", "DOUBLE PRECISION"),
        ("model_version", "TEXT DEFAULT 'v1'"),
    ]
    try:
        conn = get_db_connection()
        if conn is None:
            return False
        try:
            cur = conn.cursor()
            for name, typ in cols:
                cur.execute(f"ALTER TABLE fpl_predictions ADD COLUMN IF NOT EXISTS {name} {typ}")
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def get_prediction_history(model_version=None):
    """Return calibration rows for one model version.

    Filtering matters: the Dixon-Coles centring and sign fix changed what
    predicted_xp means, so pre-fix rows carry a systematically different bias.
    Fitting across the boundary would have the calibrator chase a discontinuity
    rather than the model's real error. Passing None returns every row, which is
    only appropriate for inspection, never for tuning.
    """
    try:
        conn = get_db_connection()
        if conn is None:
            return []
        try:
            cur = conn.cursor()
            if model_version is None:
                cur.execute(
                    "SELECT predicted_xp, actual_points, base_pts, cameo_mass, "
                    "rotation_variance, dc_sensitivity "
                    "FROM fpl_predictions WHERE actual_points IS NOT NULL")
            else:
                cur.execute(
                    "SELECT predicted_xp, actual_points, base_pts, cameo_mass, "
                    "rotation_variance, dc_sensitivity "
                    "FROM fpl_predictions WHERE actual_points IS NOT NULL "
                    "AND model_version = %s", (model_version,))
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "predicted_xp": r[0] or 0.0,
                    "actual_points": r[1] or 0.0,
                    "base_pts": r[2] if r[2] is not None else (r[0] or 0.0),
                    "cameo_mass": r[3] or 0.0,
                    "rotation_variance": r[4] or 0.0,
                    "dc_sensitivity": r[5] or 0.0,
                })
            return rows
        finally:
            conn.close()
    except Exception:
        return []


def save_bootstrap_snapshot(bootstrap, gameweek):
    """Archive the raw FPL bootstrap for later replay.

    One row per (gameweek, date), so re-running on the same day overwrites
    rather than accumulating. Payload is gzipped JSON.

    This is what the Stage 8 backtester replays against: without an archive of
    what the model could see at the time, "did this change help?" is not an
    answerable question. Every day this is not running is a day of history lost,
    which is why it lands in Stage 0 rather than alongside the backtester.
    """
    try:
        payload = gzip.compress(json.dumps(bootstrap, separators=(",", ":")).encode("utf-8"))
    except Exception:
        return False
    conn = get_db_connection()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bootstrap_snapshots (gameweek, payload) VALUES (%s, %s) "
                "ON CONFLICT (gameweek, captured_date) DO UPDATE "
                "SET payload = EXCLUDED.payload, captured_at = NOW()",
                (int(gameweek), psycopg2.Binary(payload)),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def load_bootstrap_snapshot(gameweek, captured_date=None):
    """Return an archived bootstrap dict, or None. Used by the backtester."""
    conn = get_db_connection()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            if captured_date is None:
                cur.execute(
                    "SELECT payload FROM bootstrap_snapshots WHERE gameweek = %s "
                    "ORDER BY captured_at DESC LIMIT 1", (int(gameweek),))
            else:
                cur.execute(
                    "SELECT payload FROM bootstrap_snapshots "
                    "WHERE gameweek = %s AND captured_date = %s", (int(gameweek), captured_date))
            row = cur.fetchone()
        if not row:
            return None
        return json.loads(gzip.decompress(bytes(row[0])).decode("utf-8"))
    except Exception:
        return None
    finally:
        conn.close()


def save_plan(manager_id, gameweek, plan):
    """Persist the multi-GW transfer schedule to the fpl_plans ledger."""
    try:
        conn = get_db_connection()
        if conn is None:
            return False
        try:
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS fpl_plans ("
                "id BIGSERIAL PRIMARY KEY,"
                "manager_id TEXT NOT NULL,"
                "gameweek INTEGER NOT NULL,"
                "horizon INTEGER NOT NULL,"
                "plan JSONB,"
                "created_at TIMESTAMPTZ DEFAULT NOW())"
            )
            cur.execute(
                "INSERT INTO fpl_plans (manager_id, gameweek, horizon, plan) "
                "VALUES (%s, %s, %s, %s)",
                (str(manager_id), int(gameweek), len(plan or []), json.dumps(plan or [])),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False
