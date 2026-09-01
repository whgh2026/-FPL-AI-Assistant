import streamlit as st
import fpl_tools
import squad_override
import re
import copy

st.set_page_config(page_title="FPL AI Manager", page_icon="⚽", layout="wide")

# ------------------------------------------------------------------
# Theme / styling
# ------------------------------------------------------------------
POS_COLORS = {"GK": "#f5b301", "DEF": "#38bdf8", "MID": "#34d399", "FWD": "#fb7185"}
POS_ORDER = ["GK", "DEF", "MID", "FWD"]

CSS = """
<style>
  #MainMenu, footer {visibility: hidden;}
  .block-container {padding-top: 2rem; padding-bottom: 4rem; max-width: 1240px;}
  .hero h1 {font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; margin: 0;}
  .hero .sub {color: #8b95a6; font-size: 0.95rem; margin-top: 0.2rem;}
  .card {background: linear-gradient(160deg, rgba(255,255,255,0.05), rgba(255,255,255,0.012));
         border: 1px solid rgba(255,255,255,0.09); border-radius: 16px; padding: 18px 20px; margin-bottom: 14px;}
  .card h3 {margin: 0 0 2px 0; font-size: 1.02rem; font-weight: 700;}
  .section-label {text-transform: uppercase; letter-spacing: 0.1em; font-size: 0.7rem; color: #8b95a6; margin-bottom: 10px;}
  .grid {display: grid; grid-template-columns: repeat(auto-fill, minmax(178px, 1fr)); gap: 10px;}
  .pc {border: 1px solid rgba(255,255,255,0.07); border-radius: 12px; padding: 12px 13px; background: rgba(255,255,255,0.02);}
  .pc .pos {display:inline-block; font-size: 0.66rem; font-weight: 800; letter-spacing: 0.05em; padding: 2px 7px; border-radius: 5px; color: #0b0d12;}
  .pc .nm {font-weight: 700; font-size: 0.92rem; margin: 6px 0 1px 0;}
  .pc .meta {color: #8b95a6; font-size: 0.75rem;}
  .bar {height: 5px; background: rgba(255,255,255,0.08); border-radius: 3px; margin: 8px 0 4px 0; overflow: hidden;}
  .bar-fill {height: 100%; border-radius: 3px;}
  .xp {font-weight: 800; font-size: 0.88rem;}
  .badge {display:inline-flex; align-items:center; gap:8px; padding: 8px 13px; border-radius: 999px;
          background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); font-size: 0.8rem; color:#dbe2ea;}
  .badge .g {width: 8px; height: 8px; border-radius: 50%; background: #34d399; box-shadow: 0 0 8px #34d399;}
  .transfer {display:flex; align-items:center; gap: 10px; padding: 9px 0; border-bottom: 1px solid rgba(255,255,255,0.05);}
  .transfer .arrow {color: #8b95a6;}
  .out {color: #fb7185; font-weight: 700;}
  .inn {color: #34d399; font-weight: 700;}
  .gain {margin-left: auto; font-weight: 700; font-size: 0.85rem; color: #cbd5e1;}
  .chip-row {display:flex; justify-content:space-between; gap: 12px; padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.05);}
  .chip-row .det {color: #8b95a6; font-size: 0.82rem; text-align: right;}
  .act {font-weight: 800; font-size: 0.8rem; padding: 2px 8px; border-radius: 6px;}
  .cap-card {border: 1px solid rgba(245,179,1,0.5); background: rgba(245,179,1,0.06);}
  .deadline {color: #8b95a6; font-size: 0.85rem;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ------------------------------------------------------------------
# Render helpers
# ------------------------------------------------------------------
def _pos_chip(pos: str) -> str:
    c = POS_COLORS.get(pos, "#888")
    return f'<span class="pos" style="background:{c}">{pos}</span>'


def _player_card(p, max_xp: float) -> str:
    c = POS_COLORS.get(p.get("position"), "#888")
    pct = int(round(min(100.0, (p.get("xp", 0) / max_xp) * 100))) if max_xp else 0
    return (
        f'<div class="pc" style="border-left:3px solid {c}">'
        f'{_pos_chip(p["position"])}<div class="nm">{p["name"]}</div>'
        f'<div class="meta">{p.get("team", "?")} · £{p.get("price", 0):.1f}m</div>'
        f'<div class="bar"><div class="bar-fill" style="width:{pct}%;background:{c}"></div></div>'
        f'<div class="xp" style="color:{c}">{p.get("xp", 0)} xP</div></div>'
    )


def _card(inner: str, label: str = "") -> str:
    lab = f'<div class="section-label">{label}</div>' if label else ""
    return f'<div class="card">{lab}{inner}</div>'


def _chip_action_style(action: str) -> str:
    if action in ("Use", "Plan", "Consider"):
        return "#34d399"
    return "#8b95a6"


# ------------------------------------------------------------------
# Sidebar
# ------------------------------------------------------------------
with st.sidebar:
    st.markdown("## ⚽ FPL AI Manager")
    st.caption("Automated FPL data-science assistant")

    risk_label = st.radio(
        "Risk appetite",
        ["Conservative", "Balanced", "Aggressive"],
        index=1,
        key="risk",
        help="Tunses the optimisation objective.",
    )

    risk_desc = {
        "Conservative": "Protects rank — favours high ownership, nailed-on starters, avoids hits.",
        "Balanced": "Pure expected points (xP) — maximum mathematical value.",
        "Aggressive": "Chases deficits — low-ownership differentials, high ceilings, tolerates hits.",
    }
    st.caption(risk_desc[risk_label])

    st.markdown("---")
    st.markdown(
        '<div class="badge"><span class="g"></span> Engineered by Waqas Hussain</div>',
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------
# Main dashboard
# ------------------------------------------------------------------
st.markdown('<div class="hero"><h1>Pre-Deadline Dashboard</h1><div class="sub">One button. Definitive instructions. Run it 1–2 hours before the deadline.</div></div>', unsafe_allow_html=True)

c1, c2, c3 = st.columns([2, 1, 1])
with c1:
    manager_id = st.text_input("Manager ID", "7261134", key="mid",
                               help="Found in your FPL team page URL, e.g. fantasy.premierleague.com/entry/XXXX/event/1")
with c2:
    gw = st.number_input("Gameweek", 1, 38, 3, key="gw")
with c3:
    free_transfers = st.number_input("Free transfers", 0, 5, 1, key="ft")

st.markdown(f'<div class="deadline">⏳ Deadline: {fpl_tools.get_gameweek_deadline(int(gw))}</div>', unsafe_allow_html=True)

if st.button("🚀 Generate Action Plan", type="primary", use_container_width=True, key="btn_plan"):
    with st.spinner("Ingesting squad, modelling xP, solving transfers & lineup..."):
        plan = fpl_tools.build_gameweek_briefing(manager_id.strip(), int(gw), int(free_transfers), risk=risk_label.lower())
    st.session_state["plan"] = plan

plan = st.session_state.get("plan")

if plan and "error" in plan:
    st.error("⚠️ " + plan["error"])
    st.info("Check your Manager ID (the number in your FPL team URL) and that the gameweek is correct.")
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
    transfer_html = f'<h3>Transfers</h3><div style="color:#cbd5e1;margin:4px 0 8px 0;">{plan["hit_advice"]}</div>'
    if moves:
        for m in moves:
            transfer_html += (
                f'<div class="transfer">'
                f'<span class="out">OUT {m["out"]["name"]}</span><span class="arrow">→</span>'
                f'<span class="inn">IN {m["in"]["name"]}</span>'
                f'<span class="gain">+{m["xp_gain"]} xP · £{m["cost"]:+}m</span></div>'
            )
    else:
        transfer_html += '<div style="color:#8b95a6;">No transfers recommended this week — hold.</div>'
    st.markdown(_card(transfer_html, "🚀 Transfers"), unsafe_allow_html=True)

    # Starting XI + Bench
    xi = plan["starting_xi"]
    all_xp = max([p.get("xp", 0) for p in plan["squad"]] + [1.0])
    d, m, f = xi["formation"]

    xi_html = f'<h3>Starting XI · {d}-{m}-{f}</h3><div class="grid">'
    for p in xi["xi"]:
        xi_html += _player_card(p, all_xp)
    xi_html += "</div>"

    bench_html = '<div class="grid">'
    for p in xi["bench"]:
        bench_html += _player_card(p, all_xp)
    bench_html += "</div>"

    col_xi, col_bench = st.columns([2, 1])
    with col_xi:
        st.markdown(_card(xi_html, "🛡️ Starting XI"), unsafe_allow_html=True)
    with col_bench:
        st.markdown(_card(bench_html, "🪑 Bench"), unsafe_allow_html=True)

    # Captaincy
    cap = xi["captain"]
    vcap = xi["vice_captain"]
    cap_html = f'<div class="grid"><div class="pc cap-card">{_pos_chip(cap["position"])}<div class="nm">⭐ {cap["name"]}</div><div class="meta">{cap["team"]} — Captain</div><div class="xp" style="color:#f5b301">{cap["xp"]} xP (×2 = {cap["xp"]*2:.1f})</div></div>'
    if vcap:
        cap_html += f'<div class="pc">{_pos_chip(vcap["position"])}<div class="nm">{vcap["name"]}</div><div class="meta">{vcap["team"]} — Vice-Captain</div><div class="xp" style="color:#cbd5e1">{vcap["xp"]} xP</div></div>'
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
# How it works
# ------------------------------------------------------------------
with st.expander("🔍 How it works"):
    st.markdown(
        """
