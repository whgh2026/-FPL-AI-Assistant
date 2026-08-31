import streamlit as st
import google.generativeai as genai
import fpl_tools
import os
from dotenv import load_dotenv
import re
from difflib import SequenceMatcher

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-3.5-flash-lite"


def extract_squad_from_image(image_file) -> dict:
    """Use Gemini Vision to extract squad from screenshot."""
    try:
        image_bytes = image_file.getvalue()
        model = genai.GenerativeModel(MODEL)
        
        image_part = {
            "mime_type": image_file.type,
            "data": image_bytes
        }
        
        prompt = """Analyze this FPL squad screenshot and extract ALL 15 player names, team codes, prices, and positions.
Return ONLY a list in this EXACT format (one per line):
PLAYER: [First Name] [Last Name] ([Team Code]) £[Price]m [Position]

Example:
PLAYER: Erling Haaland (MCI) £15.5m FWD
PLAYER: Bukayo Saka (ARS) £9.5m MID"""

        response = model.generate_content([image_part, prompt])
        
        players = []
        for line in response.text.split('\n'):
            if line.startswith('PLAYER:'):
                players.append(line.replace('PLAYER:', '').strip())
        
        return {
            "success": len(players) > 0,
            "player_count": len(players),
            "raw_players": players,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Image processing failed: {str(e)}"
        }


def match_players_to_fpl(bootstrap_data: dict, raw_players: list) -> list:
    """Match extracted player strings to FPL player database IDs."""
    fpl_players = bootstrap_data.get("elements", [])
    teams = {t["id"]: t["name"] for t in bootstrap_data.get("teams", [])}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    
    matched = []
    for raw in raw_players:
        match = re.match(r"(.+?)\s*\(([A-Z]{3})\)\s*£([\d.]+)m\s*(\w+)", raw)
        if not match:
            continue
        
        name, _, _, _ = match.groups()
        name = name.strip()
        
        best_match = None
        best_score = 0.5
        
        for p in fpl_players:
            fpl_name = f"{p.get('first_name')} {p.get('second_name')}".strip()
            score = SequenceMatcher(None, name.lower(), fpl_name.lower()).ratio()
            if score > best_score:
                best_score = score
                best_match = p
        
        if best_match:
            matched.append({
                "player_id": best_match["id"],
                "name": f"{best_match['first_name']} {best_match['second_name']}",
                "team": teams.get(best_match["team"], "?"),
                "position": pos_map.get(best_match["element_type"], "?"),
                "price": best_match["now_cost"] / 10,
            })
    return matched


def render_override_ui(matched_players: list, bootstrap_data: dict, bank: float):
    """Render interactive position dropdowns for manual overrides."""
    fpl_players = bootstrap_data.get("elements", [])
    teams = {t["id"]: t["name"] for t in bootstrap_data.get("teams", [])}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    
    players_by_pos = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in fpl_players:
        pos = pos_map.get(p["element_type"])
        if pos:
            display = f"{p['first_name']} {p['second_name']} ({teams.get(p['team'], '?')}) £{p['now_cost']/10:.1f}m"
            players_by_pos[pos].append((p["id"], display))
    
    squad_by_pos = {}
    for p in matched_players:
        squad_by_pos.setdefault(p["position"], []).append(p)
    
    overridden_squad = []
    quotas = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
    
    for pos, count_needed in quotas.items():
        existing = squad_by_pos.get(pos, [])
        st.markdown(f"**{pos} ({len(existing)}/{count_needed})**")
        
        for i in range(count_needed):
            current_player = existing[i] if i < len(existing) else None
            
            col1, col2, col3 = st.columns([1, 4, 1])
            with col1:
                st.caption(f"#{i+1}")
            with col2:
                default_idx = 0
                if current_player:
                    for idx, (pid, _) in enumerate(players_by_pos[pos]):
                        if pid == current_player["player_id"]:
                            default_idx = idx
                            break
                
                selected = st.selectbox(
                    f"Select {pos} {i+1}",
                    options=players_by_pos[pos],
                    format_func=lambda x: x[1],
                    index=default_idx,
                    key=f"override_{pos}_{i}",
                    label_visibility="collapsed"
                )
                
                if selected:
                    pid, display = selected
                    name = display.split(" (")[0]
                    team_match = re.search(r"\(([A-Z]{3})\)", display)
                    price_match = re.search(r"£([\d.]+)m", display)
                    
                    overridden_squad.append({
                        "player_id": pid,
                        "name": name,
                        "team": team_match.group(1) if team_match else "?",
                        "position": pos,
                        "price": float(price_match.group(1)) if price_match else 0.0,
                    })
            with col3:
                if overridden_squad:
                    st.caption(f"£{overridden_squad[-1]['price']:.1f}m")
                    
    return overridden_squad


def generate_squad_justification(analysis_result: dict) -> str:
    """Automatically generate a data-backed justification summary covering form, fixtures, and cost."""
    try:
        team = analysis_result.get("team_name")
        bank = analysis_result.get("bank")
        squad = analysis_result.get("squad", [])
        weak_links = analysis_result.get("weak_links", [])
        captain = analysis_result.get("captain")
        
        squad_summary = ", ".join([f"{p['name']} ({p['position']}, xP {p['xp']}, Status: {p['status']})" for p in squad])
        weak_summary = ", ".join([f"{p['name']} (xP {p['xp']})" for p in weak_links])
        
        prompt = f"""You are an expert FPL Data Scientist. Analyze this custom squad configuration:
Team: {team} | Bank: £{bank}m
Captain Choice: {captain['name'] if captain else 'None'} (xP {captain['xp'] if captain else 0})
Full Squad & Expected Points (xP): {squad_summary}
Identified Weak Links: {weak_summary}

Write a comprehensive, data-backed justification (max 150 words) explaining:
1. Why the captaincy pick is mathematically optimal based on expected points, form, and fixture difficulty (venue multiplier).
2. The strategic evaluation of squad cost vs. team value and bank flexibility.
3. Specific recommendations on which weak links need immediate attention based on upcoming fixture difficulty and player status."""

        model = genai.GenerativeModel(MODEL)
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Could not generate automated justification: {str(e)}"