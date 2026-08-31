from typing import Dict, Any

def _fmt_money(value):
    try:
        return f"£{float(value):.1f}m"
    except Exception:
        return "£0.0m"

def _get_optimal_xi(squad):
    # FPL Positional Max Limits
    limits = {"GK": 1, "DEF": 5, "MID": 5, "FWD": 3}
    grouped = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    
    for p in squad:
        grouped[p["position"]].append(p)
    for pos in grouped:
        grouped[pos].sort(key=lambda x: x["xp"], reverse=True)

    xi = []
    bench = []

    # 1. Fill minimum positional requirements (1 GK, 3 DEF, 1 FWD)
    if grouped["GK"]:
        xi.extend(grouped["GK"][:1])
        bench.extend(grouped["GK"][1:])
    
    if len(grouped["DEF"]) >= 3:
        xi.extend(grouped["DEF"][:3])
        pool = grouped["DEF"][3:]
    else:
        xi.extend(grouped["DEF"])
        pool = []

    if len(grouped["FWD"]) >= 1:
        xi.extend(grouped["FWD"][:1])
        pool.extend(grouped["FWD"][1:])
    else:
        xi.extend(grouped["FWD"])
    
    pool.extend(grouped["MID"])
    
    # Sort all remaining outfield players by xP descending
    pool.sort(key=lambda x: x["xp"], reverse=True)

    # Track current formation counts
    counts = {
        "GK": len([p for p in xi if p["position"] == "GK"]),
        "DEF": len([p for p in xi if p["position"] == "DEF"]),
        "MID": len([p for p in xi if p["position"] == "MID"]),
        "FWD": len([p for p in xi if p["position"] == "FWD"])
    }

    # 2. Fill the remaining spots up to 11 players, respecting max FPL limits
    for p in pool:
        if len(xi) < 11 and counts[p["position"]] < limits[p["position"]]:
            xi.append(p)
            counts[p["position"]] += 1
        else:
            bench.append(p)

    # 3. Sort Bench: GK first, then outfield strictly by descending xP
    bench_gk = [p for p in bench if p["position"] == "GK"]
    bench_out = [p for p in bench if p["position"] != "GK"]
    bench_out.sort(key=lambda x: x["xp"], reverse=True)
    
    return xi, bench_gk + bench_out, counts

def write_summary(data: Dict[str, Any]) -> str:
    """
    Generates a deterministic, pure Python FPL briefing without AI latency.
    """
    try:
        sections = []

        # --- SQUAD (LINEUP, CAPTAIN, BENCH) SECTION ---
        squad = data.get("squad")
        if squad and len(squad) >= 11:
            xi, bench, counts = _get_optimal_xi(squad)
            
            # Sort XI for clean UI display order (GK -> DEF -> MID -> FWD)
            pos_order = {"GK": 1, "DEF": 2, "MID": 3, "FWD": 4}
            xi.sort(key=lambda x: (pos_order.get(x["position"], 5), -x["xp"]))

            formation = f"{counts['DEF']}-{counts['MID']}-{counts['FWD']}"
            
            sections.append(f"### 1. Starting XI & Formation ({formation})")
            sections.append("---")
            
            for pos in ["GK", "DEF", "MID", "FWD"]:
                pos_players = [p for p in xi if p["position"] == pos]
                if pos_players:
                    line = f"**{pos}:** " + " | ".join([f"{p['name']} ({p['team']}) — {p['xp']} xP" for p in pos_players])
                    sections.append(line)

            total_xi_xp = sum(p["xp"] for p in xi)
            sections.append(f"\n**Total Projected Starting XI:** {total_xi_xp:.2f} xP\n")

            # Captaincy Allocation
            sections.append("### 2. Captaincy")
            sections.append("---")
            xi_sorted = sorted(xi, key=lambda x: x["xp"], reverse=True)
            cap = xi_sorted[0] if len(xi_sorted) > 0 else None
            vcap = xi_sorted[1] if len(xi_sorted) > 1 else None

            if cap:
                sections.append(f"• **Captain (C):** {cap['name']} ({cap['team']}) — {cap['xp']} xP (x2 = {cap['xp']*2:.2f} xP)")
            if vcap:
                sections.append(f"• **Vice-Captain (VC):** {vcap['name']} ({vcap['team']}) — {vcap['xp']} xP\n")

            # Auto-Sub Bench Order
            sections.append("### 3. Bench Priority")
            sections.append("---")
            for i, p in enumerate(bench):
                if i == 0 and p["position"] == "GK":
                    sections.append(f"• **GK Sub:** {p['name']} ({p['team']}) — {p['xp']} xP")
                else:
                    sub_num = i if bench[0]['position'] == 'GK' else i+1
                    sections.append(f"• **Sub {sub_num}:** {p['name']} ({p['team']}) — {p['xp']} xP")
            
            sections.append("\n")

        # --- TRANSFERS SECTION ---
        if data.get("best_single") or data.get("best_double"):
            sections.append("### 4. Transfer Recommendation")
            sections.append("---")
            
            bs = data.get("best_single")
            bd = data.get("best_double")
            advice = data.get("hit_advice", "")
            
            if bs:
                sections.append(
                    f"• **OUT:** {bs['out']['name']} ({bs['out']['team']}, {_fmt_money(bs['out'].get('price',0))}) -> "
                    f"**IN:** {bs['in']['name']} ({bs['in']['team']}, {_fmt_money(bs['in'].get('price',0))})"
                )
                sections.append(f"• **Net Gain:** +{bs['xp_gain']} xP | **Cost Change:** {_fmt_money(bs['cost_change'])}")
            
            if bd:
                sections.append("\n• **ALTERNATIVE DOUBLE TRANSFER:**")
                for m in bd.get("moves", []):
                    sections.append(f"  - **OUT:** {m['out']['name']} -> **IN:** {m['in']['name']} (+{m['xp_gain']} xP)")
                sections.append(f"  - **Total Double Net Gain:** +{bd.get('xp_gain', 0)} xP")

            sections.append(f"\n**Verdict:** {advice}")

        return "\n".join(sections)

    except Exception as e:
        return f"⚠️ Could not generate Data Science advice: {str(e)}"
