import os
import re
import requests
import streamlit as st
import fpl_tools
import squad_override
import dateutil.parser
from dateutil import tz
import datetime
import time
import plotly.graph_objects as go
from db import get_or_backfill_manager_history, log_decision, log_squad_health, save_chip_play, save_plan

st.set_page_config(page_title="FPL Quant Manager", page_icon="⚽", layout="wide",
                    initial_sidebar_state="expanded")

def get_caveat_html():
    """Data-freshness note. Never raises: it is decoration on every page.

    This called get_api_timestamp() unguarded, so an FPL outage took the ENTIRE
    page down from a footnote -- the user got a Streamlit traceback instead of a
    squad, for want of a timestamp.
    """
    try:
        fetch_ts = fpl_tools.get_api_timestamp()
        uk_zone = tz.gettz("Europe/London")
        dt = datetime.datetime.fromtimestamp(fetch_ts, tz=datetime.timezone.utc).astimezone(uk_zone)
        when = f"refreshed at {dt.strftime('%H:%M on %d %B %Y')} UK time"
    except Exception:
        when = "last refresh time unavailable"
    return (
        '<div class="note">ℹ️ <b>Heads up:</b> projected points are our best '
        f'guess, not a promise ({when}). Prices update once a day, at about '
        '01:30 UK time.</div>'
    )

# ------------------------------------------------------------------
# Auto-detect the upcoming gameweek straight from the FPL API
# ------------------------------------------------------------------
SAFE_TIME_STR = ""
try:
    _gw_info = fpl_tools.get_upcoming_gameweek()
    GW_ID = int(_gw_info.get("id") or 1)
    GW_NAME = _gw_info.get("name") or f"Gameweek {GW_ID}"
    _deadline_raw = _gw_info.get("deadline_time")
    if _deadline_raw:
        uk_zone = tz.gettz('Europe/London')
        _deadline_dt = dateutil.parser.isoparse(_deadline_raw).astimezone(uk_zone)
        DEADLINE_STR = _deadline_dt.strftime("%A, %d %B %Y · %H:%M")
        _safe_dt = _deadline_dt - datetime.timedelta(hours=1)
        SAFE_TIME_STR = _safe_dt.strftime("%H:%M")
    else:
        DEADLINE_STR = "Check the official site for the confirmed deadline."
except Exception:
    GW_ID = 1
    GW_NAME = "Gameweek 1"
    DEADLINE_STR = "Deadline could not be loaded right now."

# ------------------------------------------------------------------
# Styling
# ------------------------------------------------------------------
POS_COLORS = {"GK": "#f59e0b", "DEF": "#0ea5e9", "MID": "#10b981", "FWD": "#f43f5e"}
POS_ORDER = ["GK", "DEF", "MID", "FWD"]
ALL_CHIPS = ["Wildcard", "Free Hit", "Bench Boost", "Triple Captain"]


# ------------------------------------------------------------------
# Dark "Final Boss" theme overrides + analytics component styles
# ------------------------------------------------------------------





# ------------------------------------------------------------------
# Render helpers
# ------------------------------------------------------------------
def _pos_chip(pos: str) -> str:
    c = POS_COLORS.get(pos, "#94a3b8")
    return f'<span class="pos" style="background:{c}">{pos}</span>'


def _status_badge(status: str) -> str:
    if not status or status in ("Available", "a"):
        return ""
    if status in ("OUT", "Injured", "Suspended", "Unavailable"):
        return f'<span class="stat-badge stat-out">🔴 {status}</span>'
    return f'<span class="stat-badge stat-doubt">⚠️ {status}</span>'


def _player_card(p, max_xp: float, role: str = None) -> str:
    pos = p.get("position", "?")
    c = POS_COLORS.get(pos, "#94a3b8")
    pct = int(round(min(100.0, (p.get("xp", 0) / max_xp) * 100))) if max_xp else 0
    role_html = ""
    if role == "C":
        role_html = '<span class="role role-c">C</span>'
    elif role == "VC":
        role_html = '<span class="role role-vc">VC</span>'
    status_html = _status_badge(p.get("status", ""))
    return (
        f'<div class="pc" style="border-left:3px solid {c}">'
        f'<div style="margin-bottom:2px;">{_pos_chip(pos)}{role_html}{status_html}</div>'
        f'<div class="nm">{p.get("name", "?")}</div>'
        f'<div class="meta">{p.get("team", "?")} · £{p.get("price", 0):.1f}m</div>'
        f'<div class="bar"><div class="bar-fill" style="width:{pct}%;background:{c}"></div></div>'
        f'<div class="xp" style="color:{c}">{p.get("xp", 0)} xP</div></div>'
    )


def _get_fixture_context():
    """Fixture lookup + upcoming gameweek, as a view onto the single cache.

    Was a SECOND, independent session-state cache of the same data, with no TTL
    on either. Once populated, both were stale for the entire browser session --
    so price changes, injury flags and new fixtures never refreshed without a
    hard reload, and the two could disagree with each other.
    """
    ctx = _bootstrap_ctx()
    if not ctx:
        return None
    return {"lookup": ctx["lookup"], "start_event": ctx["start"]}


def _name_with_fixtures(p) -> str:
    """Append a 4-GW traffic-light string (e.g. 'B. Saka [🟢 🟡 🔴 🟢]') to a name."""
    name = p.get("name", "?")
    team_id = p.get("team_id")
    if team_id is None:
        return name
    ctx = _get_fixture_context()
    if not ctx:
        return name
    lights = fpl_tools._fixture_traffic_lights(team_id, ctx["lookup"], ctx["start_event"]).strip("[]")
    return f"{name} {lights}"


def _mini_card(p, role: str = None) -> str:
    pos = p.get("position", "?")
    c = POS_COLORS.get(pos, "#94a3b8")
    role_html = ""
    if role == "C":
        role_html = '<span class="role role-c">C</span>'
    elif role == "VC":
        role_html = '<span class="role role-vc">VC</span>'
    status_html = _status_badge(p.get("status", ""))
    return (
        f'<div class="mc" style="border-left-color:{c}">'
        f'<div style="margin-bottom:2px;"><span style="background:{c};" class="pos">{pos}</span>{role_html}{status_html}</div>'
        f'<div class="mc-nm" title="{p.get("name", "?")}">{_name_with_fixtures(p)}</div>'
        f'<div class="mc-meta">{p.get("team", "?")} · £{p.get("price", 0):.1f}m</div>'
        f'<div class="mc-xp">{p.get("xp", 0)} xP</div></div>'
    )


def _team_sheet_html(starters, bench, captain_id=None, vcap_id=None) -> str:
    pos_emoji = {"GK": "🧤", "DEF": "🛡️", "MID": "🎯", "FWD": "⚡"}

    def _sort_group(players):
        if any((p.get("pick_position") or 0) > 0 for p in players):
            return sorted(players, key=lambda p: p.get("pick_position", 99))
        return sorted(players, key=lambda p: -p.get("xp", 0))

    html = ""
    for pos in POS_ORDER:
        st_players = _sort_group([p for p in starters if p.get("position") == pos])
        be_players = _sort_group([p for p in bench if p.get("position") == pos])

        cards = ""
        for p in st_players:
            role = None
            pid = _pid(p)
            if captain_id is not None and pid == captain_id:
                role = "C"
            elif vcap_id is not None and pid == vcap_id:
                role = "VC"
            cards += _mini_card(p, role)
        if not cards:
            cards = '<div class="empty">—</div>'

        subs_html = ""
        if be_players:
            subs_html = "".join(
                f'<div class="sub-item">'
                f'<div><div>{_name_with_fixtures(p)} {_status_badge(p.get("status", ""))}</div>'
                f'<div class="sub-meta">{p.get("team", "?")} · £{p.get("price", 0):.1f}m</div></div>'
                f'<div class="sub-xp">{p.get("xp", 0)} xP</div></div>'
                for p in be_players
            )
        else:
            subs_html = '<div class="empty">—</div>'

        html += (
            f'<div class="team-row">'
            f'<div class="pos-col"><div class="pos-emoji">{pos_emoji.get(pos, "⚽")}</div>'
            f'<div class="pos-name">{pos}</div></div>'
            f'<div class="starters">{cards}</div>'
            f'<div class="subs"><div class="sub-title">Subs ({pos})</div>{subs_html}</div>'
            f'</div>'
        )
    return html


def _api_starters_bench(squad):
    starters = [p for p in squad if p.get("multiplier", 1) >= 1]
    bench = [p for p in squad if p.get("multiplier", 1) == 0]
    if len(starters) != 11 or len(bench) != 4:
        xi = fpl_tools.select_starting_xi(squad)
        return xi["xi"], xi["bench"]
    return starters, bench


def _card(inner: str, label: str = "") -> str:
    lab = f'<div class="section-label">{label}</div>' if label else ""
    return f'<div class="card">{lab}{inner}</div>'


def _pid(p) -> str:
    return p.get("player_id", p.get("id"))


def _get_dropdown_index(options_list, target_id):
    if not target_id:
        return 0
    for i, (pid, _) in enumerate(options_list):
        if pid == target_id:
            return i
    return 0


def _surname(p: dict) -> str:
    """Sortable surname key for a player dict (falls back to the last name word)."""
    sn = (p.get("second_name") or "").strip().lower()
    if sn:
        return sn
    parts = (p.get("web_name") or "").strip().lower().split()
    return parts[-1] if parts else ""


def clear_transfer_cache():
    """Bust the solver cache and mark stored transfers as stale.

    Bound to the on_change of every widget in Step 3's twin scenario cards --
    the Strategy & Risk Mode radio and the Chip Scenario Lab radio -- so a
    change to either one immediately invalidates the cached 4-GW optimisation
    and forces a fresh solve. Nothing downstream (the transfer cards, the
    baseline pitch, projected xP) can end up showing a plan that predates the
    setting it's supposed to reflect.
    """
    try:
        fpl_tools.suggest_transfers_for_custom_squad.clear()
    except Exception:
        pass
    st.session_state["_transfers_stale"] = True
    # Drop any stale manual-transfer dropdown selections too.
    for k in list(st.session_state.keys()):
        if k.startswith("man_out") or k.startswith("man_in"):
            st.session_state.pop(k, None)


# The one canonical Strategy Mode control lives in Step 3's "Strategy & Risk
# Mode" card. It used to be duplicated (sidebar + a mobile-only inline copy
# kept in sync via a pair of on_change callbacks, because Streamlit forbids
# two widgets sharing one key) -- collapsing back to a single widget removes
# that sync machinery entirely, since there's nothing left to keep in sync.
RISK_OPTIONS = [
    "Balanced", "Conservative", "Aggressive",
    "Rank Protecting (Shield)", "Rank Chasing (Hunting)",
]
RISK_DESCRIPTIONS = [
    "Chase the most points. No thumb on the scale.",
    "Play it safe. Own what your rivals own, and only move for a clear upgrade.",
    "Go hunting. Back differentials and take a hit for a big enough gain.",
    "Protect a lead. Mirror the players your rivals own so their good weeks can't hurt you.",
    "Close a gap. Target players almost nobody else has.",
]


# ------------------------------------------------------------------
# Styling
# ------------------------------------------------------------------
STYLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "app.css")


@st.cache_data(ttl=60, show_spinner=False)
def _load_css(path: str, _mtime: float) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _inject_css() -> None:
    """One stylesheet, from disk.

    Was five <style> blocks injected in sequence, where the result depended on
    injection order: .streamlit/config.toml forces a dark theme, so the light
    block was dead for every class the dark blocks redefined -- yet ten classes
    styled ONLY there still rendered light-on-dark. Selectors were also
    redefined across blocks with different values (.pitch-player 92px then 96px,
    .headshot 44x56 then 65x85).

    Keyed on mtime so editing the CSS shows up on the next rerun.
    """
    try:
        css = _load_css(STYLE_PATH, os.path.getmtime(STYLE_PATH))
    except OSError:
        return
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


_inject_css()

# ------------------------------------------------------------------
# Official Premier League asset helpers
# ------------------------------------------------------------------


def _headshot_style(player_code: str) -> str:
    """CSS background declaration for a player headshot, with a fallback layer.

    The previous version issued a blocking `requests.head` PER PLAYER to check
    the CDN before rendering. Cached for 24h, but a cold cache serialised up to
    two seconds each: fifteen on the pitch, twenty-two on the radar, two per
    transfer row. That was the single largest contributor to cold-render time.

    A single background-image layer replaces it, over the `.headshot` class's
    own solid background-color -- NOT a two-image stack. An earlier version
    layered `url(primary), url(PHOTO_FALLBACK)`, reasoning that "a layer that
    fails to load paints nothing, so the fallback beneath shows through". True
    for an outright 404, but CSS paints background-image layers front-to-back
    in listed order, and the official PL headshots are transparent-cutout
    PNGs -- so wherever a REAL, successfully-loaded photo has transparent
    pixels, the silhouette layered directly behind it showed straight through,
    composited under the player's own photo. One image layer cannot bleed
    through itself, so a plain-tile fallback beats a doubled-up one.
    """
    if not player_code:
        return "background-image:none;"
    primary = ("https://resources.premierleague.com/premierleague/photos/"
               f"players/250x250/p{player_code}.png")
    return (f"background-image:url('{primary}');"
            "background-size:cover;background-position:center;")


def _photo_code(photo: str) -> str:
    """Strip an FPL `photo` field down to its bare player code."""
    if not photo:
        return ""
    code = photo.split("/")[-1].replace(".jpg", "").replace(".png", "")
    if code.startswith("p"):
        code = code[1:]
    return code


def _badge_url(team_code) -> str:
    """Build the official CDN club badge URL from an FPL `team_code`."""
    return f"https://resources.premierleague.com/premierleague/badges/70/t{team_code}.png"


