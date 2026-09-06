import difflib
import os
import io
import json
import re
import unicodedata

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
        Analyse this Fantasy Premier League (FPL) squad screenshot.
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

        raw_players = json.loads(_strip_code_fence(response.text))
        return {"success": True, "raw_players": raw_players}

    except Exception as e:
        return {"success": False, "error": f"Gemini OCR extraction failed: {str(e)}"}


_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*|\s*```\s*$")


def _strip_code_fence(text: str) -> str:
    """Peel a markdown code fence off a JSON payload, and only the fence.

    The previous version ran .replace('json\\n', '') across the WHOLE document
    to remove the fence's language tag -- a global substitution on a string that
    contains user-visible player names. It happened to work because no name
    contains that sequence, but a parser that can corrupt its own payload is one
    bad screenshot away from a wrong squad, and the failure would be silent.
    This anchors to the ends, so nothing in the middle can be touched.
    """
    return _FENCE.sub("", (text or "").strip()).strip()


def _normalize_name(name: str) -> str:
    nfkd = unicodedata.normalize('NFKD', name)
    return u"".join([c for c in nfkd if not unicodedata.combining(c)]).lower().strip()


POS_MAP = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
FUZZY_FLOOR = 0.82        # below this a near-miss is not a match, it is a guess


def _match_score(ocr_name: str, raw_name: str, ocr_pos: str, p: dict) -> float:
    """How well one bootstrap element matches one OCR'd name. 0.0 = no match.

    Scored rather than short-circuited. The old matcher broke out of the loop on
    its first substring hit, so "Son" resolved to whichever of Jackson, Wilson,
    Robertson and Son happened to sit earliest in the bootstrap -- and the
    reader was shown a confidently wrong squad with no indication anything had
    gone wrong. Exact matches now beat prefix matches beat fuzzy ones, and the
    OCR'd position breaks ties, which is exactly what it is good for.
    """
    web = _normalize_name(p.get("web_name", ""))
    second = _normalize_name(p.get("second_name", ""))
    first = _normalize_name(p.get("first_name", ""))
    full = f"{first} {second}".strip()

    score = 0.0
    if ocr_name and ocr_name in (web, second, full):
        score = 1.0
    elif "." in raw_name:
        # "M. Salah" -- initial plus surname, the form FPL's own UI prints.
        initial, _, surname = raw_name.partition(".")
        surname = _normalize_name(surname)
        if surname and surname in (web, second) and first.startswith(_normalize_name(initial)):
            score = 0.97
    if not score and ocr_name:
        # Surname-only OCR against a multi-word full name: a whole-word match on
        # the surname is strong, a bare substring is not (it is what let "son"
        # match "jackson").
        if ocr_name in full.split() or ocr_name in web.split():
            score = 0.90
        else:
            best = max(
                difflib.SequenceMatcher(None, ocr_name, cand).ratio()
                for cand in (web, second, full) if cand
            ) if (web or second or full) else 0.0
            if best >= FUZZY_FLOOR:
                score = 0.60 + 0.30 * best      # capped below every exact form

    if score and ocr_pos and POS_MAP.get(p.get("element_type")) == ocr_pos:
        score += 0.02                            # tie-break only, never a match
    return score


def match_players_to_fpl(bootstrap: dict, raw_players: list) -> tuple:
    """Resolve OCR'd names to FPL element ids. Returns (matched, unmatched).

    A name that matches nothing well enough goes to `unmatched` and is reported,
    rather than being resolved to the nearest thing in the file.
    """
    fpl_elements = bootstrap.get("elements", [])
    matched_squad = []
    unmatched = []
    taken = set()

    for item in raw_players:
        raw_name = item.get("name", "") or ""
        ocr_name = _normalize_name(raw_name)
        ocr_pos = (item.get("position", "") or "").upper().strip()

        best, best_score = None, 0.0
        for p in fpl_elements:
            # A squad has fifteen distinct players, so a name already claimed
            # cannot be the answer to a second one.
            if p.get("id") in taken:
                continue
            s = _match_score(ocr_name, raw_name, ocr_pos, p)
            if s > best_score:
                best, best_score = p, s

        if best is not None and best_score > 0:
            taken.add(best["id"])
            matched_squad.append({
                "player_id": best["id"],
                "position": POS_MAP.get(best["element_type"], ocr_pos),
            })
        else:
            unmatched.append(raw_name)

    return matched_squad, unmatched
