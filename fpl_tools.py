import requests
import os

FPL_API = os.getenv("FPL_API_BASE", "https://fantasy.premierleague.com/api/").strip().strip("\"'").rstrip("/") + "/"

HOME_BONUS = 1.10
AWAY_PENALTY = 0.95

VALID_PL_TEAMS = {
    "Arsenal", "Aston Villa", "Bournemouth", "Brentford", "Brighton",
    "Chelsea", "Coventry", "Crystal Palace", "Everton", "Fulham",
    "Hull City", "Ipswich", "Leeds", "Liverpool", "Man City",
    "Man Utd", "Newcastle", "Nott'm Forest", "Spurs", "Sunderland",
}


def _get_bootstrap():
    return requests.get(f"{FPL_API}bootstrap-static/", timeout=10).json()


def _valid_team_ids_by_name(bootstrap):
    return {t["id"] for t in bootstrap.get("teams", []) if t["name"] in VALID_PL_TEAMS}


def _build_fixture_lookup():
    fixtures = requests.get(f"{FPL_API}fixtures/?future=1", timeout=10).json()
    lookup = {}
    for f in fixtures:
        if f.get("event") is None:
            continue
        th, ta = f.get("team_h"), f.get("team_a")
        lookup.setdefault(th, []).append(
            {"opponent": ta, "is_home": True, "difficulty": f.get("team_h_difficulty", 3)}
        )
        lookup.setdefault(ta, []).append(
            {"opponent": th, "is_home": False, "difficulty": f.get("team_a_difficulty", 3)}
        )
    return lookup


def _player_xp(p, fixture_lookup):
    status = p.get("status", "a")
    if status == "u":
        return 0.0, "OUT"
    availability = 1.0
    note = "OK"
    if status == "d":
        availability = 0.5
        note = "DOUBT"

    minutes = int(p.get("minutes", 0) or 0)
    starts = int(p.get("starts", 0) or 0)

    if starts < 1 or minutes < 90:
        return 0.0, "LOW MINS"

    form = float(p.get("form", 0) or 0)
    ppg = float(p.get("points_per_game", 0) or 0)

    try:
        xg = float(p.get("expected_goals_per_90", 0) or 0)
        xa = float(p.get("expected_assists_per_90", 0) or 0)
    except (ValueError, TypeError):
        xg, xa = 0.0, 0.0
    xgi = xg + xa

    avg_mins = minutes / max(starts, 1)
    minutes_factor = min(avg_mins / 90, 1.0)

    fixtures = fixture_lookup.get(p.get("team"), [])
    if fixtures:
        nxt = fixtures[0]
        difficulty = nxt["difficulty"]
        venue_mult = HOME_BONUS if nxt["is_home"] else AWAY_PENALTY
    else:
        difficulty = 3
        venue_mult = 1.0
    fixture_ease = (5 - difficulty)

    capped_form = min(form, 5.0)
    base = (xgi * 6.0) + (capped_form * 1.0) + (ppg * 1.0) + (fixture_ease * 1.5)
    xp = base * minutes_factor * venue_mult * availability

    return round(xp, 2), note


def rank_players_by_xp(position=None, max_price=None, limit=15) -> dict:
    try:
        bootstrap = _get_bootstrap()
        players = bootstrap.get("elements", [])
        teams = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
        fixture_lookup = _build_fixture_lookup()
        valid_teams = _valid_team_ids_by_name(bootstrap)
        pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

        ranked = []
        for p in players:
            if p.get("team") not in valid_teams:
                continue
            pos = pos_map.get(p.get("element_type"))
            if position and pos != position:
                continue
            price = p.get("now_cost", 0) / 10
            if max_price and price > float(max_price):
                continue

            xp, note = _player_xp(p, fixture_lookup)
            if xp <= 0:
                continue

            ranked.append({
                "id": p.get("id"),
                "name": f"{p.get('first_name')} {p.get('second_name')}",
                "team": teams.get(p.get("team"), "?"),
                "position": pos,
                "price": price,
                "xp": xp,
                "form": p.get("form"),
                "status": note,
            })

        ranked.sort(key=lambda x: x["xp"], reverse=True)
        return {"players": ranked[:int(limit)]}
    except Exception as e:
        return {"error": f"Failed to rank players: {str(e)}"}