def _num(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


BOOTSTRAP_TTL_SECONDS = 300
# Module-level, not session_state: the banner must render even on the very first
# pass, and session_state is unavailable in bare/script mode.
_DATA_ERROR = None
_DATA_CACHE = {"ctx": None, "ts": 0.0}


@st.cache_data(ttl=300, show_spinner=False)
def _dropdown_options(bootstrap, teams, _pos_map):
    """Player picker labels: name, club, price. No projections.

    This previously ran a full _player_xp for EVERY player in the game -- ~700
    projections, each evaluating multiple fixtures -- purely to put an xP figure
    in a dropdown label, and it re-ran on every Streamlit rerun while the
    midweek-transfers box was open. Typing a character in the Manager ID field
    paid for the whole market.

    Labels now carry only fields already present on the bootstrap row, and the
    result is cached.
    """
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    options = {pos: [(None, "— Select Player —")] for pos in POS_ORDER}
    for p in bootstrap.get("elements", []):
        pos = pos_map.get(p["element_type"])
        if not pos:
            continue
        initial = p["first_name"][0] + "." if p.get("first_name") else ""
        label = (f"{initial} {p['second_name']} ({teams.get(p['team'], '?')}) "
                 f"£{p['now_cost'] / 10:.1f}m")
        options[pos].append((p["id"], label))
    for pos in POS_ORDER:
        options[pos] = [options[pos][0]] + sorted(
            options[pos][1:], key=lambda x: x[1].split()[-2].lower() if x[1] else "")
    return options


def _bootstrap_ctx():
    """The single cached view of the live FPL data. TTL'd, and honest on failure."""
    global _DATA_ERROR
    cached = _DATA_CACHE["ctx"]
    if cached and (time.time() - _DATA_CACHE["ts"]) < BOOTSTRAP_TTL_SECONDS:
        return cached
    try:
        bootstrap = fpl_tools._get_bootstrap()
        lookup = fpl_tools._build_fixture_lookup(bootstrap)
        _DATA_CACHE["ctx"] = {
            "bootstrap": bootstrap,
            "lookup": lookup,
            "players_by_id": {p["id"]: p for p in bootstrap.get("elements", [])},
            "teams_by_id": {t["id"]: t for t in bootstrap.get("teams", [])},
            "start": fpl_tools._next_gameweek(bootstrap),
        }
        _DATA_CACHE["ts"] = time.time()
        _DATA_ERROR = None
    except Exception as exc:
        _DATA_ERROR = str(exc) or exc.__class__.__name__
        # Keep serving the last good data if there is any -- a stale squad beats
        # a blank page -- but the banner will say so.
    return _DATA_CACHE["ctx"]


def _data_error():
    """The last data-load failure, or None."""
    return _DATA_ERROR


def _data_is_stale():
    return bool(_DATA_CACHE["ctx"])


def _headshot_img(p) -> str:
    photo = p.get("photo")
    if not photo:
        ctx = _bootstrap_ctx()
        el = (ctx or {}).get("players_by_id", {}).get(_pid(p), {})
        photo = el.get("photo", "")
    style = _headshot_style(_photo_code(photo))
    badge = ""
    team_id = p.get("team_id")
    if team_id is None and isinstance(p.get("team"), int):
        team_id = p.get("team")
    if team_id is not None:
        badge = _badge_img(team_id)
    overlay = f'<div class="badge-overlay">{badge}</div>' if badge else ""
    # A div with layered backgrounds rather than an <img>: the fallback layer
    # shows through automatically if the CDN photo 404s, with no HEAD request
    # and no broken-image icon.
    return (
        f'<div class="photo-frame">'
        f'<div class="headshot" style="{style}"></div>'
        f'{overlay}'
        f'</div>'
    )


def _headshot_tile(photo: str) -> str:
    """A bare headshot with no badge overlay -- for call sites that already
    render their own, differently-positioned badge alongside it.

    Same fallback mechanism as _headshot_img: a styled div, not an <img>. A
    bare <img src=...> has no CSS-only fallback for a failed load -- under
    unsafe_allow_html Streamlit strips the onerror attribute, so a 404 paints
    the browser's own broken-image [?] icon. This div's background-image
    fails silently onto the .headshot class's own solid surface colour
    instead, which is the same dark placeholder every other photo site uses.
    """
    return f'<div class="headshot" style="{_headshot_style(_photo_code(photo))}"></div>'


def _badge_style(team_code) -> str:
    """CSS background declaration for a club crest -- one background-image
    layer over the class's own solid fallback surface, mirroring
    _headshot_style's reasoning exactly: Streamlit strips onerror under
    unsafe_allow_html, so there is no reliable way to detect a failed load
    and swap in different content, and a SECOND layer (e.g. text sitting
    behind the crest) risks bleeding through wherever a successfully-loaded,
    transparent-background crest doesn't cover it. One layer cannot bleed
    through itself."""
    if not team_code:
        return "background-image:none;"
    return (f"background-image:url('{_badge_url(team_code)}');"
            "background-size:contain;background-repeat:no-repeat;background-position:center;")


def _badge_img(team_id, large: bool = False) -> str:
    ctx = _bootstrap_ctx()
    t = (ctx or {}).get("teams_by_id", {}).get(team_id, {})
    code = t.get("code")
    cls = "badge-lg" if large else "badge-img"
    if not code:
        # No crest mapped at all (an unmapped or missing team) previously
        # rendered nothing -- a silent gap where a team identifier should
        # be. This case can render a genuine, permanent text fallback: there
        # is no image being attempted here at all, so no risk of the
        # initials bleeding through a real crest that loads fine.
        initials = (t.get("short_name") or "?")[:3].upper()
        return f'<div class="badge-crest {cls}">{initials}</div>'
    return f'<div class="badge-crest {cls}" style="{_badge_style(code)}"></div>'


def _web_name(p) -> str:
    """Resolve the short display name (e.g. 'B. Fernandes') for a player dict."""
    wn = p.get("web_name")
    if wn:
        return wn
    ctx = _bootstrap_ctx()
    el = (ctx or {}).get("players_by_id", {}).get(_pid(p), {})
    wn = el.get("web_name")
    return wn or p.get("name", "?")


def _fdr_cell(ease) -> str:
    """Colour a fixture-ease cell (5 = easiest, 1 = hardest)."""
    e = round(float(ease), 1)
    if e >= 4.0:
        bg = "#00F5A0"
    elif e >= 3.0:
        bg = "#fbbf24"
    else:
        bg = "#f43f5e"
    return f'<div class="fdr-cell" style="background:{bg}">{e}</div>'


# ------------------------------------------------------------------
# Fixtures tab — rotation solver, team strength, fixture swings
# ------------------------------------------------------------------
def _team_ease_series(team_id, lookup, start, n: int = 6):
    """Continuous fixture-ease series (higher = easier) from Dixon-Coles ratings."""
    fx = {f.get("event"): f for f in lookup.get(team_id, [])}
    series = []
    for i in range(n):
        ev = start + i
        f = fx.get(ev)
        series.append(6.0 - _num(f.get("opp_strength_def") or 3.0) if f else 0.0)
    return series


def _fixture_rotation_matrix(anchor_team_id=None, n: int = 6, top: int = 8):
    ctx = _bootstrap_ctx()
    if not ctx:
        return []
    lookup = ctx["lookup"]
    start = ctx["start"]
    team_ids = [t["id"] for t in ctx["bootstrap"].get("teams", [])]
    ease_by_id = {tid: _team_ease_series(tid, lookup, start, n) for tid in team_ids}
    pairings = []
    for i in range(len(team_ids)):
        for j in range(i + 1, len(team_ids)):
            t1, t2 = team_ids[i], team_ids[j]
            best = [max(a, b) for a, b in zip(ease_by_id[t1], ease_by_id[t2])]
            avg = sum(best) / len(best)
            pairings.append({"t1": t1, "t2": t2, "avg": avg, "series": best})
    pairings.sort(key=lambda x: x["avg"], reverse=True)
    if anchor_team_id is not None:
        pairings = [p for p in pairings if anchor_team_id in (p["t1"], p["t2"])]
    return pairings[:top]


def _team_strength_index():
    ratings = fpl_tools._team_attack_def_ratings()
    ctx = _bootstrap_ctx()
    if not ctx:
        return []
    rows = []
    for t in ctx["bootstrap"].get("teams", []):
        r = ratings.get(t["id"], {})
        rows.append({
            "id": t["id"],
            "name": t.get("name", "?"),
            "code": t.get("code"),
            "att": r.get("att", 3.0),
            "def": r.get("def", 3.0),
        })
    rows.sort(key=lambda x: (x["att"] + x["def"]), reverse=True)
    return rows


# ------------------------------------------------------------------
# Player Radar — Momentum & Shortlists
# ------------------------------------------------------------------
_FPL_MANAGERS_APPROX = 11_000_000  # approximate active managers (velocity denominator)


def _market_momentum(limit: int = 5):
    ctx = _bootstrap_ctx()
    if not ctx:
        return {"risers": [], "fallers": []}
    pos_map = fpl_tools.POS_MAP
    rows = []
    for e in ctx["bootstrap"].get("elements", []):
        tin = e.get("transfers_in_event") or 0
        tout = e.get("transfers_out_event") or 0
        net = tin - tout
        if net == 0:
            continue
        spb = _num(e.get("selected_by_percent"))
        owned = _FPL_MANAGERS_APPROX * spb / 100.0
        pct = (net / owned * 100.0) if owned > 1 else 0.0
        rows.append({
            "id": e["id"],
            "name": f"{e.get('first_name', '')} {e.get('second_name', '')}".strip(),
            "pos": pos_map.get(e.get("element_type"), "?"),
            "team": e.get("team"),
            "photo": e.get("photo", ""),
            "price": _num(e.get("now_cost")) / 10.0,
            "net": net,
            "pct": pct,
            "ownership": spb,
        })
    risers = sorted(rows, key=lambda r: -r["net"])[:limit]
    fallers = sorted(rows, key=lambda r: r["net"])[:limit]
    return {"risers": risers, "fallers": fallers}


def _momentum_row_html(r, up: bool = True) -> str:
    badge = (
        f'<span class="momentum-badge-up">+{r["pct"]:.2f}%</span>'
        if up else f'<span class="momentum-badge-down">{r["pct"]:.2f}%</span>'
    )
    arrow = "📈" if up else "📉"
    return (
        f'<div class="momentum-row">'
        f'{_headshot_tile(r["photo"])}'
        f'{_badge_img(r["team"])}'
        f'<div style="flex:1;">'
        f'<div style="font-weight:700;color:var(--text);">{r["name"]}</div>'
        f'<div class="tc-meta">{r["pos"]} · £{r["price"]:.1f}m · net {r["net"]:+,}</div>'
        f'</div>'
        f'{arrow} {badge}'
        f'</div>'
    )


def _regression_row(r, high: bool) -> str:
    color = "#ef4444" if high else "#10b981"
    arrow = "🔻" if high else "🔺"
    return (
        f'<div style="display:flex;justify-content:space-between;align-items:center;'
        f'padding:7px 0;border-bottom:1px solid #1e293b;">'
        f'<div style="min-width:0;">'
        f'<div style="font-weight:700;color:var(--text);">{r["name"]}</div>'
        f'<div class="tc-meta">{r["position"]} · {r["team"]} · £{r["price"]:.1f}m</div>'
        f'<div class="tc-meta">G {r["goals"]} A {r["assists"]} vs xG {r["xg"]} xA {r["xa"]}</div>'
        f'</div>'
        f'<div style="font-weight:800;color:{color};font-size:1.05rem;white-space:nowrap;">{arrow} {r["residual"]:+.2f}</div>'
        f'</div>'
    )


def _radar_shortlists(kind: str, limit: int = 12):
    # No `risk` parameter: the radar ranks on the projection, which is now
    # strategy-blind by construction. Keeping the argument would imply the
    # shortlist responds to strategy when it cannot.
    ctx = _bootstrap_ctx()
    if not ctx:
        return []
    lookup = ctx["lookup"]
    start = ctx["start"]
    pos_map = fpl_tools.POS_MAP
    rows = []
    for e in ctx["bootstrap"].get("elements", []):
        if e.get("status") in ("u",):
            continue
        pos = pos_map.get(e.get("element_type"))
        if not pos:
            continue
        price = _num(e.get("now_cost")) / 10.0
        xp4, note = fpl_tools._player_xp_horizon(e, lookup, start, n=4)
        if note in ("Injured", "Suspended", "Unavailable", "No minutes", "Blank"):
            continue
        if xp4 <= 0:
            continue
        rows.append({
            "id": e["id"],
            "name": f"{e.get('first_name', '')} {e.get('second_name', '')}".strip(),
            "pos": pos,
            "team": e.get("team"),
            "photo": e.get("photo", ""),
            "price": price,
            "xp": round(xp4, 2),
            "ownership": _num(e.get("selected_by_percent")),
            "value_ratio": round(xp4 / max(price, 0.1), 2),
        })
    if kind == "differentials":
        rows = [r for r in rows if r["ownership"] < 10.0]
        rows.sort(key=lambda r: -r["xp"])
    elif kind == "value":
        rows.sort(key=lambda r: -r["value_ratio"])
    else:  # points
        rows.sort(key=lambda r: -r["xp"])
    return rows[:limit]


# ------------------------------------------------------------------
# Football pitch view + side-by-side transfer cards
# ------------------------------------------------------------------
def _pitch_fixture_dots_html(team_id) -> str:
    """Real, precisely-sized circular fixture-difficulty dots for a pitch
    card -- a sized <span> per gameweek rather than an emoji glyph, so
    mobile CSS can actually control width/height (an emoji's rendered size
    is set by font-size and a platform's own emoji font, not by CSS
    width/height). Same banding _fixture_traffic_lights already shows as
    text elsewhere (the dropdown labels, the Fixtures tab), read
    structurally via _fixture_traffic_light_bands so the two can never
    silently disagree about what a given fixture rates."""
    if team_id is None:
        return '<div class="fx-dots"></div>'
    ctx = _get_fixture_context()
    if not ctx:
        return '<div class="fx-dots"></div>'
    bands = fpl_tools._fixture_traffic_light_bands(team_id, ctx["lookup"], ctx["start_event"])
    dots = "".join(f'<span class="fx-dot fx-dot-{b}"></span>' for b in bands)
    return f'<div class="fx-dots">{dots}</div>'


def _pitch_player_html(p, role: str = None) -> str:
    role_html = ""
    if role == "C":
        role_html = '<span class="cap-pill">C</span>'
    elif role == "VC":
        role_html = '<span class="cap-pill" style="background:var(--line);color:var(--text-2);">V</span>'
    name = _web_name(p)
    return (
        f'<div class="pitch-player">'
        f'{_headshot_img(p)}'
        f'<div class="nm">{name} {role_html}</div>'
        f'<div class="meta">£{p.get("price", 0):.1f}m · {p.get("xp", 0):.2f} xP</div>'
        f'{_pitch_fixture_dots_html(p.get("team_id"))}'
        f'</div>'
    )


def _pitch_html(starters, bench, captain_id=None, vcap_id=None) -> str:
    order = ["GK", "DEF", "MID", "FWD"]
    html = '<div class="pitch">'
    for pos in order:
        players = sorted([p for p in starters if p.get("position") == pos], key=lambda p: -p.get("xp", 0))
        cards = "".join(
            _pitch_player_html(p, "C" if _pid(p) == captain_id else ("VC" if _pid(p) == vcap_id else None))
            for p in players
        ) or '<div class="empty">—</div>'
        c = POS_COLORS.get(pos, "#94a3b8")
        html += f'<div class="pitch-row"><div class="pitch-row-label" style="color:{c}">{pos}</div><div class="pitch-row-cards">{cards}</div></div>'
    html += "</div>"

    bench_cards = "".join(_pitch_player_html(p) for p in bench) or '<div class="empty">—</div>'
    html += (
        '<div class="dugout"><div class="dugout-title">🪑 The Dugout (Bench)</div>'
        f'<div style="display:flex;gap:10px;flex-wrap:wrap;">{bench_cards}</div></div>'
    )
    return html


def _fixture_key_html() -> str:
    """Permanent fixture-key legend rendered directly above every pitch view."""
    return (
        '<div style="font-size:0.8rem;color:var(--muted);margin:0 0 6px 0;line-height:1.5;">'
        'Fixture Outlook: 🟢 Favourable · 🟡 Moderate · 🔴 Difficult<br>'
        '<span style="font-size:0.68rem;font-style:italic;">'
        '*Ratings derived from our proprietary algorithmic model, blending live market sentiment with opponent defensive/offensive strength.</span>'
        '</div>'
    )


def _friendly_status(status) -> str:
    s = (status or "Available").strip()
    if s in ("Available", "a", ""):
        return "✅ Fit — available for selection"
    if s == "Injured":
        return "🔴 Injured"
    if s == "Suspended":
        return "🔴 Suspended"
    if s in ("Unavailable", "OUT"):
        return "🔴 Unavailable"
    if s == "Doubtful":
        return "⚠️ Doubtful"
    if s == "No minutes":
        return "⏱️ No minutes played this season"
    if "% Chance" in s:
        return f"⚠️ {s} of playing"
    return s


_SIGNAL_DEPTS = ["GK", "DEF", "MID", "FWD", "Bench"]
_SIGNAL_EMOJI = {"GK": "🧤", "DEF": "🛡️", "MID": "🎯", "FWD": "⚡", "Bench": "🪑"}
_SIGNAL_LABELS = {
    "market": ("Bookies", "Live betting market odds stripped of the bookmaker's built-in profit margin ('vig'). This converts betting lines into true, unbiased probabilities for clean sheets, goals, and match outcomes."),
    "quant": ("FPL Quant Manager", "Our own projection model, built for this tool. It forecasts points several weeks out — weighing fixture difficulty, each player's likelihood of featuring, and recent output — then checks itself against real results and refines week by week."),
    "form": ("Recent Form Tracker", "Rolling 30-day baseline performance tracking sustained underlying shot volume and key involvements."),
}


def _signal_colour(value: float) -> str:
    if value < 40:
        return "#f43f5e"
    if value <= 70:
        return "#fbbf24"
    return "#00F5A0"


def _player_market_signal(p, ctx) -> float:
    """Implied win probability (0-100) aggregated from the live odds feed."""
    team_id = p.get("team_id")
    if team_id is None and isinstance(p.get("team"), int):
        team_id = p.get("team")
    if team_id is None:
        return 50.0
    fixtures = ctx["lookup"].get(team_id, [])
    fx = next((f for f in fixtures if f.get("event") == ctx["start"]), None)
    if fx is None and fixtures:
        fx = fixtures[0]
    if fx is None:
        return 50.0
    wp = fx.get("win_prob")
    if wp is not None:
        return max(0.0, min(100.0, wp * 100.0))
    opp_def = fx.get("opp_strength_def") or 3
    return max(0.0, min(100.0, 60.0 - (opp_def - 3) * 10.0))


def _player_form_signal(p, ctx) -> float:
    """FPL form rating (0-10) scaled to 0-100."""
    el = ctx["players_by_id"].get(_pid(p), {})
    form = _num(el.get("form"))
    return max(0.0, min(100.0, form * 10.0))


def _positional_signals(starters, bench=None):
    """Compute market/quant/form signals per department for a 15-man squad.

    `bench` is an explicit list of the 4 bench players. When omitted, bench
    assets are detected via the FPL `multiplier` (0) or `is_bench` flag.
    """
    ctx = _bootstrap_ctx()
    starters = list(starters or [])
    if bench is None:
        bench = [p for p in starters if p.get("multiplier", 1) == 0 or p.get("is_bench")]
        starters = [p for p in starters if p.get("multiplier", 1) != 0 and not p.get("is_bench")]
    else:
        bench = list(bench)

    all_players = starters + bench
    if not ctx or not all_players:
        return {}

    groups = {d: [] for d in _SIGNAL_DEPTS}
    for p in starters:
        pos = p.get("position", "?")
        dept = pos if pos in ("GK", "DEF", "MID", "FWD") else "Bench"
        groups[dept].append(p)
    groups["Bench"].extend(bench)

    xp_vals = [_num(p.get("xp")) for p in all_players]
    max_xp = max(xp_vals) if xp_vals else 1.0

    out = {}
    for dept in _SIGNAL_DEPTS:
        players = groups[dept]
        if not players:
            out[dept] = {"market": 0.0, "quant": 0.0, "form": 0.0, "composite": 0.0}
            continue
        market = sum(_player_market_signal(p, ctx) for p in players) / len(players)
        avg_xp = sum(_num(p.get("xp")) for p in players) / len(players)
        quant = (avg_xp / max_xp) * 100.0
        form = sum(_player_form_signal(p, ctx) for p in players) / len(players)
        out[dept] = {
            "market": round(market, 1),
            "quant": round(quant, 1),
            "form": round(form, 1),
            "composite": round((market + quant + form) / 3.0, 1),
        }
    return out


def _signals_html(signals, prev=None) -> str:
    html = '<div class="signal-grid">'
    for dept in _SIGNAL_DEPTS:
        sig = signals.get(dept, {})
        comp = sig.get("composite", 0.0)
        delta = ""
        if prev and dept in prev:
            d = comp - prev[dept].get("composite", 0.0)
            sign = "+" if d > 0 else ("-" if d < 0 else "")
            colour = "#00F5A0" if d >= 0 else "#f43f5e"
            delta = f'<span class="signal-delta" style="color:{colour}">{sign}{abs(d):.1f}</span>'
        rows = ""
        for key, (label, tip) in _SIGNAL_LABELS.items():
            val = sig.get(key, 0.0)
            colour = _signal_colour(val)
            rows += (
                f'<div class="signal-row">'
                f'<div class="signal-label">{label}</div>'
                f'<div class="signal-bar"><div class="signal-fill" style="width:{min(val, 100.0):.0f}%;background:{colour}"></div></div>'
                f'<div class="signal-val" style="color:{colour}">{val:.0f}</div>'
                f'</div>'
            )
        html += (
            f'<div class="signal-card">'
            f'<div class="signal-head">{_SIGNAL_EMOJI.get(dept, "")} {dept}'
            f'<span class="signal-score">{comp:.0f}{delta}</span></div>'
            f'{rows}'
            f'</div>'
        )
    html += "</div>"
    return html


def _render_positional_diagnostic(starters, bench=None, prev=None, caption: str = ""):
    """Render the 'Gaffer's Positional Diagnostic' dashboard for a squad."""
    signals = _positional_signals(starters, bench)
    if not signals:
        return None
    if caption:
        st.markdown(f'<div style="font-size:0.78rem;color:var(--muted);margin:0 0 8px 0;">{caption}</div>', unsafe_allow_html=True)
    legend_cols = st.columns(3)
    for col, (label, desc) in zip(legend_cols, _SIGNAL_LABELS.values()):
        with col:
            st.markdown(f"**{label}**")
            st.caption(desc)
    html = _signals_html(signals, prev)
    st.markdown(_card(html, "📊 Gaffer's Positional Diagnostic"), unsafe_allow_html=True)
    return signals


def _render_player_inspector(squad) -> None:
    """Selectable Player Inspector shown beneath the pitch in the Transfer Planner tab."""
    ctx = _bootstrap_ctx()
    if not ctx or not squad:
        return
    lookup = ctx["lookup"]
    teams_by_id = ctx["teams_by_id"]
    start = ctx["start"]

    seen = set()
    options = []
    for p in squad:
        pid = _pid(p)
        if pid in seen or pid is None:
            continue
        seen.add(pid)
        options.append((pid, _web_name(p)))
    if not options:
        return
    options.sort(key=lambda x: x[1].lower())
    pid_options = [pid for pid, _ in options]
    name_map = dict(options)

    sel = st.selectbox(
        "🔍 Inspect Player Profile",
        pid_options,
        format_func=lambda pid: name_map.get(pid, "?"),
        key="inspector_select",
    )
    if sel is None:
        return

    player = next((p for p in squad if _pid(p) == sel), None)
    team_id = player.get("team_id") if player else None
    if team_id is None:
        el = ctx["players_by_id"].get(sel, {})
        team_id = el.get("team")
        if player is None and el:
            player = {
                "player_id": el.get("id"),
                "name": f"{el.get('first_name', '')} {el.get('second_name', '')}",
                "team_id": team_id,
                "position": fpl_tools.POS_MAP.get(el.get("element_type"), "?"),
                "team": teams_by_id.get(team_id, {}).get("short_name", "?"),
                "price": el.get("now_cost", 0) / 10.0,
                "status": "Available",
            }
    if player is None:
        return

    fixtures = [f for f in lookup.get(team_id, []) if (f.get("event") or 0) >= start][:4]
    fx_rows = ""
    for fx in fixtures:
        opp = teams_by_id.get(fx.get("opponent"), {})
        opp_name = opp.get("name", "?")
        venue = "H" if fx.get("is_home") else "A"
        date_str = ""
        if fx.get("kickoff_time"):
            try:
                dt = dateutil.parser.isoparse(fx.get("kickoff_time")).astimezone(tz.gettz("Europe/London"))
                date_str = f" <span style='color:var(--muted-2);font-weight:normal;font-size:0.75rem;margin-left:6px;'>{dt.strftime('%d %b %H:%M')}</span>"
            except:
                pass
        wp = fx.get("win_prob")
        opp_def = fx.get("opp_strength_def") or 3
        if wp is not None:
            odds_desc = f"Market Win Probability: {wp * 100:.0f}% · Opp. defence: {opp_def:.1f}/5"
        else:
            odds_desc = f"Market Odds Pending · Opp. defence: {opp_def:.1f}/5"
            
        fx_rows += (
            f'<div class="insp-fixture">'
            f'<div style="font-weight:700;color:var(--text);">{opp_name} '
            f'<span style="font-weight:600;color:var(--muted);">({venue})</span>{date_str}</div>'
            f'<div class="tc-meta">{odds_desc}</div>'
            f'</div>'
        )
    if not fx_rows:
        fx_rows = '<div class="empty">No upcoming fixtures found.</div>'

    tightrope = bool(player.get("on_yellow_card_tightrope")) or fpl_tools._is_on_tightrope(
        ctx["players_by_id"].get(sel, {}), start
    )
    tightrope_html = (
        '<span class="stat-badge stat-doubt" style="margin-left:4px;">⚠️ 1 card from ban</span>'
        if tightrope else ""
    )

    card = (
        f'<div class="inspector-card">'
        f'<div style="display:flex;gap:16px;align-items:flex-start;">'
        f'{_headshot_img(player)}'
        f'<div style="flex:1;min-width:0;">'
        f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">'
        f'<span style="font-weight:800;color:var(--text);font-size:1.05rem;">{_web_name(player)}</span>'
        f'{_badge_img(team_id, large=True)}'
        f'</div>'
        f'<div class="tc-meta">{player.get("position", "")} · {player.get("team", "")} · £{player.get("price", 0):.1f}m</div>'
        f'<div style="margin-top:6px;font-size:0.85rem;color:var(--text);">{_friendly_status(player.get("status"))}{tightrope_html}</div>'
        f'</div></div>'
        f'<div style="margin-top:14px;"><div class="section-label">Next 4 Fixtures</div>{fx_rows}</div>'
        f'</div>'
    )
    st.markdown(card, unsafe_allow_html=True)


# Display order and plain-English labels for the objective decomposition. The
# keys come straight from _solve_squad, so what is shown here IS what was
# maximised -- previously the UI recomputed a naive sum(xp_in - xp_out) that
# excluded captaincy, bench weights, CVaR, stacking, EO and tax, and could rank
# moves differently from the solver that chose them.
WATERFALL_ROWS = [
    ("points_hit", "Points hit"),
    ("transfer_bar", "The bar a transfer has to clear"),
    ("banked_transfer_value", "Free transfer you'd have banked"),
    ("chip_cost", "Cost of burning the chip"),
    ("cash_optionality", "Money kept in the bank"),
]


def _waterfall_html(breakdown: dict) -> str:
    """Render the objective decomposition, with an interaction residual.

    The residual is the honest part: these components are direct deltas, not a
    Shapley decomposition, so they need not sum exactly to the net figure. When
    the unexplained remainder is large the move is driven by interactions
    between components and the row-by-row story is misleading -- so the page
    says so rather than presenting a tidy sum that hides it.
    """
    if not breakdown:
        return ""
    rows, shown = [], 0.0
    for key, label in WATERFALL_ROWS:
        val = float(breakdown.get(key, 0.0) or 0.0)
        if abs(val) < 0.005:
            continue
        shown += val
        colour = "var(--pos)" if val > 0 else "var(--neg)"
        rows.append(
            f'<div class="wf-row"><span>{label}</span>'
            f'<span style="color:{colour};font-variant-numeric:tabular-nums;">'
            f'{val:+.1f}</span></div>')
    net = float(breakdown.get("net", 0.0) or 0.0)
    residual = net - shown
    if abs(residual) >= 0.05:
        rows.append(
            f'<div class="wf-row"><span>Everything else combined</span>'
            f'<span style="color:var(--muted);font-variant-numeric:tabular-nums;">'
            f'{residual:+.1f}</span></div>')
    rows.append(
        f'<div class="wf-row wf-net"><span>Net</span>'
        f'<span style="font-variant-numeric:tabular-nums;">{net:+.1f}</span></div>')

    warn = ""
    if abs(net) > 0.01 and abs(residual) > 0.2 * abs(net):
        warn = ('<div class="wf-warn">⚠️ A fair chunk of this move comes from how '
                "the pieces interact rather than any single line above, so treat "
                "the breakdown as a rough guide.</div>")
    return f'<div class="wf">{"".join(rows)}{warn}</div>'


# Official 1-5 FDR -> traffic-light colour, per the exact bands requested:
# green 1-2 (favourable), amber 3 (moderate), red 4-5 (difficult).
_FX_RUN_FDR_COLOR = {1: "var(--pos)", 2: "var(--pos)", 3: "var(--warn)",
                     4: "var(--neg)", 5: "var(--neg)"}


def _fixture_run_html(fixture_run) -> str:
    """Render one player's next-N-fixture horizontal track for the transfer
    card: a circular FDR badge, opponent + venue, and that gameweek's
    projected xP underneath, one cell per fpl_tools._player_fixture_run()
    entry. That helper is always fixed-length (a blank gameweek still gets an
    entry with fdr/opponent/venue all None), so this never has to special-case
    a short list -- it only special-cases what to draw inside one cell."""
    cells = []
    for fx in fixture_run:
        if fx.get("is_blank"):
            cells.append(
                '<div class="fx-run-cell">'
                '<div class="fx-run-badge fx-run-blank">–</div>'
                '<div class="fx-run-opp">Blank</div>'
                '<div class="fx-run-xp">–</div>'
                '</div>'
            )
            continue
        fdr = fx.get("fdr")
        color = _FX_RUN_FDR_COLOR.get(fdr, "var(--muted)")
        opp = fx.get("opponent") or "?"
        venue = fx.get("venue") or "?"
        dgw = " ×2" if fx.get("is_double") else ""
        cells.append(
            f'<div class="fx-run-cell">'
            f'<div class="fx-run-badge" style="background:{color};">{fdr if fdr is not None else "?"}</div>'
            f'<div class="fx-run-opp">{opp} ({venue}){dgw}</div>'
            f'<div class="fx-run-xp">{fx.get("xp", 0):.1f} xP</div>'
            f'</div>'
        )
    return f'<div class="fx-run">{"".join(cells)}</div>'


def _player_fixture_run_html(p) -> str:
    """Resolve a transfer-card player dict (an fpl_tools pool entry, keyed by
    id, not the raw bootstrap element _player_fixture_run needs) back to its
    bootstrap element via the same cached context every other lookup in this
    file uses, then render its fixture track. Empty string if the data isn't
    available -- the card still renders fine without the track."""
    ctx = _bootstrap_ctx()
    if not ctx or not ctx.get("start"):
        return ""
    element = ctx.get("players_by_id", {}).get(_pid(p))
    if not element:
        return ""
    run = fpl_tools._player_fixture_run(element, ctx["lookup"], ctx["teams_by_id"], ctx["start"])
    return _fixture_run_html(run)


def _transfer_pair_html(moves) -> str:
    html = ""
    for m in moves:
        out = m["out"]
        inn = m["in"]
        in_tightrope = (
            '<div class="tc-meta" style="color:var(--warn-fg);font-weight:600;">⚠️ 1 card from ban</div>'
            if inn.get("on_yellow_card_tightrope") else ""
        )
        html += (
            f'<div class="transfer-pair">'
            f'<div class="transfer-card tc-out">'
            f'<div class="tc-meta">Transfer Out</div>'
            f'<div class="tc-head">{_headshot_img(out)}<div><div class="tc-name">⬇️ {_web_name(out)}</div>'
            f'<div class="tc-meta">{out.get("position", "")} · {out.get("team", "")} · £{out.get("price", 0):.1f}m</div></div></div>'
            f'{_player_fixture_run_html(out)}'
            f'</div>'
            f'<div class="transfer-arrow">➔</div>'
            f'<div class="transfer-card tc-in">'
            f'<div class="tc-meta">Transfer In</div>'
            f'<div class="tc-head">{_headshot_img(inn)}<div><div class="tc-name">⬆️ {_web_name(inn)}</div>'
            f'<div class="tc-meta">{inn.get("position", "")} · {inn.get("team", "")} · £{inn.get("price", 0):.1f}m</div>'
            f'{in_tightrope}</div></div>'
            f'{_player_fixture_run_html(inn)}'
            f'</div>'
            f'<div class="tc-score" style="min-width:110px;text-align:right;">'
            f'<div class="rot-score">+{m.get("xp_gain", 0)}</div>'
            f'<div class="tc-meta">xP · £{m.get("cost", 0):+.1f}m</div>'
            f'</div></div>'
        )
        rationale = m.get("rationale")
        if rationale:
            html += f'<div style="font-size:0.78rem;color:var(--muted);margin:0 0 8px 0;">{rationale}</div>'
    return html


NAILED_ON_FRACTION = 0.8      # 72+ expected minutes reads as a starter


def _set_piece_note(element) -> str:
    """Which dead balls a player is first choice for, from the bootstrap orders."""
    if not element:
        return ""
    duties = []
    if element.get("penalties_order") == 1:
        duties.append("pens")
    if element.get("direct_freekicks_order") == 1:
        duties.append("free-kicks")
    if element.get("corners_and_indirect_freekicks_order") == 1:
        duties.append("corners")
    return ", ".join(duties)


def _squad_scorecard(starters, bench, players_by_id) -> dict:
    """The four numbers that describe a squad's shape, not its scoreline.

    Points tell you how good the XI is. These tell you whether the squad is
    BUILT right -- whether the bench is dead money, whether the XI actually
    starts, and who takes the dead balls. A 60-point XI resting on four
    rotation risks and £22m of bench is a different proposition from the same
    60 points on eleven nailed starters, and the old three-metric row
    ("Final Starting XI xP / Final Bench xP / Final Squad xP") could not
    distinguish them.
    """
    nailed, takers = 0, []
    for p in starters:
        el = players_by_id.get(p.get("player_id"))
        if el:
            try:
                frac = fpl_tools._expected_playing_fraction(el, el.get("status", "a"))
            except Exception:
                frac = 0.0
            if frac >= NAILED_ON_FRACTION:
                nailed += 1
            duty = _set_piece_note(el)
            if duty:
                takers.append((_web_name(p), duty))
    return {
        "xi_points": round(sum(p.get("xp", 0) for p in starters), 1),
        "bench_cost": round(sum(p.get("price", 0) for p in bench), 1),
        "nailed": nailed,
        "of": len(starters),
        "takers": takers,
    }


def _scorecard_html(card: dict, delta_xi=None) -> str:
    """Four tiles, in the words a manager already uses (Part E)."""
    bench = card["bench_cost"]
    # £16m is the standard-gameweek bench budget the construction solves target.
    bench_tone, bench_note = (
        ("var(--pos-2)", "sensible") if bench <= 16.0 else
        ("var(--warn-2)", "a bit rich") if bench <= 19.0 else
        ("var(--neg-2)", "dead money")
    )
    nailed_tone = (
        "var(--pos-2)" if card["nailed"] >= 9 else
        "var(--warn-2)" if card["nailed"] >= 7 else "var(--neg-2)"
    )
    if card["takers"]:
        takers = " · ".join(f"{n} ({d})" for n, d in card["takers"][:4])
        takers_extra = f" +{len(card['takers']) - 4} more" if len(card["takers"]) > 4 else ""
        takers_html = f'<div class="sc-takers">{takers}{takers_extra}</div>'
        takers_val = str(len(card["takers"]))
        takers_tone = "var(--pos-2)"
    else:
        takers_html = '<div class="sc-takers">Nobody in your XI is on dead balls.</div>'
        takers_val = "0"
        takers_tone = "var(--neg-2)"

    delta = ""
    if delta_xi:
        tone = "var(--pos-2)" if delta_xi > 0 else "var(--neg-2)"
        delta = f'<div class="sc-sub" style="color:{tone};">{delta_xi:+.1f} vs your original XI</div>'

    return (
        '<div class="scorecard">'
        f'<div class="sc-tile"><div class="sc-label">Starting XI points</div>'
        f'<div class="sc-value">{card["xi_points"]:.1f}</div>'
        f'<div class="sc-sub">projected, this gameweek</div>{delta}</div>'
        f'<div class="sc-tile"><div class="sc-label">Money on the bench</div>'
        f'<div class="sc-value" style="color:{bench_tone};">£{bench:.1f}m</div>'
        f'<div class="sc-sub">{bench_note}</div></div>'
        f'<div class="sc-tile"><div class="sc-label">Who\'s nailed on</div>'
        f'<div class="sc-value" style="color:{nailed_tone};">{card["nailed"]}<span class="sc-of">/{card["of"]}</span></div>'
        f'<div class="sc-sub">expected to play the full 90</div></div>'
        f'<div class="sc-tile"><div class="sc-label">Set-piece takers</div>'
        f'<div class="sc-value" style="color:{takers_tone};">{takers_val}</div>'
        f'{takers_html}</div>'
        '</div>'
    )


# Colours match static/app.css's :root palette -- Plotly cannot read CSS
# custom properties, so these are the same hex values copied over rather
# than re-derived, to keep the chart from looking like a different app.
_GANTT_COLOURS = {
    "held": "#38BDF8",          # --info-2: unbroken tenure, nothing happened
    "buy": "#00F5A0",           # --pos: entered the squad
    "sold": "#F43F5E",          # --neg: left the squad
    "chip_highlight": "#FBBF24",  # --warn
    "captain": "#FBBF24",       # --warn
    "vice_captain": "#94A3B8",  # --muted
}


def _transfer_gantt_figure(gantt: dict) -> "go.Figure":
    """Render fpl_tools.build_transfer_gantt_data's output as a horizontal
    Gantt-style tenure chart: one bar per held segment, chip weeks as
    shaded column highlights, captain/vice-captain as markers.

    A plain go.Bar(base=..., orientation="h") per segment rather than
    px.timeline -- px.timeline expects datetime axes, and gameweeks are
    small integers, not dates.
    """
    fig = go.Figure()
    bars = gantt.get("bars", [])
    start_gw, end_gw = gantt.get("start_gw"), gantt.get("end_gw")

    # Row order: earliest-arriving player at the top reads naturally as a
    # top-to-bottom timeline; Plotly draws categorical y-axes bottom-to-top
    # by default, so the order is reversed to compensate.
    row_order = list(dict.fromkeys(b["name"] for b in bars))
    row_order.reverse()

    legend_seen = set()
    for bar in bars:
        colour = _GANTT_COLOURS["buy"] if bar["entered_via"] == "buy" else (
            _GANTT_COLOURS["sold"] if bar["left_via"] == "sold" else _GANTT_COLOURS["held"])
        label = ("Transfer in" if bar["entered_via"] == "buy" else
                 "Transferred out" if bar["left_via"] == "sold" else "Held")
        width = bar["end_gw"] - bar["start_gw"] + 1
        fig.add_trace(go.Bar(
            base=[bar["start_gw"] - 0.4],
            x=[width],
            y=[bar["name"]],
            orientation="h",
            marker_color=colour,
            name=label,
            legendgroup=label,
            showlegend=label not in legend_seen,
            hovertemplate=(f"<b>{bar['name']}</b> ({bar['position']})<br>"
                           f"GW{bar['start_gw']}–GW{bar['end_gw']}<br>{label}<extra></extra>"),
        ))
        legend_seen.add(label)

    # Chip weeks as shaded column highlights.
    for ev in gantt.get("chip_events", []):
        fig.add_vrect(
            x0=ev["gw"] - 0.5, x1=ev["gw"] + 0.5,
            fillcolor=_GANTT_COLOURS["chip_highlight"], opacity=0.18,
            layer="below", line_width=0,
            annotation_text=ev["chip"], annotation_position="top",
            annotation_font_color=_GANTT_COLOURS["chip_highlight"],
        )

    # Captain / vice-captain markers, drawn on top of the bars.
    cap_x, cap_y, vc_x, vc_y = [], [], [], []
    for gw, pick in gantt.get("captains", {}).items():
        if pick.get("captain"):
            cap_x.append(gw); cap_y.append(pick["captain"])
        if pick.get("vice_captain"):
            vc_x.append(gw); vc_y.append(pick["vice_captain"])
    if cap_x:
        fig.add_trace(go.Scatter(
            x=cap_x, y=cap_y, mode="markers+text", text=["C"] * len(cap_x),
            textposition="middle center", textfont=dict(color="#0B1320", size=10, weight="bold"),
            marker=dict(symbol="circle", size=20, color=_GANTT_COLOURS["captain"],
                       line=dict(color="#0B1320", width=1)),
            name="Captain", hovertemplate="Captain: %{y} (GW%{x})<extra></extra>",
        ))
    if vc_x:
        fig.add_trace(go.Scatter(
            x=vc_x, y=vc_y, mode="markers+text", text=["V"] * len(vc_x),
            textposition="middle center", textfont=dict(color="#0B1320", size=9, weight="bold"),
            marker=dict(symbol="circle", size=16, color=_GANTT_COLOURS["vice_captain"],
                       line=dict(color="#0B1320", width=1)),
            name="Vice-captain", hovertemplate="Vice-captain: %{y} (GW%{x})<extra></extra>",
        ))

    n_rows = max(len(row_order), 1)
    fig.update_layout(
        barmode="overlay",
        height=max(240, 34 * n_rows + 90),
        margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="#1E293B",     # --surface
        plot_bgcolor="#0F172A",      # --surface-2
        font=dict(color="#E2E8F0"),  # --text
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0,
                    bgcolor="rgba(0,0,0,0)"),
        xaxis=dict(
            title="Gameweek",
            tickmode="linear",
            dtick=1,
            range=[(start_gw or 0) - 0.5, (end_gw or 0) + 0.5],
            gridcolor="#334155",     # --line
            zeroline=False,
        ),
        yaxis=dict(
            categoryorder="array", categoryarray=row_order,
            gridcolor="#334155", zeroline=False,
        ),
    )
    return fig


