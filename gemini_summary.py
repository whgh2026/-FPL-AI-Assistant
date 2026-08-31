import os
from typing import Dict, Any, List, Optional

import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Ensure this matches your active endpoint
MODEL = "gemini-3.6-flash"


def _fmt_money(value):
    try:
        return f"£{float(value):.1f}m"
    except Exception:
        return "£0.0m"


def _build_transfer_facts(transfer_result: Dict[str, Any]) -> str:
    team = transfer_result.get("team_name", "Unknown")
    bank = transfer_result.get("bank", 0)
    advice = transfer_result.get("hit_advice", "")

    lines = [
        f"Manager team: {team}",
        f"Bank: {_fmt_money(bank)}",
        f"Hit advice: {advice}",
    ]

    bs = transfer_result.get("best_single")
    if bs:
        lines.append(
            "Best single transfer: "
            f"OUT {bs['out']['name']} ({bs['out']['team']}, xP {bs['out']['xp']}, price {_fmt_money(bs['out'].get('price', 0))}) "
            f"-> IN {bs['in']['name']} ({bs['in']['team']}, xP {bs['in']['xp']}, price {_fmt_money(bs['in'].get('price', 0))}) "
            f"gain +{bs['xp_gain']} xP, cost change {_fmt_money(bs['cost_change'])}."
        )

    bd = transfer_result.get("best_double")
    if bd:
        move_text = []
        for m in bd.get("moves", []):
            move_text.append(
                f"OUT {m['out']['name']} ({m['out']['team']}, xP {m['out']['xp']}) "
                f"-> IN {m['in']['name']} ({m['in']['team']}, xP {m['in']['xp']}), gain +{m['xp_gain']}."
            )
        lines.append("Best double transfer: " + " ".join(move_text))
        lines.append(f"Total double-transfer gain: +{bd.get('xp_gain', 0)} xP")

    return "\n".join(lines)


def _build_squad_facts(squad_result: Dict[str, Any]) -> str:
    team = squad_result.get("team_name", "Unknown")
    bank = squad_result.get("bank", 0)
    team_value = squad_result.get("team_value", 0)
    gw = squad_result.get("gameweek_used", "?")
    
    squad = squad_result.get("squad", [])
    weak_links = squad_result.get("weak_links", [])

    lines = [
        f"Manager team: {team}",
        f"Gameweek: {gw}",
        f"Team value: {_fmt_money(team_value)}",
        f"Bank: {_fmt_money(bank)}",
    ]

    if squad:
        lines.append("Squad ranking by xP and context:")
        for idx, p in enumerate(sorted(squad, key=lambda x: x.get("xp", 0), reverse=True), start=1):
            lines.append(
                f"{idx}. {p.get('name')} | {p.get('position')} | {p.get('team')} | "
                f"price {_fmt_money(p.get('price', 0))} | xP {p.get('xp')} | status {p.get('status')}"
            )

    if weak_links:
        lines.append("Weak links to prioritise:")
        for p in weak_links:
            lines.append(
                f"- {p.get('name')} | {p.get('position')} | {p.get('team')} | "
                f"price {_fmt_money(p.get('price', 0))} | xP {p.get('xp')} | status {p.get('status')}"
            )

    return "\n".join(lines)

def write_summary(data: Dict[str, Any]) -> str:
    """
    Generate a short but well-justified plain-English summary for either:
    * transfer recommendations
    * squad analysis / manual override analysis
    """
    try:
        if data.get("best_single") or data.get("best_double"):
            facts = _build_transfer_facts(data)

            prompt = f"""
You are an elite Fantasy Premier League analyst.

Using ONLY the facts below, write a concise weekly transfer briefing in plain English.
Make it decisive, practical, and specific.

You MUST justify recommendations using:
* expected points ranking
* price / value
* fixture difficulty
* home vs away advantage
* player form or status
* whether the move is worth a hit

Structure:
* Short headline
* Best move
* Why it is better
* Whether to take a hit or hold
* One sentence conclusion

Keep it under 140 words.

FACTS:
{facts}
"""
        else:
            facts = _build_squad_facts(data)
            
            # UPDATED: Tactical FPL prompt enforcing formations, captaincy, and bench logic
            prompt = f"""
You are an elite Fantasy Premier League analyst.

Using ONLY the facts below, write a comprehensive and decisive squad briefing. 
The xP (Expected Points) metric provided already factors in recent performance, fixture difficulty, and home vs. away advantage. Use it as your primary guide.

You MUST follow strict FPL rules for the Starting 11: exactly 1 Goalkeeper, minimum 3 Defenders, and minimum 1 Forward. 

Structure your response with these exact sections:
* **Headline:** A short, punchy summary of the squad's outlook.
* **Formation & Starting XI:** State the best valid FPL formation (e.g., 3-5-2, 3-4-3) based on the highest xP players available. Briefly explain why this setup maximizes points this week.
* **Captain & Vice-Captain:** Pick the best Captain (highest xP) and Vice-Captain (2nd highest xP) and justify the choice based on their specific fixture or form.
* **Bench Order:** List the 4 benched players. List the backup GK first, followed by the 3 outfield players ordered 1st, 2nd, and 3rd sub based on their remaining xP and status.
* **Transfer Priorities:** Name 1-2 weakest links to sell and why they are dead weight.
* **Conclusion:** One sentence final thought.

Keep the analysis sharp, professional, and under 250 words.

FACTS:
{facts}
"""

        model = genai.GenerativeModel(MODEL)
        response = model.generate_content(prompt)
        return (response.text or "").strip()

    except Exception as e:
        return f"(Could not generate summary: {str(e)})"
