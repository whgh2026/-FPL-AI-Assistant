import gzip
import math
import os
import json
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


CONNECT_TIMEOUT_SECONDS = 10
POOL_MIN, POOL_MAX = 1, 8

_POOL = None
_LAST_DB_ERROR = None      # (kind, message) or None


def last_db_error():
    """Why the most recent connection attempt failed, or None.

    get_db_connection returns None on every failure, which is the right shape
    for callers that must degrade -- but it threw away the REASON, so a page
    could only ever say "unavailable". "No DATABASE_URL configured" and "the
    database refused the connection" need different responses from whoever is
    reading, and one of them is a five-second fix.

    Also records a QUERY failure, not just a connection one. get_db_connection
    itself was fine -- it already distinguished driver/config/connect -- but
    every caller that connects successfully and then has its query fail was
    swallowing that exception in a bare `except Exception: return None`,
    overwriting nothing here because the connection HAD succeeded. So a missing
    table (migrations never ran on a fresh deploy) or a dead pooled connection
    (Supabase's free tier pauses an idle project; the first query after that
    gets a closed socket) looked identical to "never configured" -- the UI's
    generic fallback message is exactly that gap.
    """
    return _LAST_DB_ERROR


def _log_db_error(kind, exc):
    """Record AND print a failure, so it survives past this process's memory.

    Railway captures stdout/stderr, so a plain print is the whole fix for "we
    can't see why it's failing in the logs" -- no logging config, no handler
    setup, nothing that can itself be silently misconfigured.
    """
    global _LAST_DB_ERROR
    detail = str(exc) or exc.__class__.__name__
    _LAST_DB_ERROR = (kind, detail)
    print(f"[db] {kind} failed: {detail}", file=sys.stderr, flush=True)