# ------------------------------------------------------------------
# Sidebar
# ------------------------------------------------------------------
with st.sidebar:
    st.markdown("## ⚽ FPL Quant Manager")
    st.caption("Work out your best move before the deadline")

    # Strategy Mode (and the rival-shadow ID it can unlock) now lives entirely
    # in Step 3's "Strategy & Risk Mode" card -- see the Transfer Planner
    # section below. It used to render here as well, kept in sync with a
    # mobile-only duplicate via a pair of on_change callbacks; one canonical
    # control is simpler and cannot drift out of sync with itself.

    with st.expander("🧠 How it thinks", expanded=False):
        st.markdown(
            "**Why only four gameweeks?**  \n"
            "Planning ten weeks ahead sounds clever right up until Pep roulette, a "
            "hamstring, and pure vibes derail the lot. Four weeks is far enough to "
            "catch a fixture swing and near enough to still be true.\n\n"
            "**Why it so often says do nothing**  \n"
            "Because a banked transfer is worth something and a marginal one isn't. "
            "Every move has to clear a bar: the four points if you're taking a hit, "
            "plus a margin for how uncertain the gain is. Under that bar, sitting on "
            "your hands genuinely is the better play."
        )

    st.markdown("---")
    st.markdown(
        '<div class="badge"><span class="g"></span> Built by Waqas Hussain</div>',
        unsafe_allow_html=True,
    )


