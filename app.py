import streamlit as st
import fpl_tools
import squad_override
import dateutil.parser

st.set_page_config(page_title="FPL AI Manager", page_icon="⚽", layout="wide")

# ------------------------------------------------------------------
# Auto-detect the upcoming gameweek straight from the FPL API
# ------------------------------------------------------------------
try:
    _gw_info = fpl_tools.get_upcoming_gameweek()
    GW_ID = int(_gw_info.get("id") or 1)
    GW_NAME = _gw_info.get("name") or f"Gameweek {GW_ID}"
    _deadline_raw = _gw_info.get("deadline_time")
    if _deadline_raw:
        _deadline_dt = dateutil.parser.isoparse(_deadline_raw)
        DEADLINE_STR = _deadline_dt.strftime("%A, %d %B %Y · %H:%M")
    else:
        DEADLINE_STR = "Check the official site for the confirmed deadline."
except Exception:
    GW_ID = 1
    GW_NAME = "Gameweek 1"
    DEADLINE_STR = "Deadline could not be loaded right now."

# ------------------------------------------------------------------
# Theme / styling — light, friendly, vibrant
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
  .chip-row {display:flex; justify-content:space-between; gap: 12px; padding: 8px 0; border-bottom: 1px solid #e2e8f0;}
  .chip-row .det {color: #64748b; font-size: 0.82rem; text-align: right;}
  .act {font-weight: 800; font-size: 0.8rem; padding: 2px 8px; border-radius: 6px;}
  .cap-card {border: 1px solid #fbbf24; background: #fff7ed;}
  .role {display:inline-block; font-size: 0.62rem; font-weight: 800; padding: 1px 6px; border-radius: 5px;
         margin-left: 5px; vertical-align: middle;}
  .role-c {background: #f59e0b; color: #fff;}
  .role-vc {background: #cbd5e1; color: #334155;}
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
  .starters {flex:1; display:grid; grid-template-columns:repeat(auto-fill,minmax(138px,1fr)); gap:8px; align-content:start;}
  .subs {flex:0 0 178px; background:#f8fafc; border:1px dashed #cbd5e1; border-radius:10px; padding:8px 10px;}
  .sub-title {font-size:0.62rem; font-weight:800; letter-spacing:.08em; text-transform:uppercase;
              color:#94a3b8; margin-bottom:4px;}
  .sub-item {font-size:0.78rem; color:#0f172a; font-weight:600; padding:3px 0; border-bottom:1px dotted #e2e8f0;
             display:flex; justify-content:space-between; gap:6px;}
  .sub-item:last-child {border-bottom:none;}
  .sub-xp {color:#64748b; font-weight:700; white-space:nowrap;}
  .empty {color:#cbd5e1; font-size:0.8rem;}
  .mc {border:1px solid #e2e8f0; border-left:3px solid #94a3b8; border-radius:10px; padding:8px 10px;
       background:#fff; min-width:0;}
  .mc .pos {display:inline-block; font-size:0.6rem; font-weight:800; letter-spacing:0.04em;
            padding:1px 6px; border-radius:4px; color:#fff;}
  .mc-nm {font-weight:700; font-size:0.82rem; color:#0f172a; margin-top:3px;
          white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .mc-meta {color:#64748b; font-size:0.7rem;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ------------------------------------------------------------------
# Render helpers
# ------------------------------------------------------------------
def _pos_chip(pos: str) -> str:
    c = POS_COLORS.get(pos, "#94a3b8")
    return f'<span class="pos" style="background:{c}">{pos}</span>'


def _player_card(p, max_xp: float, role: str = None) -> str:
    pos = p.get("position", "?")
    c = POS_COLORS.get(pos, "#94a3b8")
    pct = int(round(min(100.0, (p.get("xp", 0) / max_xp) * 100))) if max_xp else 0
    role_html = ""
    if role == "C":
        role_html = '<span class="role role-c">C</span>'
    elif role == "VC":
        role_html = '<span class="role role-vc">VC</span>'
    return (
        f'<div class="pc" style="border-left:3px solid {c}">'
        f'<div>{_pos_chip(pos)}{role_html}</div>'
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
    return (
        f'<div class="mc" style="border-left-color:{c}">'
        f'<div><span style="background:{c};" class="pos">{pos}</span>{role_html}</div>'
        f'<div class="mc-nm" title="{p.get("name", "?")}">{p.get("name", "?")}</div>'
        f'<div class="mc-meta">{p.get("team", "?")} · {p.get("xp", 0)} xP</div></div>'
    )


def _team_sheet_html(starters, bench, captain_id=None, vcap_id=None) -> str:
    """Grouped team sheet: one row per position, with a Subs column on the right."""
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
                f'<div class="sub-item"><span>{p.get("name", "?")}</span>'
                f'<span class="sub-xp">{p.get("xp", 0)} xP</span></div>'
                for p in be_players
            )
        else:
            subs_html = '<div class="empty">—</div>'

        html += (
            f'<div class="team-row">'
            f'<div class="pos-col"><div class="pos-emoji">{pos_emoji.get(pos, "⚽")}</div>'
            f'<div class="pos-name">{pos}</div></div>'
            f'<div class="starters">{cards}</div>'
            f'<div class="subs"><div class="sub-title">Subs</div>{subs_html}</div>'
            f'</div>'
        )
    return html


def _api_starters_bench(squad):
    """Split an FPL API squad into starters and bench using the multiplier field."""
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


def _chip_action_style(action: str) -> str:
    if action in ("Use", "Plan", "Consider"):
        return "#059669"
    return "#64748b"


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
        '<div class="badge"><span class="g"></span> Engineered by Waqas Hussain</div>',
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------
# Hero, deadline & plain-English overview
# ------------------------------------------------------------------
st.markdown(
    '<div class="hero"><h1>Pre-deadline dashboard</h1>'
    '<div class="sub">One button. Clear, definitive instructions for your FPL week — run it an hour or two before the deadline.</div></div>',
    unsafe_allow_html=True,
)

st.markdown(
    f'<div class="deadline-hero"><div class="dl-gw">⏰ {GW_NAME} deadline</div>'
    f'<div class="dl-time">{DEADLINE_STR}</div>'
    f'<div class="dl-sub">Your plan is built around this gameweek — no manual gameweek setting needed.</div></div>',
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="overview">'
    '<b>What this does:</b> pop in your Manager ID and we’ll pull up your current squad, work out which players are '
    'likely to score the most points this week, and hand you a clear to-do list — who to transfer in and out, your '
    'best starting eleven and bench order, and who to captain.'
    '<br><br>'
    '⚠️ <b>Already made a midweek transfer?</b> The official site hides those changes until the deadline, so we’ll '
    'still see your old squad. Pop your changes into the <b>Manual Squad Override</b> below and we’ll plan from that instead.'
    '</div>',
    unsafe_allow_html=True,
)

with st.expander("🔍 How it works", expanded=True):
    st.markdown(
        """
🧑‍💻 **Your squad.** We read your current 15 players straight from the official FPL site, along with your bank
balance and team value. Your active captain and vice-captain are taken directly from your team.

📊 **Player ratings.** Every player gets an expected-points score for the upcoming gameweek. This blends their recent
form, expected goals and assists, clean-sheet chances, likely minutes, and how strong their opponent is — while
respecting the official FPL scoring rules.

🧠 **The plan.** A solver picks the transfers and starting eleven that give you the highest projected points while
staying within the budget, the 2–5–5–3 squad shape, and the three-players-per-team limit. It only takes a points hit
when the boost clearly outweighs the cost.

⚖️ **Risk appetite.** The sidebar toggle changes the objective: *Conservative* leans on popular, dependable starters;
*Balanced* is pure expected points; *Aggressive* chases low-ownership, high-upside picks and accepts more risk.

🎲 **Chips.** We look ahead at double and blank gameweeks and suggest when to use your Triple Captain, Bench Boost,
Free Hit and Wildcard.
        """
    )


# ------------------------------------------------------------------
# Step 1 — Manager ID & current squad (loads dynamically)
# ------------------------------------------------------------------
with st.container(border=True):
    st.markdown("### 1 · Your team")

    manager_id = st.text_input(
        "Manager ID",
        key="mid_input",
        help="Your unique FPL ID — the number in your team-page URL.",
    )
    st.caption(
        'Find your ID in your team-page URL — the number after <span class="dummy-url">/entry/</span> '
        'e.g. <span class="dummy-url">fantasy.premierleague.com/entry/7261134/event/1</span>',
        unsafe_allow_html=True,
    )

    load_clicked = st.button("📋 Load my team", type="secondary", use_container_width=True, key="btn_load")

    if load_clicked:
        if not manager_id.strip():
            st.warning("Pop your Manager ID in first — it’s the number in your FPL team-page URL.")
        else:
            with st.spinner("Fetching your current squad and free transfers…"):
                try:
                    preview = fpl_tools.score_my_squad(manager_id.strip(), GW_ID, risk=risk_label.lower())
                    st.session_state["squad_preview"] = preview
                    try:
                        st.session_state["api_free_transfers"] = fpl_tools.get_free_transfers(manager_id.strip())
                    except Exception:
                        st.session_state["api_free_transfers"] = 1
                except Exception as e:
                    st.session_state["squad_preview"] = {"error": str(e)}
                    st.session_state["api_free_transfers"] = 1

    preview = st.session_state.get("squad_preview")

    if preview and "error" in preview:
        st.error("⚠️ " + preview["error"])
        st.info("Check your Manager ID (the number in your FPL team URL) and try again.")
    elif preview:
        api_ft = st.session_state.get("api_free_transfers", 1)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Team", preview["team_name"])
        m2.metric("Bank", f"£{preview['bank']}m")
        m3.metric("Team value", f"£{preview['team_value']}m")
        m4.metric("Free transfers", api_ft)

        squad = preview.get("squad", [])
        starters, bench = _api_starters_bench(squad)
        cap = next((p for p in squad if p.get("is_captain")), None)
        vc = next((p for p in squad if p.get("is_vice_captain")), None)

        sheet = f'<div class="team-sheet">{_team_sheet_html(starters, bench, _pid(cap) if cap else None, _pid(vc) if vc else None)}</div>'
        st.markdown(_card(sheet, "Your current squad · C = captain · VC = vice-captain"), unsafe_allow_html=True)

        if cap:
            cap_line = f"**Captain:** {cap['name']} ({cap['team']})"
            if vc:
                cap_line += f" · **Vice-captain:** {vc['name']} ({vc['team']})"
            st.caption(cap_line)


# ------------------------------------------------------------------
# Step 2 — Generate the action plan
# ------------------------------------------------------------------
st.markdown("---")
if st.button("🚀 Generate Action Plan", type="primary", use_container_width=True, key="btn_plan"):
    if not manager_id.strip():
        st.warning("Pop your Manager ID in first — it’s the number in your FPL team-page URL.")
    else:
        free_transfers = st.session_state.get("api_free_transfers")
        if free_transfers is None:
            try:
                free_transfers = fpl_tools.get_free_transfers(manager_id.strip())
            except Exception:
                free_transfers = 1

        with st.spinner("Pulling your squad, modelling points, solving transfers & line-up…"):
            try:
                plan = fpl_tools.build_gameweek_briefing(
                    manager_id.strip(), GW_ID, int(free_transfers), risk=risk_label.lower()
                )
                st.session_state["plan"] = plan
                st.session_state["squad_preview"] = plan
                st.session_state["api_free_transfers"] = int(free_transfers)
            except Exception as e:
                st.session_state["plan"] = {"error": str(e)}

plan = st.session_state.get("plan")

if plan and "error" in plan:
    st.error("⚠️ " + plan["error"])
    st.info("Check your Manager ID (the number in your FPL team URL).")
elif plan:
    st.markdown("---")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Team", plan["team_name"])
    m2.metric("Bank", f"£{plan['bank']}m")
    m3.metric("Team value", f"£{plan['team_value']}m")
    m4.metric("Projected XI", f"{plan['starting_xi']['total_xp']} xP")

    st.markdown("---")

    moves = plan.get("transfers", [])
    transfer_html = f'<h3>Transfers</h3><div style="color:#475569;margin:4px 0 8px 0;">{plan["hit_advice"]}</div>'
    if moves:
        for m in moves:
            transfer_html += (
                f'<div class="transfer">'
                f'<span class="out">OUT {m["out"]["name"]}</span><span class="arrow">→</span>'
                f'<span class="inn">IN {m["in"]["name"]}</span>'
                f'<span class="gain">+{m["xp_gain"]} xP · £{m["cost"]:+}m</span></div>'
            )
    else:
        transfer_html += '<div style="color:#64748b;">No transfers recommended this week — hold.</div>'
    st.markdown(_card(transfer_html, "🚀 Transfers"), unsafe_allow_html=True)

    xi = plan["starting_xi"]
    cap = xi.get("captain")
    vcap = xi.get("vice_captain")
    sheet = f'<div class="team-sheet">{_team_sheet_html(xi["xi"], xi["bench"], _pid(cap) if cap else None, _pid(vcap) if vcap else None)}</div>'
    st.markdown(_card(sheet, f'🛡️ Best starting XI · {xi["formation"][0]}-{xi["formation"][1]}-{xi["formation"][2]} · C = captain · VC = vice-captain'), unsafe_allow_html=True)

    cap_html = ""
    if cap:
        cap_html = f'<div class="grid"><div class="pc cap-card">{_pos_chip(cap["position"])}<div class="nm">⭐ {cap["name"]}</div><div class="meta">{cap["team"]} — Captain</div><div class="xp" style="color:#f59e0b">{cap["xp"]} xP (×2 = {cap["xp"]*2:.1f})</div></div>'
    if vcap:
        cap_html += f'<div class="pc">{_pos_chip(vcap["position"])}<div class="nm">{vcap["name"]}</div><div class="meta">{vcap["team"]} — Vice-Captain</div><div class="xp" style="color:#475569">{vcap["xp"]} xP</div></div>'
    cap_html += "</div>"
    st.markdown(_card(cap_html, "⭐ Captaincy"), unsafe_allow_html=True)

    chip_html = ""
    for ch in plan["chips"]:
        color = _chip_action_style(ch["action"])
        chip_html += (
            f'<div class="chip-row"><div><b>{ch["chip"]}</b> '
            f'<span class="act" style="color:{color};border:1px solid {color}">{ch["action"]}</span></div>'
            f'<div class="det">{ch["detail"]}</div></div>'
        )
    st.markdown(_card(chip_html, "🎲 Chips"), unsafe_allow_html=True)

    st.caption("⚡ Change the Risk Appetite in the sidebar and hit Generate again to re-solve under a different strategy.")


# ------------------------------------------------------------------
# Manual squad override (midweek moves) — clearly defined section
# ------------------------------------------------------------------
st.markdown(
    '<div class="override-head">⚙️ Manual Squad Override <span>midweek transfers</span></div>',
    unsafe_allow_html=True,
)

with st.container(border=True):
    st.info(
        "Changed your team since the last gameweek? The official FPL site hides midweek transfers until the "
        "deadline. Either upload a screenshot of your new squad, or tick the box below to enter just the transfers "
        "you made. You can also correct your free transfers and bank here."
    )

    try:
        bootstrap = fpl_tools._get_bootstrap()
    except Exception:
        bootstrap = {"elements": [], "teams": []}

    players_by_id = {p["id"]: p for p in bootstrap["elements"]}
    teams = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    dropdown_options = {pos: [] for pos in POS_ORDER}
    for p in bootstrap["elements"]:
        pos = pos_map.get(p["element_type"])
        if pos:
            display = f"{p['first_name']} {p['second_name']} ({teams.get(p['team'], '?')}) £{p['now_cost']/10:.1f}m"
            dropdown_options[pos].append((p["id"], display))

    all_options = []
    for pos in POS_ORDER:
        all_options.extend(dropdown_options[pos])
    all_options.sort(key=lambda x: x[1].lower())

    squad_default = (st.session_state.get("squad_preview") or {}).get("squad") or []

    default_ft = st.session_state.get("api_free_transfers", 1)
    plan_bank = float((st.session_state.get("squad_preview") or {}).get("bank", 0.0))

    ob1, ob2 = st.columns(2)
    with ob1:
        bank_override = st.number_input("Bank balance (£m)", 0.0, 50.0, plan_bank, 0.1, key="ov_bank")
    with ob2:
        ft_override = st.number_input("Free transfers", 0, 5, int(default_ft), key="ov_ft")

    default_selections = {pos: [] for pos in POS_ORDER}
    for p in squad_default:
        pos = p["position"]
        for idx, (pid, _) in enumerate(dropdown_options[pos]):
            if pid == p.get("player_id"):
                if idx not in default_selections[pos]:
                    default_selections[pos].append(idx)
                break

    def _score_player_ids(player_ids, risk):
        fixture_lookup = fpl_tools._build_fixture_lookup()
        analysed = []
        for pid in player_ids:
            fpl_p = players_by_id.get(pid)
            if not fpl_p:
                continue
            xp, note = fpl_tools._player_xp(fpl_p, fixture_lookup, event=GW_ID, risk=risk)
            analysed.append({
                "player_id": pid,
                "name": f"{fpl_p['first_name']} {fpl_p['second_name']}",
                "team": teams.get(fpl_p["team"], "?"),
                "team_id": fpl_p["team"],
                "position": pos_map.get(fpl_p["element_type"], "?"),
                "price": fpl_p["now_cost"] / 10.0,
                "xp": xp,
                "status": note,
                "is_captain": False,
            })
        return analysed

    def _store_override(team_name, analysed, bank, ft):
        transfers = fpl_tools.suggest_transfers_for_custom_squad(
            analysed, float(bank), int(ft), event=GW_ID, risk=risk_label.lower())
        lineup = fpl_tools.select_starting_xi(analysed)
        chips = fpl_tools.recommend_chips(bootstrap, analysed, fpl_tools._build_gameweek_map(), GW_ID)
        st.session_state["override_analysis"] = {
            "team_name": team_name, "bank": float(bank),
            "team_value": round(sum(p["price"] for p in analysed), 1), "squad": analysed,
            "transfers": transfers, "starting_xi": lineup, "chips": chips,
        }

    tab_full, tab_transfers = st.tabs(["📸 Full squad (screenshot)", "🔄 I’ve made transfers"])

    # ---- Tab 1: full squad via screenshot / dropdowns ----
    with tab_full:
        uploaded_image = st.file_uploader("Upload squad screenshot (optional)", type=["png", "jpg", "jpeg"], key="override_image")

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
            if image_matched:
                st.success(f"Screenshot parsed: {len(image_matched)} players matched.")
            image_unmatched = st.session_state.get("cached_image_unmatched", [])
            if image_unmatched:
                st.warning(f"⚠️ {len(image_unmatched)} unmatched: {', '.join(image_unmatched)}")

        image_by_pos = {pos: [] for pos in POS_ORDER}
        for p in image_matched:
            if p["position"] in image_by_pos:
                image_by_pos[p["position"]].append(p)

        override_squad = []
        quotas = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
        for pos, count in quotas.items():
            st.markdown(f"**{pos}**")
            cols = st.columns(min(count, 5))
            for i in range(count):
                with cols[i % len(cols)]:
                    img_state = f"{uploaded_image.name}_{uploaded_image.size}" if uploaded_image else "none"
                    key_name = f"ov_{pos}_{i}_{img_state}"
                    idx_default = default_selections[pos][i] if i < len(default_selections[pos]) else 0
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
                    if selected:
                        override_squad.append(selected[0])

        if st.button("Apply & Reanalyse", type="primary", key="btn_override"):
            if len(override_squad) != 15:
                st.error("Select exactly 15 players (2 GK, 5 DEF, 5 MID, 3 FWD).")
            else:
                with st.spinner("Scoring your squad and solving transfers, line-up & chips…"):
                    try:
                        analysed = _score_player_ids(override_squad, risk_label.lower())
                        _store_override("Manual Override Squad", analysed, bank_override, ft_override)
                    except Exception as e:
                        st.error(f"Could not analyse your squad: {e}")

    # ---- Tab 2: enter just the transfers made ----
    with tab_transfers:
        current_squad = squad_default
        if not current_squad:
            st.info("Load your team first (section 1) so we know which players you had before your midweek transfers.")
        else:
            made_transfers = st.checkbox("✅ I made transfers this week", key="midweek_transfers")
            if made_transfers:
                n_moves = st.number_input("How many transfers did you make?", 1, 3, 1, key="n_moves")

                cur_options = sorted(
                    [(p["player_id"], f"{p['name']} ({p['team']})") for p in current_squad],
                    key=lambda x: (POS_ORDER.index(next((q["position"] for q in current_squad if q["player_id"] == x[0]), "MID")), x[1]),
                )
                out_ids = []
                in_ids = []
                for i in range(int(n_moves)):
                    c1, c2 = st.columns(2)
                    out_sel = c1.selectbox(
                        f"Transfer out {i+1}", options=cur_options,
                        format_func=lambda x: x[1], key=f"mt_out_{i}",
                        help="The player you sold.",
                    )
                    in_sel = c2.selectbox(
                        f"Transfer in {i+1}", options=all_options,
                        format_func=lambda x: x[1], key=f"mt_in_{i}",
                        help="The player you bought.",
                    )
                    out_ids.append(out_sel[0])
                    in_ids.append(in_sel[0])

                if st.button("Apply transfers & reanalyse", type="primary", key="btn_apply_transfers"):
                    errors = []
                    if len(set(out_ids)) != len(out_ids):
                        errors.append("Please pick a different player for each 'transfer out'.")
                    if len(set(in_ids)) != len(in_ids):
                        errors.append("Please pick a different player for each 'transfer in'.")
                    for o, i in zip(out_ids, in_ids):
                        if o == i:
                            errors.append("A 'transfer out' and 'transfer in' can't be the same player.")

                    if errors:
                        for e in errors:
                            st.error(e)
                    else:
                        new_ids = [p["player_id"] for p in current_squad]
                        for oid in out_ids:
                            if oid in new_ids:
                                new_ids.remove(oid)
                        for iid in in_ids:
                            if iid in new_ids:
                                errors.append("You can't transfer in a player you already own.")
                                break
                            new_ids.append(iid)

                        if not errors:
                            counts = {"GK": 0, "DEF": 0, "MID": 0, "FWD": 0}
                            for pid in new_ids:
                                fpl_p = players_by_id.get(pid)
                                pos = pos_map.get(fpl_p["element_type"], "?") if fpl_p else "?"
                                counts[pos] = counts.get(pos, 0) + 1
                            if counts != {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}:
                                st.error("That combination doesn't make a valid 2-5-5-3 squad. Check your transfers.")
                            else:
                                with st.spinner("Scoring your updated squad and solving line-up & chips…"):
                                    try:
                                        analysed = _score_player_ids(new_ids, risk_label.lower())
                                        _store_override("Updated squad (with my transfers)", analysed, bank_override, ft_override)
                                    except Exception as e:
                                        st.error(f"Could not analyse your squad: {e}")

    # ---- Render the override result ----
    ov = st.session_state.get("override_analysis")
    if ov:
        st.markdown("---")
        st.success(f"**{ov['team_name']}** — Bank £{ov['bank']}m · {ov['starting_xi']['total_xp']} xP projected")

        tr = ov["transfers"]
        moves = tr.get("transfers", [])
        transfer_html = f'<h3>Transfers</h3><div style="color:#475569;margin:4px 0 8px 0;">{tr.get("hit_advice", "")}</div>'
        if moves:
            for m in moves:
                transfer_html += (
                    f'<div class="transfer"><span class="out">OUT {m["out"]["name"]}</span>'
                    f'<span class="arrow">→</span><span class="inn">IN {m["in"]["name"]}</span>'
                    f'<span class="gain">+{m["xp_gain"]} xP · £{m["cost"]:+}m</span></div>'
                )
        else:
            transfer_html += '<div style="color:#64748b;">No transfers recommended.</div>'
        st.markdown(_card(transfer_html, "🚀 Transfers"), unsafe_allow_html=True)

        xi = ov["starting_xi"]
        cap = xi["captain"]
        vcap = xi["vice_captain"]
        sheet = f'<div class="team-sheet">{_team_sheet_html(xi["xi"], xi["bench"], _pid(cap) if cap else None, _pid(vcap) if vcap else None)}</div>'
        st.markdown(_card(sheet, f'🛡️ Best starting XI · {xi["formation"][0]}-{xi["formation"][1]}-{xi["formation"][2]}'), unsafe_allow_html=True)

        cap_html = f'<div class="grid"><div class="pc cap-card">{_pos_chip(cap["position"])}<div class="nm">⭐ {cap["name"]}</div><div class="meta">{cap["team"]} — Captain</div><div class="xp" style="color:#f59e0b">{cap["xp"]} xP (×2 = {cap["xp"]*2:.1f})</div></div>'
        if vcap:
            cap_html += f'<div class="pc">{_pos_chip(vcap["position"])}<div class="nm">{vcap["name"]}</div><div class="meta">{vcap["team"]} — Vice-Captain</div><div class="xp" style="color:#475569">{vcap["xp"]} xP</div></div>'
        cap_html += "</div>"
        st.markdown(_card(cap_html, "⭐ Captaincy"), unsafe_allow_html=True)

        chip_html = "".join(
            f'<div class="chip-row"><div><b>{ch["chip"]}</b> <span class="act" style="color:{_chip_action_style(ch["action"])};border:1px solid {_chip_action_style(ch["action"])}">{ch["action"]}</span></div><div class="det">{ch["detail"]}</div></div>'
            for ch in ov["chips"]
        )
        st.markdown(_card(chip_html, "🎲 Chips"), unsafe_allow_html=True)


# ------------------------------------------------------------------
# Top players
# ------------------------------------------------------------------
with st.expander("📊 Top players by xP"):
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
        if "error" in r:
            st.error(r["error"])
        else:
            ov_xp = max([p["xp"] for p in r["players"]] + [1.0])
            html = '<div class="grid">'
            for p in r["players"]:
                html += _player_card({"position": p["position"], "name": p["name"], "team": p["team"],
                                      "price": p["price"], "xp": p["xp"]}, ov_xp)
            html += "</div>"
            st.markdown(_card(html, "📈 Ranked by xP"), unsafe_allow_html=True)