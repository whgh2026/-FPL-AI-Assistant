import os
import io
import json

try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
except Exception:
    genai = None
    types = None
    _GENAI_AVAILABLE = False

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except Exception:
    Image = None
    _PIL_AVAILABLE = False

MODEL = "gemini-3.1-flash-lite"

def extract_squad_from_image(uploaded_file) -> dict:
    """
    Performs OCR on the uploaded FPL squad screenshot using Gemini Flash-Lite.
    """
    if not _GENAI_AVAILABLE or not _PIL_AVAILABLE:
        return {"success": False, "error": "Gemini OCR dependencies (google-genai, Pillow) are not installed."}

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"success": False, "error": "GEMINI_API_KEY environment variable not set."}

    try:
        client = genai.Client(api_key=api_key)
        image = Image.open(uploaded_file).convert("RGB")
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        image_part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")

        prompt = """
        Analyze this Fantasy Premier League (FPL) squad screenshot.
        Extract all 15 visible players.
        Return ONLY a JSON list of objects with the exact schema:
        [
          {"name": "Player Name", "position": "GK|DEF|MID|FWD"}
        ]
        Do not include markdown or extra text.
        """

        response = client.models.generate_content(
            model=MODEL,
            contents=[image_part, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )

        raw_players = json.loads(response.text)
        return {"success": True, "raw_players": raw_players}

    except Exception as e:
        return {"success": False, "error": f"Gemini OCR extraction failed: {str(e)}"}

def match_players_to_fpl(bootstrap: dict, raw_players: list) -> tuple:
    """
    Matches OCR player name strings against official FPL element records.
    Returns (matched_squad, unmatched_names).
    """
    fpl_elements = bootstrap.get("elements", [])
    matched_squad = []
    unmatched = []

    for item in raw_players:
        ocr_name = item.get("name", "").lower().strip()
        ocr_pos = item.get("position", "").upper().strip()

        best_match = None
        for p in fpl_elements:
            web_name = p.get("web_name", "").lower()
            second_name = p.get("second_name", "").lower()
            first_name = p.get("first_name", "").lower()
            full_name = f"{first_name} {second_name}".lower()

            if ocr_name == web_name or ocr_name == second_name or ocr_name == full_name or ocr_name in full_name:
                best_match = p
                break

        if best_match:
            pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
            matched_squad.append({
                "player_id": best_match["id"],
                "position": pos_map.get(best_match["element_type"], ocr_pos)
            })
        else:
            unmatched.append(item.get("name", ""))

    return matched_squad, unmatched
