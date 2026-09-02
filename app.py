import streamlit as st
import fpl_tools
import squad_override
import dateutil.parser
from dateutil import tz
import datetime
import time

st.set_page_config(page_title="FPL AI Manager", page_icon="https://img.icons8.com/?size=100&id=Ao7bhT7J2dd9&format=png&color=000000", layout="wide")

def get_caveat_html():
    fetch_ts = fpl_tools.get_api_timestamp()
    uk_zone = tz.gettz('Europe/London')
    dt = datetime.datetime.fromtimestamp(fetch_ts, tz=datetime.timezone.utc).astimezone(uk_zone)
    time_str = dt.strftime("%H:%M on %d %B %Y")
    return f'<div style="font-size:0.78rem;color:#64748b;margin:6px 0 10px 0;">ℹ️ <b>Note:</b> Player values and expected points (xP) are based on live FPL API data fetched at {time_str} UK time. Prices update once daily at roughly 01:30 UK time.</div>'

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
# Render helpers
# ------------------------------------------------------------------
def _pos_chip(pos: str) -> str:
    c = POS_COLORS.get(pos, "#94a3b8")
    return f'<span class="pos" style="background:{c}">{pos}</span>'


def _status_badge(status: str) -> str:
    if not status or status in ("Available", "a"):
        return ""
    if status in ("OUT", "Injured", "Suspended", "Unavailable"):
        return f'<span class="stat-badge stat-out">{status}</span>'
    return f'<span class="stat-badge stat-doubt">{status}</span>'


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
        f'<div class="mc-nm" title="{p.get("name", "?")}">{p.get("name", "?")}</div>'
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
                f'<div><div>{p.get("name", "?")} {_status_badge(p.get("status", ""))}</div>'
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