def score_my_squad(manager_id, gameweek) -> dict:
    try:
        manager_id = int(manager_id)
        gameweek = int(gameweek)

        bootstrap = _get_bootstrap()
        players_by_id = {p["id"]: p for p in bootstrap["elements"]}
        teams = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
        fixture_lookup = _build_fixture_lookup()
        pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

        picks_data = None
        used_gw = gameweek
        for gw in range(gameweek, 0, -1):
            r = requests.get(f"{FPL_API}entry/{manager_id}/event/{gw}/picks/", timeout=10)
            if r.status_code == 200:
                picks_data = r.json()
                used_gw = gw
                break
        if picks_data is None:
            return {"error": f"No saved squad found for manager {manager_id}."}

        squad = []
        for pick in picks_data.get("picks", []):
            p = players_by_id.get(pick["element"])
            if not p:
                continue
            xp, note = _player_xp(p, fixture_lookup)
            squad.append({
                "player_id": p["id"],
                "name": f"{p['first_name']} {p['second_name']}",
                "team": teams.get(p["team"], "?"),
                "position": pos_map.get(p["element_type"]),
                "price": p["now_cost"] / 10,
                "xp": xp,
                "status": note,
                "is_captain": pick["is_captain"],
            })

        weak_links = sorted(squad, key=lambda x: x["xp"])[:4]
        best_xi = [p for p in squad if p["xp"] > 0]
        captain = max(best_xi, key=lambda x: x["xp"]) if best_xi else None

        entry_data = requests.get(f"{FPL_API}entry/{manager_id}/", timeout=10).json()
        return {
            "gameweek_used": used_gw,
            "team_name": entry_data.get("name", "Unknown"),
            "bank": entry_data.get("last_deadline_bank", 0) / 10,
            "team_value": entry_data.get("last_deadline_value", 0) / 10,
            "squad": squad,
            "weak_links": weak_links,
            "captain": captain,
        }
    except Exception as e:
        return {"error": f"Failed to score squad: {str(e)}"}


def _all_scored_players():
    bootstrap = _get_bootstrap()
    players = bootstrap.get("elements", [])
    teams = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    fixture_lookup = _build_fixture_lookup()
    valid_teams = _valid_team_ids_by_name(bootstrap)
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    scored = []
    for p in players:
        if p.get("team") not in valid_teams:
            continue
        xp, note = _player_xp(p, fixture_lookup)
        if xp <= 0:
            continue
        scored.append({
            "id": p["id"],
            "name": f"{p['first_name']} {p['second_name']}",
            "team_id": p["team"],
            "team": teams.get(p["team"], "?"),
            "position": pos_map.get(p["element_type"]),
            "price": p["now_cost"] / 10,
            "xp": xp,
        })
    return scored


def optimise_full_squad(budget=100.0) -> dict:
    try:
        import pulp

        budget = float(budget)
        players = _all_scored_players()
        if not players:
            return {"error": "No players available to optimise."}

        prob = pulp.LpProblem("FPL_Squad", pulp.LpMaximize)
        choices = {p["id"]: pulp.LpVariable(f"p_{p['id']}", cat="Binary") for p in players}

        prob += pulp.lpSum(choices[p["id"]] * p["xp"] for p in players)
        prob += pulp.lpSum(choices[p["id"]] * p["price"] for p in players) <= budget
        prob += pulp.lpSum(choices[p["id"]] for p in players) == 15

        quotas = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
        for pos, count in quotas.items():
            prob += pulp.lpSum(choices[p["id"]] for p in players if p["position"] == pos) == count

        team_ids = set(p["team_id"] for p in players)
        for tid in team_ids:
            prob += pulp.lpSum(choices[p["id"]] for p in players if p["team_id"] == tid) <= 3

        prob.solve(pulp.PULP_CBC_CMD(msg=0))

        chosen = [p for p in players if choices[p["id"]].value() == 1]
        chosen.sort(key=lambda x: (x["position"], -x["xp"]))

        total_price = round(sum(p["price"] for p in chosen), 1)
        total_xp = round(sum(p["xp"] for p in chosen), 2)

        return {
            "squad": chosen,
            "total_price": total_price,
            "total_xp": total_xp,
            "budget": budget,
        }
    except Exception as e:
        return {"error": f"Optimisation failed: {str(e)}"}


