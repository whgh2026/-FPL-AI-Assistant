import streamlit as st
import fpl_tools
import gemini_summary

# New helper module you should add
import squad_override

st.set_page_config(page_title="FPL AI Assistant", page_icon="⚽", layout="wide")
st.title("⚽ FPL AI Assistant")
st.caption("Your personal fantasy football data scientist")

tab1, tab2, tab3, tab4 = st.tabs(
    ["Weekly Transfers", "My Team", "Optimal Squad", "Top Players"]
)

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
            res = fpl_tools.suggest_weekly_transfers(
                mid_transfer.strip(),
                int(gw_transfer),
                int(ft)
            )
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
                st.markdown(
                    f"- OUT **{m['out']['name']}** -> IN **{m['in']['name']}** (+{m['xp_gain']} xP)"
                )
            st.markdown(f"**Total xP gain:** +{bd['xp_gain']}")

        st.markdown("---")

        if st.button("Generate Plain-English Summary", key="btn_summary"):
            with st.spinner("Writing your weekly briefing..."):
                summary = gemini_summary.write_summary(res)
            st.session_state["transfer_summary"] = summary

        if st.session_state.get("transfer_summary"):
            st.markdown("### Your Weekly Briefing")
            st.write(st.session_state["transfer_summary"])

with tab2:
    st.subheader("Analyse Your Current Squad")

    api_tab, manual_tab = st.tabs(["From FPL API", "Manual Override"])

    with api_tab:
        c1, c2 = st.columns(2)
        with c1:
            mid_squad = st.text_input("Manager ID", "7261134", key="mid_squad")
        with c2:
            gw_squad = st.number_input("Gameweek", 1, 38, 3, key="gw_squad")

        if st.button("Analyse My Team", type="primary", key="btn_analyse"):
            with st.spinner("Loading your squad..."):
                res2 = fpl_tools.score_my_squad(mid_squad.strip(), int(gw_squad))
            if "error" in res2:
                st.error(res2["error"])
            else:
                st.session_state["current_squad_analysis"] = res2
                st.session_state["current_squad_summary"] = gemini_summary.write_summary({
                    "team_name": res2["team_name"],
                    "bank": res2["bank"],
                    "best_single": None,
                    "best_double": None,
                    "hit_advice": "API squad analysis loaded.",
                })

        res2 = st.session_state.get("current_squad_analysis")
        if res2 and "error" not in res2:
            st.success(f"{res2['team_name']} (GW{res2['gameweek_used']})")
            a, b = st.columns(2)
            a.metric("Team Value", f"£{res2['team_value']}m")
            b.metric("In the Bank", f"£{res2['bank']}m")

            cap = res2.get("captain")
            if cap:
                st.markdown(
                    f"### Recommended Captain: **{cap['name']}** ({cap['team']}) - xP {cap['xp']}"
                )

            st.markdown("### Your Squad")
            st.table([
                {
                    "Player": p["name"],
                    "Pos": p["position"],
                    "Team": p["team"],
                    "Price": f"£{p['price']}m",
                    "xP": p["xp"],
                    "Status": p["status"],
                    "C": "C" if p["is_captain"] else ""
                }
                for p in res2["squad"]
            ])

            st.markdown("### Weakest Links")
            st.table([
                {
                    "Player": p["name"],
                    "Pos": p["position"],
                    "Team": p["team"],
                    "xP": p["xp"]
                }
                for p in res2["weak_links"]
            ])

            if st.session_state.get("current_squad_summary"):
                st.markdown("### Plain-English Summary")
                st.write(st.session_state["current_squad_summary"])

    with manual_tab:
        st.info(
            "Upload a screenshot of your current squad. If the image is unclear, "
            "you can manually correct players using dropdowns."
        )

        uploaded_image = st.file_uploader(
            "Upload squad screenshot",
            type=["png", "jpg", "jpeg"],
            key="squad_image_upload"
        )

        c1, c2 = st.columns(2)
        with c1:
            manual_gameweek = st.number_input("Gameweek", 1, 38, 3, key="manual_gw")
        with c2:
            manual_bank = st.number_input("Bank Balance (£m)", 0.0, 50.0, 0.0, 0.1, key="manual_bank")

        bootstrap = None
        extracted_players = []
        matched_players = []

        if uploaded_image:
            st.image(uploaded_image, caption="Uploaded squad screenshot", use_container_width=True)

            with st.spinner("Reading squad image..."):
                bootstrap = fpl_tools._get_bootstrap()
                extraction = squad_override.extract_squad_from_image(uploaded_image)

            if extraction.get("success"):
                extracted_players = extraction.get("raw_players", [])
                matched_players = squad_override.match_players_to_fpl(bootstrap, extracted_players)

                st.success(f"Detected {len(matched_players)} players from the screenshot.")

                if extracted_players:
                    with st.expander("Raw OCR / AI extraction"):
                        st.write(extracted_players)

                if matched_players:
                    with st.expander("Matched players"):
                        st.table([
                            {
                                "Player": p["name"],
                                "Team": p["team"],
                                "Pos": p["position"],
                                "Price": f"£{p['price']}m"
                            }
                            for p in matched_players
                        ])
            else:
                st.warning(
                    extraction.get("error", "Could not read the image properly. "
                    "Use the dropdowns below to build the squad manually.")
                )

        if bootstrap is None:
            bootstrap = fpl_tools._get_bootstrap()

        st.markdown("### Build or Correct Squad")
        override_squad = squad_override.render_override_ui(
            matched_players=matched_players,
            bootstrap_data=bootstrap,
            bank=manual_bank
        )

        if st.button("Apply Override & Analyse", type="primary", key="btn_apply_override"):
            if not override_squad:
                st.error("No squad selected yet.")
            else:
                with st.spinner("Analysing overridden squad..."):
                    fixture_lookup = fpl_tools._build_fixture_lookup()
                    players_by_id = {p["id"]: p for p in bootstrap["elements"]}

                    analysed_squad = []
                    for p in override_squad:
                        fpl_player = players_by_id.get(p["player_id"])
                        if fpl_player:
                            xp, note = fpl_tools._player_xp(fpl_player, fixture_lookup)
                            analysed_squad.append({
                                "player_id": p["player_id"],
                                "name": p["name"],
                                "team": p["team"],
                                "position": p["position"],
                                "price": p["price"],
                                "xp": xp,
                                "status": note,
                                "is_captain": False,
                            })

                    weak_links = sorted(analysed_squad, key=lambda x: x["xp"])[:4]
                    best_xi = [p for p in analysed_squad if p["xp"] > 0]
                    captain = max(best_xi, key=lambda x: x["xp"]) if best_xi else None

                    analysis_result = {
                        "gameweek_used": int(manual_gameweek),
                        "team_name": "Manual Override Squad",
                        "bank": float(manual_bank),
                        "team_value": round(sum(p["price"] for p in analysed_squad), 1),
                        "squad": analysed_squad,
                        "weak_links": weak_links,
                        "captain": captain,
                    }

                    st.session_state["override_analysis"] = analysis_result
                    st.session_state["override_summary"] = gemini_summary.write_summary({
                        "team_name": analysis_result["team_name"],
                        "bank": analysis_result["bank"],
                        "best_single": None,
                        "best_double": None,
                        "hit_advice": "Manual squad override applied.",
                    })

                st.success("Squad overridden and analysed.")

        analysis = st.session_state.get("override_analysis")
        if analysis:
            st.markdown("---")
            st.success(f"{analysis['team_name']} (GW{analysis['gameweek_used']})")

            a, b = st.columns(2)
            a.metric("Team Value", f"£{analysis['team_value']}m")
            b.metric("In the Bank", f"£{analysis['bank']}m")

            cap = analysis.get("captain")
            if cap:
                st.markdown(
                    f"### Recommended Captain: **{cap['name']}** ({cap['team']}) - xP {cap['xp']}"
                )

            st.markdown("### Your Squad")
            st.table([
                {
                    "Player": p["name"],
                    "Pos": p["position"],
                    "Team": p["team"],
                    "Price": f"£{p['price']}m",
                    "xP": p["xp"],
                    "Status": p["status"],
                }
                for p in analysis["squad"]
            ])

            st.markdown("### Weakest Links")
            st.table([
                {
                    "Player": p["name"],
                    "Pos": p["position"],
                    "Team": p["team"],
                    "xP": p["xp"]
                }
                for p in analysis["weak_links"]
            ])

            if st.session_state.get("override_summary"):
                st.markdown("### Plain-English Summary")
                st.write(st.session_state["override_summary"])

with tab3:
    st.subheader("Build the Optimal Squad From Scratch (Wildcard Mode)")
    budget = st.slider("Budget (£m)", 80.0, 100.0, 100.0, 0.5, key="budget_slider")

    if st.button("Build Optimal Squad", type="primary", key="btn_optimal"):
        with st.spinner("Crunching the numbers..."):
            res3 = fpl_tools.optimise_full_squad(budget=budget)
        if "error" in res3:
            st.error(res3["error"])
        else:
            st.success(
                f"Squad built: £{res3['total_price']}m / £{res3['budget']}m  -  Total xP: {res3['total_xp']}"
            )
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

with tab4:
    st.subheader("Top Players by Expected Points")
    c1, c2 = st.columns(2)
    with c1:
        pos_filter = st.selectbox(
            "Position",
            ["All", "GK", "DEF", "MID", "FWD"],
            key="pos_select"
        )
    with c2:
        max_price = st.number_input(
            "Max price (£m, 0 = no limit)",
            0.0,
            15.5,
            0.0,
            0.5,
            key="max_price_input"
        )

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