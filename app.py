import streamlit as st
import fpl_tools
import gemini_summary
import squad_override
import re

st.set_page_config(page_title="FPL Data Science Assistant", page_icon="⚽", layout="wide")
st.title("⚽ FPL Data Science Assistant")
st.caption("Your personal fantasy football data scientist")

tab1, tab2, tab3, tab4 = st.tabs(
    ["Weekly Transfers", "My Team", "Optimal Squad", "Top Players"]
)

# ------------------------------------------------------------------
# TAB 1: Weekly Transfers
# ------------------------------------------------------------------
with tab1:
    st.subheader("Best Transfers For This Week")
    c1, c2, c3 = st.columns(3)
    with c1:
        mid_transfer = st.text_input("Manager ID", "7261134", key="mid_transfer")
    with c2:
        gw_transfer = st.number_input("Gameweek", 1, 38, 3, key="gw_transfer")
    with c3:
        ft = st.number_input("Free transfers", 0, 5, 1, key="ft_count")

    if st.button("Find Best Transfers", type="primary", key="btn_transfers"):
        with st.spinner("Analysing your squad and the market..."):
            res = fpl_tools.suggest_weekly_transfers(mid_transfer.strip(), int(gw_transfer), int(ft))
        if "error" in res:
            st.error(res["error"])
        else:
            st.session_state["transfer_res"] = res

    res = st.session_state.get("transfer_res")
    if res and "error" not in res:
        st.success(f"{res['team_name']}  -  Bank £{res['bank']}m")
        st.info(f"Advice: {res['hit_advice']}")

        bs = res["best_single"]
        if bs:
            st.markdown("### Best Single Transfer")
            st.markdown(
                f"**OUT:** {bs['out']['name']} ({bs['out']['team']}) - xP {bs['out']['xp']}  \n"
                f"**IN:** {bs['in']['name']} ({bs['in']['team']}) - xP {bs['in']['xp']}  \n"
                f"**xP gain:** +{bs['xp_gain']}  -  **Cost change:** £{bs['cost_change']}m"
            )
        else:
            st.write("No beneficial single transfer found.")

        bd = res["best_double"]
        if bd:
            st.markdown("### Best Double Transfer")
            for m in bd["moves"]:
                st.markdown(f"- OUT **{m['out']['name']}** -> IN **{m['in']['name']}** (+{m['xp_gain']} xP)")
            st.markdown(f"**Total xP gain:** +{bd['xp_gain']}")

        if "transfer_summary" not in st.session_state or not st.session_state.get("transfer_summary"):
            with st.spinner("Generating your weekly data science advice..."):
                st.session_state["transfer_summary"] = gemini_summary.write_summary(res)

        st.markdown("---")
        st.markdown("### Your Weekly Data Science Advice")
        st.write(st.session_state.get("transfer_summary", ""))