def prepare_database_url(url):
    """Normalise a DATABASE_URL for psycopg2 -- in particular, for Supabase's
    Supavisor/PgBouncer transaction pooler (port 6543), which this app should
    be pointed at rather than the direct session port (5432): Railway (and
    most autoscaled/ephemeral hosts) can open many short-lived connections
    across container restarts and Streamlit reruns, and Supabase's direct
    Postgres port has a low connection ceiling a fleet of such clients can
    exhaust, while the pooler exists precisely for that traffic shape.

    This does NOT choose the host or port. Supabase's pooler is not the same
    host on a different port -- it is a separate, region-specific hostname
    (aws-0-<region>.pooler.supabase.com) with its own username format
    (postgres.<project-ref> rather than postgres), neither of which is
    derivable from a direct-connection URL. Routing through the pooler is a
    DATABASE_URL secret change made in the Supabase dashboard (Project
    Settings -> Database -> Connection Pooling -> Transaction mode) and then
    in Railway's environment variables -- not something this function can
    safely invent.

    What this DOES do, on whatever URL it is handed:

    1. Strips a `pgbouncer=true` query parameter if present. Supabase's own
       dashboard includes it in the pooler connection string it hands out --
       it is a convention some ORMs read to disable prepared-statement
       caching, not a real libpq parameter, and psycopg2 raises at DSN-parse
       time on an unrecognised one ("invalid URI query parameter: pgbouncer")
       *before any network call is attempted*. Handed to psycopg2 verbatim,
       Supabase's own copy-pasted pooler string breaks every connection this
       app makes. Silently stripping it is what makes copy-pasting that
       string here safe.
    2. Ensures sslmode=require is set (Supabase requires TLS; some pooler
       strings omit the parameter and rely on it being the client default,
       which it is not for a bare psycopg2 connection).
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except Exception:
        return url
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.pop("pgbouncer", None)
    query.setdefault("sslmode", "require")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _database_url():
    if st is not None:
        try:
            url = st.secrets.get("DATABASE_URL")
            if url:
                return prepare_database_url(url)
        except Exception:
            pass
    return prepare_database_url(os.getenv("DATABASE_URL"))


class _PooledConnection:
    """A pooled connection that behaves like a plain one at the call site.

    All thirteen callers do `conn.close()` in a finally block, which against a
    pool would destroy the connection rather than return it. This intercepts
    close() to hand it back instead.

    It also ROLLS BACK first, which matters more than the pooling: several
    callers catch an exception and return a default inside a try/finally that
    closes the connection, leaving the transaction aborted. Handed to the next
    caller unchanged, that connection fails every subsequent statement with
    "current transaction is aborted" -- one bad query would poison the pool for
    the life of the process.
    """

    def __init__(self, conn, pool):
        self._conn = conn
        self._pool = pool
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def close(self):
        if self._closed:
            return
        self._closed = True
        broken = False
        try:
            self._conn.rollback()
        except Exception:
            broken = True          # unusable; do not return it to the pool
        try:
            self._pool.putconn(self._conn, close=broken)
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass


def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    url = _database_url()
    if not url:
        return None
    from psycopg2 import pool as _pgpool
    _POOL = _pgpool.ThreadedConnectionPool(
        POOL_MIN, POOL_MAX, url, connect_timeout=CONNECT_TIMEOUT_SECONDS)
    return _POOL


def get_db_connection():
    """A pooled psycopg2 connection, or None on failure.

    Pooled because thirteen call sites each opened a fresh TCP connection and
    ran authentication, and Streamlit re-executes the whole script on every
    interaction -- so a single page could pay for several full connection
    handshakes before rendering anything.

    connect_timeout is the more important half. There was none, so an
    unreachable database did not fail: it HUNG, and took the page render with
    it for as long as the OS was willing to wait on the socket.
    """
    global _LAST_DB_ERROR, _POOL
    if not _PSYCOPG2:
        _log_db_error("driver", "psycopg2 is not installed")
        return None
    if not _database_url():
        _log_db_error("config", "DATABASE_URL is not set")
        return None
    try:
        pool = _get_pool()
        if pool is None:
            _log_db_error("config", "DATABASE_URL is not set")
            return None
        conn = _PooledConnection(pool.getconn(), pool)
        _LAST_DB_ERROR = None
        return conn
    except Exception as exc:
        _log_db_error("connect", exc)
        # A pool that cannot hand out connections is not worth keeping: drop it
        # so the next attempt rebuilds rather than reusing a broken one.
        _POOL = None
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
        -- Three columns, not two: a fresh database should start with the key
        -- ensure_predictions_upsert_key() migrates existing ones onto, so a
        -- snapshot can UPSERT per model_version instead of colliding across
        -- one. (player_id, gameweek) alone would reject a re-snapshot of an
        -- already-snapshotted gameweek taken under a bumped MODEL_VERSION.
        UNIQUE (player_id, gameweek, model_version))
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
        # Stage 8: the surrogate needs the cameo-penalty CLAMP input and the
        # ep_next blend weight to reproduce _player_xp_raw. Without them it
        # modelled the penalty unclamped and unblended, over-attributing a unit
        # change by up to 2x. Nullable on purpose -- rows written before this
        # fall back to the old arithmetic in reproject() rather than being lost.
        ("raw_total", "DOUBLE PRECISION"),
        ("xp_cameo", "DOUBLE PRECISION"),
        ("ep_w", "DOUBLE PRECISION"),
        ("ep_term", "DOUBLE PRECISION"),
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


def ensure_predictions_upsert_key():
    """Move fpl_predictions' identity from (player_id, gameweek) to
    (player_id, gameweek, model_version), so a re-snapshot can UPSERT on
    conflict instead of the previous DELETE-then-INSERT.

    The original UNIQUE(player_id, gameweek) constraint (declared inline in
    the CREATE TABLE, hence Postgres's deterministic auto-generated name
    below) has never actually blocked anything in production -- a gameweek is
    only ever snapshotted once, before it happens, so it was never hit even
    across a MODEL_VERSION bump -- but it WOULD reject an UPSERT targeting
    the three-column key with a genuine unique-constraint violation, on top
    of the ON CONFLICT clause simply not matching any index at all. Both
    statements are idempotent (DROP ... IF EXISTS, CREATE ... IF NOT EXISTS),
    safe to call on every boot alongside the rest of the migrations.
    """
    try:
        conn = get_db_connection()
        if conn is None:
            return False
        try:
            cur = conn.cursor()
            cur.execute(
                "ALTER TABLE fpl_predictions DROP CONSTRAINT "
                "IF EXISTS fpl_predictions_player_id_gameweek_key"
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS fpl_predictions_upsert_key "
                "ON fpl_predictions (player_id, gameweek, model_version)"
            )
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
                    "rotation_variance, dc_sensitivity, raw_total, xp_cameo, "
                    "ep_w, ep_term "
                    "FROM fpl_predictions WHERE actual_points IS NOT NULL")
            else:
                cur.execute(
                    "SELECT predicted_xp, actual_points, base_pts, cameo_mass, "
                    "rotation_variance, dc_sensitivity, raw_total, xp_cameo, "
                    "ep_w, ep_term "
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
                    # None, not 0.0: reproject() distinguishes "no clamp input
                    # recorded" (fall back to the old unclamped arithmetic) from
                    # "the cameo projection really was zero", and 0.0 would make
                    # the clamp bind on every legacy row.
                    "raw_total": r[6],
                    "xp_cameo": r[7],
                    "ep_w": r[8],
                    "ep_term": r[9] or 0.0,
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


def count_checked_predictions(model_version=None):
    """How many predictions have been paired with a real result, for one model
    version. Returns None when the database is unreachable -- distinct from 0,
    which means "connected, nothing banked yet".

    Drives the UI's recalibration copy. Hardcoding a gameweek there would be
    wrong twice over: the count restarts at each model_version bump, and with
    the active-player filter 5,000 rows is ~18 gameweeks, not the ~7 an
    unfiltered count suggests.
    """
    conn = get_db_connection()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            if model_version is None:
                cur.execute("SELECT count(*) FROM fpl_predictions "
                            "WHERE actual_points IS NOT NULL")
            else:
                cur.execute("SELECT count(*) FROM fpl_predictions "
                            "WHERE actual_points IS NOT NULL AND model_version = %s",
                            (model_version,))
            row = cur.fetchone()
        global _LAST_DB_ERROR
        _LAST_DB_ERROR = None      # a prior failure is now stale; don't haunt the UI
        return int(row[0]) if row else 0
    except Exception as exc:
        # This is the swallow that produced the Model Health tab's generic
        # "couldn't be reached" message: get_db_connection had ALREADY
        # succeeded (it clears _LAST_DB_ERROR on success), so a query failure
        # here -- missing table because migrations never ran, a dead pooled
        # connection from a Supabase idle-pause, a permissions error -- looked
        # identical to "never configured" until this was logged.
        _log_db_error("query", exc)
        return None
    finally:
        conn.close()


def prediction_accuracy_by_gw(model_version=None, limit=12):
    """Per-gameweek forecast accuracy, newest last. [] when unavailable.

    The app claims a self-checking model, so the check has to be visible: this
    is what the Model Health tab reads. RMSE says how far off the projections
    were, bias says which way (positive = we over-projected), and corr() is the
    Pearson correlation between projected and actual -- the number that says
    whether the RANKING was right, which matters more for transfer advice than
    the absolute level does.

    Rows with no result yet are excluded, as are gameweeks with too few paired
    rows for the statistics to mean anything.
    """
    conn = get_db_connection()
    if conn is None:
        return []
    try:
        where = "actual_points IS NOT NULL"
        params = []
        if model_version is not None:
            where += " AND model_version = %s"
            params.append(model_version)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT gameweek, count(*), "
                "  sqrt(avg(power(predicted_xp - actual_points, 2))), "
                "  avg(predicted_xp - actual_points), "
                "  corr(predicted_xp, actual_points) "
                f"FROM fpl_predictions WHERE {where} "
                "GROUP BY gameweek HAVING count(*) >= 20 "
                "ORDER BY gameweek DESC LIMIT %s", (*params, limit))
            rows = [
                {"gameweek": int(r[0]), "n": int(r[1]),
                 "rmse": float(r[2]) if r[2] is not None else None,
                 "bias": float(r[3]) if r[3] is not None else None,
                 "corr": float(r[4]) if r[4] is not None else None}
                for r in cur.fetchall()
            ]
        rows.reverse()
        global _LAST_DB_ERROR
        _LAST_DB_ERROR = None      # a prior failure is now stale; don't haunt the UI
        return rows
    except Exception as exc:
        _log_db_error("query", exc)
        return []
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
