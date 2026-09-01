import streamlit as st
import fpl_tools
import squad_override
import re
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
  .block-container {padding-top: 1.4rem; padding-bottom: 4rem; max-width: 1180px;}
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
         color: #3730a3; font-size: 0.94rem; line-height: 1.55; margin-bottom: 16px;}
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
    'likely to score the most points this week, and hand you a clear to-do list — who to transfer in and out, your best '
    'starting eleven and bench order, and who to captain.'
    '<br><br>'
    '⚠️ <b>Already made a midweek transfer?</b> The official site hides those changes until the deadline, so we’ll still '
    'see your old squad. Pop your new squad into the <b>Manual Squad Override</b> below and we’ll plan from that instead.'
    '</div>',
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------
# Step 1 — Manager ID & current squad (loads dynamically)
# ------------------------------------------------------------------
with st.container(border=True):
    st.markdown("### 1 · Your team")

    manager_id = st.text_input(
        "Manager ID",
        placeholder="e.g. 7261134",
        key="mid_input",
        help="Your unique FPL ID — the number in your team-page URL "
             "(e.g. fantasy.premierleague.com/entry/7261134/event/1). Works for any manager's ID.",
    )

    col_load, col_ft = st.columns([1, 1])
    with col_load:
        load_clicked = st.button("📋 Load my team", type="secondary", use_container_width=True, key="btn_load")
    with col_ft:
        free_transfers = st.number_input("Free transfers", 0, 5, 1, key="ft")

    if load_clicked:
        if not manager_id.strip():
            st.warning("Pop your Manager ID in first — it’s the number in your FPL team-page URL.")
        else:
            with st.spinner("Fetching your current squad…"):
                try:
                    preview = fpl_tools.score_my_squad(manager_id.strip(), GW_ID, risk=risk_label.lower())
                    st.session_state["squad_preview"] = preview
                except Exception as e:
                    st.session_state["squad_preview"] = {"error": str(e)}

    preview = st.session_state.get("squad_preview")

    if preview and "error" in preview:
        st.error("⚠️ " + preview["error"])
        st.info("Check your Manager ID (the number in your FPL team URL) and try again.")
    elif preview:
        m1, m2, m3 = st.columns(3)
        m1.metric("Team", preview["team_name"])
        m2.metric("Bank", f"£{preview['bank']}m")
        m3.metric("Team value", f"£{preview['team_value']}m")

        pv_xp = max([p.get("xp", 0) for p in preview.get("squad", [])] + [1.0])
        pv_html = '<div class="grid">'
        for p in preview.get("squad", []):
            role = "C" if p.get("is_captain") else ("VC" if p.get("is_vice_captain") else None)
            pv_html += _player_card(p, pv_xp, role)
        pv_html += "</div>"
        st.markdown(_card(pv_html, "Your current squad · C = captain · VC = vice-captain"), unsafe_allow_html=True)

        # Friendly confirmation of the active captain / vice-captain.
        cap = next((p for p in preview.get("squad", []) if p.get("is_captain")), None)
        vc = next((p for p in preview.get("squad", []) if p.get("is_vice_captain")), None)
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
        with st.spinner("Pulling your squad, modelling points, solving transfers & line-up…"):
            try:
                plan = fpl_tools.build_gameweek_briefing(
                    manager_id.strip(), GW_ID, int(free_transfers), risk=risk_label.lower()
                )
                st.session_state["plan"] = plan
                st.session_state["squad_preview"] = plan
            except Exception as e:
                st.session_state["plan"] = {"error": str(e)}

plan = st.session_state.get("plan")

if plan and "error" in plan:
    st.error("⚠️ " + plan["error"])
    st.info("Check your Manager ID (the number in your FPL team URL).")
