import streamlit as st
import fpl_tools
import gemini_summary

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

        st.markdown("---")
        if st.button("Generate Plain-English Summary", key="btn_summary"):
            with st.spinner("Writing your weekly briefing..."):
                summary = gemini_summary.write_summary(res)
            st.markdown("### Your Weekly Briefing")
            st.write(summary)

with tab2:
    st.subheader("Analyse Your Current Squad")
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
            st.success(f"{res2['team_name']} (GW{res2['gameweek_used']})")
            a, b = st.columns(2)
            a.metric("Team Value", f"£{res2['team_value']}m")
            b.metric("In the Bank", f"£{res2['bank']}m")
            cap = res2.get("captain")
            if cap:
                st.markdown(f"### Recommended Captain: **{cap['name']}** ({cap['team']}) - xP {cap['xp']}")
            st.markdown("### Your Squad")
            st.table([
                {"Player": p["name"], "Pos": p["position"], "Team": p["team"],
                 "Price": f"£{p['price']}m", "xP": p["xp"], "Status": p["status"],
                 "C": "C" if p["is_captain"] else ""}
                for p in res2["squad"]
            ])
            st.markdown("### Weakest Links")
            st.table([
                {"Player": p["name"], "Pos": p["position"], "Team": p["team"], "xP": p["xp"]}
                for p in res2["weak_links"]
            ])
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
                    {"Player": p["name"], "Team": p["team"],
                     "Price": f"£{p['price']}m", "xP": p["xp"]}
                    for p in players
                ])
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
                {"Player": p["name"], "Pos": p["position"], "Team": p["team"],
                 "Price": f"£{p['price']}m", "xP": p["xp"], "Status": p["status"]}
                for p in res4["players"]
            ])