def suggest_weekly_transfers(manager_id, gameweek, free_transfers=1) -> dict:
    try:
        manager_id = int(manager_id)
        gameweek = int(gameweek)
        free_transfers = int(free_transfers)

        bootstrap = _get_bootstrap()
        players_by_id = {p["id"]: p for p in bootstrap["elements"]}
        teams = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
        fixture_lookup = _build_fixture_lookup()
        valid_teams = _valid_team_ids_by_name(bootstrap)
        pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

        picks_data = None
        used_gw = gameweek
        for gw in range(gameweek, 0, -1):
            r = requests.get(f"{FPL_API}entry/{manager_id}/event/{gw}/picks/", timeout=10)
            if r.status_code == 200:
                picks_data = r.json()
                used_gw = gw
                break
        if picks_data is None:
            return {"error": f"No saved squad found for manager {manager_id}."}

        owned_ids = set()
        squad = []
        club_counts = {}
        for pick in picks_data.get("picks", []):
            p = players_by_id.get(pick["element"])
            if not p:
                continue
            owned_ids.add(p["id"])
            xp, note = _player_xp(p, fixture_lookup)
            club_counts[p["team"]] = club_counts.get(p["team"], 0) + 1
            squad.append({
                "id": p["id"],
                "name": f"{p['first_name']} {p['second_name']}",
                "team_id": p["team"],
                "team": teams.get(p["team"], "?"),
                "position": pos_map.get(p["element_type"]),
                "price": p["now_cost"] / 10,
                "xp": xp,
                "status": note,
            })

        entry_data = requests.get(f"{FPL_API}entry/{manager_id}/", timeout=10).json()
        bank = entry_data.get("last_deadline_bank", 0) / 10

        pool = []
        for p in bootstrap["elements"]:
            if p["team"] not in valid_teams:
                continue
            if p["id"] in owned_ids:
                continue
            xp, note = _player_xp(p, fixture_lookup)
            if xp <= 0:
                continue
            pool.append({
                "id": p["id"],
                "name": f"{p['first_name']} {p['second_name']}",
                "team_id": p["team"],
                "team": teams.get(p["team"], "?"),
                "position": pos_map.get(p["element_type"]),
                "price": p["now_cost"] / 10,
                "xp": xp,
            })

        single_swaps = []
        for out_p in squad:
            spendable = bank + out_p["price"]
            for in_p in pool:
                if in_p["position"] != out_p["position"]:
                    continue
                if in_p["price"] > spendable:
                    continue
                new_count = club_counts.get(in_p["team_id"], 0)
                if in_p["team_id"] == out_p["team_id"]:
                    new_count -= 1
                if new_count >= 3:
                    continue
                gain = round(in_p["xp"] - out_p["xp"], 2)
                if gain <= 0:
                    continue
                single_swaps.append({
                    "out": out_p,
                    "in": in_p,
                    "xp_gain": gain,
                    "cost_change": round(in_p["price"] - out_p["price"], 1),
                })

        single_swaps.sort(key=lambda x: x["xp_gain"], reverse=True)
        best_single = single_swaps[0] if single_swaps else None

        best_double = None
        if len(single_swaps) >= 2:
            top = single_swaps[:15]
            for i in range(len(top)):
                for j in range(i + 1, len(top)):
                    a, b = top[i], top[j]
                    if a["out"]["id"] == b["out"]["id"]:
                        continue
                    if a["in"]["id"] == b["in"]["id"]:
                        continue
                    combined = round(a["xp_gain"] + b["xp_gain"], 2)
                    if best_double is None or combined > best_double["xp_gain"]:
                        best_double = {"moves": [a, b], "xp_gain": combined}

        if free_transfers == 0:
            if best_single and best_single["xp_gain"] >= 4:
                hit_advice = "You have 0 free transfers. This move is worth a -4 hit (xP gain beats the 4-point cost)."
            else:
                hit_advice = "You have 0 free transfers. No move is worth a -4 hit this week - hold."
        elif free_transfers >= 2 and best_double:
            hit_advice = "You have 2+ free transfers - make both moves for free."
        elif best_double and best_single and (best_double["xp_gain"] - best_single["xp_gain"]) >= 4:
            hit_advice = "A second transfer (-4 hit) looks worth it this week."
        else:
            hit_advice = "Stick to one transfer - a -4 hit is not worth it this week."

        return {
            "gameweek_used": used_gw,
            "team_name": entry_data.get("name", "Unknown"),
            "bank": bank,
            "best_single": best_single,
            "best_double": best_double,
            "hit_advice": hit_advice,
        }
    except Exception as e:
        return {"error": f"Failed to suggest transfers: {str(e)}"}