elif plan:
    st.markdown("---")

    # Header metrics
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Team", plan["team_name"])
    m2.metric("Bank", f"£{plan['bank']}m")
    m3.metric("Team value", f"£{plan['team_value']}m")
    m4.metric("Projected XI", f"{plan['starting_xi']['total_xp']} xP")

    st.markdown("---")

    # Transfers
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

    # Starting XI + Bench
    xi = plan["starting_xi"]
    all_xp = max([p.get("xp", 0) for p in plan["squad"]] + [1.0])
    d, m, f = xi["formation"]

    cap = xi.get("captain")
    vcap = xi.get("vice_captain")
    cap_id = _pid(cap) if cap else None
    vcap_id = _pid(vcap) if vcap else None

    xi_html = f'<h3>Starting XI · {d}-{m}-{f}</h3><div class="grid">'
    for p in xi["xi"]:
        role = None
        if cap_id is not None and _pid(p) == cap_id:
            role = "C"
        elif vcap_id is not None and _pid(p) == vcap_id:
            role = "VC"
        xi_html += _player_card(p, all_xp, role)
    xi_html += "</div>"

    bench_html = '<div class="grid">'
    for p in xi["bench"]:
        bench_html += _player_card(p, all_xp)
    bench_html += "</div>"

    col_xi, col_bench = st.columns([2, 1])
    with col_xi:
        st.markdown(_card(xi_html, "🛡️ Starting XI (C = captain · VC = vice-captain)"), unsafe_allow_html=True)
    with col_bench:
        st.markdown(_card(bench_html, "🪑 Bench"), unsafe_allow_html=True)

    # Captaincy
    cap_html = ""
    if cap:
        cap_html = f'<div class="grid"><div class="pc cap-card">{_pos_chip(cap["position"])}<div class="nm">⭐ {cap["name"]}</div><div class="meta">{cap["team"]} — Captain</div><div class="xp" style="color:#f59e0b">{cap["xp"]} xP (×2 = {cap["xp"]*2:.1f})</div></div>'
    if vcap:
        cap_html += f'<div class="pc">{_pos_chip(vcap["position"])}<div class="nm">{vcap["name"]}</div><div class="meta">{vcap["team"]} — Vice-Captain</div><div class="xp" style="color:#475569">{vcap["xp"]} xP</div></div>'
    cap_html += "</div>"
    st.markdown(_card(cap_html, "⭐ Captaincy"), unsafe_allow_html=True)

    # Chips
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
        "deadline. Upload a screenshot of your updated squad (or correct the dropdowns below) and hit "
        "“Apply & Reanalyse” to build the plan from your new team instead."
    )

    uploaded_image = st.file_uploader("Upload squad screenshot (optional)", type=["png", "jpg", "jpeg"], key="override_image")

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

    squad_default = (st.session_state.get("squad_preview") or {}).get("squad") or []
    plan_bank = float((st.session_state.get("squad_preview") or {}).get("bank", 0.0))

    default_selections = {pos: [] for pos in POS_ORDER}
    for p in squad_default:
        pos = p["position"]
        for idx, (pid, _) in enumerate(dropdown_options[pos]):
            if pid == p.get("player_id"):
                if idx not in default_selections[pos]:
                    default_selections[pos].append(idx)
                break

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

    ob1, ob2 = st.columns(2)
    with ob1:
        bank_override = st.number_input("Bank balance (£m)", 0.0, 50.0, plan_bank, 0.1, key="ov_bank")
    with ob2:
        ft_override = st.number_input("Free transfers", 0, 5, int(free_transfers), key="ov_ft")

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
                    pid, display = selected
                    team_match = re.search(r"\((.*?)\)", display)
                    price_match = re.search(r"£([\d.]+)m", display)
                    override_squad.append({
                        "player_id": pid,
                        "name": display.split(" (")[0],
                        "team": team_match.group(1) if team_match else "?",
                        "position": pos,
                        "price": float(price_match.group(1)) if price_match else 0.0,
                    })

    if st.button("Apply & Reanalyse", type="primary", key="btn_override"):
        if len(override_squad) != 15:
            st.error("Select exactly 15 players (2 GK, 5 DEF, 5 MID, 3 FWD).")
        else:
            with st.spinner("Scoring your squad and solving transfers, line-up & chips…"):
                try:
                    fixture_lookup = fpl_tools._build_fixture_lookup()
                    analysed = []
                    for p in override_squad:
                        fpl_p = players_by_id.get(p["player_id"])
                        if fpl_p:
                            xp, note = fpl_tools._player_xp(fpl_p, fixture_lookup, event=GW_ID, risk=risk_label.lower())
                            analysed.append({
                                "player_id": p["player_id"], "name": p["name"], "team": p["team"],
                                "team_id": fpl_p["team"], "position": p["position"], "price": p["price"],
                                "xp": xp, "status": note, "is_captain": False,
                            })
                    transfers = fpl_tools.suggest_transfers_for_custom_squad(
                        analysed, float(bank_override), int(ft_override), event=GW_ID, risk=risk_label.lower())
                    lineup = fpl_tools.select_starting_xi(analysed)
                    chips = fpl_tools.recommend_chips(bootstrap, analysed, fpl_tools._build_gameweek_map(), GW_ID)
                    st.session_state["override_analysis"] = {
                        "team_name": "Manual Override Squad", "bank": float(bank_override),
                        "team_value": round(sum(p["price"] for p in analysed), 1), "squad": analysed,
                        "transfers": transfers, "starting_xi": lineup, "chips": chips,
                    }
                except Exception as e:
                    st.error(f"Could not analyse your squad: {e}")

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
        ov_xp = max([p.get("xp", 0) for p in ov["squad"]] + [1.0])
        d, m, f = xi["formation"]
        xi_html = f'<h3>Starting XI · {d}-{m}-{f}</h3><div class="grid">' + "".join(_player_card(p, ov_xp) for p in xi["xi"]) + "</div>"
        bench_html = '<div class="grid">' + "".join(_player_card(p, ov_xp) for p in xi["bench"]) + "</div>"
        c1, c2 = st.columns([2, 1])
        with c1:
            st.markdown(_card(xi_html, "🛡️ Starting XI"), unsafe_allow_html=True)
        with c2:
            st.markdown(_card(bench_html, "🪑 Bench"), unsafe_allow_html=True)

        cap = xi["captain"]
        vcap = xi["vice_captain"]
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
# How it works — plain English
# ------------------------------------------------------------------
with st.expander("🔍 How it works"):
    st.markdown(
        """
**Your squad.** We read your current 15 players straight from the official FPL site, along with your bank balance
and team value. Your active captain and vice-captain are taken directly from your team, so they’re exactly what you set.

**Player ratings.** Every player gets an expected-points score for the upcoming gameweek. This blends their recent
form, goals and assists expected, clean-sheet chances, likely minutes, and how strong their opponent is — while also
respecting the official FPL point scoring rules.

**The plan.** A solver picks the transfers and starting eleven that give you the highest projected points while staying
within the budget, the 2–5–5–3 squad shape, and the three-players-per-team limit. It only takes a points hit when the
boost clearly outweighs the cost.

**Risk appetite.** The sidebar toggle changes the objective: *Conservative* leans on popular, dependable starters;
*Balanced* is pure expected points; *Aggressive* chases low-ownership, high-upside picks and accepts more risk.

**Chips.** We look ahead at double and blank gameweeks over the next few weeks and suggest when to use your Triple
Captain, Bench Boost, Free Hit and Wildcard.
        """
    )


# ------------------------------------------------------------------
# Wildcard builder
# ------------------------------------------------------------------
with st.expander("🛠️ Build optimal squad (Wildcard mode)"):
    w1, w2 = st.columns([2, 1])
    with w1:
        budget = st.slider("Budget (£m)", 80.0, 100.0, 100.0, 0.5, key="wc_budget")
    with w2:
        if st.button("Build", type="secondary", key="btn_wc"):
            with st.spinner("Solving your optimal squad…"):
                opt = fpl_tools.optimise_full_squad(budget=budget, risk=risk_label.lower())
            st.session_state["wildcard"] = opt
    opt = st.session_state.get("wildcard")
    if opt:
        if "error" in opt:
            st.error(opt["error"])
        else:
            st.success(f"£{opt['total_price']}m / £{opt['budget']}m — {opt['total_xp']} xP")
            ov_xp = max([p["xp"] for p in opt["squad"]] + [1.0])
            html = '<div class="grid">'
            for p in sorted(opt["squad"], key=lambda x: (POS_ORDER.index(x["position"]), -x["xp"])):
                html += _player_card(p, ov_xp)
            html += "</div>"
            st.markdown(_card(html, "🧱 15-man squad"), unsafe_allow_html=True)


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