**Data ingestion.** Every run pulls three live sources from the official FPL API: `bootstrap-static`
(players, prices, team strengths), `fixtures` (upcoming matches + difficulty), and `entry/{id}/event/{gw}/picks`
(your 15-man squad, captain, bank).

**xP modelling.** Each player gets an expected-points (xP) score built from position-aware FPL scoring:
expected goals & assists (per-90, *regularised* toward the league average so 1-minute wonders can't skew it),
clean-sheet probability via a Poisson model on expected goals conceded, saves for goalkeepers, expected
minutes (from chance-of-playing / starts), bonus & card priors — then adjusted for the opponent's real
attack/defence strength and home/away, and blended with FPL's own `ep_next` prediction.

**Optimisation.** Squad selection is a mixed-integer program (`pulp`) maximising total xP under hard
constraints: the £100m budget, 2 GK / 5 DEF / 5 MID / 3 FWD, and ≤3 players per team. Transfers re-solve
the same program with a −4-point cost per transfer beyond your free transfers, so a hit is only taken when
it's mathematically worth it. The starting XI is the exact best formation out of all 8 valid shapes; the
captain is the highest-xP player.

**Risk appetite.** The sidebar toggle shifts the objective: *Conservative* rewards high ownership and nailed-on
minutes and raises the hit cost; *Aggressive* rewards low-ownership differentials and attacking ceiling and
lowers the hit cost; *Balanced* is pure expected value.

**Chips.** Double- and blank-gameweek detection over the next 8 weeks drives per-chip verdicts (Triple Captain,
Bench Boost, Free Hit, Wildcard).
        """
    )


# ------------------------------------------------------------------
# Manual squad override (midweek moves) — OCR + dropdowns
# ------------------------------------------------------------------
with st.expander("⚙️ Manual squad override (midweek moves)"):
    st.info("The FPL API hides midweek transfers until the deadline. Upload a screenshot or correct the dropdowns to override.")

    uploaded_image = st.file_uploader("Upload squad screenshot (optional)", type=["png", "jpg", "jpeg"], key="override_image")

    bootstrap = fpl_tools._get_bootstrap()
    players_by_id = {p["id"]: p for p in bootstrap["elements"]}
    teams = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    dropdown_options = {pos: [] for pos in POS_ORDER}
    for p in bootstrap["elements"]:
        pos = pos_map.get(p["element_type"])
        if pos:
            display = f"{p['first_name']} {p['second_name']} ({teams.get(p['team'], '?')}) £{p['now_cost']/10:.1f}m"
            dropdown_options[pos].append((p["id"], display))

    # Seed defaults from the last API squad if available
    default_selections = {pos: [] for pos in POS_ORDER}
    last_squad = st.session_state.get("plan", {}).get("squad")
    if last_squad:
        for p in last_squad:
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
            with st.spinner("Reading squad image with Gemini Vision..."):
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
            st.success(f"Image parsed: {len(image_matched)} players matched.")
        image_unmatched = st.session_state.get("cached_image_unmatched", [])
        if image_unmatched:
            st.warning(f"⚠️ {len(image_unmatched)} unmatched: {', '.join(image_unmatched)}")

    ob1, ob2 = st.columns(2)
    with ob1:
        bank_override = st.number_input("Bank balance (£m)", 0.0, 50.0, float(plan.get("bank", 0.0)) if plan else 0.0, 0.1, key="ov_bank")
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
                    index=min(idx_default, len(dropdown_options[pos]) - 1),
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
            with st.spinner("Scoring squad and solving transfers, lineup & chips..."):
                fixture_lookup = fpl_tools._build_fixture_lookup()
                analysed = []
                for p in override_squad:
                    fpl_p = players_by_id.get(p["player_id"])
                    if fpl_p:
                        xp, note = fpl_tools._player_xp(fpl_p, fixture_lookup, event=int(gw), risk=risk_label.lower())
                        analysed.append({
                            "player_id": p["player_id"], "name": p["name"], "team": p["team"],
                            "team_id": fpl_p["team"], "position": p["position"], "price": p["price"],
                            "xp": xp, "status": note, "is_captain": False,
                        })
                transfers = fpl_tools.suggest_transfers_for_custom_squad(
                    analysed, float(bank_override), int(ft_override), event=int(gw), risk=risk_label.lower())
                lineup = fpl_tools.select_starting_xi(analysed)
                chips = fpl_tools.recommend_chips(bootstrap, analysed, fpl_tools._build_gameweek_map(), int(gw))
                st.session_state["override_analysis"] = {
                    "team_name": "Manual Override Squad", "bank": float(bank_override),
                    "team_value": round(sum(p["price"] for p in analysed), 1), "squad": analysed,
                    "transfers": transfers, "starting_xi": lineup, "chips": chips,
                }

    ov = st.session_state.get("override_analysis")
    if ov:
        st.markdown("---")
        st.success(f"**{ov['team_name']}** — Bank £{ov['bank']}m · {ov['starting_xi']['total_xp']} xP projected")

        tr = ov["transfers"]
        moves = tr.get("transfers", [])
        transfer_html = f'<h3>Transfers</h3><div style="color:#cbd5e1;margin:4px 0 8px 0;">{tr.get("hit_advice", "")}</div>'
        if moves:
            for m in moves:
                transfer_html += (
                    f'<div class="transfer"><span class="out">OUT {m["out"]["name"]}</span>'
                    f'<span class="arrow">→</span><span class="inn">IN {m["in"]["name"]}</span>'
                    f'<span class="gain">+{m["xp_gain"]} xP · £{m["cost"]:+}m</span></div>'
                )
        else:
            transfer_html += '<div style="color:#8b95a6;">No transfers recommended.</div>'
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
        cap_html = f'<div class="grid"><div class="pc cap-card">{_pos_chip(cap["position"])}<div class="nm">⭐ {cap["name"]}</div><div class="meta">{cap["team"]} — Captain</div><div class="xp" style="color:#f5b301">{cap["xp"]} xP (×2 = {cap["xp"]*2:.1f})</div></div>'
        if vcap:
            cap_html += f'<div class="pc">{_pos_chip(vcap["position"])}<div class="nm">{vcap["name"]}</div><div class="meta">{vcap["team"]} — Vice-Captain</div><div class="xp" style="color:#cbd5e1">{vcap["xp"]} xP</div></div>'
        cap_html += "</div>"
        st.markdown(_card(cap_html, "⭐ Captaincy"), unsafe_allow_html=True)

        chip_html = "".join(
            f'<div class="chip-row"><div><b>{ch["chip"]}</b> <span class="act" style="color:{_chip_action_style(ch["action"])};border:1px solid {_chip_action_style(ch["action"])}">{ch["action"]}</span></div><div class="det">{ch["detail"]}</div></div>'
            for ch in ov["chips"]
        )
        st.markdown(_card(chip_html, "🎲 Chips"), unsafe_allow_html=True)


# ------------------------------------------------------------------
# Wildcard builder
# ------------------------------------------------------------------
with st.expander("🛠️ Build optimal squad (Wildcard mode)"):
    w1, w2 = st.columns([2, 1])
    with w1:
        budget = st.slider("Budget (£m)", 80.0, 100.0, 100.0, 0.5, key="wc_budget")
    with w2:
        if st.button("Build", type="secondary", key="btn_wc"):
            with st.spinner("Solving optimal squad..."):
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
        with st.spinner("Ranking..."):
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
