import math
import os

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
