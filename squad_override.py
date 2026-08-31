import streamlit as st
import google.generativeai as genai
import json
import re
from difflib import SequenceMatcher
import os
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# UPDATED: Use the vision-optimized JSON model
MODEL = "gemini-2.5-flash"

def extract_squad_from_image(image_file) -> dict:
    """Use Gemini Vision to accurately extract squad player names and teams, outputting JSON."""
    try:
        image_bytes = image_file.getvalue()
        model = genai.GenerativeModel(MODEL)
        
        image_part = {
            "mime_type": image_file.type,
            "data": image_bytes
        }
        
        # UPDATED: Prompt specifically addresses the shirt vs opponent box issue and enforces JSON
        prompt = """Look at this Fantasy Premier League (FPL) screenshot. 
Extract the 15 players shown on the pitch. 

CRITICAL INSTRUCTIONS FOR TEAM IDENTIFICATION:
1. Ignore the text in the lower white box (e.g., 'NFO (A)' or 'LEE (H)'). That is their upcoming OPPONENT, not their club!
2. You MUST deduce the player's actual team strictly by looking at their shirt color, design, and sponsor (e.g., AIA pink shirt = Spurs, American Express blue/white = Brighton, Etihad blue = Man City, Snapdragon red = Man Utd).

Return a valid JSON array of objects. Each object should have these keys:
- "name": The player's display name.
- "team": The 3-letter abbreviation of their deduced team.
- "position": Based on pitch rows (Top row = "GK", 2nd row = "DEF", 3rd row = "MID", 4th row = "FWD").

Return ONLY the raw JSON array. Do not include markdown formatting like ```json."""

        response = model.generate_content([image_part, prompt])
        
        # Clean up any potential markdown formatting the AI might add
        raw_text = response.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
            
        players_data = json.loads(raw_text.strip())
        
        return {
            "success": len(players_data) > 0,
            "player_count": len(players_data),
            "raw_players": players_data, 
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Image processing failed: {str(e)}"
        }


def match_players_to_fpl(bootstrap_data: dict, raw_players: list) -> list:
    """Fuzzy match extracted JSON players to FPL player database IDs."""
    fpl_players = bootstrap_data.get("elements", [])
    teams = {t["id"]: t["name"] for t in bootstrap_data.get("teams", [])}
    team_code_map = {
        "ARS": "Arsenal", "AVL": "Aston Villa", "BOU": "Bournemouth", "BRE": "Brentford",
        "BHA": "Brighton", "CHE": "Chelsea", "CRY": "Crystal Palace", "EVE": "Everton",
        "FUL": "Fulham", "IPS": "Ipswich", "LEI": "Leicester", "LIV": "Liverpool",
        "MCI": "Man City", "MUN": "Man Utd", "NEW": "Newcastle", "NFO": "Nott'm Forest",
        "TOT": "Spurs", "WHU": "West Ham", "WOL": "Wolves", "SOU": "Southampton"
    }
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    
    matched = []
    for raw in raw_players:
        if not isinstance(raw, dict):
            continue

        name = raw.get("name", "").strip()
        team_hint = raw.get("team", "").strip()
        pos = raw.get("position", "").strip()

        best_match = None
        best_score = 0.45 
        
        resolved_team = team_code_map.get(team_hint.upper(), team_hint)

        for p in fpl_players:
            p_pos = pos_map.get(p.get("element_type"))
            
            fpl_first = p.get("first_name", "").lower()
            fpl_second = p.get("second_name", "").lower()
            fpl_web = p.get("web_name", "").lower()
            
            target = name.lower()
            
            # Calculate match score based on Web Name, Last Name, or Full Name
            score = max(
                SequenceMatcher(None, target, fpl_second).ratio(),
                SequenceMatcher(None, target, fpl_web).ratio(),
                SequenceMatcher(None, target, fpl_first + " " + fpl_second).ratio()
            )
            
            # Boost score if the club matches
            p_team_name = teams.get(p.get("team"), "").lower()
            if resolved_team and (resolved_team.lower() in p_team_name or p_team_name in resolved_team.lower()):
                score += 0.25
                
            # Apply a penalty if the position is wrong (instead of skipping completely)
            if pos and p_pos != pos:
                score -= 0.3

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
