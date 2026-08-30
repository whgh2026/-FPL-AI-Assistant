import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-3.1-flash-lite"


def write_summary(transfer_result) -> str:
    try:
        bs = transfer_result.get("best_single")
        bd = transfer_result.get("best_double")
        bank = transfer_result.get("bank")
        team = transfer_result.get("team_name")
        advice = transfer_result.get("hit_advice")

        facts = f"Team: {team}. Bank: £{bank}m. Hit advice: {advice}.\n"
        if bs:
            facts += (
                f"Best single transfer: OUT {bs['out']['name']} (xP {bs['out']['xp']}) "
                f"-> IN {bs['in']['name']} (xP {bs['in']['xp']}), gain +{bs['xp_gain']}.\n"
            )
        if bd:
            moves = "; ".join(
                f"OUT {m['out']['name']} -> IN {m['in']['name']} (+{m['xp_gain']})"
                for m in bd["moves"]
            )
            facts += f"Best double transfer: {moves}. Total gain +{bd['xp_gain']}.\n"

        prompt = (
            "You are an FPL analyst. Using ONLY the data below, write a short, "
            "punchy weekly summary (max 120 words) telling the manager what to do "
            "and why. Be decisive and clear.\n\n" + facts
        )

        model = genai.GenerativeModel(MODEL)
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"(Could not generate summary: {str(e)})"