# ------------------------------------------------------------------
# TAB 2: My Team — API + Manual Override
# ------------------------------------------------------------------
with tab2:
    st.subheader("Analyse Your Current Squad")

    st.markdown("### Step 1 — Fetch Your Squad")
    c1, c2 = st.columns(2)
    with c1:
        mid_squad = st.text_input("Manager ID", "7261134", key="mid_squad")
    with c2:
        gw_squad = st.number_input("Gameweek", 1, 38, 3, key="gw_squad")

    if st.button("Fetch Squad", type="primary", key="btn_fetch"):
        with st.spinner("Loading from FPL API..."):
            res_api = fpl_tools.score_my_squad(mid_squad.strip(), int(gw_squad))
        if "error" in res_api:
            st.error(res_api["error"])
            st.session_state["api_result"] = None
        else:
            st.session_state["api_result"] = res_api
            with st.spinner("Generating data science advice..."):
                st.session_state["api_summary"] = gemini_summary.write_summary({
                    "team_name": res_api["team_name"],
                    "bank": res_api["bank"],
                    "best_single": None,
                    "best_double": None,
                    "hit_advice": "API squad loaded.",
                    "gameweek_used": res_api["gameweek_used"],
                    "team_value": res_api["team_value"],
                    "squad": res_api["squad"],
                    "weak_links": res_api["weak_links"],
                    "captain": res_api.get("captain"),
                })

    api_res = st.session_state.get("api_result")
    if api_res and "error" not in api_res:
        st.success(f"API Squad — {api_res['team_name']} (GW{api_res['gameweek_used']})")
        col_info1, col_info2 = st.columns(2)
        col_info1.metric("Team Value", f"£{api_res['team_value']}m")
        col_info2.metric("Bank", f"£{api_res['bank']}m")

        st.markdown("**API Squad — Player Cards**")
        squad_cards = api_res["squad"]
        
        for pos in ["GK", "DEF", "MID", "FWD"]:
            pos_players = [p for p in squad_cards if p["position"] == pos]
            
            if pos_players:
                st.markdown(f"**{pos}**")
                cols = st.columns(len(pos_players))
                
                for i, p in enumerate(pos_players):
                    with cols[i]:
                        pos_color = {
                            "GK": "#FFE082",
                            "DEF": "#90CAF9",
                            "MID": "#A5D6A7",
                            "FWD": "#EF9A9A"
                        }.get(p["position"], "#EEEEEE")
                        
                        cap_mark = " ⭐" if p.get("is_captain") else ""
                        
                        st.markdown(
                            f"<div style='padding:8px;border-radius:10px;background:{pos_color};font-size:0.85rem;margin-bottom:15px;'>"
                            f"<b>{p['name']}</b>{cap_mark}<br>"
                            f"<span style='font-size:0.75rem;color:#555;'>{p['team']} • £{p['price']}m</span><br>"
                            f"<span style='font-size:0.8rem;font-weight:bold;'>xP {p['xp']}</span> • "
                            f"<span style='font-size:0.75rem;'>{p['status']}</span>"
                            f"</div>",
                            unsafe_allow_html=True
                        )

        if st.session_state.get("api_summary"):
            with st.expander("Data Science Advice (API Squad)"):
                st.markdown(st.session_state["api_summary"])

    st.markdown("---")
    st.markdown("### Step 2 — Midweek Override")
    st.info(
        "⚠️ **The FPL API hides midweek transfers before the deadline.** "
        "Override with your exact roster & bank if you made changes after the last deadline."
    )

    uploaded_image = st.file_uploader(
        "Upload screenshot of your current squad (optional)",
        type=["png", "jpg", "jpeg"],
        key="override_image"
    )

    bootstrap = fpl_tools._get_bootstrap()
    players_by_id = {p["id"]: p for p in bootstrap["elements"]}
    teams = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    dropdown_options = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in bootstrap["elements"]:
        pos = pos_map.get(p["element_type"])
        if pos:
            display = f"{p['first_name']} {p['second_name']} ({teams.get(p['team'], '?')}) £{p['now_cost']/10:.1f}m"
            dropdown_options[pos].append((p["id"], display))

    default_selections = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    if api_res and "squad" in api_res:
        for p in api_res["squad"]:
            pos = p["position"]
            found = False
            for idx, (pid, _) in enumerate(dropdown_options[pos]):
                if pid == p.get("player_id"):
                    default_selections[pos].append(idx)
                    found = True
                    break
            if not found:
                default_selections[pos].append(0)

    image_matched = []
    if uploaded_image:
        file_id = f"{uploaded_image.name}_{uploaded_image.size}"
        
        if st.session_state.get("last_uploaded_file") != file_id:
            with st.spinner("Reading squad image with Gemini Vision..."):
                extraction = squad_override.extract_squad_from_image(uploaded_image)
            
            if extraction.get("success"):
                st.session_state["cached_image_matched"] = squad_override.match_players_to_fpl(
                    bootstrap,
                    extraction.get("raw_players", [])
                )
            else:
                st.session_state["cached_image_matched"] = []
                st.warning(extraction.get("error", "Could not read the image properly."))
            
            st.session_state["last_uploaded_file"] = file_id
            
        image_matched = st.session_state.get("cached_image_matched", [])
        
        if image_matched:
            st.success(f"Image parsed: {len(image_matched)} players matched.")
            for p in image_matched:
                pos = p["position"]
                for idx, (pid, _) in enumerate(dropdown_options[pos]):
                    if pid == p["player_id"]:
                        if idx not in default_selections[pos]:
                            default_selections[pos].append(idx)
                        break

    col_bank, col_ft = st.columns(2)
    with col_bank:
        bank_override = st.number_input(
            "Bank Balance (£m)",
            0.0,
            50.0,
            float(api_res["bank"]) if api_res else 0.0,
            0.1,
            key="override_bank"
        )
    with col_ft:
        ft_override = st.number_input(
            "Free Transfers",
            0,
            5,
            1,
            key="override_ft"
        )

    image_matched_by_pos = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    if image_matched:
        for p in image_matched:
            if p["position"] in image_matched_by_pos:
                image_matched_by_pos[p["position"]].append(p)

    st.markdown("### Your Squad (correct any player using dropdowns)")
    override_squad = []

    quotas = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
    for pos, count in quotas.items():
        st.markdown(f"**{pos}**")
        cols = st.columns(min(count, 5))
        for i in range(count):
            with cols[i % len(cols)]:
                img_state = uploaded_image.name if uploaded_image else "none"
                key_name = f"sel_{pos}_{i}_{img_state}_{mid_squad}_{gw_squad}"
                idx_default = 0
                
                if i < len(default_selections[pos]):
                    idx_default = default_selections[pos][i]

                if image_matched_by_pos[pos] and i < len(image_matched_by_pos[pos]):
                    matched_p = image_matched_by_pos[pos][i]
                    for idx, (pid, _) in enumerate(dropdown_options[pos]):
                        if pid == matched_p["player_id"]:
                            idx_default = idx
                            break

                selected = st.selectbox(
                    f"{pos} {i+1}",
                    options=dropdown_options[pos],
                    format_func=lambda x: x[1],
                    index=min(idx_default, len(dropdown_options[pos]) - 1),
                    key=key_name,
                    label_visibility="collapsed"
                )

                if selected:
                    pid, display = selected
                    name = display.split(" (")[0]
                    team_match = re.search(r"\(([A-Z]{3})\)", display)
                    price_match = re.search(r"£([\d.]+)m", display)

                    override_squad.append({
                        "player_id": pid,
                        "name": name,
                        "team": team_match.group(1) if team_match else "?",
                        "position": pos,
                        "price": float(price_match.group(1)) if price_match else 0.0,
                    })

    if st.button("Apply Override & Analyse", type="primary", key="btn_apply_override"):
        if len(override_squad) != 15:
            st.error("Please select exactly 15 players (2 GK, 5 DEF, 5 MID, 3 FWD).")
        else:
            with st.spinner("Analysing overridden squad..."):
                fixture_lookup = fpl_tools._build_fixture_lookup()
                analysed = []

                for p in override_squad:
                    fpl_p = players_by_id.get(p["player_id"])
                    if fpl_p:
                        xp, note = fpl_tools._player_xp(fpl_p, fixture_lookup)
                        analysed.append({
                            "player_id": p["player_id"],
                            "name": p["name"],
                            "team": p["team"],
                            "position": p["position"],
                            "price": p["price"],
                            "xp": xp,
                            "status": note,
                            "is_captain": False,
                        })

                weak_links = sorted(analysed, key=lambda x: x["xp"])[:4]
                best_xi = [p for p in analysed if p["xp"] > 0]
                captain = max(best_xi, key=lambda x: x["xp"]) if best_xi else None

                analysis_result = {
                    "gameweek_used": int(gw_squad),
                    "team_name": "Manual Override Squad",
                    "bank": float(bank_override),
                    "team_value": round(sum(p["price"] for p in analysed), 1),
                    "squad": analysed,
                    "weak_links": weak_links,
                    "captain": captain,
                }

                with st.spinner("Calculating optimal transfers..."):
                    transfer_result = fpl_tools.suggest_transfers_for_custom_squad(
                        analysed, 
                        float(bank_override), 
                        int(ft_override)
                    )
                    analysis_result["transfers"] = transfer_result

                with st.spinner("Generating Data Science tactical & transfer advice..."):
                    st.session_state["override_summary"] = gemini_summary.write_summary(analysis_result)
                    
                    if "error" not in transfer_result:
                        st.session_state["override_transfer_summary"] = gemini_summary.write_summary(transfer_result)
                    else:
                        st.session_state["override_transfer_summary"] = None

                st.session_state["override_analysis"] = analysis_result
                st.success("Override applied and fully analysed.")

    override_res = st.session_state.get("override_analysis")
    if override_res:
        st.markdown("---")
        st.success(f"{override_res['team_name']} (GW{override_res['gameweek_used']})")

        a, b = st.columns(2)
        a.metric("Team Value", f"£{override_res['team_value']}m")
        b.metric("Bank", f"£{override_res['bank']}m")

        if st.session_state.get("override_summary"):
            st.markdown("### Data Science Tactical Advice")
            st.info(st.session_state["override_summary"])

        if st.session_state.get("override_transfer_summary"):
            st.markdown("### Data Science Transfer Advice")
            st.success(st.session_state["override_transfer_summary"])