def _calibration_status_html() -> str:
    """Recalibration progress, from a live row count rather than a fixed date.

    The count restarts whenever MODEL_VERSION bumps (three times during this
    rebuild), and with the active-player filter the 5,000-row threshold is about
    eighteen gameweeks rather than the seven an unfiltered count implies. Any
    hardcoded "recalibrating after Gameweek N" would therefore have been wrong
    on both counts -- and the whole point of this copy pass is to stop the page
    claiming things the engine does not do.
    """
    try:
        import db as _db
        banked = _db.count_checked_predictions(fpl_tools.MODEL_VERSION)
        target = 5000
    except Exception:
        banked = None
    if banked is None:
        return ('<br><br><span class="note-inline">Model baseline active. '
                "Progress unavailable — can't reach the results database.</span>")
    if banked >= target:
        return ('<br><br><span class="note-inline">✅ Recalibrating from banked '
                f'results ({banked:,} checked predictions).</span>')
    pct = int(100 * banked / target)
    return ('<br><br><span class="note-inline">Model baseline active — '
            f'recalibration starts at {target:,} checked predictions '
            f'({banked:,} so far, {pct}%).</span>')


# ------------------------------------------------------------------
# Hero & Overview
# ------------------------------------------------------------------
safe_banner = f'<div class="dl-warning">⚠️ <b>Pro Tip:</b> Aim to confirm your transfers by <b>{SAFE_TIME_STR}</b> to avoid FPL server crashes.</div>' if SAFE_TIME_STR else ''

# Spacer so the deadline banner doesn't touch the very top of the viewport.
st.markdown("<div style='margin-top: 2rem;'></div>", unsafe_allow_html=True)

# Data-health banner, first thing on the page.
#
# Without it a failed load renders a complete-looking page built on nothing:
# _build_fixture_lookup used to return {} on error, which makes every player
# project 0.0 as a "Blank", so the squad reads as fifteen worthless assets and
# the engine dutifully recommends selling all of them. A total outage rendered
# as confident advice. It now raises, and this is where the user is told.
_bootstrap_ctx()          # attempt a load so the banner reflects reality
_err = _data_error()
if _err:
    _have_stale = _data_is_stale()
    if _have_stale:
        st.warning(
            "⚠️ **Can't reach the FPL API right now**, so these numbers are from "
            "the last successful refresh. Prices, injuries and fixtures may have "
            "moved since. Worth a reload before you commit any transfers."
        )
    else:
        st.error(
            "🔌 **Can't reach the FPL API**, so there's nothing to show yet. "
            "This is almost always temporary — FPL takes the API down around "
            "price changes and after matches. Try again in a few minutes.\n\n"
            "Nothing below is real data, so don't act on it."
        )

