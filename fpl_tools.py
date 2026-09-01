import requests
from typing import Dict, Any, List, Tuple
from datetime import datetime
import dateutil.parser

BASE_URL = "https://fantasy.premierleague.com/api"

def _get_bootstrap() -> Dict[str, Any]:
    url = f"{BASE_URL}/bootstrap-static/"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()

def get_gameweek_deadline(gw: int) -> str:
    """Fetches and formats the official deadline for a specific gameweek."""
    try:
        bootstrap = _get_bootstrap()
        for event in bootstrap.get("events", []):
            if event["id"] == gw:
                dt = dateutil.parser.isoparse(event["deadline_time"])
                return dt.strftime("%A, %d %B at %H:%M")
        return "Unknown Deadline"
    except Exception:
        return "Unknown Deadline"

def _build_fixture_lookup() -> Dict[int, List[Dict[str, Any]]]:
    """Maps team_id -> upcoming fixture info."""
    fixtures_url = f"{BASE_URL}/fixtures/?future=1"
    try:
        resp = requests.get(fixtures_url, timeout=10)
        resp.raise_for_status()
        fixtures = resp.json()
    except Exception:
        return {}

    lookup = {}
    for f in fixtures:
        h, a = f["team_h"], f["team_a"]
        h_diff, a_diff = f.get("team_h_difficulty", 3), f.get("team_a_difficulty", 3)

        lookup.setdefault(h, []).append({"is_home": True, "opponent": a, "difficulty": h_diff})
        lookup.setdefault(a, []).append({"is_home": False, "opponent": h, "difficulty": a_diff})
    return lookup

def _player_xp(p: Dict[str, Any], fixture_lookup: Dict[int, List[Dict[str, Any]]]) -> Tuple[float, str]:
    """
    Data-driven xP calculation with softened early-season minutes handling.
    """
    status = p.get("status", "a")
    if status in ["i", "s", "u"]:
        return 0.0, "OUT"
    
    # Base expected points from form, goals, assists
    try:
        form = float(p.get("form", 0.0) or 0.0)
    except Exception:
        form = 0.0
    form = min(form, 5.0)

    try:
        xg = float(p.get("expected_goals", 0.0) or 0.0)
        xa = float(p.get("expected_assists", 0.0) or 0.0)
    except Exception:
        xg, xa = 0.0, 0.0

    pos_id = p.get("element_type")
    # Base baseline per 90 mins
    base_xp = 2.0 + form + (xg * 4.0 if pos_id == 4 else xg * 5.0) + (xa * 3.0)

    # Fixture difficulty & Home/Away adjustment
    team_id = p.get("team")
    fixtures = fixture_lookup.get(team_id, [])
    if fixtures:
        next_fix = fixtures[0]
        diff = next_fix["difficulty"]
        # FDR modifier: scale around 3
        diff_mod = 1.0 + (3 - diff) * 0.10
        base_xp *= diff_mod

        if next_fix["is_home"]:
            base_xp *= 1.10
        else:
            base_xp *= 0.95

    # Status modifiers
    note = "Available"
    if status == "d":
        base_xp *= 0.5
        note = "Doubtful"

    # Softened minutes penalty (early-season protection)
    starts = int(p.get("starts", 0) or 0)
    minutes = int(p.get("minutes", 0) or 0)
    if starts < 1 and minutes < 90:
        base_xp *= 0.75
        note = "Rotation Risk"

    return round(max(base_xp, 0.5), 2), note

def score_my_squad(manager_id: str, gw: int) -> Dict[str, Any]:
    bootstrap = _get_bootstrap()
    players_by_id = {p["id"]: p for p in bootstrap["elements"]}
    teams_by_id = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    picks_url = f"{BASE_URL}/entry/{manager_id}/event/{gw}/picks/"
    resp = requests.get(picks_url, timeout=10)
    if resp.status_code != 200:
        return {"error": f"Could not fetch squad for Manager ID {manager_id} (GW {gw})."}

    picks_data = resp.json()
    fixture_lookup = _build_fixture_lookup()

    squad = []
    for pick in picks_data.get("picks", []):
        p_data = players_by_id.get(pick["element"])
        if not p_data:
            continue
        xp, note = _player_xp(p_data, fixture_lookup)
        squad.append({
            "player_id": p_data["id"],
            "name": f"{p_data['first_name']} {p_data['second_name']}",
            "team": teams_by_id.get(p_data["team"], "?"),
            "position": pos_map.get(p_data["element_type"], "?"),
            "price": p_data["now_cost"] / 10.0,
            "xp": xp,
            "status": note,
            "is_captain": pick.get("is_captain", False),
            "is_vice_captain": pick.get("is_vice_captain", False)
        })

    entry_url = f"{BASE_URL}/entry/{manager_id}/"
    entry_resp = requests.get(entry_url, timeout=10)
    entry_data = entry_resp.json() if entry_resp.status_code == 200 else {}

    return {
        "team_name": entry_data.get("name", "My Team"),
        "bank": picks_data.get("entry_history", {}).get("bank", 0) / 10.0,
        "team_value": picks_data.get("entry_history", {}).get("value", 1000) / 10.0,
        "gameweek_used": gw,
        "squad": squad
    }