# ------------------------------------------------------------------
# Sidebar
# ------------------------------------------------------------------
with st.sidebar:
    st.markdown("## ⚽ FPL AI Manager")
    st.caption("Your friendly pre-deadline assistant")

    risk_label = st.radio(
        "Risk appetite",
        ["Conservative", "Balanced", "Aggressive"],
        index=1,
        key="risk",
        help="Tunes the recommendation style.",
    )

    risk_desc = {
        "Conservative": "Protects your rank — favours popular, reliable starters and avoids points hits.",
        "Balanced": "Pure expected points — the highest-projected line-up, full stop.",
        "Aggressive": "Chases gains — low-ownership differentials, high ceilings, and willing to take hits.",
    }
    st.caption(risk_desc[risk_label])

    st.markdown("---")
    st.markdown(
        '<div class="badge"><span class="g"></span> Built by Waqas Hussain</div>',
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------
# Hero & Overview
# ------------------------------------------------------------------
safe_banner = f'<div class="dl-warning">⚠️ <b>Pro Tip:</b> Aim to confirm your transfers by <b>{SAFE_TIME_STR}</b> to avoid FPL server crashes.</div>' if SAFE_TIME_STR else ''

st.markdown(
    f'<div class="deadline-hero"><div class="dl-gw">⏰ {GW_NAME} deadline</div>'
    f'<div class="dl-time">{DEADLINE_STR}</div>'
    f'{safe_banner}</div>',
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="overview">'
    '<b>What this does:</b> Pop in your Manager ID to pull your official current squad. Then input your variables '
    '(Free Transfers, Bank, Active Chips) and verify your actual live team. The algorithm will calculate the optimal '
    'transfers to maximize expected points (xP) and generate your best starting XI, bench order, and captaincy.'
    '</div>',
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------
# Step 1 — Your Baseline Team
# ------------------------------------------------------------------
st.markdown(
    '<div class="override-head">Step 1: Your Baseline Team</div>',
    unsafe_allow_html=True,
)

with st.container(border=True):
    st.caption(
        "Enter your Manager ID to pull your official baseline squad. This populates your starting 15 players."
    )

    col1, col2 = st.columns([3, 1])
    with col1:
        manager_id = st.text_input(
            "Manager ID",
            key="mid_input",
            help="Your unique FPL ID — the number in your team-page URL.",
        )
    with col2:
        st.write("")
        st.write("")
        load_clicked = st.button("📋 Load my team", type="secondary", use_container_width=True, key="btn_load")

    st.caption(
        'Find your ID in your team-page URL — the number after <span class="dummy-url">/entry/</span> '
        'e.g. <span class="dummy-url">fantasy.premierleague.com/entry/[THIS IS YOUR MANAGER ID]/event/1</span>',
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
        
        m1, m2, m3 = st.columns(3)
        m1.metric("Team", preview["team_name"])
        m2.metric("Bank (Unspent)", f"£{preview['bank']}m")
        m3.metric("Team value", f"£{preview['team_value']}m")

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
        x1.metric("🛡️ Baseline Starting XI xP", f"{st_xp} xP")
        x2.metric("🪑 Baseline Bench xP", f"{be_xp} xP")
        x3.metric("📊 Baseline Squad xP", f"{tot_xp} xP")

        st.markdown("<br>", unsafe_allow_html=True)
        sheet = f'<div class="team-sheet">{_team_sheet_html(starters, bench, _pid(cap) if cap else None, _pid(vc) if vc else None)}</div>'
        st.markdown(_card(sheet, "Your Baseline Squad · C = Captain · VC = Vice-Captain"), unsafe_allow_html=True)
        st.markdown(get_caveat_html(), unsafe_allow_html=True)


# ------------------------------------------------------------------
# Step 2 — Verify Variables & Midweek Changes
# ------------------------------------------------------------------
if st.session_state.get("squad_preview") and not "error" in st.session_state.get("squad_preview"):
    st.markdown(
        '<div class="override-head">Step 2: Verify Variables & Midweek Changes</div>',
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

            dropdown_options = {pos: [(None, "— Select Player —")] for pos in POS_ORDER}
            for p in bootstrap.get("elements", []):
                pos = pos_map.get(p["element_type"])
                if pos:
                    xp, _ = fpl_tools._player_xp(p, fixture_lookup, event=GW_ID, risk=risk_label.lower())
                    initial = p['first_name'][0] + "." if p.get('first_name') else ""
                    display = f"{initial} {p['second_name']} ({teams.get(p['team'], '?')}) £{p['now_cost']/10:.1f}m | {xp} xP"
                    dropdown_options[pos].append((p["id"], display))

            all_options = [(None, "— Select Player —")]
            for pos in POS_ORDER:
                all_options.extend(dropdown_options[pos][1:])
            all_options.sort(key=lambda x: x[1].lower() if x[0] else "")

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
        if st.button("Run Algorithmic Optimizer", type="primary", use_container_width=True, key="btn_analyse_override"):
            
            active_squad_ids = override_squad if show_override else [p["player_id"] for p in st.session_state.get("squad_preview", {}).get("squad", [])]
            
            if len(active_squad_ids) != 15:
                st.error("⚠️ Ensure you have exactly 15 valid players selected before continuing.")
            else:
                with st.spinner("Scoring your squad and calculating optimal transfers…"):
                    try:
                        analysed = []
                        
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
                                "price": fpl_p["now_cost"] / 10.0, "xp": xp, "status": note, "is_captain": False
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
            transfer_advice = f"<b>{confirmed_chip} Active:</b> {len(moves)} transfers optimized with 0 point penalties."
        else:
            moves = tr.get("standard_transfers", tr.get("transfers", []))
            transfer_advice = tr.get("hit_advice", "")

        transfer_html = f'<div style="color:#475569;margin:4px 0 8px 0; font-weight:600;">{transfer_advice}</div>'
        if moves:
            for m in moves:
                transfer_html += (
                    f'<div class="transfer"><span class="out">OUT {m["out"]["name"]}</span>'
                    f'<span class="arrow">→</span><span class="inn">IN {m["in"]["name"]}</span>'
                    f'<span class="gain">+{m["xp_gain"]} xP · £{m["cost"]:+}m</span></div>'
                )
        else:
            transfer_html += '<div style="color:#64748b;">No transfers recommended.</div>'
        st.markdown(_card(transfer_html, "⚙️ Optimized Transfers"), unsafe_allow_html=True)

        target_default_moves = len(moves)
        last_chip_tracked = st.session_state.get("last_confirmed_chip_tracker")
        if last_chip_tracked != confirmed_chip or "n_moves_manual" not in st.session_state:
            st.session_state["n_moves_manual"] = target_default_moves
            st.session_state["last_confirmed_chip_tracker"] = confirmed_chip

        c_fast1, c_fast2 = st.columns([2, 1])
        with c_fast1:
            fast_hold = st.button("⏭️ Make No Changes (Hold Squad) & Proceed to Lineup", type="secondary", use_container_width=True, key="btn_fast_hold")
        
        if fast_hold:
            with st.spinner("Generating Final Lineup with current squad…"):
                analysed_current = ov["analysed_squad"]
                lineup = fpl_tools.select_starting_xi(analysed_current)
                lineup["confirmed_chip"] = confirmed_chip
                st.session_state["manual_final"] = lineup
                st.rerun()

        st.markdown("---")
        st.markdown("#### Or, Customize Your Transfers Below")
        st.caption(
            "Review or modify the recommended moves. You can adjust the transfer count, customize individual player selections, "
            "or accept the AI defaults below."
        )

        cur_options = [(None, "— Select Player —")] + [(p["player_id"], f"{p['name']} ({p['team']})") for p in ov["analysed_squad"]]
        
        n_moves = st.number_input("Number of transfers to apply", 0, 15, key="n_moves_manual")
        
        out_ids, in_ids = [], []
        try:
            bootstrap = fpl_tools._get_bootstrap()
            players_by_id = {p["id"]: p for p in bootstrap.get("elements", [])}
            teams = {t["id"]: t["short_name"] for t in bootstrap.get("teams", [])}
            pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
            
            all_options = [(None, "— Select Player —")]
            for p in bootstrap.get("elements", []):
                pos = pos_map.get(p["element_type"])
                if pos:
                    initial = p['first_name'][0] + "." if p.get('first_name') else ""
                    display = f"{initial} {p['second_name']} ({teams.get(p['team'], '?')}) £{p['now_cost']/10:.1f}m"
                    all_options.append((p["id"], display))
            all_options.sort(key=lambda x: x[1].lower() if x[0] else "")
        except:
            all_options = cur_options

        for i in range(int(n_moves)):
            c1, c2 = st.columns(2)
            def_out_idx = 0
            def_in_idx = 0
            if i < len(moves):
                def_out_idx = _get_dropdown_index(cur_options, moves[i]["out"]["id"])
                def_in_idx = _get_dropdown_index(all_options, moves[i]["in"]["id"])
                
            out_sel = c1.selectbox(f"Transfer Out {i+1}", options=cur_options, format_func=lambda x: x[1], index=def_out_idx, key=f"man_out_{i}")
            in_sel = c2.selectbox(f"Transfer In {i+1}", options=all_options, format_func=lambda x: x[1], index=def_in_idx, key=f"man_in_{i}")
            
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
                                "team": teams.get(fpl_p["team"], "?"), "position": pos_map.get(fpl_p["element_type"], "?"),
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

        sheet = f'<div class="team-sheet">{_team_sheet_html(xi["xi"], xi["bench"], _pid(cap) if cap else None, _pid(vcap) if vcap else None)}</div>'
        st.markdown(_card(sheet, f'🛡️ Final Starting XI · {xi["formation"][0]}-{xi["formation"][1]}-{xi["formation"][2]} · C = Captain · VC = Vice-Captain'), unsafe_allow_html=True)
        st.markdown(get_caveat_html(), unsafe_allow_html=True)
        
        mult_str = "×3" if is_tc else "×2"
        mult_val = cap["xp"] * 3 if is_tc else cap["xp"] * 2
        cap_role_title = "Captain (Triple Captain Active)" if is_tc else "Captain"
        
        cap_html = f'<div class="grid"><div class="pc cap-card">{_pos_chip(cap["position"])}<div style="margin-bottom:2px;" class="nm">⭐ {cap["name"]}</div><div class="meta">{cap["team"]} — {cap_role_title}</div><div class="xp" style="color:#f59e0b; margin-top:4px;">{cap["xp"]} xP ({mult_str} = {mult_val:.2f} xP)</div></div>'
        if vcap:
            cap_html += f'<div class="pc">{_pos_chip(vcap["position"])}<div style="margin-bottom:2px;" class="nm">{vcap["name"]}</div><div class="meta">{vcap["team"]} — Vice-Captain</div><div class="xp" style="color:#475569; margin-top:4px;">{vcap["xp"]} xP</div></div>'
        cap_html += "</div>"
        st.markdown(_card(cap_html, "⭐ Captaincy"), unsafe_allow_html=True)


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
            html += _player_card({"position": p["position"], "name": p["name"], "team": p["team"],
                                  "price": p["price"], "xp": p["xp"], "status": p.get("status", "")}, ov_xp)
        html += "</div>"
        st.markdown(_card(html, "📈 Ranked by xP"), unsafe_allow_html=True)
        st.markdown(get_caveat_html(), unsafe_allow_html=True)