st.markdown(
    f'<div class="deadline-hero"><div class="dl-gw">⏰ {GW_NAME} deadline</div>'
    f'<div class="dl-time">{DEADLINE_STR}</div>'
    f'{safe_banner}</div>',
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="overview">'
    '<b>📊 What this thing actually does</b><br><br>'
    'Two jobs, in order.<br><br>'
    '• <b>It works out what every player is likely to score.</b> Who\'s going to '
    'start, how good the opposition is, and what each player has actually been '
    'producing — turned into a points forecast for the next four gameweeks.<br><br>'
    '• <b>It then finds the best squad you can legally build.</b> Every valid '
    '15, checked against your budget, your free transfers and the three-per-club '
    'rule, and it picks the one that scores most. The reasoning is shown in full '
    'underneath the recommendation — nothing is hidden.<br><br>'
    '<hr style="border:none;border-top:1px solid var(--line);margin:14px 0;">'
    '<b>🔁 It marks its own homework</b><br>'
    'Every Friday it saves what it predicted. Every Tuesday it checks that '
    'against what actually happened. Once enough checked predictions are banked, '
    'it starts correcting itself — and the week-by-week record is on the '
    '<b>Model Health</b> tab, so you can check our homework rather than take '
    'our word for it.'
    f'{_calibration_status_html()}'
    '</div>',
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------
# Risk strategy state — read from session state so `risk_label` stays
# available to every step. The interactive selector now lives in Step 3.
# ------------------------------------------------------------------
risk_label = st.session_state.get("risk", "Balanced")

# ------------------------------------------------------------------
def _gameweek_status_banner() -> str:
    ctx = _bootstrap_ctx()
    if not ctx:
        return ""
    events = ctx["bootstrap"].get("events", [])
    current = next((e for e in events if e.get("is_current") and not e.get("finished")), None)
    upcoming = next((e for e in events if e.get("is_next")), None)

    def _deadline(ev):
        try:
            raw = ev.get("deadline_time")
            if not raw:
                return ""
            dt = dateutil.parser.isoparse(raw).astimezone(tz.gettz("Europe/London"))
            return dt.strftime("%a %d %b %Y · %H:%M %Z")
        except Exception:
            return ""

    if current is not None:
        return f"⚽ Gameweek {current['id']} Live In Progress"
    if upcoming is not None:
        d = _deadline(upcoming)
        return f"Upcoming: Gameweek {upcoming['id']} · Deadline: {d}" if d else f"Upcoming: Gameweek {upcoming['id']}"
    return ""


# Main navigation
# ------------------------------------------------------------------
# Named for what the reader wants rather than what the module does.
# "Insights Lab" and "Player Radar & Market" told you nothing about which one
# holds the fixture ticker. Model Health is new: the hero copy promises "you
# can watch the scorecard rather than take our word for it", and until now
# there was nowhere to watch it.
#
# The squad views stay inside My Plan rather than getting a tab of their own.
# They are produced by the planner wizard -- a Squad tab would sit empty until
# you had run a plan, then duplicate what the plan already shows, which is more
# disjointed, not less.
#
# Transfer Roadmap is the exception to that rule: the Gantt timeline used to
# render nested inside Step 3 (My Plan > Step 3 > a sub-section two levels
# deep), which is exactly the "tucked away" problem the rest of this comment
# argues against for a Squad tab. The difference is that a rolling multi-week
# view genuinely is a distinct question from "what do I do THIS gameweek" --
# worth its own tab rather than a widget buried in the answer to that one.
tab_planner, tab_roadmap, tab_fixtures, tab_players, tab_health = st.tabs(
    ["🏟️ My Plan", "🗺️ Transfer Roadmap", "🗓️ Fixtures", "📡 Players", "🩺 Model Health"])

with tab_planner:

    # ------------------------------------------------------------------
    # Step 1 — Your Baseline Team
    # ------------------------------------------------------------------
    _gw_banner = _gameweek_status_banner()
    if _gw_banner:
        st.info(_gw_banner)

    st.markdown(
        '<div class="override-head">Step 1: Your Baseline Team</div>',
        unsafe_allow_html=True,
    )
    
    with st.container(border=True):
        st.caption(
            "Enter your Manager ID to pull your official baseline squad. This populates your current starting 11 + subs."
        )
    
        col1, col2 = st.columns([3, 1])
        if "mid" in st.query_params and "mid_input" not in st.session_state:
            st.session_state["mid_input"] = st.query_params["mid"]

        with col1:
            manager_id = st.text_input(
                "Enter your FPL Manager ID (We promise not to laugh at your overall rank)",
                key="mid_input",
                help=(
                    "Your unique FPL ID — the number in your team-page URL. "
                    "Note: the Manager ID cannot be viewed inside the official FPL iOS or Android apps. "
                    "To find it on the web: "
                    "1) Log into fantasy.premierleague.com in a web browser. "
                    "2) Go to 'Pick Team' or 'Points', then select 'Gameweek History'. "
                    "3) Check the address bar: https://fantasy.premierleague.com/entry/XXXXXXX/history. "
                    "4) The numbers replacing XXXXXXX (e.g. 1234567) are your Manager ID."
                ),
            )
        with col2:
            st.write("")
            st.write("")
            load_clicked = st.button("📋 Load my team", type="secondary", use_container_width=True, key="btn_load")
    
        st.caption(
            "Manager ID not visible inside the official FPL iOS or Android apps. On a web browser, log into "
            "fantasy.premierleague.com → 'Pick Team' or 'Points' → 'Gameweek History', then read the URL: "
            '<span class="dummy-url">https://fantasy.premierleague.com/entry/XXXXXXX/history</span> — the '
            "numbers replacing XXXXXXX (e.g. 1234567) are your Manager ID.",
            unsafe_allow_html=True,
        )
    
        if st.query_params.get("mid") and "squad_preview" not in st.session_state and not load_clicked:
            load_clicked = True
            manager_id = st.query_params["mid"]

        if load_clicked:
            if not manager_id.strip():
                st.warning("Pop your Manager ID in first — it’s the number in your FPL team-page URL.")
            else:
                st.query_params["mid"] = manager_id.strip()
                with st.spinner("Judging your recent managerial decisions... Fetching squad…"):
                    try:
                        preview = fpl_tools.score_my_squad(manager_id.strip(), GW_ID, risk=risk_label.lower())
                        st.session_state["squad_preview"] = preview
                        
                        try:
                            st.session_state["api_free_transfers_default"] = fpl_tools.get_free_transfers(manager_id.strip())
                        except:
                            st.session_state["api_free_transfers_default"] = 0
                            
                        st.rerun()
                    except Exception as e:
                        st.session_state["squad_preview"] = {"error": str(e)}
    
        preview = st.session_state.get("squad_preview")
    
        if preview and "error" in preview:
            st.error("⚠️ " + preview["error"])
            st.info("Check your Manager ID (the number in your FPL team URL) and try again.")
        elif preview:
            
            m1, m2, m3 = st.columns(3)
            m1.metric("Team", preview["team_name"])
            m2.metric("Bank (£m)", f"£{preview['bank']}m")
            m3.metric("Team Value (£m)", f"£{preview['team_value']}m")
    
            squad = preview.get("squad", [])
            starters, bench = _api_starters_bench(squad)
            cap = next((p for p in squad if p.get("is_captain")), None)
            vc = next((p for p in squad if p.get("is_vice_captain")), None)
    
            st_xp = round(sum(p.get("xp", 0) for p in starters), 2)
            if cap: st_xp += cap.get("xp", 0)
            be_xp = round(sum(p.get("xp", 0) for p in bench), 2)
            tot_xp = round(st_xp + be_xp, 2)
            
            st.session_state["base_st_xp"] = st_xp
            st.session_state["base_be_xp"] = be_xp
            st.session_state["base_tot_xp"] = tot_xp
    
            x1, x2, x3 = st.columns(3)
            x1.metric("🛡️ Baseline Starting XI xP", f"{st_xp:.2f} xP")
            x2.metric("🪑 Baseline Bench xP", f"{be_xp:.2f} xP")
            x3.metric("📊 Baseline Squad xP", f"{tot_xp:.2f} xP")
    
            with st.expander("🔍 How Your Budget & Squad Value Are Calculated", expanded=False):
                st.markdown(
                    "Your **Bank (Unspent)** reflects true FPL selling liquidity, not current market price. "
                    "When you sell a player, you only keep **50% of any profit** (rounded down to £0.1m), "
                    "so a player bought at £5.0m who rises to £5.4m sells for £5.2m. "
                    "**Team value** is your current squad's market value, but your actual spending power is "
                    "lower — the algorithm budgets using each player's **selling price**, exactly as FPL does."
                )
    
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown(_fixture_key_html(), unsafe_allow_html=True)
            pitch = _pitch_html(starters, bench, _pid(cap) if cap else None, _pid(vc) if vc else None)
            st.markdown(_card(pitch, "⚽ Your Baseline Pitch · C = Captain · V = Vice-Captain"), unsafe_allow_html=True)
            st.markdown(get_caveat_html(), unsafe_allow_html=True)
            _render_player_inspector(starters + bench)

            st.markdown(
                '<div style="font-size:0.95rem;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;color:var(--muted);margin:12px 0 10px 0;">'
                'Here is what the models and market consensus say about your squad across three core analytical engines:'
                '</div>',
                unsafe_allow_html=True,
            )
            baseline_signals = _render_positional_diagnostic(starters, bench)
            if baseline_signals:
                st.session_state["baseline_signals"] = baseline_signals
    
    
    # ------------------------------------------------------------------
    # Step 2 — Verify Variables & Midweek Changes
    # ------------------------------------------------------------------
    if st.session_state.get("squad_preview") and not "error" in st.session_state.get("squad_preview"):
        st.markdown(
            '<div class="override-head">Step 2: The Gaffer&#39;s Override (Force Players In/Out)</div>',
            unsafe_allow_html=True,
        )
    
        with st.container(border=True):
            
            st.markdown("#### 1. Confirm Your Variables")
            plan_bank = float(st.session_state.get("squad_preview", {}).get("bank", 0.0))
            
            var_col1, var_col2 = st.columns(2)
            with var_col1:
                ft_val = st.number_input("Available Free Transfers", min_value=0, max_value=5, value=1, step=1, key="available_ft", help="Set the exact number of Free Transfers you currently hold on fantasy.premierleague.com.")
            with var_col2:
                bank_val = st.number_input("Remaining Budget in Bank (£m)", 0.0, 50.0, plan_bank, 0.1, key="ov_bank")
            allow_hits = st.checkbox("Allow Point Hits (-4 pts per additional transfer)", value=False, key="ov_allow_hits", help="Disabled by default to enforce elite transfer conservation. When checked, the solver may suggest taking point deductions only if an incoming player's immediate gain outweighs the 4-point penalty.")
                
            st.markdown("---")
            st.markdown("#### 2. Midweek Transfers")
            show_override = st.checkbox("🚨 **I have already made midweek transfers** (Click to manually update your squad or upload a screenshot)", key="cb_override")
            
            override_squad = []
            if show_override:
                st.caption("Upload a screenshot or adjust the dropdowns to match your live 15-man squad.")
                
                _ctx = _bootstrap_ctx()
                if _ctx:
                    bootstrap = _ctx["bootstrap"]
                    fixture_lookup = _ctx["lookup"]
                else:
                    bootstrap = {"elements": [], "teams": []}
                    fixture_lookup = {}

                players_by_id = {p["id"]: p for p in bootstrap.get("elements", [])}
                teams = {t["id"]: t["short_name"] for t in bootstrap.get("teams", [])}
                pos_map = fpl_tools.POS_MAP

                dropdown_options = _dropdown_options(bootstrap, teams, pos_map)
    
                squad_default = st.session_state.get("squad_preview", {}).get("squad", [])
                uploaded_image = st.file_uploader("Upload screenshot to auto-fill midweek changes", type=["png", "jpg", "jpeg"], key="override_image")
    
                image_matched = []
                if uploaded_image:
                    file_id = f"{uploaded_image.name}_{uploaded_image.size}"
                    if st.session_state.get("last_uploaded_file") != file_id:
                        with st.spinner("Reading your squad screenshot…"):
                            extraction = squad_override.extract_squad_from_image(uploaded_image)
                        if extraction.get("success"):
                            matched, unmatched = squad_override.match_players_to_fpl(bootstrap, extraction.get("raw_players", []))
                            st.session_state["cached_image_matched"] = matched
                            st.session_state["cached_image_unmatched"] = unmatched
                        else:
                            st.session_state["cached_image_matched"] = []
                            st.session_state["cached_image_unmatched"] = []
                            st.warning(extraction.get("error", "Could not read the image."))
                        st.session_state["last_uploaded_file"] = file_id
                    
                    image_matched = st.session_state.get("cached_image_matched", [])
                    image_unmatched = st.session_state.get("cached_image_unmatched", [])
                    if image_unmatched:
                        st.warning(f"⚠️ Could not parse: {', '.join(image_unmatched)}. Please select them manually below.")
    
                image_by_pos = {pos: [] for pos in POS_ORDER}
                for p in image_matched:
                    if p["position"] in image_by_pos:
                        image_by_pos[p["position"]].append(p)
    
                default_selections = {pos: [] for pos in POS_ORDER}
                for p in squad_default:
                    pos = p["position"]
                    for idx, (pid, _) in enumerate(dropdown_options[pos]):
                        if pid == p.get("player_id"):
                            if idx not in default_selections[pos]:
                                default_selections[pos].append(idx)
                            break
    
                quotas = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
                
                for pos, count in quotas.items():
                    st.markdown(f"**{pos}**")
                    cols = st.columns(min(count, 3))
                    for i in range(count):
                        with cols[i % len(cols)]:
                            img_state = f"{uploaded_image.name}_{uploaded_image.size}" if uploaded_image else "none"
                            key_name = f"ov_{pos}_{i}_{img_state}"
                            idx_default = 0 
                            
                            if i < len(default_selections[pos]):
                                idx_default = default_selections[pos][i]
                            
                            if image_by_pos[pos] and i < len(image_by_pos[pos]):
                                mp = image_by_pos[pos][i]
                                for idx, (pid, _) in enumerate(dropdown_options[pos]):
                                    if pid == mp["player_id"]:
                                        idx_default = idx
                                        break
                                        
                            selected = st.selectbox(
                                f"{pos} {i+1}", options=dropdown_options[pos],
                                format_func=lambda x: x[1],
                                index=min(idx_default, len(dropdown_options[pos]) - 1) if dropdown_options[pos] else 0,
                                key=key_name, label_visibility="collapsed",
                            )
                            if selected and selected[0] is not None:
                                override_squad.append(selected[0])
    
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("⚽ Run the Numbers (In the Engine We Trust)", type="primary", use_container_width=True, key="btn_analyse_override"):
                
                active_squad_ids = override_squad if show_override else [p["player_id"] for p in st.session_state.get("squad_preview", {}).get("squad", [])]
                
                if len(active_squad_ids) != 15:
                    st.error("⚠️ Ensure you have exactly 15 valid players selected before continuing.")
                else:
                    with st.spinner("Scoring your squad and calculating optimal transfers…"):
                        try:
                            analysed = []
                            preview_squad = st.session_state.get("squad_preview", {}).get("squad", [])
                            sell_by_id = {p["player_id"]: p.get("selling_price", p.get("price")) for p in preview_squad}
                            
                            try:
                                bootstrap = fpl_tools._get_bootstrap()
                                fixture_lookup = fpl_tools._build_fixture_lookup(bootstrap)
                                players_by_id = {p["id"]: p for p in bootstrap.get("elements", [])}
                                teams = {t["id"]: t["short_name"] for t in bootstrap.get("teams", [])}
                                pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
                            except Exception:
                                pass
                                
                            for pid in active_squad_ids:
                                fpl_p = players_by_id.get(pid)
                                xp, note = fpl_tools._player_xp(fpl_p, fixture_lookup, event=GW_ID)
                                analysed.append({
                                    "player_id": pid, "name": f"{fpl_p['first_name']} {fpl_p['second_name']}",
                                    "team": teams.get(fpl_p["team"], "?"), "team_id": fpl_p["team"],
                                    "position": pos_map.get(fpl_p["element_type"], "?"),
                                    "price": fpl_p["now_cost"] / 10.0,
                                    "selling_price": sell_by_id.get(pid, fpl_p["now_cost"] / 10.0),
                                    "xp": xp, "status": note, "is_captain": False,
                                    "on_yellow_card_tightrope": fpl_tools._is_on_tightrope(fpl_p, GW_ID),
                                })
                            
                            holding_map = get_or_backfill_manager_history(manager_id, GW_ID)
                            rival_ids = st.session_state.get("rival_ids")
                            transfers = fpl_tools.suggest_transfers_for_custom_squad(
                                analysed, float(bank_val), int(st.session_state.get("available_ft", 1)), eval_chips=ALL_CHIPS, event=GW_ID, risk=risk_label.lower(),
                                holding_map=holding_map, current_gw=GW_ID,
                                allow_hits=st.session_state.get("ov_allow_hits", False),
                                rival_ids=rival_ids)

                            try:
                                log_decision(
                                    manager_id.strip(), GW_ID, "plan",
                                    float(transfers.get("net_gain", 0.0)),
                                    hits=int(transfers.get("hits", 0)),
                                    chip=None,
                                    transfers="; ".join(
                                        f"{m['out']['name']}->{m['in']['name']}"
                                        for m in transfers.get("standard_transfers", [])
                                    ) or "HOLD",
                                )
                            except Exception:
                                pass

                            st.session_state["override_analysis"] = {
                                "analysed_squad": analysed,
                                "bank": float(bank_val),
                                "ft": int(ft_val),
                                "transfers": transfers
                            }
                            st.session_state["_transfers_stale"] = False
                        except Exception as e:
                            st.error(f"Could not analyse squad: {e}")
    
    # ------------------------------------------------------------------
    # Step 3 — Transfer Planner
    # ------------------------------------------------------------------
    if "override_analysis" in st.session_state:
        st.markdown(
            '<div class="override-head">Step 3: Transfer Planner</div>',
            unsafe_allow_html=True,
        )
        with st.container(border=True):

            ov = st.session_state["override_analysis"]
            tr = ov["transfers"]

            # Twin scenario cards: Strategy & Risk Mode on the left, Chip
            # Scenario Lab on the right. st.columns stacks vertically on a
            # narrow viewport by itself (Streamlit's default since 1.32), so
            # this is the one control surface for both -- no sidebar
            # duplicate, no separately-keyed widget to keep in sync.
            col_strategy, col_chip = st.columns([1, 1])

            with col_strategy:
                st.markdown("##### 🎯 Strategy & Risk Mode")
                st.caption(
                    "Choose your risk profile. Switching dynamically recalibrates "
                    "transfer targets and projected upside across the multi-week planner."
                )
                risk_label = st.radio(
                    "Strategy",
                    RISK_OPTIONS,
                    index=0,
                    key="risk",
                    label_visibility="collapsed",
                    help="How big a gain we demand before pulling the trigger.",
                    captions=RISK_DESCRIPTIONS,
                    on_change=clear_transfer_cache,
                )
                _risk_desc = RISK_DESCRIPTIONS[RISK_OPTIONS.index(risk_label)]
                st.info(f"**{risk_label}:** {_risk_desc}")

                if risk_label in ("Conservative", "Rank Protecting (Shield)"):
                    st.text_input(
                        "Rival's team ID (optional)",
                        key="rival_id_input",
                        help="We'll shadow this rival's squad so their good weeks can't hurt you.",
                    )

            # Resolving the rival and (re)solving the plan is common to both
            # cards -- it has to run before the right column, since that
            # column reads `tr`, but after the left column, since it needs
            # `risk_label`. Streamlit reruns the whole script on any widget
            # change, so by the time execution reaches here `risk_label`
            # already reflects whichever widget the user just touched.
            if risk_label in ("Conservative", "Rank Protecting (Shield)"):
                rival_manager_id = st.session_state.get("rival_id_input", "")
                _rk = rival_manager_id.strip() if rival_manager_id else ""
                if _rk != st.session_state.get("_rival_resolved", ""):
                    st.session_state["_rival_resolved"] = _rk
                    if _rk:
                        try:
                            _rv = fpl_tools.score_my_squad(_rk, GW_ID)
                            st.session_state["rival_ids"] = {p["player_id"] for p in _rv.get("squad", [])}
                        except Exception:
                            st.session_state["rival_ids"] = None
                    else:
                        st.session_state["rival_ids"] = None
                    clear_transfer_cache()
            else:
                st.session_state["rival_ids"] = None
                st.session_state["_rival_resolved"] = ""

            # Recalculate transfers if the strategy mode (or the chip
            # scenario, or the rival) changed -- cache-busted via each
            # widget's own on_change callback, so nothing below this point
            # can ever show a plan that predates the setting it's supposed
            # to reflect.
            if st.session_state.get("_transfers_stale"):
                with st.spinner("Recalculating optimal transfers for new strategy..."):
                    try:
                        # Refresh per-player xP under the new strategy, then re-solve.
                        try:
                            _bootstrap = fpl_tools._get_bootstrap()
                            _fl = fpl_tools._build_fixture_lookup(_bootstrap)
                            _players = {p["id"]: p for p in _bootstrap.get("elements", [])}
                            for _p in ov["analysed_squad"]:
                                _fp = _players.get(_p["player_id"])
                                if _fp:
                                    _xp, _note = fpl_tools._player_xp(_fp, _fl, event=GW_ID)
                                    _p["xp"] = _xp
                                    _p["status"] = _note
                        except Exception:
                            pass
                        holding_map = get_or_backfill_manager_history(manager_id, GW_ID)
                        tr = fpl_tools.suggest_transfers_for_custom_squad(
                            ov["analysed_squad"], ov["bank"], ov["ft"],
                            eval_chips=ALL_CHIPS, event=GW_ID, risk=risk_label.lower(),
                            holding_map=holding_map, current_gw=GW_ID,
                            allow_hits=st.session_state.get("ov_allow_hits", False),
                            rival_ids=st.session_state.get("rival_ids")
                        )
                        ov["transfers"] = tr
                    except Exception as e:
                        st.warning(f"Could not recalculate transfers: {e}")
                st.session_state["_transfers_stale"] = False

            evals = tr.get("chip_evaluations", [])

            with col_chip:
                if evals:
                    st.caption(
                        "Compares competing strategy paths (e.g., rolling a transfer, "
                        "aggressive hits, or activating Wildcard/Free Hit) side-by-side "
                        "across your planning horizon to show which path yields the "
                        "highest net expected points."
                    )
                    eval_html = "".join(f"<div style='margin-bottom:6px;'>{e}</div>" for e in evals)
                    st.markdown(_card(eval_html, "🎟️ Active Chip Analysis & Recommendations"), unsafe_allow_html=True)

                chip_options = ["None (Hold Chips)"] + ALL_CHIPS

                set1_remaining = []
                try:
                    inv = fpl_tools._chip_inventory(GW_ID)
                    played = fpl_tools.get_played_chips(manager_id.strip())
                    set1_remaining = [c for c in inv["set1"] if c not in played]
                    if inv["set1"]:
                        st.markdown(f"**🎟️ Chip Scenario Lab (Set 1 · Expire GW{inv['expiry_gw']})**")
                    else:
                        st.markdown(f"**🎟️ Chip Scenario Lab (Set 2 · GW{inv['expiry_gw'] + 1}–38)**")
                    st.caption(
                        "Simulate playing a chip this week to see how your lineup and "
                        "projected points shift. Leave blank for standard rolling "
                        "transfer strategy."
                    )
                    if GW_ID >= 17 and set1_remaining:
                        st.warning(f"⚠️ Set 1 chips ({', '.join(set1_remaining)}) expire at the GW{inv['expiry_gw']} deadline — play or lose them.")
                except Exception:
                    st.markdown("**🎟️ Chip Scenario Lab**")
                    st.caption(
                        "Simulate playing a chip this week to see how your lineup and "
                        "projected points shift. Leave blank for standard rolling "
                        "transfer strategy."
                    )
                confirmed_chip = st.radio(
                    "Play a chip this Gameweek (only one allowed)",
                    chip_options,
                    index=0,
                    horizontal=True,
                    key="confirmed_chip_radio",
                    label_visibility="collapsed",
                    on_change=clear_transfer_cache,
                )
                st.session_state["active_confirmed_chip"] = confirmed_chip

            st.markdown("---")
            
            if confirmed_chip in ("Wildcard", "Free Hit"):
                moves = tr.get("wildcard_transfers", tr.get("transfers", []))
                transfer_advice = f"<b>{confirmed_chip} Active:</b> {len(moves)} transfers optimised with 0 point penalties."
            else:
                moves = tr.get("standard_transfers", tr.get("transfers", []))
                transfer_advice = tr.get("hit_advice", "")
            # Remember precisely what the screen shows, so the AI critiques the
            # plan the user is actually looking at.
            st.session_state["displayed_moves"] = moves
            st.session_state["displayed_chip"] = confirmed_chip
    
            # ---- Market Alert & Value Tracker (rendered above transfer recommendations) ----
            try:
                movers = fpl_tools.get_market_movers()
                faller_ids = {m["id"] for m in movers.get("fallers", [])}
                riser_ids = {m["id"] for m in movers.get("risers", [])}
                market_rows = []
                for p in ov["analysed_squad"]:
                    if p["player_id"] in faller_ids:
                        market_rows.append(f'<div style="margin-bottom:4px;">⚠️ <b>Imminent Price Fall Risk:</b> {p["name"]} ({p["team"]})</div>')
                for m in moves:
                    if m["in"]["id"] in riser_ids:
                        market_rows.append(f'<div style="margin-bottom:4px;">📈 <b>Imminent Price Rise Target:</b> {m["in"]["name"]} ({m["in"]["team"]})</div>')
                if market_rows:
                    market_html = '<div style="margin-bottom:6px;color:var(--line-2);">Prices update overnight (~01:30–02:30 UK). Act before the next update:</div>' + "".join(market_rows)
                else:
                    market_html = '<div style="color:var(--muted-2);">No imminent price changes detected for your squad or transfer targets.</div>'
                st.markdown(_card(market_html, "📊 Market Alert & Value Tracker"), unsafe_allow_html=True)
            except Exception:
                pass
    
            transfer_html = (
                '<div style="font-size:0.78rem;color:var(--muted);font-style:italic;margin:0 0 10px 0;">'
                'Projected points — our best guess, not a promise.'
                '</div>'
                f'<div style="color:var(--line-2);margin:4px 0 8px 0; font-weight:600;">{transfer_advice}</div>'
            )
            if moves:
                transfer_html += _transfer_pair_html(moves)
                transfer_html += _waterfall_html(tr.get("breakdown") or {})
            else:
                transfer_html += (
                    '<div style="color:var(--muted);">'
                    "Sit on your hands. Nothing on the market is worth your transfer "
                    "this week — bank it and you'll have two next week."
                    "</div>")
            st.markdown(_card(transfer_html, "⚙️ The move"), unsafe_allow_html=True)
            if moves:
                st.caption(
                    "Fixtures colored by FDR (Fixture Difficulty Rating): 🟢 Favourable, "
                    "🟡 Moderate, 🔴 Difficult. Values indicate projected points for that "
                    "specific fixture."
                )

            # ---- Scenario Distribution (SAA floor vs ceiling) ----
            try:
                sd = tr.get("scenario_distribution") or {}
                if sd:
                    p5, p50, p95 = sd.get("p5", 0.0), sd.get("p50", 0.0), sd.get("p95", 0.0)
                    dist_html = (
                        '<div style="display:flex;gap:16px;justify-content:space-between;text-align:center;">'
                        f'<div><div class="tc-meta">Floor</div><div style="font-weight:800;color:var(--neg-2);font-size:1.3rem;">{p5}</div></div>'
                        f'<div><div class="tc-meta">Expected</div><div style="font-weight:800;color:var(--text);font-size:1.3rem;">{p50}</div></div>'
                        f'<div><div class="tc-meta">Ceiling</div><div style="font-weight:800;color:var(--pos-2);font-size:1.3rem;">{p95}</div></div>'
                        '</div>'
                    )
                    st.caption(
                        "Simulates 500 gameweek outcomes using each player's variance "
                        "and minutes volatility. This reveals your realistic ceiling "
                        "(95th percentile), expected baseline (median), and floor (5th "
                        "percentile), rather than relying on a single static score."
                    )
                    st.markdown(_card(dist_html, "🎲 How it could go"), unsafe_allow_html=True)
            except Exception:
                pass

            # ---- Multi-GW Transfer Schedule ----
            try:
                plan = tr.get("multi_gw_plan") or []
                active = [s for s in plan if s.get("transfers") or s.get("buys") or s.get("sells")]
                if active:
                    rows = []
                    for s in plan:
                        buys = ", ".join(s["buys"]) or "—"
                        sells = ", ".join(s["sells"]) or "—"
                        hit = f" (-{4 * s['hits']})" if s.get("hits") else ""
                        rows.append(
                            f'<div style="display:flex;gap:10px;padding:6px 0;border-bottom:1px solid #1e293b;align-items:flex-start;">'
                            f'<div style="flex:0 0 52px;font-weight:800;color:var(--info);">GW{s["gw"]}</div>'
                            f'<div style="flex:1;min-width:0;">'
                            f'<div class="tc-meta">Sell: {sells}</div>'
                            f'<div class="tc-meta">Buy: {buys}</div>'
                            f'<div class="tc-meta">Transfers: {s["transfers"]}{hit} · FT after: {s["ft_after"]} · Bank: £{s["bank_after"]:.1f}m</div>'
                            f'</div></div>'
                        )
                    st.caption(
                        "The mathematically optimal gameweek-by-gameweek transfer "
                        "sequence, showing when to bank free transfers, when to spend "
                        "them, and how your squad carries forward."
                    )
                    st.markdown(_card("".join(rows), "🗓️ The next few weeks"), unsafe_allow_html=True)
                    # The same schedule, laid out as a Gantt-style timeline, now
                    # lives in its own "Transfer Roadmap" tab rather than tucked
                    # away here -- see tab_roadmap below.
            except Exception:
                pass





            with st.expander("🩺 Is your squad set up right?", expanded=False):
                try:
                    health = fpl_tools._squad_structural_health(ov["analysed_squad"], float(ov.get("bank", 0.0)))
                    try:
                        log_squad_health(manager_id.strip(), GW_ID, health)
                    except Exception:
                        pass
                    for h in health:
                        icon = "✅" if h["ok"] else "⚠️"
                        st.markdown(
                            f'<div style="display:flex;gap:8px;align-items:flex-start;padding:5px 0;border-bottom:1px solid #1e293b;">'
                            f'<span style="flex:0 0 auto;">{icon}</span>'
                            f'<div style="flex:1;min-width:0;">'
                            f'<div style="color:var(--text);font-weight:600;">{h["label"]}</div>'
                            f'<div class="tc-meta">{h["detail"]}</div>'
                            f'</div></div>',
                            unsafe_allow_html=True,
                        )
                except Exception:
                    st.caption("Structural health unavailable.")

            with st.expander("📊 Where you're exposed", expanded=False):
                st.caption(
                    "Tracks unowned or non-captained players heavily backed by "
                    "your rivals."
                )
                try:
                    eo_map = fpl_tools._eo_map()
                    _b = fpl_tools._get_bootstrap()
                    names = {e["id"]: e.get("web_name") or e.get("second_name") or str(e["id"]) for e in _b.get("elements", [])}
                    lineup = fpl_tools.select_starting_xi(ov["analysed_squad"])
                    xi_ids = {p["player_id"] for p in lineup["xi"]}
                    cap_id = lineup["captain"]["player_id"] if lineup.get("captain") else None
                    squad_ids = {p["player_id"] for p in ov["analysed_squad"]}
                    rows = []
                    for p in ov["analysed_squad"]:
                        eo = eo_map.get(p["player_id"], {}).get("eo", 0.0)
                        if p["player_id"] in xi_ids and eo > 100.0 and p["player_id"] != cap_id:
                            per_pt = fpl_tools._rank_exposure(1, eo, 1.0)
                            rows.append(("⚠️ Inverted", names.get(p["player_id"], p.get("name", "?")), f"EO {eo:.0f}% · {per_pt:+.2f} pts/point (rank drops when they score)"))
                    for pid, eo_d in eo_map.items():
                        if eo_d.get("eo", 0.0) > 80.0 and pid not in squad_ids:
                            per_pt = fpl_tools._rank_exposure(0, eo_d.get("eo", 0.0), 1.0)
                            rows.append(("🔻 Short", names.get(pid, str(pid)), f"Unowned · {eo_d.get('eo', 0.0):.0f}% rival ownership — If he scores, your rank drops by ~{abs(per_pt):.2f} pts per point scored."))
                    if rows:
                        html = "".join(
                            f'<div style="display:flex;gap:8px;padding:6px 0;border-bottom:1px solid #1e293b;">'
                            f'<div style="flex:0 0 auto;font-weight:700;">{flag}</div>'
                            f'<div style="flex:1;min-width:0;"><div style="color:var(--text);font-weight:600;">{name}</div>'
                            f'<div class="tc-meta">{detail}</div></div></div>'
                            for flag, name, detail in rows
                        )
                        st.markdown(_card(html, "Rank-risk exposures"), unsafe_allow_html=True)
                    else:
                        st.caption("Nothing daft here — your squad's exposure looks sensible.")
                except Exception:
                    st.caption("EO diagnostic unavailable.")

            with st.expander("💡 Why we're telling you to do this", expanded=False):
                explainer_bullets = []

                # 1. Base logic (always true).
                explainer_bullets.append(
                    "* **We checked every legal 15 you could build this week.** "
                    "This one scores highest over the next four gameweeks, once "
                    "your budget, your free transfers and the three-per-club rule "
                    "are all accounted for.")

                hits_taken = int(tr.get("hits", 0))

                # 2. Hits (only if hits > 0).
                if hits_taken > 0:
                    explainer_bullets.append(
                        f"* **The {-4 * hits_taken} points is worth paying.** These "
                        "moves are projected to win that back and then some — "
                        "otherwise we'd have told you to sit tight.")

                # 3. Goalkeeper swaps (only if a GK is transferred in or out).
                gk_involved_in_transfer = any(m["out"]["position"] == "GK" or m["in"]["position"] == "GK" for m in moves)
                if gk_involved_in_transfer:
                    explainer_bullets.append(
                        "* **There's a keeper in this.** You must carry exactly two, "
                        "so the swap keeps your money in the right places between "
                        "the sticks.")

                # 4. Late fitness gating (only if an outgoing player has a doubtful status).
                flagged_player_transferred_out = any(m["out"].get("status", "Available") != "Available" for m in moves)
                if flagged_player_transferred_out:
                    explainer_bullets.append(
                        "* **You're shifting a fitness doubt.** Anyone carrying a flag "
                        "gets marked down hard, because a player who doesn't start "
                        "scores nothing at all.")

                # 5. Banked transfer (only if 0 transfers were made).
                if len(moves) == 0:
                    explainer_bullets.append(
                        "* **Sit on your hands.** Nothing on the market is worth your "
                        "transfer this week — bank it and you'll have two next week, "
                        "which is worth more than a marginal move now.")

                if explainer_bullets:
                    st.markdown("\n".join(explainer_bullets))

            # Decide, having read the case. These buttons used to sit ABOVE the
            # three reasoning panels, which asked the reader to commit their
            # gameweek before showing them why -- the explanation only became
            # visible once you had scrolled past the decision.
            st.markdown("---")
            c_fast1, c_fast2 = st.columns(2)
            with c_fast1:
                accept_all = st.button(
                    "✅ Do it — make these moves", type="primary",
                    use_container_width=True, key="btn_accept_all")
            with c_fast2:
                fast_hold = st.button(
                    "⏭️ Leave it — keep this squad", type="secondary",
                    use_container_width=True, key="btn_fast_hold")

            if fast_hold:
                with st.spinner("Generating Final Lineup with current squad…"):
                    analysed_current = ov["analysed_squad"]
                    lineup = fpl_tools.select_starting_xi(analysed_current)
                    lineup["confirmed_chip"] = confirmed_chip
                    try:
                        log_decision(manager_id.strip(), GW_ID, "hold", 0.0,
                                     hits=0, chip=confirmed_chip, transfers="HOLD")
                    except Exception:
                        pass
                    st.session_state["manual_final"] = lineup
                    st.rerun()
    
            if accept_all:
                with st.spinner("Applying Quant transfers and generating final lineup…"):
                    sold_ids = {m["out"]["id"] for m in moves}
                    final_squad = [p for p in ov["analysed_squad"] if p["player_id"] not in sold_ids]
                    for m in moves:
                        final_squad.append({
                            "player_id": m["in"]["id"],
                            "name": m["in"]["name"],
                            "team": m["in"]["team"],
                            "team_id": m["in"]["team_id"],
                            "position": m["in"]["position"],
                            "price": m["in"]["price"],
                            "xp": m["in"].get("xp_gw", m["in"]["xp"]),
                            "status": m["in"]["status"],
                            "is_captain": False,
                            "on_yellow_card_tightrope": m["in"].get("on_yellow_card_tightrope", False),
                        })
                    lineup = fpl_tools.select_starting_xi(final_squad)
                    lineup["confirmed_chip"] = confirmed_chip
                    try:
                        log_decision(
                            manager_id.strip(), GW_ID, "accept",
                            float(tr.get("net_gain", 0.0)),
                            hits=int(tr.get("hits", 0)),
                            chip=confirmed_chip,
                            transfers="; ".join(f"{m['out']['name']}->{m['in']['name']}" for m in moves) or "HOLD",
                        )
                        if confirmed_chip and confirmed_chip != "None (Hold Chips)":
                            save_chip_play(manager_id.strip(), GW_ID, confirmed_chip)
                        save_plan(manager_id.strip(), GW_ID, tr.get("multi_gw_plan") or [])
                    except Exception:
                        pass
                    st.session_state["manual_final"] = lineup
                    st.rerun()
    
            target_default_moves = len(moves)
            last_chip_tracked = st.session_state.get("last_confirmed_chip_tracker")
            if last_chip_tracked != confirmed_chip or "n_moves_manual" not in st.session_state:
                st.session_state["n_moves_manual"] = target_default_moves
                st.session_state["last_confirmed_chip_tracker"] = confirmed_chip
                # Reset stale manual-transfer dropdown selections so the new chip's
                # recommended moves re-initialise the dropdowns from scratch.
                for k in list(st.session_state.keys()):
                    if k.startswith("man_out") or k.startswith("man_in"):
                        st.session_state.pop(k, None)
    
    
            st.markdown("---")
            st.markdown("#### Or, Customise Your Transfers Below")
            st.caption(
                "The Transfer Market: Where seasons are made or ruined. Lock in your moves below."
            )
    
            surname_by_id = {p["player_id"]: (p.get("name", "").strip().split() or [""])[-1].lower() for p in ov["analysed_squad"]}
            cur_options = [(None, "— Select Player —")] + sorted(
                [(p["player_id"], f"{p['name']} ({p['team']})") for p in ov["analysed_squad"]],
                key=lambda x: surname_by_id.get(x[0], ""),
            )
    
            # Dynamic cache-busting signature: changing the recommended moves (or the
            # chip) changes this signature and therefore the widget keys, forcing
            # Streamlit to rebuild the transfer dropdowns instead of retaining stale picks.
            moves_sig = "*".join(f"{m['out']['id']}-{m['in']['id']}" for m in moves) or "nomoves"
    
            # Cap the transfer count to a legal range (and clamp any stale session value).
            if confirmed_chip in ("Wildcard", "Free Hit"):
                n_moves_max = 15
            else:
                n_moves_max = max(len(moves), int(ov.get("ft", 0)) + 3)
            if int(st.session_state.get("n_moves_manual", 0)) > n_moves_max:
                st.session_state["n_moves_manual"] = n_moves_max
            
            n_moves = st.number_input("Number of transfers to apply", 0, n_moves_max, key="n_moves_manual")
            
            out_ids, in_ids = [], []
            try:
                bootstrap = fpl_tools._get_bootstrap()
                players_by_id = {p["id"]: p for p in bootstrap.get("elements", [])}
                teams = {t["id"]: t["short_name"] for t in bootstrap.get("teams", [])}
                pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    
                # Incoming options filtered strictly by position so the UI can never
                # display a cross-positional swap (outgoing DEF only pairs with incoming DEF).
                in_options_by_pos = {pos: [(None, "— Select Player —")] for pos in POS_ORDER}
                for p in bootstrap.get("elements", []):
                    pos = pos_map.get(p["element_type"])
                    if pos:
                        initial = p['first_name'][0] + "." if p.get('first_name') else ""
                        display = f"{initial} {p['second_name']} ({teams.get(p['team'], '?')}) £{p['now_cost']/10:.1f}m"
                        in_options_by_pos[pos].append((p["id"], display))
                surname_by_id = {p["id"]: _surname(p) for p in bootstrap.get("elements", [])}
                for pos in POS_ORDER:
                    in_options_by_pos[pos] = [in_options_by_pos[pos][0]] + sorted(
                        in_options_by_pos[pos][1:], key=lambda x: surname_by_id.get(x[0], "")
                    )
            except:
                in_options_by_pos = {pos: cur_options for pos in POS_ORDER}
    
            cur_id_to_pos = {p["player_id"]: p["position"] for p in ov["analysed_squad"]}
    
            # Render rows in the exact priority order of `moves` (same as the
            # "Optimized Transfers" card). Reducing n_moves shows only the top N rows.
            for i in range(int(n_moves)):
                c1, c2 = st.columns(2)
                def_out_idx = 0
                if i < len(moves):
                    def_out_idx = _get_dropdown_index(cur_options, moves[i]["out"]["id"])
    
                key_out = f"man_out*{i}*{moves_sig}"
                out_sel = c1.selectbox(f"Transfer Out {i+1}", options=cur_options, format_func=lambda x: x[1], index=def_out_idx, key=key_out)
    
                # Lock the incoming list strictly to the outgoing player's position.
                out_pos = cur_id_to_pos.get(out_sel[0]) if out_sel and out_sel[0] is not None else None
                if out_pos is None and i < len(moves):
                    out_pos = moves[i]["out"].get("position")
                if out_pos not in in_options_by_pos:
                    out_pos = "MID"
                in_options = in_options_by_pos[out_pos]
    
                # Locate the recommended incoming player directly in the filtered list.
                def_in_idx = 0
                if i < len(moves):
                    def_in_idx = _get_dropdown_index(in_options, moves[i]["in"]["id"])
    
                key_in = f"man_in*{i}_{moves_sig}"
                in_sel = c2.selectbox(f"Transfer In {i+1}", options=in_options, format_func=lambda x: x[1], index=def_in_idx, key=key_in)
    
                if out_sel[0]: out_ids.append(out_sel[0])
                if in_sel[0]: in_ids.append(in_sel[0])
    
            st.markdown("<br>", unsafe_allow_html=True)
            apply_btn = st.button("Apply Custom Transfers & Generate Final Lineup", type="primary", use_container_width=True, key="btn_apply_man")
    
            if apply_btn:
                errors = []
                new_ids = [p["player_id"] for p in ov["analysed_squad"]]
                
                if len(out_ids) != int(n_moves) or len(in_ids) != int(n_moves):
                    errors.append("Please complete all transfer selections.")
                
                for oid in out_ids:
                    if oid in new_ids: new_ids.remove(oid)
                for iid in in_ids:
                    if iid in new_ids: errors.append("You already own that player.")
                    new_ids.append(iid)
    
                if not errors:
                    try:
                        original_cost = sum(players_by_id[p["player_id"]]["now_cost"] / 10.0 for p in ov["analysed_squad"] if p["player_id"] in players_by_id)
                        new_cost = sum(players_by_id[pid]["now_cost"] / 10.0 for pid in new_ids if pid in players_by_id)
                        available_budget = original_cost + ov["bank"]
                        
                        if new_cost > available_budget + 0.001: 
                            errors.append(f"Not enough funds! Your manual transfers cost £{new_cost:.1f}m, but your maximum budget is £{available_budget:.1f}m.")
                    except Exception:
                        pass
    
                if not errors:
                    counts = {"GK": 0, "DEF": 0, "MID": 0, "FWD": 0}
                    for pid in new_ids:
                        fpl_p = players_by_id.get(pid)
                        pos = pos_map.get(fpl_p["element_type"], "?") if fpl_p else "?"
                        counts[pos] = counts.get(pos, 0) + 1
                    if counts != {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}:
                        st.error("That combination doesn't make a valid 2-5-5-3 squad. Check your transfers.")
                    else:
                        with st.spinner("Generating Final Lineup…"):
                            analysed_final = []
                            fixture_lookup = fpl_tools._build_fixture_lookup(bootstrap)
                            
                            for pid in new_ids:
                                fpl_p = players_by_id.get(pid)
                                xp, note = fpl_tools._player_xp(fpl_p, fixture_lookup, event=GW_ID)
                                analysed_final.append({
                                    "player_id": pid, "name": f"{fpl_p['first_name']} {fpl_p['second_name']}",
                                    "team": teams.get(fpl_p["team"], "?"), "team_id": fpl_p["team"],
                                    "position": pos_map.get(fpl_p["element_type"], "?"),
                                    "price": fpl_p["now_cost"] / 10.0, "xp": xp, "status": note, "is_captain": False
                                })
                                
                            lineup = fpl_tools.select_starting_xi(analysed_final)
                            lineup["confirmed_chip"] = confirmed_chip
                            st.session_state["manual_final"] = lineup
                            st.rerun()
    
                if errors:
                    for e in errors: st.markdown(f'<div class="alert-box">{e}</div>', unsafe_allow_html=True)
    
    # ------------------------------------------------------------------
    # Step 4 — Final Lineup Output
    # ------------------------------------------------------------------
    man_final = st.session_state.get("manual_final")
    if man_final:
        st.markdown(
            '<div class="override-head">Step 4: Final Lineup & Captaincy</div>',
            unsafe_allow_html=True,
        )
        with st.container(border=True):
            st.caption("Here is your optimal starting formation, bench order, and captaincy based on your final decisions.")

            # Live H2H vs Rival (if a rival was specified).
            try:
                _rival_id = st.session_state.get("rival_id_input")
                if _rival_id and _rival_id.strip():
                    live = fpl_tools.get_live_event(GW_ID)
                    my_squad = st.session_state.get("squad_preview", {}).get("squad", [])
                    _rv = fpl_tools.score_my_squad(_rival_id.strip(), GW_ID)
                    h2h = fpl_tools.compute_h2h(my_squad, _rv.get("squad", []), live)
                    if h2h.get("my_rows"):
                        margin = h2h["margin"]
                        colour = "#10b981" if margin >= 0 else "#ef4444"
                        h2h_html = (
                            '<div style="display:flex;gap:16px;justify-content:space-between;text-align:center;margin-bottom:10px;">'
                            f'<div><div class="tc-meta">You</div><div style="font-weight:800;font-size:1.4rem;color:var(--text);">{h2h["my_total"]}</div></div>'
                            f'<div><div class="tc-meta">Margin</div><div style="font-weight:800;font-size:1.4rem;color:{colour};">{margin:+.1f}</div></div>'
                            f'<div><div class="tc-meta">Rival</div><div style="font-weight:800;font-size:1.4rem;color:var(--text);">{h2h["rival_total"]}</div></div>'
                            '</div>'
                        )
                        for r in h2h["my_rows"]:
                            prog = " 🕒" if r["in_progress"] else ""
                            cap = " (C)" if r["multiplier"] > 1 else ""
                            h2h_html += (
                                f'<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #1e293b;">'
                                f'<span style="color:var(--text);">{r["name"]}{cap}{prog}</span>'
                                f'<span style="font-weight:700;color:var(--text);">{r["points"]}</span></div>'
                            )
                        st.markdown(_card(h2h_html, "🆚 Live H2H vs Rival"), unsafe_allow_html=True)
            except Exception:
                pass
            
            xi = man_final
            cap = xi["captain"]
            vcap = xi["vice_captain"]
            active_chip = xi.get("confirmed_chip", "None (Hold Chips)")
            
            is_tc = "Triple Captain" in active_chip
            is_bb = "Bench Boost" in active_chip
            is_wc = "Wildcard" in active_chip
            is_fh = "Free Hit" in active_chip
    
            if is_tc:
                st.markdown(f'<div class="chip-banner">⭐ <b>Triple Captain Active:</b> {cap["name"]}\'s score is multiplied by 3!</div>', unsafe_allow_html=True)
            elif is_bb:
                st.markdown('<div class="chip-banner">🚀 <b>Bench Boost Active:</b> All 4 bench players actively score points towards your Gameweek total!</div>', unsafe_allow_html=True)
            elif is_wc or is_fh:
                st.markdown(f'<div class="chip-banner">🃏 <b>{active_chip} Active:</b> Squad restructured with 0 transfer point hits applied.</div>', unsafe_allow_html=True)
    
            base_st = st.session_state.get("base_st_xp", 0.0)
            base_be = st.session_state.get("base_be_xp", 0.0)
            base_tot = st.session_state.get("base_tot_xp", 0.0)
    
            st_xp = xi["total_xp"]
            if is_tc and cap:
                st_xp = round(st_xp + cap.get("xp", 0), 2)
                
            be_xp = round(sum(p.get("xp", 0) for p in xi["bench"]), 2)
            
            if is_bb:
                st_xp = round(st_xp + be_xp, 2)
                be_xp = 0.0 
    
            tot_xp = round(st_xp + be_xp, 2)
            
            delta_st = st_xp - base_st
            delta_be = be_xp - base_be
            delta_tot = tot_xp - base_tot
            
            # Scorecard first: it says whether the squad is BUILT right. The
            # points row underneath says what it is expected to score. The old
            # layout showed only the second, so a squad could look healthy on
            # points while resting on four rotation risks and £22m of bench.
            _sc_ctx = _bootstrap_ctx() or {}
            _sc = _squad_scorecard(xi["xi"], xi["bench"], _sc_ctx.get("players_by_id", {}))
            _sc["xi_points"] = round(st_xp, 1)
            st.markdown(
                _card(_scorecard_html(_sc, delta_st if delta_st else None),
                      "📋 How your squad stacks up"),
                unsafe_allow_html=True,
            )

            y1, y2, y3 = st.columns(3)
            y1.metric("🛡️ Starting XI", f"{st_xp:.2f} pts", f"{delta_st:+.2f}" if delta_st != 0 else None)
            y2.metric("🪑 Bench", f"{be_xp:.2f} pts", f"{delta_be:+.2f}" if delta_be != 0 else None)
            y3.metric("📊 Whole squad", f"{tot_xp:.2f} pts", f"{delta_tot:+.2f}" if delta_tot != 0 else None)
            st.caption("Projected points for this gameweek, against the squad you started with.")

            with st.expander("🛡️ How Your Starting XI, Captain & Bench Are Picked", expanded=False):
                st.markdown(
                    "The line-up maximises projected points while **always respecting legal FPL formations** "
                    "(at least 3 defenders and 1 forward). Bench ordering prioritises legal auto-substitutions: "
                    "the **first bench slot is reserved to cover a minimum 3-DEF or 1-FWD formation** if a starter "
                    "returns zero minutes. The **backup goalkeeper is locked to Bench Slot 4** and can only replace "
                    "the starting keeper. The **captain** is your highest-projected scorer, with the **vice-captain** "
                    "chosen from a different fixture to guard against postponements."
                )
    
            st.markdown(_fixture_key_html(), unsafe_allow_html=True)
            pitch_final = _pitch_html(xi["xi"], xi["bench"], _pid(cap) if cap else None, _pid(vcap) if vcap else None)
            st.markdown(_card(pitch_final, f'⚽ Final Pitch View · {xi["formation"][0]}-{xi["formation"][1]}-{xi["formation"][2]} · C = Captain · V = Vice-Captain'), unsafe_allow_html=True)
            st.markdown(get_caveat_html(), unsafe_allow_html=True)
            
            _render_positional_diagnostic(
                xi["xi"], xi["bench"],
                prev=st.session_state.get("baseline_signals", {}),
                caption="The final roll call — each department measured against your original XI (green is progress).",
            )

            mult_str = "×3" if is_tc else "×2"
            mult_val = cap["xp"] * 3 if is_tc else cap["xp"] * 2
            cap_role_title = "Captain (Triple Captain Active)" if is_tc else "Captain"
            
            cap_html = f'<div class="grid"><div class="pc cap-card">{_pos_chip(cap["position"])}<div style="margin-bottom:2px;" class="nm">⭐ {cap["name"]}</div><div class="meta">{cap["team"]} — {cap_role_title}</div><div class="xp" style="color:var(--warn-2); margin-top:4px;">{cap["xp"]} xP ({mult_str} = {mult_val:.2f} xP)</div></div>'
            if vcap:
                cap_html += f'<div class="pc">{_pos_chip(vcap["position"])}<div style="margin-bottom:2px;" class="nm">{vcap["name"]}</div><div class="meta">{vcap["team"]} — Vice-Captain</div><div class="xp" style="color:var(--line-2); margin-top:4px;">{vcap["xp"]} xP</div></div>'
            cap_html += "</div>"
            st.markdown(_card(cap_html, "⭐ Captaincy"), unsafe_allow_html=True)
    


    st.markdown("---")
    st.markdown("### 👾 The FPL Final Boss")
    st.markdown("Step into the manager's office. Present your transfers to the Final Boss for a brutal tactical interrogation.")

    if "ai_response" not in st.session_state:
        st.session_state.ai_response = None
    if "last_ai_prompt" not in st.session_state:
        st.session_state.last_ai_prompt = None

    fb_lineup = st.session_state.get("manual_final")

    fb_ai_prompt = None
    fb_system_prompt = None
    if fb_lineup:
        fb_tr = st.session_state.get("override_analysis", {}).get("transfers", {})
        # The exact moves rendered above. Previously this read
        # fb_tr.get("transfers", ...) -- and the engine returns "transfers" and
        # "standard_transfers" as the SAME object, with "wildcard_transfers"
        # separate. So with a Wildcard or Free Hit confirmed the screen showed
        # the 15-transfer chip plan while the AI was handed the standard
        # 1-transfer plan and reviewed something the user could not see.
        fb_moves = st.session_state.get("displayed_moves")
        if fb_moves is None:
            fb_moves = fb_tr.get("transfers", fb_tr.get("standard_transfers", []))
        fb_hits = int(fb_tr.get("hits", 0))
        fb_hit_cost = fpl_tools._risk_profile(risk_label.lower()).get("hit_cost", 4.0)
        fb_total_hit = fb_hits * fb_hit_cost

        fb_xi_str = "; ".join(f"{p.get('name', '?')} ({p.get('team', '?')})" for p in fb_lineup.get("xi", []))
        fb_bench_str = "; ".join(f"{p.get('name', '?')} ({p.get('team', '?')})" for p in fb_lineup.get("bench", []))
        fb_move_str = "; ".join(f"{m['out']['name']} -> {m['in']['name']}" for m in fb_moves) or "None (holding)"

        fb_net_gain = float(fb_tr.get("net_gain", 0.0))
        fb_context = (
            f"Starting XI: {fb_xi_str}\n"
            f"Bench: {fb_bench_str}\n"
            f"Proposed transfers: {fb_move_str}\n"
            f"Transfers made: {len(fb_moves)}\n"
            f"Net hit deduction: -{fb_total_hit} ({fb_hits} hits)\n"
            f"Projected net xP delta: +{fb_net_gain:.1f}\n"
        )

        fb_system_prompt = (
            "You are The FPL Final Boss. You are auditing an already-solved quantitative transfer plan. "
            "You must critique, stress-test, and contextualise THESE EXACT MOVES. Never propose conflicting "
            "moves or alternative transfers.\n"
            "CHIP DISCIPLINE: If chips are disabled or inactive in the user context, you are strictly "
            "FORBIDDEN from suggesting a Wildcard, Free Hit, Bench Boost, or Triple Captain. Never suggest "
            "them as alternatives.\n"
            "CRITICAL FPL MATH: Transfer point deductions are strictly multiples of 4 (-4, -8, -12). NEVER "
            "invent figures such as -9.\n"
            "CAPTAINCY: Validate the highest-ceiling asset. If an elite premium faces weak opposition (e.g., "
            "Haaland vs newly promoted or struggling opposition), validate the quantitative favourite. Do not "
            "recommend contrarian differentials for the sake of it.\n"
            "You are a former overall Fantasy Premier League winner and quantitative macro planner. Be concise, "
            "highly tactical, and data-driven. Return exactly 3-4 short bullet points."
        )
        fb_ai_prompt = (
            "Audit this already-solved FPL transfer plan over the next 4 gameweeks and give 3-4 concise "
            "tactical bullets. These moves are immutable — critique, stress-test, and contextualise them; do "
            "not propose conflicting moves or alternative transfers.\n"
            "1. 'Killing It' Benchmark: if the engine recommends 0 transfers (banking the free transfer), "
            "validate structural health and endorse rolling the transfer for future leverage.\n"
            "2. 'Crisis' Benchmark: if it recommends a -8 hit or worse, or flags widespread "
            "injury/suspension disruption, stress-test the cost of the point hits against the projected net "
            "xP delta.\n"
            "3. Managerial changes & tactical upheaval: flag assets at clubs with recent real-world managerial "
            "sackings or new appointments, weighing 'new manager bounce' upside against role uncertainty.\n"
            "4. Macro calendar & disciplinary flags: upcoming fixture swings beyond 4 weeks, yellow-card "
            "suspension thresholds, European fixture congestion, and mid-season tournaments (e.g. AFCON).\n"
            "5. Captaincy Sanity Check: validate that the armband is anchored to the highest-ceiling, most "
            "reliable premium asset; if a differential is being captained, flag the risk.\n"
            "6. Bench Balance Audit: warn if too much team value is trapped on the bench (bench fodder should "
            "have secure baseline minutes at minimal cost, not premium rotational assets).\n\n"
            f"Immutable team context:\n{fb_context}"
        )

    if not fb_lineup:
        st.info("Generate your final lineup (Step 4) to unlock the AI summary.")
    else:
        if st.button("Press here to face the Final Boss (If you dare)", type="primary"):
            with st.spinner("The Final Boss is reviewing your tactics... brace yourself for impact."):
                api_key = os.environ.get("DEEPSEEK_API_KEY")
                if not api_key:
                    st.warning("API key missing. Please configure the environment variable.")
                else:
                    response_text = None
                    for attempt in range(3):
                        try:
                            st.toast(f"The Final Boss is pondering deeply... ({attempt + 1}/3)")
                            resp = requests.post(
                                "https://api.deepseek.com/chat/completions",
                                headers={
                                    "Authorization": f"Bearer {api_key}",
                                    "Content-Type": "application/json",
                                },
                                json={
                                    "model": "deepseek-v4-flash",
                                    "messages": [
                                        {"role": "system", "content": fb_system_prompt},
                                        {"role": "user", "content": fb_ai_prompt},
                                    ],
                                    "temperature": 0.4,
                                },
                                timeout=120,
                            )
                            resp.raise_for_status()
                            response_text = resp.json()["choices"][0]["message"]["content"]
                            break
                        except Exception as e:
                            if attempt < 2:
                                continue
                            st.error(f"API Request Failed: {str(e)}")

                    if response_text is not None:
                        st.session_state.ai_response = response_text
                        st.session_state.last_ai_prompt = fb_ai_prompt

        if st.session_state.ai_response:
            if st.session_state.last_ai_prompt == fb_ai_prompt:
                st.markdown(st.session_state.ai_response)
            else:
                st.warning("⚠️ Tactics altered! The previous verdict is void. Face the Final Boss again to validate your new setup.")

with tab_roadmap:
    st.markdown(
        """
### 🗺️ The Rolling Transfer Roadmap
*A gameweek-by-gameweek forecast of how the solver plans to shape your 15-man squad over the upcoming horizon. Chip weeks are shaded, and **C** / **V** mark projected captaincy picks.*

> ⚠️ **Key caveats to keep in mind:**
> - **A roadmap, not a contract:** Projections assume current player health, availability, and prices. Midweek European injuries, press-conference updates, sudden benchings, and price swings will alter upcoming moves.
> - **Dynamic recalculation:** The optimizer re-solves before every deadline with fresh data. Treat later gameweeks as an indicator of team structure rather than locked-in transfers.
        """
    )
    if "override_analysis" not in st.session_state:
        st.info("Run Step 2 in **My Plan** first — the roadmap builds on the "
                "same optimiser output your transfer plan does.")
    else:
        try:
            ov = st.session_state["override_analysis"]
            tr = ov["transfers"]
            plan = tr.get("multi_gw_plan") or []
            xp_lookup = {p["name"]: p.get("xp", 0.0) for p in ov["analysed_squad"]}
            gantt = fpl_tools.build_transfer_gantt_data(
                ov["analysed_squad"], plan, xp_lookup=xp_lookup)
            if gantt["bars"]:
                st.plotly_chart(
                    _transfer_gantt_figure(gantt),
                    width="stretch",
                    config={"displayModeBar": False},
                )
            else:
                st.caption("No schedule to show yet — the optimiser found nothing "
                          "worth changing across the horizon.")
        except Exception as e:
            st.caption(f"Timeline unavailable: {e}")

with tab_fixtures:
    st.markdown("### 🗓️ Fixtures & form")
    ctx = _bootstrap_ctx()
    if not ctx:
        st.info("Live FPL data could not be loaded. Check your connection and refresh.")
    else:
        teams_by_id = ctx["teams_by_id"]
        team_names = sorted([(t["id"], t["name"]) for t in ctx["bootstrap"].get("teams", [])], key=lambda x: x[1])

        st.markdown("#### 🔄 Fixture Rotation Matrix")
        st.markdown(
            "*Identifies optimal budget pairings (e.g. rotating two £4.5m defenders or £4.5m goalkeepers) so you "
            "consistently field an asset with a favourable fixture every Gameweek, mathematically eliminating schedule dead-ends.*"
        )
        anchor_name = st.selectbox("Anchor Team (optional)", ["— Any —"] + [n for _, n in team_names], key="rot_anchor")
        anchor_id = None
        if anchor_name != "— Any —":
            anchor_id = next((tid for tid, n in team_names if n == anchor_name), None)

        pairings = _fixture_rotation_matrix(anchor_team_id=anchor_id, n=6, top=8)
        if pairings:
            html = ""
            for p in pairings:
                t1 = teams_by_id[p["t1"]]; t2 = teams_by_id[p["t2"]]
                cells = "".join(_fdr_cell(v) for v in p["series"])
                html += (
                    f'<div class="rot-card">'
                    f'<div class="rot-score">{p["avg"]:.2f}</div>'
                    f'<div style="flex:1;">'
                    f'<div style="display:flex;align-items:center;gap:8px;font-weight:700;color:var(--text);">'
                    f'{_badge_img(p["t1"])} {t1["name"]} <span style="color:var(--muted);">+</span> {_badge_img(p["t2"])} {t2["name"]}'
                    f'</div>'
                    f'<div class="fdr-strip">{cells}</div>'
                    f'</div></div>'
                )
            st.markdown(_card(html, "Top Rotation Pairings · highest combined ease"), unsafe_allow_html=True)
        else:
            st.info("No rotation pairings found.")

        st.markdown("#### 🛡️ Who's actually any good")
        strengths = _team_strength_index()
        if strengths:
            grid = '<div class="grid">'
            for s in strengths:
                att_pct = int(s["att"] / 5 * 100)
                def_pct = int(s["def"] / 5 * 100)
                grid += (
                    f'<div class="strength-card">'
                    f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">'
                    f'{_badge_img(s["id"], large=True)} <span style="font-weight:700;color:var(--text);">{s["name"]}</span>'
                    f'</div>'
                    f'<div style="font-size:0.7rem;color:var(--muted);">Attack {s["att"]:.1f}/5</div>'
                    f'<div class="strength-bar"><div class="strength-fill-home" style="width:{att_pct}%"></div></div>'
                    f'<div style="font-size:0.7rem;color:var(--muted);">Defence {s["def"]:.1f}/5</div>'
                    f'<div class="strength-bar"><div class="strength-fill-away" style="width:{def_pct}%"></div></div>'
                    f'</div>'
                )
            grid += "</div>"
            st.markdown(_card(grid, "20 Clubs Ranked by Attack + Defence"), unsafe_allow_html=True)

        st.markdown("#### 📈 Fixtures turning good & turning bad")
        swings = fpl_tools._fixture_swing_scores(ctx["lookup"], ctx["start"], n=6)
        if swings:
            enter = [s for s in swings if s["att_slope"] > 0][:5]
            exit_ = sorted([s for s in swings if s["att_slope"] < 0], key=lambda s: s["att_slope"])[:5]
            c_enter, c_exit = st.columns(2)
            with c_enter:
                html = "".join(
                    f'<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #1e293b;">'
                    f'{_badge_img(s["team_id"])} <span style="font-weight:600;color:var(--text);">{s["name"]}</span>'
                    f'<span style="margin-left:auto;color:var(--pos-2);font-weight:700;">+{s["att_slope"]:.2f}</span></div>'
                    for s in enter
                )
                st.markdown(_card(html or '<div style="color:var(--muted-2);">No improving fixtures.</div>', "🟢 Prime entry windows (attackers)"), unsafe_allow_html=True)
            with c_exit:
                html = "".join(
                    f'<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #1e293b;">'
                    f'{_badge_img(s["team_id"])} <span style="font-weight:600;color:var(--text);">{s["name"]}</span>'
                    f'<span style="margin-left:auto;color:var(--neg-2);font-weight:700;">{s["att_slope"]:.2f}</span></div>'
                    for s in exit_
                )
                st.markdown(_card(html or '<div style="color:var(--muted-2);">No deteriorating fixtures.</div>', "🔴 Exit windows (attackers)"), unsafe_allow_html=True)

with tab_players:
    st.markdown("### 📡 Players & the market")
    risk = risk_label.lower()

    st.markdown("#### 📈 This week's ins and outs")
    mom = _market_momentum(limit=5)
    tr_, tf_ = st.tabs(["🔥 Risers", "🧊 Fallers"])
    with tr_:
        if mom["risers"]:
            html = "".join(_momentum_row_html(r, True) for r in mom["risers"])
            st.markdown(_card(html, "Top 5 Transfer Surges"), unsafe_allow_html=True)
        else:
            st.info("No riser data available.")
    with tf_:
        if mom["fallers"]:
            html = "".join(_momentum_row_html(r, False) for r in mom["fallers"])
            st.markdown(_card(html, "Top 5 Sell-offs"), unsafe_allow_html=True)
        else:
            st.info("No faller data available.")

    st.markdown("#### 🎯 Player Radar Shortlists")
    kind = st.radio("Filter", ["Differentials", "Best Value", "Top Points"], horizontal=True, key="radar_kind")
    kmap = {"Differentials": "differentials", "Best Value": "value", "Top Points": "points"}
    rows = _radar_shortlists(kmap[kind], limit=12)
    if rows:
        ctx = _bootstrap_ctx()
        lookup = ctx["lookup"]; start = ctx["start"]
        grid = '<div class="grid">'
        for r in rows:
            lights = fpl_tools._fixture_traffic_lights(r["team"], lookup, start).strip("[]")
            grid += (
                f'<div class="radar-card pmc">'
                f'<div class="pmc-photo">{_headshot_tile(r["photo"])}</div>'
                f'<div class="pmc-info">'
                f'<div class="nm">{r["name"]}</div>'
                f'<div class="meta">{r["pos"]} · {_badge_img(r["team"])} · £{r["price"]:.1f}m</div>'
                f'<div class="meta">Owned {r["ownership"]:.1f}% · {lights}</div>'
                f'</div>'
                f'<div class="pmc-xp">{r["xp"]} xP</div>'
                f'</div>'
            )
        grid += "</div>"
        sort_label = "xP per £1m" if kind == "Best Value" else "4-GW xP"
        st.markdown(_card(grid, f"{kind} · sorted by {sort_label}"), unsafe_allow_html=True)
    else:
        st.info("No players match this filter.")

    st.markdown("#### 🧲 Running hot & running cold")
    try:
        reg = fpl_tools.get_regression_candidates()
        c_sell, c_buy = st.columns(2)
        with c_sell:
            sell_rows = "".join(_regression_row(r, True) for r in reg["sell_high"][:8])
            st.markdown(
                _card(sell_rows or '<div style="color:var(--muted-2);">No clear over-performers right now.</div>',
                      "📉 Over-performing — SELL-HIGH candidates"),
                unsafe_allow_html=True,
            )
        with c_buy:
            buy_rows = "".join(_regression_row(r, False) for r in reg["buy_low"][:8])
            st.markdown(
                _card(buy_rows or '<div style="color:var(--muted-2);">No clear under-performers right now.</div>',
                      "📈 Under-performing — BUY-LOW candidates"),
                unsafe_allow_html=True,
            )
    except Exception:
        pass
    st.markdown(get_caveat_html(), unsafe_allow_html=True)

    
    # ------------------------------------------------------------------
    # Player Scout
    # ------------------------------------------------------------------
    with st.expander("📊 Player Scout & xP Rankings", expanded=False):
        t1, t2 = st.columns(2)
        with t1:
            pos_filter = st.selectbox("Position", ["All"] + POS_ORDER, key="tp_pos")
        with t2:
            max_price = st.number_input("Max price (£m, 0 = no limit)", 0.0, 15.5, 0.0, 0.5, key="tp_price")
        if st.button("Rank", type="secondary", key="btn_rank"):
            with st.spinner("Ranking…"):
                r = fpl_tools.rank_players_by_xp(
                    position=None if pos_filter == "All" else pos_filter,
                    max_price=None if max_price == 0 else max_price,
                    limit=20, risk=risk_label.lower(),
                )
            st.session_state["rankings"] = r
        r = st.session_state.get("rankings")
        if r:
            ov_xp = max([p["xp"] for p in r["players"]] + [1.0])
            html = '<div class="grid">'
            for p in r["players"]:
                name = p["name"]
                if p.get("hazard"):
                    name = f"{name} {p['hazard']}"
                html += _player_card({"position": p["position"], "name": name, "team": p["team"],
                                      "price": p["price"], "xp": p["xp"], "status": p.get("status", "")}, ov_xp)
            html += "</div>"
            st.markdown(_card(html, "📈 Ranked by xP"), unsafe_allow_html=True)
            st.markdown(get_caveat_html(), unsafe_allow_html=True)


def _public_model_label(version: str) -> str:
    """A generic build label for the one place the internal model-version
    string was shown verbatim to every visitor.

    fpl_tools.MODEL_VERSION carries a descriptive slug (e.g. "v7-minutes-
    recency") on purpose -- auto_tune and the version-boundary tests need
    exactly that specificity, since it says precisely which forecast change
    the stamp marks. Showing that same slug on a public page hands anyone who
    loads it a dated log of what the last algorithmic change was, for free.
    A manager gets nothing from the codename; a competitor gets a changelog.
    Only the DISPLAY is generic -- fpl_tools.MODEL_VERSION itself, and every
    internal consumer of it, is untouched.
    """
    m = re.match(r"^v(\d+)-", version or "")
    return f"Model build {m.group(1)}" if m else "Model baseline"


# ------------------------------------------------------------------
# Model Health -- marking our own homework, in public
# ------------------------------------------------------------------
with tab_health:
    st.markdown("### 🩺 How well is the model actually doing?")
    st.caption(
        "Every Friday we save what we predicted. Every Tuesday we check it "
        "against what happened. This page is that record — including the weeks "
        "we got it wrong."
    )

    _mh_target = 5000
    try:
        import db as _mh_db
        _mh_banked = _mh_db.count_checked_predictions(fpl_tools.MODEL_VERSION)
        _mh_rows = _mh_db.prediction_accuracy_by_gw(fpl_tools.MODEL_VERSION)
    except Exception:
        _mh_banked, _mh_rows = None, []

    if _mh_banked is None:
        # Say WHICH failure it was. "Unavailable" is the same message for a
        # missing environment variable and a database that is refusing
        # connections, and only one of those is a five-second fix.
        try:
            import db as _db_err
            _kind, _detail = _db_err.last_db_error() or ("unknown", "")
        except Exception:
            _kind, _detail = "unknown", ""
        _why = {
            "config": "No results database is configured for this deployment "
                      "(`DATABASE_URL` isn't set).",
            "driver": "The database driver isn't installed in this deployment.",
            "connect": "The results database refused the connection or timed out.",
            "query": "Connected, but the query itself failed — most likely the "
                     "`fpl_predictions` table or its columns don't exist yet "
                     "(migrations haven't run), or the connection was dropped "
                     "mid-query.",
        }.get(_kind, "The results database couldn't be reached.")
        st.warning(
            f"**No scorecard to show.** {_why}\n\n"
            "The projections on the other tabs are unaffected — they don't need it."
            + (f"\n\n`{_detail[:200]}`" if _detail else "")
        )
    else:
        pct = min(100, int(100 * _mh_banked / _mh_target))
        # The third tile used to print _public_model_label(fpl_tools.MODEL_VERSION)
        # (e.g. "Model build 8") -- a masked but still versioned readout. There has
        # never been a user-facing control behind it: every query on this page reads
        # fpl_tools.MODEL_VERSION directly, is never parameterised by anything the
        # visitor picks, and stays that way here too. This tile now says only that
        # the pipeline is live, using the same .badge/.g "active" pill as the
        # attribution line at the foot of the page, not what build number it is.
        st.markdown(
            _card(
                '<div class="mh-grid">'
                f'<div class="sc-tile"><div class="sc-label">Predictions checked</div>'
                f'<div class="sc-value">{_mh_banked:,}</div>'
                f'<div class="sc-sub">against real results</div></div>'
                f'<div class="sc-tile"><div class="sc-label">Self-correction starts at</div>'
                f'<div class="sc-value">{_mh_target:,}</div>'
                f'<div class="mh-bar"><div style="width:{pct}%;"></div></div>'
                f'<div class="sc-sub">{pct}% of the way there</div></div>'
                f'<div class="sc-tile"><div class="sc-label">Model Engine</div>'
                f'<div class="sc-value" style="font-size:0.95rem;">'
                f'<span class="badge" style="margin:0;"><span class="g"></span>AutoPilot (Active)</span>'
                f'</div>'
                f'<div class="sc-sub">Continuously calibrated via live match actuals</div></div>'
                '</div>',
                "📦 Where we're up to",
            ),
            unsafe_allow_html=True,
        )
        if _mh_banked < _mh_target:
            st.info(
                "**Not self-correcting yet — and we're not going to pretend "
                "otherwise.** The model tunes itself only once there's enough "
                "checked history to tune against; fitting to a few hundred rows "
                "would chase noise, not signal. Until then the projections run "
                "on the model as built."
            )

    if _mh_rows:
        _hdr = (
            '<div class="wf-row" style="font-weight:700;color:var(--text);">'
            '<span>Gameweek</span><span style="min-width:70px;text-align:right;">How far off</span>'
            '<span style="min-width:80px;text-align:right;">Over/under</span>'
            '<span style="min-width:80px;text-align:right;">Right order</span></div>'
        )
        _body = ""
        for r in _mh_rows:
            rmse = f'{r["rmse"]:.2f}' if r["rmse"] is not None else "—"
            if r["bias"] is None:
                bias, bias_tone = "—", "var(--muted-2)"
            else:
                # Positive bias means we projected more than they scored.
                bias = f'{r["bias"]:+.2f}'
                bias_tone = "var(--neg-2)" if abs(r["bias"]) > 0.5 else "var(--muted)"
            if r["corr"] is None:
                corr, corr_tone = "—", "var(--muted-2)"
            else:
                corr = f'{r["corr"]:.2f}'
                corr_tone = "var(--pos-2)" if r["corr"] >= 0.4 else "var(--warn-2)"
            _body += (
                f'<div class="wf-row"><span>GW{r["gameweek"]} '
                f'<span class="tc-meta">({r["n"]:,} players)</span></span>'
                f'<span style="min-width:70px;text-align:right;">{rmse}</span>'
                f'<span style="min-width:80px;text-align:right;color:{bias_tone};">{bias}</span>'
                f'<span style="min-width:80px;text-align:right;color:{corr_tone};">{corr}</span></div>'
            )
        st.markdown(_card(_hdr + _body, "📈 Week by week"), unsafe_allow_html=True)
        st.markdown(
            '<div class="note"><b>How far off</b> — average miss, in points. Lower is better. '
            '<b>Over/under</b> — which way we lean; positive means we projected more than '
            'they scored. <b>Right order</b> — did the players we rated highest actually '
            'score highest? That one matters most: transfers are a ranking decision, not '
            'a forecast of the exact score.</div>',
            unsafe_allow_html=True,
        )
    elif _mh_banked:
        st.caption(
            "No gameweek has enough checked rows yet to report on. Needs at "
            "least 20 paired predictions in a week before the numbers mean anything."
        )

    with st.expander("🔧 What actually changes when it self-corrects", expanded=False):
        st.markdown(
            "It doesn't rewrite the model. It nudges a handful of dials — how much "
            "weight to put on a player's recent form, how harshly to treat rotation "
            "risk, how far ahead fixture difficulty should count — and only in small "
            "steps, so one freak gameweek can't drag it around.\n\n"
            "Everything the model does is visible on the other tabs before any of "
            "this kicks in. The self-correction makes it sharper; it isn't what "
            "makes it work."
        )


# ------------------------------------------------------------------
# Persistent developer attribution (visible on mobile, outside the sidebar).
# ------------------------------------------------------------------
st.markdown(
    """
    <hr style="margin-top: 3rem; margin-bottom: 1rem; border: none; border-top: 1px solid #e0e0e0;">
    <div style="text-align: center; color:var(--muted-2); font-size: 0.85rem; font-weight: 500; letter-spacing: 0.5px;">
        Built by Waqas Hussain
    </div>
    """,
    unsafe_allow_html=True
)