def suggest_transfers_for_custom_squad(squad: List[Dict[str, Any]], bank: float, free_transfers: int) -> Dict[str, Any]:
    bootstrap = _get_bootstrap()
    fixture_lookup = _build_fixture_lookup()
    teams_by_id = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    current_ids = {p["player_id"] for p in squad}
    candidates = []
    for p in bootstrap["elements"]:
        if p["id"] in current_ids:
            continue
        xp, note = _player_xp(p, fixture_lookup)
        if xp > 3.0 and note != "OUT":
            candidates.append({
                "id": p["id"],
                "name": f"{p['first_name']} {p['second_name']}",
                "team": teams_by_id.get(p["team"], "?"),
                "position": pos_map.get(p["element_type"], "?"),
                "price": p["now_cost"] / 10.0,
                "xp": xp
            })

    # Evaluate Best Single Transfer
    best_single = None
    max_gain = 0.0

    for out_p in squad:
        for in_p in candidates:
            if in_p["position"] == out_p["position"]:
                cost_diff = in_p["price"] - out_p["price"]
                if cost_diff <= bank:
                    gain = round(in_p["xp"] - out_p["xp"], 2)
                    if gain > max_gain:
                        max_gain = gain
                        best_single = {
                            "out": {"id": out_p["player_id"], "name": out_p["name"], "team": out_p["team"], "price": out_p["price"], "xp": out_p["xp"]},
                            "in": in_p,
                            "xp_gain": gain,
                            "cost_change": round(cost_diff, 1)
                        }

    # Evaluate Best Double Transfer
    best_double = None
    max_double_gain = 0.0
    if len(squad) >= 2 and len(candidates) >= 2:
        for i in range(len(squad)):
            for j in range(i + 1, len(squad)):
                out1, out2 = squad[i], squad[j]
                for c1 in candidates[:15]:
                    if c1["position"] != out1["position"]:
                        continue
                    for c2 in candidates[:15]:
                        if c2["position"] != out2["position"] or c1["id"] == c2["id"]:
                            continue
                        total_cost = (c1["price"] - out1["price"]) + (c2["price"] - out2["price"])
                        if total_cost <= bank:
                            gain1 = c1["xp"] - out1["xp"]
                            gain2 = c2["xp"] - out2["xp"]
                            total_gain = round(gain1 + gain2, 2)
                            if total_gain > max_double_gain:
                                max_double_gain = total_gain
                                best_double = {
                                    "moves": [
                                        {"out": {"id": out1["player_id"], "name": out1["name"]}, "in": c1, "xp_gain": round(gain1, 2)},
                                        {"out": {"id": out2["player_id"], "name": out2["name"]}, "in": c2, "xp_gain": round(gain2, 2)}
                                    ],
                                    "xp_gain": total_gain,
                                    "cost_change": round(total_cost, 1)
                                }

    # Hit Logic Formulation
    advice = "Hold transfers this week."
    if free_transfers == 0:
        if best_single and best_single["xp_gain"] >= 4.0:
            advice = "You have 0 free transfers. Single move is worth a -4 hit (xP gain beats point cost)."
        else:
            advice = "You have 0 free transfers. No move is worth a -4 hit this week — hold."
    elif free_transfers == 1:
        if best_double and best_single and (best_double["xp_gain"] - best_single["xp_gain"]) >= 4.0:
            advice = "A double move (-4 hit) is mathematically worth it over a single transfer."
        elif best_single and best_single["xp_gain"] > 0:
            advice = "Execute 1 free transfer. A second transfer (-4 hit) is not worth it."
    else:
        advice = "You have 2+ free transfers — execute both moves for free."

    return {
        "best_single": best_single,
        "best_double": best_double,
        "hit_advice": advice
    }

def suggest_weekly_transfers(manager_id: str, gw: int, free_transfers: int) -> Dict[str, Any]:
    squad_res = score_my_squad(manager_id, gw)
    if "error" in squad_res:
        return squad_res
    transfers = suggest_transfers_for_custom_squad(squad_res["squad"], squad_res["bank"], free_transfers)
    return {**squad_res, **transfers}

def optimise_full_squad(budget: float = 100.0) -> Dict[str, Any]:
    bootstrap = _get_bootstrap()
    fixture_lookup = _build_fixture_lookup()
    teams_by_id = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    pool = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in bootstrap["elements"]:
        pos = pos_map.get(p["element_type"])
        if pos:
            xp, note = _player_xp(p, fixture_lookup)
            if note != "OUT":
                pool[pos].append({
                    "player_id": p["id"],
                    "name": f"{p['first_name']} {p['second_name']}",
                    "team": teams_by_id.get(p["team"], "?"),
                    "position": pos,
                    "price": p["now_cost"] / 10.0,
                    "xp": xp
                })

    for pos in pool:
        pool[pos].sort(key=lambda x: x["xp"] / max(x["price"], 4.0), reverse=True)

    selected = pool["GK"][:2] + pool["DEF"][:5] + pool["MID"][:5] + pool["FWD"][:3]
    total_price = round(sum(p["price"] for p in selected), 1)
    total_xp = round(sum(p["xp"] for p in selected), 2)

    return {
        "budget": budget,
        "total_price": total_price,
        "total_xp": total_xp,
        "squad": selected
    }

def rank_players_by_xp(position: str = None, max_price: float = None, limit: int = 20) -> Dict[str, Any]:
    bootstrap = _get_bootstrap()
    fixture_lookup = _build_fixture_lookup()
    teams_by_id = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    ranked = []
    for p in bootstrap["elements"]:
        pos = pos_map.get(p["element_type"])
        price = p["now_cost"] / 10.0
        if position and pos != position:
            continue
        if max_price and max_price > 0 and price > max_price:
            continue

        xp, note = _player_xp(p, fixture_lookup)
        ranked.append({
            "name": f"{p['first_name']} {p['second_name']}",
            "position": pos,
            "team": teams_by_id.get(p["team"], "?"),
            "price": price,
            "xp": xp,
            "status": note
        })

    ranked.sort(key=lambda x: x["xp"], reverse=True)
    return {"players": ranked[:limit]}
