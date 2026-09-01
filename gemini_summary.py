from typing import Dict, Any

def _get_optimal_xi(squad):
    limits = {"GK": 1, "DEF": 5, "MID": 5, "FWD": 3}
    grouped = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    
    for p in squad:
        grouped[p["position"]].append(p)
    for pos in grouped:
        grouped[pos].sort(key=lambda x: x["xp"], reverse=True)

    xi = []
    bench = []

    # 1. Positional minimums (1 GK, 3 DEF, 1 FWD)
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
    pool.sort(key=lambda x: x["xp"], reverse=True)

    counts = {
        "GK": len([p for p in xi if p["position"] == "GK"]),
        "DEF": len([p for p in xi if p["position"] == "DEF"]),
        "MID": len([p for p in xi if p["position"] == "MID"]),
        "FWD": len([p for p in xi if p["position"] == "FWD"])
    }

    # 2. Complete Starting XI
    for p in pool:
        if len(xi) < 11 and counts[p["position"]] < limits[p["position"]]:
            xi.append(p)
            counts[p["position"]] += 1
        else:
            bench.append(p)

    # 3. Order Bench: GK first, then outfield strictly by xP descending
    bench_gk = [p for p in bench if p["position"] == "GK"]
    bench_out = [p for p in bench if p["position"] != "GK"]
    bench_out.sort(key=lambda x: x["xp"], reverse=True)
    
    return xi, bench_gk + bench_out, counts

def write_summary(data: Dict[str, Any]) -> str:
    """
    Takes 15 players and outputs the mathematically perfect lineup, captaincy, and bench.
    """
    try:
        sections = []
        squad = data.get("squad", [])

        if squad and len(squad) >= 11:
            xi, bench, counts = _get_optimal_xi(squad)
            
            pos_order = {"GK": 1, "DEF": 2, "MID": 3, "FWD": 4}
            xi.sort(key=lambda x: (pos_order.get(x["position"], 5), -x["xp"]))
            formation = f"{counts['DEF']}-{counts['MID']}-{counts['FWD']}"
            
            sections.append(f"**Formation:** {formation}\n")
            
            for pos in ["GK", "DEF", "MID", "FWD"]:
                pos_players = [p for p in xi if p["position"] == pos]
                if pos_players:
                    line = f"**{pos}:** " + " | ".join([f"{p['name']} ({p['team']}) — {p['xp']} xP" for p in pos_players])
                    sections.append(line)

            total_xi_xp = sum(p["xp"] for p in xi)
            sections.append(f"\n**Starting XI Projected Score:** {total_xi_xp:.2f} xP\n")

            # Captaincy
            xi_sorted = sorted(xi, key=lambda x: x["xp"], reverse=True)
            cap = xi_sorted[0] if len(xi_sorted) > 0 else None
            vcap = xi_sorted[1] if len(xi_sorted) > 1 else None

            sections.append(f"### Captaincy Allocation")
            sections.append("---")
            if cap:
                sections.append(f"• **Captain (C):** {cap['name']} ({cap['team']}) — {cap['xp']} xP (x2 = {cap['xp']*2:.2f} xP)")
            if vcap:
                sections.append(f"• **Vice-Captain (VC):** {vcap['name']} ({vcap['team']}) — {vcap['xp']} xP\n")

            # Bench
            sections.append(f"### Auto-Sub Bench Priority")
            sections.append("---")
            for i, p in enumerate(bench):
                if i == 0 and p["position"] == "GK":
                    sections.append(f"• **GK Sub:** {p['name']} ({p['team']}) — {p['xp']} xP")
                else:
                    sub_num = i if bench[0]['position'] == 'GK' else i + 1
                    sections.append(f"• **Sub {sub_num}:** {p['name']} ({p['team']}) — {p['xp']} xP")

        return "\n".join(sections)

    except Exception as e:
        return f"⚠️ Could not generate Data Science advice: {str(e)}"
