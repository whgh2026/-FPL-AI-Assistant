from typing import Dict, Any
import fpl_tools


def write_summary(data: Dict[str, Any]) -> str:
    """
    Renders the mathematically optimal lineup, captaincy, and bench order
    (computed by fpl_tools.select_starting_xi).
    """
    try:
        squad = data.get("squad", [])
        if not squad or len(squad) < 11:
            return "⚠️ Need at least 11 players to build a lineup."

        xi = fpl_tools.select_starting_xi(squad)

        sections = []
        d, m, f = xi["formation"]
        sections.append(f"**Formation:** {d}-{m}-{f}\n")

        for pos in ["GK", "DEF", "MID", "FWD"]:
            players = [p for p in xi["xi"] if p.get("position") == pos]
            if players:
                line = f"**{pos}:** " + " | ".join(f"{p['name']} ({p['team']}) — {p['xp']} xP" for p in players)
                sections.append(line)

        sections.append(f"\n**Starting XI Projected Score:** {xi['total_xp']:.2f} xP\n")

        cap = xi.get("captain")
        vcap = xi.get("vice_captain")
        sections.append("### Captaincy Allocation")
        sections.append("---")
        if cap:
            sections.append(f"• **Captain (C):** {cap['name']} ({cap['team']}) — {cap['xp']} xP (x2 = {cap['xp'] * 2:.2f} xP)")
        if vcap:
            sections.append(f"• **Vice-Captain (VC):** {vcap['name']} ({vcap['team']}) — {vcap['xp']} xP\n")

        sections.append("### Auto-Sub Bench Priority")
        sections.append("---")
        for i, p in enumerate(xi["bench"]):
            if p.get("position") == "GK":
                sections.append(f"• **GK Sub:** {p['name']} ({p['team']}) — {p['xp']} xP")
            else:
                sub_num = i if xi["bench"][0].get("position") == "GK" else i + 1
                sections.append(f"• **Sub {sub_num}:** {p['name']} ({p['team']}) — {p['xp']} xP")

        return "\n".join(sections)

    except Exception as e:
        return f"⚠️ Could not generate Data Science advice: {str(e)}"
