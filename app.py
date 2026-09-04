import os
import requests
import streamlit as st
import fpl_tools
import squad_override
import dateutil.parser
from dateutil import tz
import datetime
import time

st.set_page_config(page_title="FPL Quant Manager", page_icon="⚽", layout="wide")

def get_caveat_html():
    fetch_ts = fpl_tools.get_api_timestamp()
    uk_zone = tz.gettz('Europe/London')
    dt = datetime.datetime.fromtimestamp(fetch_ts, tz=datetime.timezone.utc).astimezone(uk_zone)
    time_str = dt.strftime("%H:%M on %d %B %Y")
    return f'<div style="font-size:0.78rem;color:#64748b;margin:6px 0 10px 0;">ℹ️ <b>Note:</b> Player values and expected points (xP) are derived from our closed-loop algorithmic simulation model, refreshed at {time_str} UK time. Prices update once daily at roughly 01:30 UK time.</div>'

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

CSS = """
<style>
  #MainMenu, footer {visibility: hidden;}
  .block-container {padding-top: 1.4rem; padding-bottom: 4rem; max-width: 1240px;}
  .hero {background: linear-gradient(120deg, #4f46e5 0%, #6d5cf0 45%, #38bdf8 100%);
         color: #fff; border-radius: 18px; padding: 26px 28px; margin-bottom: 14px;
         box-shadow: 0 12px 30px rgba(79,70,229,0.22);}
  .hero h1 {font-size: 1.7rem; font-weight: 800; letter-spacing: -0.02em; margin: 0;}
  .hero .sub {color: #eef2ff; font-size: 0.95rem; margin-top: 0.4rem; line-height: 1.45;}
  .deadline-hero {background: #fff; border: 1px solid #e2e8f0; border-left: 6px solid #4f46e5;
         border-radius: 14px; padding: 18px 22px; margin-bottom: 16px;
         box-shadow: 0 6px 18px rgba(15,23,42,0.06);}
  .dl-gw {font-size: 0.78rem; font-weight: 800; letter-spacing: 0.14em; color: #4f46e5; text-transform: uppercase;}
  .dl-time {font-size: 1.9rem; font-weight: 800; color: #0f172a; line-height: 1.15; margin-top: 2px;}
  .dl-sub {color: #64748b; font-size: 0.9rem; margin-top: 2px;}
  .dl-warning {color: #e11d48; font-size: 0.88rem; margin-top: 6px;}
  .overview {background: #eef2ff; border: 1px solid #e0e7ff; border-radius: 14px; padding: 16px 20px;
         color: #3730a3; font-size: 0.94rem; line-height: 1.55; margin-bottom: 8px;}
  .overview b {color: #312e81;}
  .card {background: #fff; border: 1px solid #e2e8f0; border-radius: 14px; padding: 18px 20px;
         margin-bottom: 14px; box-shadow: 0 4px 14px rgba(15,23,42,0.05);}
  .card h3 {margin: 0 0 2px 0; font-size: 1.02rem; font-weight: 700; color: #0f172a;}
  .section-label {text-transform: uppercase; letter-spacing: 0.1em; font-size: 0.7rem; color: #64748b; margin-bottom: 10px;}
  .grid {display: grid; grid-template-columns: repeat(auto-fill, minmax(178px, 1fr)); gap: 10px;}
  .pc {border: 1px solid #e2e8f0; border-radius: 12px; padding: 12px 13px; background: #fff;}
  .pc .pos {display:inline-block; font-size: 0.66rem; font-weight: 800; letter-spacing: 0.05em;
            padding: 2px 7px; border-radius: 5px; color: #fff;}
  .pc .nm {font-weight: 700; font-size: 0.92rem; margin: 6px 0 1px 0; color: #0f172a;}
  .pc .meta {color: #64748b; font-size: 0.75rem;}
  .bar {height: 5px; background: #e2e8f0; border-radius: 3px; margin: 8px 0 4px 0; overflow: hidden;}
  .bar-fill {height: 100%; border-radius: 3px;}
  .xp {font-weight: 800; font-size: 0.88rem;}
  .badge {display:inline-flex; align-items:center; gap:8px; padding: 8px 13px; border-radius: 999px;
          background:#eef2ff; border:1px solid #e0e7ff; font-size:0.8rem; color:#3730a3;}
  .badge .g {width: 8px; height: 8px; border-radius: 50%; background: #10b981; box-shadow: 0 0 8px #10b981;}
  .transfer {display:flex; align-items:center; gap: 10px; padding: 9px 0; border-bottom: 1px solid #e2e8f0;}
  .transfer .arrow {color: #94a3b8;}
  .out {color: #e11d48; font-weight: 700;}
  .inn {color: #059669; font-weight: 700;}
  .gain {margin-left: auto; font-weight: 700; font-size: 0.85rem; color: #475569;}
  .cap-card {border: 1px solid #fbbf24; background: #fff7ed;}
  .role {display:inline-block; font-size: 0.62rem; font-weight: 800; padding: 1px 6px; border-radius: 5px;
         margin-left: 5px; vertical-align: middle;}
  .role-c {background: #f59e0b; color: #fff;}
  .role-vc {background: #cbd5e1; color: #334155;}
  .stat-badge {display:inline-block; font-size:0.6rem; font-weight:700; padding:1px 5px; border-radius:4px; margin-left:4px; white-space:nowrap; vertical-align: middle;}
  .stat-out {background:#fee2e2; color:#b91c1c; border:1px solid #fecaca;}
  .stat-doubt {background:#fef3c7; color:#b45309; border:1px solid #fde68a;}
  .chip-banner {background: #f0fdf4; border: 1px solid #bbf7d0; border-left: 5px solid #16a34a; border-radius: 10px; padding: 12px 16px; margin-bottom: 12px; font-size: 0.9rem; color: #166534;}
  .override-head {background: #fff7ed; border: 1px solid #fed7aa; border-left: 6px solid #f59e0b;
         border-radius: 14px 14px 0 0; padding: 16px 20px; font-size: 1.15rem; font-weight: 800;
         color: #9a3412; margin-top: 26px;}
  .override-head span {color: #c2410c; font-size: 0.82rem; font-weight: 600; margin-left: 8px;}
  .dummy-url {display:inline-block; background:#f8fafc; border:1px dashed #cbd5e1; border-radius:6px;
              padding:2px 8px; font-family:ui-monospace, SFMono-Regular, Menlo, monospace;
              font-size:0.78rem; color:#64748b; margin-left:6px;}
  .team-row {display:flex; gap:12px; align-items:stretch; margin-bottom:10px;}
  .pos-col {flex:0 0 64px; display:flex; flex-direction:column; align-items:center; justify-content:center;
            background:#f1f5f9; border-radius:10px; padding:8px;}
  .pos-emoji {font-size:1.3rem;}
  .pos-name {font-weight:800; font-size:0.68rem; letter-spacing:.06em; color:#475569; margin-top:2px;}
  .starters {flex:1; display:grid; grid-template-columns:repeat(auto-fill,minmax(148px,1fr)); gap:8px; align-content:start;}
  .subs {flex:0 0 205px; background:#f8fafc; border:1px dashed #cbd5e1; border-radius:10px; padding:8px 10px;}
  .sub-title {font-size:0.62rem; font-weight:800; letter-spacing:.08em; text-transform:uppercase;
              color:#94a3b8; margin-bottom:4px;}
  .sub-item {font-size:0.75rem; color:#0f172a; font-weight:600; padding:4px 0; border-bottom:1px dotted #e2e8f0;
             display:flex; justify-content:space-between; align-items:center; gap:4px;}
  .sub-item:last-child {border-bottom:none;}
  .sub-meta {color:#64748b; font-size:0.68rem; margin-top:2px;}
  .sub-xp {color:#0f172a; font-weight:800; white-space:nowrap; margin-top:2px;}
  .empty {color:#cbd5e1; font-size:0.8rem;}
  .mc {border:1px solid #e2e8f0; border-left:3px solid #94a3b8; border-radius:10px; padding:8px 10px;
       background:#fff; min-width:0;}
  .mc .pos {display:inline-block; font-size:0.6rem; font-weight:800; letter-spacing:0.04em;
            padding:1px 6px; border-radius:4px; color:#fff; vertical-align: middle;}
  .mc-nm {font-weight:700; font-size:0.82rem; color:#0f172a; margin-top:4px;
          white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .mc-meta {color:#64748b; font-size:0.7rem; margin-top:2px;}
  .mc-xp {font-weight:800; font-size:0.8rem; margin-top:3px; color:#0f172a;}
  .alert-box {background: #fff1f2; border: 1px solid #fecdd3; border-left: 4px solid #e11d48; padding: 12px 16px; border-radius: 8px; margin: 10px 0;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# ------------------------------------------------------------------
# Dark "Final Boss" theme overrides + analytics component styles
# ------------------------------------------------------------------
DARK_CSS = """
<style>
  .stApp {background: #0B1320;}
  .block-container {color: #E2E8F0;}
  [data-testid="stSidebar"] {background: #0F172A; border-right: 1px solid #334155;}
  [data-testid="stSidebar"] * {color: #E2E8F0;}
  [data-testid="stHeader"] {background: rgba(11,19,32,0.5);}

  .deadline-hero {background: #1E293B; border-color: #334155; border-left-color: #00F5A0; box-shadow: none;}
  .dl-time {color: #E2E8F0;}
  .dl-gw {color: #00F5A0;}
  .dl-sub {color: #94a3b8;}
  .dl-warning {color: #fda4af;}
  .overview {background: #1E293B; border-color: #334155; color: #cbd5e1;}
  .overview b {color: #00F5A0;}
  .card {background: #1E293B; border-color: #334155; box-shadow: 0 4px 14px rgba(0,0,0,0.25);}
  .card h3 {color: #E2E8F0;}
  .section-label {color: #00F5A0;}
  .pc {background: #0F172A; border-color: #334155;}
  .pc .nm {color: #E2E8F0;}
  .pc .meta {color: #94a3b8;}
  .bar {background: #334155;}
  .badge {background: #1E293B; border-color: #334155; color: #00F5A0;}
  .badge .g {background: #00F5A0; box-shadow: 0 0 8px #00F5A0;}
  .transfer {border-bottom-color: #334155;}
  .out {color: #fda4af;}
  .inn {color: #00F5A0;}
  .gain {color: #94a3b8;}
  .cap-card {border-color: #00F5A0; background: #0f2b20;}
  .role-vc {background: #334155; color: #cbd5e1;}
  .role-c {background: #00F5A0; color: #0B1320;}
  .stat-out {background: #3b1220; color: #fda4af; border-color: #7f1d1d;}
  .stat-doubt {background: #3b2f12; color: #fbbf24; border-color: #92400e;}
  .chip-banner {background: #0f2b20; border-color: #16a34a; border-left-color: #00F5A0; color: #bbf7d0;}
  .override-head {background: #1E293B; border-color: #334155; border-left-color: #00F5A0; color: #E2E8F0;}
  .override-head span {color: #00F5A0;}
  .dummy-url {background: #0F172A; border-color: #334155; color: #94a3b8;}
  .pos-col {background: #0F172A; border-color: #334155;}
  .pos-name {color: #94a3b8;}
  .subs {background: #0F172A; border-color: #334155;}
  .sub-item {color: #E2E8F0; border-bottom-color: #334155;}
  .sub-meta {color: #94a3b8;}
  .sub-xp {color: #00F5A0;}
  .empty {color: #475569;}
  .mc {background: #0F172A; border-color: #334155;}
  .mc-nm {color: #E2E8F0;}
  .mc-meta {color: #94a3b8;}
  .mc-xp {color: #00F5A0;}
  .mc .pos {color: #0B1320;}
  .alert-box {background: #3b1220; border-color: #7f1d1d; border-left-color: #fda4af; color: #fecdd3;}
</style>
"""
st.markdown(DARK_CSS, unsafe_allow_html=True)

DARK_CSS2 = """
<style>
  .headshot {width: 44px; height: 56px; object-fit: cover; border-radius: 6px; background: #0F172A; border: 1px solid #334155;}
  .badge-img {width: 26px; height: 26px; object-fit: contain; vertical-align: middle;}
  .badge-lg {width: 40px; height: 40px; object-fit: contain;}

  .pitch {background: linear-gradient(180deg, #113824 0%, #0b2418 100%); border: 2px solid #334155; border-radius: 16px; padding: 20px 16px; margin-bottom: 14px;}
  .pitch-row {display: flex; justify-content: space-around; align-items: center; margin: 14px 0;}
  .pitch-player {text-align: center; width: 92px;}
  .pitch-player .nm {font-size: 0.72rem; font-weight: 700; color: #E2E8F0; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
  .pitch-player .meta {font-size: 0.62rem; color: #94a3b8;}
  .pitch-player .xp {font-size: 0.72rem; font-weight: 800; color: #00F5A0;}
  .dugout {background: #0F172A; border: 1px dashed #334155; border-radius: 12px; padding: 14px 16px; margin-bottom: 14px;}
  .dugout-title {font-size: 0.62rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; color: #64748b; margin-bottom: 10px;}

  .transfer-pair {display: flex; align-items: center; gap: 10px; margin-bottom: 10px;}
  .transfer-card {flex: 1; border-radius: 12px; padding: 12px 14px; border: 1px solid #334155; background: #0F172A;}
  .transfer-card.tc-out {border-left: 4px solid #f43f5e;}
  .transfer-card.tc-in {border-left: 4px solid #00F5A0;}
  .transfer-arrow {font-size: 1.4rem; color: #00F5A0; font-weight: 800;}
  .tc-name {font-weight: 700; font-size: 0.9rem; color: #E2E8F0;}
  .tc-meta {font-size: 0.72rem; color: #94a3b8;}

  .rot-card {background: #0F172A; border: 1px solid #334155; border-radius: 12px; padding: 14px 16px; margin-bottom: 10px; display: flex; align-items: center; gap: 14px;}
  .rot-score {font-size: 1.1rem; font-weight: 800; color: #00F5A0; min-width: 54px;}
  .fdr-strip {display: flex; gap: 4px; margin-top: 8px;}
  .fdr-cell {width: 28px; height: 28px; border-radius: 6px; display: flex; align-items: center; justify-content: center; font-size: 0.72rem; font-weight: 800; color: #0B1320;}

  .strength-card {background: #0F172A; border: 1px solid #334155; border-radius: 12px; padding: 14px 16px; margin-bottom: 10px;}
  .strength-bar {height: 6px; background: #334155; border-radius: 3px; overflow: hidden; margin: 4px 0;}
  .strength-fill-home {height: 100%; background: #38bdf8;}
  .strength-fill-away {height: 100%; background: #00F5A0;}
  .strength-num {font-weight: 800; color: #00F5A0;}

  .momentum-row {display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid #334155;}
  .momentum-row:last-child {border-bottom: none;}
  .momentum-badge-up {background: #0f2b20; color: #00F5A0; border: 1px solid #16a34a; padding: 2px 8px; border-radius: 999px; font-size: 0.72rem; font-weight: 800; white-space: nowrap;}
  .momentum-badge-down {background: #3b1220; color: #fda4af; border: 1px solid #7f1d1d; padding: 2px 8px; border-radius: 999px; font-size: 0.72rem; font-weight: 800; white-space: nowrap;}

  .radar-card {background: #0F172A; border: 1px solid #334155; border-radius: 12px; padding: 12px 14px;}
  .radar-card .nm {font-weight: 700; font-size: 0.85rem; color: #E2E8F0;}
  .radar-card .meta {color: #94a3b8; font-size: 0.7rem;}

  .cap-pill {background: #00F5A0; color: #0B1320; font-weight: 800; font-size: 0.6rem; padding: 1px 5px; border-radius: 4px;}
</style>
"""
st.markdown(DARK_CSS2, unsafe_allow_html=True)

DARK_CSS3 = """
<style>
  .photo-frame {position: relative; display: inline-block; line-height: 0;}
  .photo-frame .badge-overlay {position: absolute; right: -3px; bottom: -3px; line-height: 0;}
  .photo-frame .badge-overlay .badge-img {width: 18px; height: 18px; border-radius: 50%; background: #0B1320; border: 1px solid #0B1320;}

  /* Pitch player cards: strict fixed dimensions so a 404 fallback never collapses the grid. */
  .pitch-player {display: flex; flex-direction: column; align-items: center; gap: 3px; text-align: center; width: 96px;}
  .pitch-player .photo-frame {width: 65px; height: 85px; display: flex; align-items: center; justify-content: center;}
  .pitch-player .headshot {width: 65px; height: 85px; object-fit: cover; border-radius: 8px; display: block;}
  .pitch-player .nm {width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 0.72rem; font-weight: 700; color: #E2E8F0;}
  .pitch-player .meta {width: 100%; font-size: 0.68rem; color: #94a3b8; white-space: nowrap;}
  .pitch-player .fx-dots {font-size: 0.74rem; letter-spacing: 1.5px; white-space: nowrap;}

  /* Pitch row position labels */
  .pitch-row {display: flex; align-items: center; gap: 10px; margin: 14px 0;}
  .pitch-row-label {flex: 0 0 34px; font-size: 0.62rem; font-weight: 800; letter-spacing: 0.06em; text-align: center; text-transform: uppercase;}
  .pitch-row-cards {flex: 1; display: flex; justify-content: space-around; align-items: center; gap: 8px;}

  .tc-head {display: flex; align-items: center; gap: 8px;}

  /* Player Inspector */
  .inspector-card {background: #0F172A; border: 1px solid #334155; border-radius: 14px; padding: 16px 18px; margin-top: 4px;}
  .insp-fixture {display: flex; justify-content: space-between; align-items: center; padding: 7px 0; border-bottom: 1px dotted #334155;}
  .insp-fixture:last-child {border-bottom: none;}
  @media (max-width: 768px) {
    .pitch { padding: 12px 6px !important; }
    .pitch-row-label { display: none !important; }
    .pitch-row-cards { width: 100% !important; gap: 2px !important; justify-content: space-evenly !important; }
    .pitch-player { width: auto !important; flex: 1 1 0 !important; max-width: 20% !important; min-width: 0 !important; padding: 2px !important; gap: 1px !important; }
    .pitch-player .photo-frame, .pitch-player .headshot { display: none !important; }
    .pitch-player .nm { font-size: 0.65rem !important; }
    .pitch-player .meta { font-size: 0.58rem !important; }
    .pitch-player .fx-dots { font-size: 0.62rem !important; letter-spacing: 0.5px !important; }
    .dugout { padding: 10px 8px !important; }
    .dugout > div:last-child { gap: 4px !important; justify-content: space-evenly !important; }
  }
</style>
"""
st.markdown(DARK_CSS3, unsafe_allow_html=True)


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
    """Cached fixture lookup + upcoming gameweek for traffic-light indicators."""
    if "fx_context" not in st.session_state:
        try:
            bootstrap = fpl_tools._get_bootstrap()
            st.session_state["fx_context"] = {
                "lookup": fpl_tools._build_fixture_lookup(bootstrap),
                "start_event": fpl_tools._next_gameweek(bootstrap),
            }
        except Exception:
            st.session_state["fx_context"] = None
    return st.session_state["fx_context"]


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

    Bound to the Strategy Mode widget's on_change callback so a strategy switch
    invalidates the cached 4-GW optimization and forces Step 3 to recalculate.
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


# ------------------------------------------------------------------
# Official Premier League asset helpers
# ------------------------------------------------------------------
@st.cache_data(ttl=86400)
def _headshot_url(player_code: str) -> str:
    """Return a validated CDN headshot URL, falling back to the placeholder.

    Uses a fast HEAD request so a broken primary image never renders as the
    browser's default broken-image icon (Streamlit strips the HTML `onerror`
    attribute when `unsafe_allow_html=True`).
    """
    fallback_url = "https://resources.premierleague.com/premierleague/photos/players/110x140/Photo-Missing.png"
    if not player_code:
        return fallback_url
    primary_url = f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{player_code}.png"
    try:
        if requests.head(primary_url, timeout=2).status_code == 200:
            return primary_url
    except Exception:
        pass
    return fallback_url


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


def _bootstrap_ctx():
    """Cached access to the live FPL bootstrap + fixture lookup."""
    if "bootstrap_ctx" not in st.session_state:
        try:
            bootstrap = fpl_tools._get_bootstrap()
            lookup = fpl_tools._build_fixture_lookup(bootstrap)
            st.session_state["bootstrap_ctx"] = {
                "bootstrap": bootstrap,
                "lookup": lookup,
                "players_by_id": {p["id"]: p for p in bootstrap.get("elements", [])},
                "teams_by_id": {t["id"]: t for t in bootstrap.get("teams", [])},
                "start": fpl_tools._next_gameweek(bootstrap),
            }
        except Exception:
            st.session_state["bootstrap_ctx"] = None
    return st.session_state["bootstrap_ctx"]


def _headshot_img(p) -> str:
    photo = p.get("photo")
    if not photo:
        ctx = _bootstrap_ctx()
        el = (ctx or {}).get("players_by_id", {}).get(_pid(p), {})
        photo = el.get("photo", "")
    url = _headshot_url(_photo_code(photo))
    badge = ""
    team_id = p.get("team_id")
    if team_id is None and isinstance(p.get("team"), int):
        team_id = p.get("team")
    if team_id is not None:
        badge = _badge_img(team_id)
    overlay = f'<div class="badge-overlay">{badge}</div>' if badge else ""
    return (
        f'<div class="photo-frame">'
        f'<img class="headshot" src="{url}" alt="" loading="lazy">'
        f'{overlay}'
        f'</div>'
    )


def _badge_img(team_id, large: bool = False) -> str:
    ctx = _bootstrap_ctx()
    t = (ctx or {}).get("teams_by_id", {}).get(team_id, {})
    code = t.get("code")
    if not code:
        return ""
    cls = "badge-lg" if large else "badge-img"
    return f'<img class="{cls}" src="{_badge_url(code)}" alt="">'


def _web_name(p) -> str:
    """Resolve the short display name (e.g. 'B. Fernandes') for a player dict."""
    wn = p.get("web_name")
    if wn:
        return wn
    ctx = _bootstrap_ctx()
    el = (ctx or {}).get("players_by_id", {}).get(_pid(p), {})
    wn = el.get("web_name")
    return wn or p.get("name", "?")


def _fdr_cell(fdr) -> str:
    fdr = int(fdr)
    if fdr <= 2:
        bg = "#00F5A0"
    elif fdr == 3:
        bg = "#fbbf24"
    else:
        bg = "#f43f5e"
    return f'<div class="fdr-cell" style="background:{bg}">{fdr}</div>'


# ------------------------------------------------------------------
# Insights Lab — Fixture Rotation Solver & Team Strength Index
# ------------------------------------------------------------------
def _team_fdr_series(team_id, lookup, start, n: int = 6):
    fx = {f.get("event"): f for f in lookup.get(team_id, [])}
    series = []
    for i in range(n):
        ev = start + i
        f = fx.get(ev)
        series.append(int(f.get("difficulty") or 5) if f else 5)
    return series


def _fixture_rotation_matrix(anchor_team_id=None, n: int = 6, top: int = 8):
    ctx = _bootstrap_ctx()
    if not ctx:
        return []
    lookup = ctx["lookup"]
    start = ctx["start"]
    team_ids = [t["id"] for t in ctx["bootstrap"].get("teams", [])]
    fdr_by_id = {tid: _team_fdr_series(tid, lookup, start, n) for tid in team_ids}
    pairings = []
    for i in range(len(team_ids)):
        for j in range(i + 1, len(team_ids)):
            t1, t2 = team_ids[i], team_ids[j]
            mins = [min(a, b) for a, b in zip(fdr_by_id[t1], fdr_by_id[t2])]
            avg = sum(mins) / len(mins)
            pairings.append({"t1": t1, "t2": t2, "avg": avg, "series": mins})
    pairings.sort(key=lambda x: x["avg"])
    if anchor_team_id is not None:
        pairings = [p for p in pairings if anchor_team_id in (p["t1"], p["t2"])]
    return pairings[:top]


def _team_strength_index():
    ctx = _bootstrap_ctx()
    if not ctx:
        return []
    rows = []
    for t in ctx["bootstrap"].get("teams", []):
        rows.append({
            "id": t["id"],
            "name": t.get("name", "?"),
            "code": t.get("code"),
            "home": _num(t.get("strength_overall_home")),
            "away": _num(t.get("strength_overall_away")),
        })
    all_v = [r["home"] for r in rows] + [r["away"] for r in rows]
    lo, hi = min(all_v), max(all_v)
    span = (hi - lo) or 1.0
    for r in rows:
        r["home5"] = round(1.0 + 4.0 * (r["home"] - lo) / span, 1)
        r["away5"] = round(1.0 + 4.0 * (r["away"] - lo) / span, 1)
        r["combined"] = round((r["home5"] + r["away5"]) / 2.0, 2)
    rows.sort(key=lambda x: x["combined"], reverse=True)
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
        f'<img class="headshot" src="{_headshot_url(_photo_code(r["photo"]))}" alt="" loading="lazy">'
        f'{_badge_img(r["team"])}'
        f'<div style="flex:1;">'
        f'<div style="font-weight:700;color:#E2E8F0;">{r["name"]}</div>'
        f'<div class="tc-meta">{r["pos"]} · £{r["price"]:.1f}m · net {r["net"]:+,}</div>'
        f'</div>'
        f'{arrow} {badge}'
        f'</div>'
    )


def _radar_shortlists(kind: str, limit: int = 12, risk: str = "balanced"):
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
        xp4, note = fpl_tools._player_xp_horizon(e, lookup, start, risk=risk, n=4)
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
def _pitch_player_html(p, role: str = None) -> str:
    lights = ""
    if p.get("team_id") is not None:
        ctx = _get_fixture_context()
        if ctx:
            lights = fpl_tools._fixture_traffic_lights(p["team_id"], ctx["lookup"], ctx["start_event"]).strip("[]")
    role_html = ""
    if role == "C":
        role_html = '<span class="cap-pill">C</span>'
    elif role == "VC":
        role_html = '<span class="cap-pill" style="background:#334155;color:#cbd5e1;">V</span>'
    name = _web_name(p)
    return (
        f'<div class="pitch-player">'
        f'{_headshot_img(p)}'
        f'<div class="nm">{name} {role_html}</div>'
        f'<div class="meta">£{p.get("price", 0):.1f}m · {p.get("xp", 0):.2f} xP</div>'
        f'<div class="fx-dots">{lights}</div>'
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
        '<div style="font-size:0.8rem;color:#94a3b8;margin:0 0 6px 0;line-height:1.5;">'
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
                date_str = f" <span style='color:#64748b;font-weight:normal;font-size:0.75rem;margin-left:6px;'>{dt.strftime('%d %b %H:%M')}</span>"
            except:
                pass
        wp = fx.get("win_prob")
        fdr = fx.get("difficulty") or 3
        if wp is not None:
            odds_desc = f"Market Win Probability: {wp * 100:.0f}% · Official FPL Difficulty: {fdr}/5"
        else:
            odds_desc = f"Market Odds Pending · Official FPL Difficulty: {fdr}/5"
            
        fx_rows += (
            f'<div class="insp-fixture">'
            f'<div style="font-weight:700;color:#E2E8F0;">{opp_name} '
            f'<span style="font-weight:600;color:#94a3b8;">({venue})</span>{date_str}</div>'
            f'<div class="tc-meta">{odds_desc}</div>'
            f'</div>'
        )
    if not fx_rows:
        fx_rows = '<div class="empty">No upcoming fixtures found.</div>'

    card = (
        f'<div class="inspector-card">'
        f'<div style="display:flex;gap:16px;align-items:flex-start;">'
        f'{_headshot_img(player)}'
        f'<div style="flex:1;min-width:0;">'
        f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">'
        f'<span style="font-weight:800;color:#E2E8F0;font-size:1.05rem;">{_web_name(player)}</span>'
        f'{_badge_img(team_id, large=True)}'
        f'</div>'
        f'<div class="tc-meta">{player.get("position", "")} · {player.get("team", "")} · £{player.get("price", 0):.1f}m</div>'
        f'<div style="margin-top:6px;font-size:0.85rem;color:#E2E8F0;">{_friendly_status(player.get("status"))}</div>'
        f'</div></div>'
        f'<div style="margin-top:14px;"><div class="section-label">Next 4 Fixtures</div>{fx_rows}</div>'
        f'</div>'
    )
    st.markdown(card, unsafe_allow_html=True)


def _transfer_pair_html(moves) -> str:
    html = ""
    for m in moves:
        out = m["out"]
        inn = m["in"]
        html += (
            f'<div class="transfer-pair">'
            f'<div class="transfer-card tc-out">'
            f'<div class="tc-meta">Transfer Out</div>'
            f'<div class="tc-head">{_headshot_img(out)}<div><div class="tc-name">⬇️ {_web_name(out)}</div>'
            f'<div class="tc-meta">{out.get("position", "")} · {out.get("team", "")} · £{out.get("price", 0):.1f}m</div></div></div>'
            f'</div>'
            f'<div class="transfer-arrow">➔</div>'
            f'<div class="transfer-card tc-in">'
            f'<div class="tc-meta">Transfer In</div>'
            f'<div class="tc-head">{_headshot_img(inn)}<div><div class="tc-name">⬆️ {_web_name(inn)}</div>'
            f'<div class="tc-meta">{inn.get("position", "")} · {inn.get("team", "")} · £{inn.get("price", 0):.1f}m</div></div></div>'
            f'</div>'
            f'<div style="min-width:110px;text-align:right;">'
            f'<div class="rot-score">+{m.get("xp_gain", 0)}</div>'
            f'<div class="tc-meta">xP · £{m.get("cost", 0):+.1f}m</div>'
            f'</div></div>'
        )
        rationale = m.get("rationale")
        if rationale:
            html += f'<div style="font-size:0.78rem;color:#94a3b8;margin:0 0 8px 0;">{rationale}</div>'
    return html


# ------------------------------------------------------------------
# Sidebar
# ------------------------------------------------------------------
with st.sidebar:
    st.markdown("## ⚽ FPL Quant Manager")
    st.caption("Your data-driven pre-deadline quant engine")

    with st.expander("🧠 How the Engine Forecasts the Future", expanded=False):
        st.markdown(
            "**Why 4 Gameweeks?**  \n"
            "Proprietary dark arts. Planning 10 weeks ahead sounds great until Pep roulette, hamstring tweaks, and "
            "pure vibes derail your season. The engine models a protected 4-week tactical horizon—just far enough to "
            "target form and fixtures without walking into a trap.\n\n"
            "**Transfer Friction:**  \n"
            "Step away from the knee-jerk. Banked transfers win mini-leagues. The model slaps a strict mathematical "
            "penalty on itchy trigger fingers; unless an incoming player is a decisive, undeniable upgrade, the "
            "engine banks the transfer and lets your rivals burn their rank."
        )

    st.markdown("---")
    st.markdown(
        '<div class="badge"><span class="g"></span> Built by Waqas Hussain</div>',
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------
# Hero & Overview
# ------------------------------------------------------------------
safe_banner = f'<div class="dl-warning">⚠️ <b>Pro Tip:</b> Aim to confirm your transfers by <b>{SAFE_TIME_STR}</b> to avoid FPL server crashes.</div>' if SAFE_TIME_STR else ''

# Spacer so the deadline banner doesn't touch the very top of the viewport.
st.markdown("<div style='margin-top: 2rem;'></div>", unsafe_allow_html=True)

st.markdown(
    f'<div class="deadline-hero"><div class="dl-gw">⏰ {GW_NAME} deadline</div>'
    f'<div class="dl-time">{DEADLINE_STR}</div>'
    f'{safe_banner}</div>',
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="overview">'
    '<b>📊 Your Personal FPL Quant Engine</b><br><br>'
    'This system operates as a fully automated quantitative analyst for your Fantasy Premier League team. '
    'It removes human bias by combining predictive modelling, mathematical optimisation, and continuous machine learning:<br><br>'
    '• <b>The Baseline Projections:</b> It calculates Expected Points (xP) for every player by aggregating '
    'underlying statistics, fixture difficulty ratings, and positional baselines.<br><br>'
    '• <b>The Optimiser:</b> Our closed-loop algorithmic simulation model finds the mathematically optimal transfers, '
    'starting XI, and captaincy — respecting your specific budget, chip strategy, and transfer constraints.<br><br>'
    '<hr style="border:none;border-top:1px solid #c7d2fe;margin:14px 0;">'
    '<b>🌟 The Self-Learning Quant Engine 🌟</b><br>'
    'Most FPL tools are just static calculators. This is a living quantitative model.<br><br>'
    '<i>Every Gameweek, our closed-loop validation engine benchmarks ex-ante projections against realised Premier '
    'League match outcomes to assess predictive variance. The system dynamically recalibrates its underlying '
    'statistical coefficients, ensuring continuous model refinement without exposing execution mechanics.</i><br><br>'
    'Put simply: it learns from reality. The deeper we get into the season, the smarter and more ruthless the '
    'algorithm becomes, giving you a compounding edge over your mini-league rivals.'
    '</div>',
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------
# Strategy selector (prominent, above Manager ID)
# ------------------------------------------------------------------
st.markdown("### 🎯 Select Your Strategy")
risk_label = st.radio(
    "Strategy mode",
    ["Balanced", "Conservative", "Aggressive", "Rank Protecting (Shield)", "Rank Chasing (Hunting)"],
    index=0,
    key="risk",
    help="Tunes the transfer hurdle rate and competitive posture.",
    captions=[
        "Standard transfer hurdle rate; balanced risk-reward profile.",
        "High hurdle rate; prioritises rolling and banking free transfers (Park the Bus).",
        "Lower hurdle rate; accepts point hits (-4) if immediate upside justifies it.",
        "Weights effective ownership (EO) to mirror template picks and protect rank.",
        "Deprecates template picks; targets low-ownership differentials with high underlying metrics (Fergie Time).",
    ],
    on_change=clear_transfer_cache,
)

# ------------------------------------------------------------------
# Main navigation
# ------------------------------------------------------------------
tab_planner, tab_insights, tab_radar = st.tabs(["🏟️ Quant Auto Transfer Planner", "🔬 Insights Lab", "📡 Player Radar & Market"])

with tab_planner:

    # ------------------------------------------------------------------
    # Step 1 — Your Baseline Team
    # ------------------------------------------------------------------
    st.markdown(
        '<div class="override-head">Step 1: Your Baseline Team</div>',
        unsafe_allow_html=True,
    )
    
    with st.container(border=True):
        st.caption(
            "Enter your Manager ID to pull your official baseline squad. This populates your current starting 11 + subs."
        )
    
        col1, col2 = st.columns([3, 1])
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
    
        if load_clicked:
            if not manager_id.strip():
                st.warning("Pop your Manager ID in first — it’s the number in your FPL team-page URL.")
            else:
                with st.spinner("Fetching your current squad…"):
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
            
            ft = st.session_state.get("api_free_transfers_default", 0)
        fetch_ts = fpl_tools.get_api_timestamp()
        dt = datetime.datetime.fromtimestamp(fetch_ts, tz=datetime.timezone.utc).astimezone(tz.gettz('Europe/London'))
        d_str = dt.strftime('%d %b %H:%M')

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Team", preview["team_name"])
        m2.metric("Free Transfers", f"{ft}", f"As of {d_str}", delta_color="off")
        m3.metric("Bank (Unspent)", f"£{preview['bank']}m", f"As of {d_str}", delta_color="off")
        m4.metric("Team value", f"£{preview['team_value']}m")
    
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
            default_ft = st.session_state.get("api_free_transfers_default", 0)
            
            var_col1, var_col2 = st.columns(2)
            with var_col1:
                ft_val = st.number_input("Current Free Transfers", 0, 15, int(default_ft), key="ov_ft")
            with var_col2:
                bank_val = st.number_input("Remaining Budget in Bank (£m)", 0.0, 50.0, plan_bank, 0.1, key="ov_bank")
                
            chips_val = st.multiselect(
                "Select Active Chip(s) to Evaluate",
                ["Wildcard", "Free Hit", "Bench Boost", "Triple Captain"],
                default=[],
                key="ov_chips"
            )
            
            st.markdown("---")
            st.markdown("#### 2. Midweek Transfers")
            show_override = st.checkbox("🚨 **I have already made midweek transfers** (Click to manually update your squad or upload a screenshot)", key="cb_override")
            
            override_squad = []
            if show_override:
                st.caption("Upload a screenshot or adjust the dropdowns to match your live 15-man squad.")
                
                try:
                    bootstrap = fpl_tools._get_bootstrap()
                    fixture_lookup = fpl_tools._build_fixture_lookup(bootstrap)
                except Exception:
                    bootstrap = {"elements": [], "teams": []}
                    fixture_lookup = {}
    
                players_by_id = {p["id"]: p for p in bootstrap.get("elements", [])}
                teams = {t["id"]: t["short_name"] for t in bootstrap.get("teams", [])}
                pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    
                surname_by_id = {p["id"]: _surname(p) for p in bootstrap.get("elements", [])}
    
                dropdown_options = {pos: [(None, "— Select Player —")] for pos in POS_ORDER}
                for p in bootstrap.get("elements", []):
                    pos = pos_map.get(p["element_type"])
                    if pos:
                        xp, _ = fpl_tools._player_xp(p, fixture_lookup, event=GW_ID, risk=risk_label.lower())
                        initial = p['first_name'][0] + "." if p.get('first_name') else ""
                        display = f"{initial} {p['second_name']} ({teams.get(p['team'], '?')}) £{p['now_cost']/10:.1f}m | {xp} xP"
                        dropdown_options[pos].append((p["id"], display))
    
                for pos in POS_ORDER:
                    dropdown_options[pos] = [dropdown_options[pos][0]] + sorted(
                        dropdown_options[pos][1:], key=lambda x: surname_by_id.get(x[0], "")
                    )
    
                all_options = [(None, "— Select Player —")]
                for pos in POS_ORDER:
                    all_options.extend(dropdown_options[pos][1:])
                all_options.sort(key=lambda x: "" if x[0] is None else surname_by_id.get(x[0], ""))
    
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
                                xp, note = fpl_tools._player_xp(fpl_p, fixture_lookup, event=GW_ID, risk=risk_label.lower())
                                analysed.append({
                                    "player_id": pid, "name": f"{fpl_p['first_name']} {fpl_p['second_name']}",
                                    "team": teams.get(fpl_p["team"], "?"), "team_id": fpl_p["team"],
                                    "position": pos_map.get(fpl_p["element_type"], "?"),
                                    "price": fpl_p["now_cost"] / 10.0,
                                    "selling_price": sell_by_id.get(pid, fpl_p["now_cost"] / 10.0),
                                    "xp": xp, "status": note, "is_captain": False
                                })
                            
                            transfers = fpl_tools.suggest_transfers_for_custom_squad(
                                analysed, float(bank_val), int(ft_val), eval_chips=chips_val, event=GW_ID, risk=risk_label.lower())
                            
                            st.session_state["override_analysis"] = {
                                "analysed_squad": analysed,
                                "bank": float(bank_val),
                                "ft": int(ft_val),
                                "chips": chips_val,
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
    
            # Recalculate transfers if the strategy mode changed (cache-busted via the
            # sidebar on_change callback), so Step 3 never shows stale recommendations.
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
                                    _xp, _note = fpl_tools._player_xp(_fp, _fl, event=GW_ID, risk=risk_label.lower())
                                    _p["xp"] = _xp
                                    _p["status"] = _note
                        except Exception:
                            pass
                        tr = fpl_tools.suggest_transfers_for_custom_squad(
                            ov["analysed_squad"], ov["bank"], ov["ft"],
                            eval_chips=ov.get("chips", []), event=GW_ID, risk=risk_label.lower()
                        )
                        ov["transfers"] = tr
                    except Exception as e:
                        st.warning(f"Could not recalculate transfers: {e}")
                st.session_state["_transfers_stale"] = False
            
            evals = tr.get("chip_evaluations", [])
            if evals:
                eval_html = "".join(f"<div style='margin-bottom:6px;'>{e}</div>" for e in evals)
                st.markdown(_card(eval_html, "🎟️ Active Chip Analysis & Recommendations"), unsafe_allow_html=True)
    
            rec_chip = tr.get("recommended_chip", "None (Hold Chips)")
            valid_chips = [c for c in ov.get("chips", []) if c in ["Wildcard", "Free Hit", "Bench Boost", "Triple Captain"]]
            chip_options = ["None (Hold Chips)"] + valid_chips
            
            def_chip_idx = 0
            if rec_chip in chip_options:
                def_chip_idx = chip_options.index(rec_chip)
    
            st.markdown("#### Confirm Active Chip")
            confirmed_chip = st.radio(
                "Select which chip you will actively play this Gameweek (Only 1 allowed):",
                chip_options,
                index=def_chip_idx,
                horizontal=True,
                key="confirmed_chip_radio"
            )
            st.session_state["active_confirmed_chip"] = confirmed_chip
    
            st.markdown("---")
            
            if confirmed_chip in ("Wildcard", "Free Hit"):
                moves = tr.get("wildcard_transfers", tr.get("transfers", []))
                transfer_advice = f"<b>{confirmed_chip} Active:</b> {len(moves)} transfers optimised with 0 point penalties."
            else:
                moves = tr.get("standard_transfers", tr.get("transfers", []))
                transfer_advice = tr.get("hit_advice", "")
    
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
                    market_html = '<div style="margin-bottom:6px;color:#475569;">Prices update overnight (~01:30–02:30 UK). Act before the next update:</div>' + "".join(market_rows)
                else:
                    market_html = '<div style="color:#64748b;">No imminent price changes detected for your squad or transfer targets.</div>'
                st.markdown(_card(market_html, "📊 Market Alert & Value Tracker"), unsafe_allow_html=True)
            except Exception:
                pass
    
            transfer_html = (
                '<div style="font-size:0.78rem;color:#94a3b8;font-style:italic;margin:0 0 10px 0;">'
                'Note: xP (Expected Points) is a projection from our closed-loop algorithmic simulation model — a forecast of potential performance, not a guaranteed outcome.'
                '</div>'
                f'<div style="color:#475569;margin:4px 0 8px 0; font-weight:600;">{transfer_advice}</div>'
            )
            if moves:
                transfer_html += _transfer_pair_html(moves)
            else:
                transfer_html += '<div style="color:#64748b;">No transfers recommended.</div>'
            st.markdown(_card(transfer_html, "⚙️ Optimised Transfers"), unsafe_allow_html=True)

            c_fast1, c_fast2 = st.columns(2)
            with c_fast1:
                accept_all = st.button("✅ Accept All Quant Transfers & Proceed to Lineup", type="primary", use_container_width=True, key="btn_accept_all")
            with c_fast2:
                fast_hold = st.button("⏭️ Make No Changes (Hold Squad) & Proceed to Lineup", type="secondary", use_container_width=True, key="btn_fast_hold")
            
            if fast_hold:
                with st.spinner("Generating Final Lineup with current squad…"):
                    analysed_current = ov["analysed_squad"]
                    lineup = fpl_tools.select_starting_xi(analysed_current)
                    lineup["confirmed_chip"] = confirmed_chip
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
                        })
                    lineup = fpl_tools.select_starting_xi(final_squad)
                    lineup["confirmed_chip"] = confirmed_chip
                    st.session_state["manual_final"] = lineup
                    st.rerun()



    
            with st.expander("💡 The Variables Driving Your Transfer Recommendations", expanded=False):
                explainer_bullets = []
    
                # 1. Base logic (always true).
                explainer_bullets.append("* **Expected Value Maximisation** — the solver identified these specific moves to maximise your net Expected Points (xP) over the horizon, adjusting for positional baseline metrics.")
    
                hits_taken = int(tr.get("hits", 0))
    
                # 2. Hit amortisation (only if hits > 0).
                if hits_taken > 0:
                    explainer_bullets.append(f"* **Hit amortisation** — the {-4 * hits_taken} point hit is mathematically justified. The engine calculates that these upgrades will recover the penalty points and clear the transfer-friction hurdle.")
    
                # 3. Goalkeeper swaps (only if a GK is transferred in or out).
                gk_involved_in_transfer = any(m["out"]["position"] == "GK" or m["in"]["position"] == "GK" for m in moves)
                if gk_involved_in_transfer:
                    explainer_bullets.append("* **Goalkeeper structuring** — the solver navigated the strict 2-GK squad rule, ensuring your premium/budget balance in goal remains optimal.")
    
                # 4. Late fitness gating (only if an outgoing player has a doubtful status).
                flagged_player_transferred_out = any(m["out"].get("status", "Available") != "Available" for m in moves)
                if flagged_player_transferred_out:
                    explainer_bullets.append("* **Late fitness gating** — doubtful assets were ruthlessly penalised in the projections, prompting the solver to eject injury risks before the deadline.")
    
                # 5. Banked transfer (only if 0 transfers were made).
                if len(moves) == 0:
                    explainer_bullets.append("* **Transfer Conservation** — the mathematically optimal move is no move. Rolling the transfer preserves structural flexibility and option value for next week.")
    
                if explainer_bullets:
                    st.markdown("\n".join(explainer_bullets))
    
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
                                xp, note = fpl_tools._player_xp(fpl_p, fixture_lookup, event=GW_ID, risk=risk_label.lower())
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
            
            y1, y2, y3 = st.columns(3)
            y1.metric("🛡️ Final Starting XI xP", f"{st_xp:.2f} xP", f"{delta_st:+.2f} xP" if delta_st != 0 else None)
            y2.metric("🪑 Final Bench xP", f"{be_xp:.2f} xP", f"{delta_be:+.2f} xP" if delta_be != 0 else None)
            y3.metric("📊 Final Squad xP", f"{tot_xp:.2f} xP", f"{delta_tot:+.2f} xP" if delta_tot != 0 else None)
    
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
            
            mult_str = "×3" if is_tc else "×2"
            mult_val = cap["xp"] * 3 if is_tc else cap["xp"] * 2
            cap_role_title = "Captain (Triple Captain Active)" if is_tc else "Captain"
            
            cap_html = f'<div class="grid"><div class="pc cap-card">{_pos_chip(cap["position"])}<div style="margin-bottom:2px;" class="nm">⭐ {cap["name"]}</div><div class="meta">{cap["team"]} — {cap_role_title}</div><div class="xp" style="color:#f59e0b; margin-top:4px;">{cap["xp"]} xP ({mult_str} = {mult_val:.2f} xP)</div></div>'
            if vcap:
                cap_html += f'<div class="pc">{_pos_chip(vcap["position"])}<div style="margin-bottom:2px;" class="nm">{vcap["name"]}</div><div class="meta">{vcap["team"]} — Vice-Captain</div><div class="xp" style="color:#475569; margin-top:4px;">{vcap["xp"]} xP</div></div>'
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

with tab_insights:
    st.markdown("### 🔬 Insights Lab")
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
                    f'<div style="display:flex;align-items:center;gap:8px;font-weight:700;color:#E2E8F0;">'
                    f'{_badge_img(p["t1"])} {t1["name"]} <span style="color:#94a3b8;">+</span> {_badge_img(p["t2"])} {t2["name"]}'
                    f'</div>'
                    f'<div class="fdr-strip">{cells}</div>'
                    f'</div></div>'
                )
            st.markdown(_card(html, "Top Rotation Pairings · lowest combined FDR"), unsafe_allow_html=True)
        else:
            st.info("No rotation pairings found.")

        st.markdown("#### 🛡️ Team Strength Index")
        strengths = _team_strength_index()
        if strengths:
            grid = '<div class="grid">'
            for s in strengths:
                home_pct = int(s["home5"] / 5 * 100)
                away_pct = int(s["away5"] / 5 * 100)
                grid += (
                    f'<div class="strength-card">'
                    f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">'
                    f'{_badge_img(s["id"], large=True)} <span style="font-weight:700;color:#E2E8F0;">{s["name"]}</span>'
                    f'</div>'
                    f'<div style="font-size:0.7rem;color:#94a3b8;">Home {s["home5"]:.1f}/5</div>'
                    f'<div class="strength-bar"><div class="strength-fill-home" style="width:{home_pct}%"></div></div>'
                    f'<div style="font-size:0.7rem;color:#94a3b8;">Away {s["away5"]:.1f}/5</div>'
                    f'<div class="strength-bar"><div class="strength-fill-away" style="width:{away_pct}%"></div></div>'
                    f'<div style="margin-top:6px;">Combined <span class="strength-num">{s["combined"]:.2f}</span> / 5</div>'
                    f'</div>'
                )
            grid += "</div>"
            st.markdown(_card(grid, "20 Clubs Ranked by Combined Strength"), unsafe_allow_html=True)

with tab_radar:
    st.markdown("### 📡 Player Radar & Market")
    risk = risk_label.lower()

    st.markdown("#### 📈 24h Ownership Momentum")
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
    rows = _radar_shortlists(kmap[kind], limit=12, risk=risk)
    if rows:
        ctx = _bootstrap_ctx()
        lookup = ctx["lookup"]; start = ctx["start"]
        grid = '<div class="grid">'
        for r in rows:
            lights = fpl_tools._fixture_traffic_lights(r["team"], lookup, start).strip("[]")
            grid += (
                f'<div class="radar-card">'
                f'<div style="display:flex;gap:8px;align-items:flex-start;">'
                f'<img class="headshot" src="{_headshot_url(_photo_code(r["photo"]))}" alt="" loading="lazy">'
                f'<div style="flex:1;min-width:0;">'
                f'<div class="nm">{r["name"]}</div>'
                f'<div class="meta">{r["pos"]} · {_badge_img(r["team"])} · £{r["price"]:.1f}m</div>'
                f'<div class="meta">Owned {r["ownership"]:.1f}% · {lights}</div>'
                f'<div style="font-weight:800;color:#00F5A0;margin-top:4px;">{r["xp"]} xP</div>'
                f'</div></div></div>'
            )
        grid += "</div>"
        sort_label = "xP per £1m" if kind == "Best Value" else "4-GW xP"
        st.markdown(_card(grid, f"{kind} · sorted by {sort_label}"), unsafe_allow_html=True)
    else:
        st.info("No players match this filter.")
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
    


# ------------------------------------------------------------------
# Persistent developer attribution (visible on mobile, outside the sidebar).
# ------------------------------------------------------------------
st.markdown(
    """
    <hr style="margin-top: 3rem; margin-bottom: 1rem; border: none; border-top: 1px solid #e0e0e0;">
    <div style="text-align: center; color: #6b7280; font-size: 0.85rem; font-weight: 500; letter-spacing: 0.5px;">
        Built by Waqas Hussain
    </div>
    """,
    unsafe_allow_html=True
)