# ------------------------------------------------------------------
# TAB 3: Optimal Squad
# ------------------------------------------------------------------
with tab3:
    st.subheader("Build the Optimal Squad From Scratch (Wildcard Mode)")
    budget = st.slider("Budget (£m)", 80.0, 100.0, 100.0, 0.5, key="budget_slider")
    
    if st.button("Build Optimal Squad", type="primary", key="btn_optimal"):
        with st.spinner("Crunching the numbers..."):
            res3 = fpl_tools.optimise_full_squad(budget=budget)
        if "error" in res3:
            st.error(res3["error"])
        else:
            st.success(f"Squad built: £{res3['total_price']}m / £{res3['budget']}m  -  Total xP: {res3['total_xp']}")
            for pos in ["GK", "DEF", "MID", "FWD"]:
                players = [p for p in res3["squad"] if p["position"] == pos]
                st.markdown(f"**{pos}**")
                st.table([
                    {
                        "Player": p["name"],
                        "Team": p["team"],
                        "Price": f"£{p['price']}m",
                        "xP": p["xp"]
                    }
                    for p in players
                ])

# ------------------------------------------------------------------
# TAB 4: Top Players
# ------------------------------------------------------------------
with tab4:
    st.subheader("Top Players by Expected Points")
    c1, c2 = st.columns(2)
    with c1:
        pos_filter = st.selectbox("Position", ["All", "GK", "DEF", "MID", "FWD"], key="pos_select")
    with c2:
        max_price = st.number_input("Max price (£m, 0 = no limit)", 0.0, 15.5, 0.0, 0.5, key="max_price_input")

    if st.button("Show Rankings", type="primary", key="btn_rankings"):
        with st.spinner("Ranking players..."):
            res4 = fpl_tools.rank_players_by_xp(
                position=None if pos_filter == "All" else pos_filter,
                max_price=None if max_price == 0 else max_price,
                limit=20,
            )
        if "error" in res4:
            st.error(res4["error"])
        else:
            st.table([
                {
                    "Player": p["name"],
                    "Pos": p["position"],
                    "Team": p["team"],
                    "Price": f"£{p['price']}m",
                    "xP": p["xp"],
                    "Status": p["status"]
                }
                for p in res4["players"]
            ])
