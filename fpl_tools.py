import math
import re
import time
import requests
from typing import Dict, Any, List, Tuple, Optional
import dateutil.parser

try:
    import pulp
    HAS_PULP = True
except ImportError:
    HAS_PULP = False

BASE_URL = "https://fantasy.premierleague.com/api"

# ------------------------------------------------------------------
# Configuration / constants
# ------------------------------------------------------------------
EP_BLEND = 0.5                 
HIT_COST = 4.0                 
MAX_HIT_TRANSFERS = 3          
PRIOR_MINUTES = 270.0          

RISK_PROFILES = {
    "conservative": {"hit_cost": 6.0, "ow_weight": 0.8, "threat_weight": 0.0, "floor_weight": 1.0},
    "balanced":     {"hit_cost": 4.0, "ow_weight": 0.0, "threat_weight": 0.0, "floor_weight": 0.0},
    "aggressive":   {"hit_cost": 3.0, "ow_weight": -0.8, "threat_weight": 1.2, "floor_weight": -0.2},
}

POS_MAP = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
GOAL_PTS = {1: 6, 2: 6, 3: 5, 4: 4}
CS_PTS = {1: 4, 2: 4, 3: 1, 4: 0}
POS_COUNTS = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
OUT_STATUSES = {"i", "s", "u", "n"}

VALID_FORMATIONS = [
    (d, m, f)
    for d in range(3, 6) for m in range(2, 6) for f in range(1, 4)
    if d + m + f == 10
]

_CACHE: Dict[str, Dict[str, Any]] = {}
_LAST_FETCH_TIME = None

def _cached_json(url: str, ttl: int = 300) -> Any:
    global _LAST_FETCH_TIME
    now = time.time()
    hit = _CACHE.get(url)
    if hit and now - hit["t"] < ttl:
        return hit["data"]
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    _CACHE[url] = {"t": now, "data": data}
    if "bootstrap-static" in url:
        _LAST_FETCH_TIME = now
    return data

def get_api_timestamp() -> float:
    global _LAST_FETCH_TIME
    if _LAST_FETCH_TIME is None:
        _get_bootstrap()
    return _LAST_FETCH_TIME or time.time()

def _get_bootstrap() -> Dict[str, Any]:
    return _cached_json(f"{BASE_URL}/bootstrap-static/")

def _get_fixtures() -> List[Dict[str, Any]]:
    return _cached_json(f"{BASE_URL}/fixtures/?future=1")

def _to_float(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0

def _risk_profile(risk: str) -> Dict[str, float]:
    return RISK_PROFILES.get((risk or "balanced").lower(), RISK_PROFILES["balanced"])

def _clean_manager_id(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return raw
    m = re.search(r"entry/(\d+)", raw)
    if m:
        return m.group(1)
    m = re.search(r"\d+", raw)
    if m:
        return m.group(0)
    return raw

def _next_gameweek(bootstrap: Dict[str, Any]) -> int:
    for e in bootstrap.get("events", []):
        if e.get("is_next"):
            return e["id"]
    for e in bootstrap.get("events", []):
        if not e.get("finished"):
            return e["id"]
    return 1

_LEAGUE_AVG: Optional[Dict[str, Dict[str, float]]] = None
_LEAGUE_AVG_TS: float = 0.0

def _league_averages() -> Dict[str, Dict[str, float]]:
    global _LEAGUE_AVG, _LEAGUE_AVG_TS
    if _LEAGUE_AVG is not None and (time.time() - _LEAGUE_AVG_TS) < 300:
        return _LEAGUE_AVG

    bootstrap = _get_bootstrap()
    sums = {pos: {"xg": 0.0, "xa": 0.0, "xgc": 0.0, "saves": 0.0, "n": 0} for pos in POS_COUNTS}
    for e in bootstrap.get("elements", []):
        pos = POS_MAP.get(e["element_type"])
        if not pos:
            continue
        if _to_float(e.get("minutes")) < 180:
            continue
        sums[pos]["xg"] += min(_to_float(e.get("expected_goals_per_90")), 5.0)
        sums[pos]["xa"] += min(_to_float(e.get("expected_assists_per_90")), 5.0)
        sums[pos]["xgc"] += min(_to_float(e.get("expected_goals_conceded_per_90")), 5.0)
        sums[pos]["saves"] += min(_to_float(e.get("saves_per_90")), 12.0)
        sums[pos]["n"] += 1

    _LEAGUE_AVG = {
        pos: {k: sums[pos][k] / (sums[pos]["n"] or 1) for k in ("xg", "xa", "xgc", "saves")}
        for pos in sums
    }
    _LEAGUE_AVG_TS = time.time()
    return _LEAGUE_AVG

def _team_strength(team: Dict[str, Any], away: bool, which: str) -> float:
    suffix = "away" if away else "home"
    if which == "def":
        val = team.get(f"strength_defence_{suffix}")
    else:
        val = team.get(f"strength_attack_{suffix}")
    if val in (None, 0):
        val = team.get(f"strength_overall_{suffix}")
    if val in (None, 0):
        val = 3.0
    return float(val)

def _build_fixture_lookup(bootstrap: Optional[Dict[str, Any]] = None) -> Dict[int, List[Dict[str, Any]]]:
    if bootstrap is None:
        bootstrap = _get_bootstrap()
    teams = {t["id"]: t for t in bootstrap.get("teams", [])}

    try:
        fixtures = _get_fixtures()
    except Exception:
        return {}

    lookup: Dict[int, List[Dict[str, Any]]] = {}
    for f in fixtures:
        h, a = f["team_h"], f["team_a"]
        event = f.get("event")
        home_fx = {
            "event": event,
            "is_home": True,
            "opponent": a,
            "difficulty": f.get("team_h_difficulty") or 3,
            "opp_strength_def": _team_strength(teams.get(a, {}), away=True, which="def"),
            "opp_strength_att": _team_strength(teams.get(a, {}), away=True, which="att"),
        }
        away_fx = {
            "event": event,
            "is_home": False,
            "opponent": h,
            "difficulty": f.get("team_a_difficulty") or 3,
            "opp_strength_def": _team_strength(teams.get(h, {}), away=False, which="def"),
            "opp_strength_att": _team_strength(teams.get(h, {}), away=False, which="att"),
        }
        lookup.setdefault(h, []).append(home_fx)
        lookup.setdefault(a, []).append(away_fx)

    for t in lookup:
        lookup[t].sort(key=lambda x: x["event"] if x["event"] is not None else 999)
    return lookup

def _expected_minute_fraction(p: Dict[str, Any], status: str) -> float:
    chance = p.get("chance_of_playing_next_round")
    if chance is None:
        chance = p.get("chance_of_playing_this_round")
    if chance is not None:
        frac = _to_float(chance) / 100.0
    else:
        starts = _to_float(p.get("starts_per_90"))
        if starts >= 0.9:
            frac = 0.94
        elif starts >= 0.6:
            frac = 0.75
        elif starts >= 0.3:
            frac = 0.45
        else:
            frac = 0.20
    if status == "d":
        frac *= 0.5
    return max(0.0, min(1.0, frac))

def _risk_adjust(p: Dict[str, Any], xp: float, risk: str) -> float:
    prof = _risk_profile(risk)
    if xp <= 0 or prof["ow_weight"] == prof["threat_weight"] == prof["floor_weight"] == 0.0:
        return xp

    ow = _to_float(p.get("selected_by_percent"))
    ow_score = max(-1.0, min(1.0, (ow - 15.0) / 20.0))      
    threat = _to_float(p.get("threat"))
    threat_score = max(0.0, min(1.0, threat / 300.0))        
    floor = _expected_minute_fraction(p, p.get("status", "a"))

    adj = (
        prof["ow_weight"] * ow_score
        + prof["threat_weight"] * threat_score
        + prof["floor_weight"] * (floor - 0.7)
    )
    return max(0.0, xp + adj)

def _xp_for_fixture(p: Dict[str, Any], f: Dict[str, Any], emin: float, pos_id: int) -> float:
    avg = _league_averages().get(POS_MAP.get(pos_id, "MID"), {"xg": 0.3, "xa": 0.2, "xgc": 1.3, "saves": 0.5})
    minutes = _to_float(p.get("minutes"))

    def _reg(raw: Any, prior: float) -> float:
        raw = min(_to_float(raw), 5.0)
        return (raw * minutes + prior * PRIOR_MINUTES) / (minutes + PRIOR_MINUTES)

    xg90 = _reg(p.get("expected_goals_per_90"), avg["xg"])
    xa90 = _reg(p.get("expected_assists_per_90"), avg["xa"])
    xgc90 = _reg(p.get("expected_goals_conceded_per_90"), avg["xgc"])
    saves90 = _reg(p.get("saves_per_90"), avg["saves"])

    def_adj = 3.0 / max(_to_float(f.get("opp_strength_def")), 1.0)
    att_adj = max(_to_float(f.get("opp_strength_att")), 1.0) / 3.0
    venue_att = 1.08 if f.get("is_home") else 0.95
    venue_def = 0.95 if f.get("is_home") else 1.05

    frac = emin / 90.0

    xg = xg90 * frac * def_adj * venue_att
    xa = xa90 * frac * def_adj * venue_att
    attack_pts = xg * GOAL_PTS.get(pos_id, 4) + xa * 3.0

    xgc = xgc90 * frac * att_adj * venue_def
    p_cs = math.exp(-xgc) if xgc < 10 else 0.0
    cs_pts = CS_PTS.get(pos_id, 0) * p_cs * min(emin / 60.0, 1.0)

    conceded_pts = -0.5 * xgc if pos_id in (1, 2) else 0.0
    saves_pts = saves90 * frac / 3.0 if pos_id == 1 else 0.0

    minutes_pts = 2.0 if emin >= 60 else (1.0 if emin > 0 else 0.0)

    bonus_pts = (0.7 if pos_id in (3, 4) else 0.4) * frac
    card_pts = -0.12 if pos_id in (2, 3) else -0.05

    return max(minutes_pts + attack_pts + cs_pts + conceded_pts + saves_pts + bonus_pts + card_pts, 0.0)

def _player_xp(p: Dict[str, Any], fixture_lookup: Dict[int, List[Dict[str, Any]]], event: Optional[int] = None, risk: str = "balanced") -> Tuple[float, str]:
    status = p.get("status", "a")
    chance_val = p.get("chance_of_playing_next_round")

    if status == "i":
        return 0.0, "Injured"
    elif status == "s":
        return 0.0, "Suspended"
    elif status in ("u", "n"):
        return 0.0, "Unavailable"

    pos_id = p.get("element_type")
    team_id = p.get("team")
    min_frac = _expected_minute_fraction(p, status)

    fixtures = fixture_lookup.get(team_id, [])
    if event is not None:
        target = [f for f in fixtures if f.get("event") == event]
        if not target:
            return 0.0, "Blank"
    else:
        target = fixtures[:1] if fixtures else []

    if not target:
        return 0.0, "Blank"

    note = "Available"
    if status == "d":
        note = f"{chance_val}% Chance" if chance_val is not None else "Doubtful"
    elif chance_val is not None and chance_val < 100:
        note = f"{chance_val}% Chance"

    our_total = sum(_xp_for_fixture(p, f, 90.0 * min_frac, pos_id) for f in target)

    ep_next = _to_float(p.get("ep_next"))
    if ep_next > 0:
        xp = (1.0 - EP_BLEND) * our_total + EP_BLEND * ep_next
    else:
        xp = our_total

    xp = _risk_adjust(p, xp, risk)
    return round(max(xp, 0.0), 2), note

def get_upcoming_gameweek() -> Dict[str, Any]:
    bootstrap = _get_bootstrap()
    gw = _next_gameweek(bootstrap)
    for event in bootstrap.get("events", []):
        if event["id"] == gw:
            return {
                "id": gw,
                "name": event.get("name") or f"Gameweek {gw}",
                "deadline_time": event.get("deadline_time"),
            }
    return {"id": gw, "name": f"Gameweek {gw}", "deadline_time": None}

def get_free_transfers(manager_id: str, target_gw: Optional[int] = None) -> int:
    manager_id = _clean_manager_id(manager_id)
    try:
        entry = requests.get(f"{BASE_URL}/entry/{manager_id}/", timeout=10).json()
        started_event = int(entry.get("started_event") or 1)
        if target_gw is None:
            target_gw = _next_gameweek(_get_bootstrap())

        history = requests.get(f"{BASE_URL}/entry/{manager_id}/history/", timeout=10).json()
        transfers = requests.get(f"{BASE_URL}/entry/{manager_id}/transfers/", timeout=10).json()

        transfers_per_event: Dict[int, int] = {}
        for t in transfers:
            ev = t.get("event")
            if ev is not None:
                transfers_per_event[int(ev)] = transfers_per_event.get(int(ev), 0) + 1

        reset_events = set()
        for c in history.get("chips", []):
            if (c.get("name") or "").lower() in ("wildcard", "freehit"):
                reset_events.add(int(c.get("event") or 0))

        ft = 1
        for ev in range(started_event, int(target_gw)):
            if ev in reset_events:
                ft = 1
            else:
                ft = min(ft + 1, 5)
                made = transfers_per_event.get(ev, 0)
                ft = max(ft - made, 0)

        if int(target_gw) in reset_events:
            ft = 1
        else:
            made_this_week = transfers_per_event.get(int(target_gw), 0)
            ft = max(ft - made_this_week, 0)

        return ft
    except Exception:
        return 1

def _pool_entry(e: Dict[str, Any], teams_by_id: Dict[int, str], xp: float, note: str, pos: str) -> Dict[str, Any]:
    return {
        "id": e["id"],
        "name": f"{e['first_name']} {e['second_name']}",
        "team_id": e["team"],
        "team": teams_by_id.get(e["team"], "?"),
        "position": pos,
        "price": e["now_cost"] / 10.0,
        "xp": xp,
        "status": note,
    }

def _solve_squad(
    pool: List[Dict[str, Any]],
    budget: float,
    must_include_ids: Optional[set] = None,
    hit_config: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[List[int]], Optional[float]]:
    if not HAS_PULP:
        return None, None

    by_id = {p["id"]: p for p in pool}
    ids = list(by_id.keys())
    x = pulp.LpVariable.dicts("x", ids, cat="Binary")

    prob = pulp.LpProblem("fpl_squad", pulp.LpMaximize)

    prob += pulp.lpSum(by_id[pid]["price"] * x[pid] for pid in ids) <= budget, "budget"

    for pos, cnt in POS_COUNTS.items():
        prob += pulp.lpSum(x[pid] for pid in ids if by_id[pid]["position"] == pos) == cnt, f"pos_{pos}"

    for t in {by_id[pid]["team_id"] for pid in ids}:
        prob += pulp.lpSum(x[pid] for pid in ids if by_id[pid]["team_id"] == t) <= 3, f"team_{t}"

    xp_expr = pulp.lpSum(by_id[pid]["xp"] * x[pid] for pid in ids)

    if must_include_ids is not None and hit_config is not None:
        transfers = pulp.lpSum((1 - x[pid]) for pid in must_include_ids if pid in by_id)
        max_t = hit_config.get("max_transfers")
        if max_t is not None:
            prob += transfers <= max_t, "max_transfers"
        hits = pulp.LpVariable("hits", lowBound=0, cat="Integer")
        prob += hits >= transfers - hit_config["free_transfers"], "hits_lb"
        prob.setObjective(xp_expr - hit_config["hit_cost"] * hits)
    else:
        prob.setObjective(xp_expr)

    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[prob.status] != "Optimal":
        return None, None

    selected = [pid for pid in ids if x[pid].varValue is not None and x[pid].varValue > 0.5]
    return selected, pulp.value(prob.objective)

def score_my_squad(manager_id: str, gw: int, risk: str = "balanced") -> Dict[str, Any]:
    manager_id = _clean_manager_id(manager_id)
    bootstrap = _get_bootstrap()
    players_by_id = {p["id"]: p for p in bootstrap["elements"]}
    teams_by_id = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    fixture_lookup = _build_fixture_lookup(bootstrap)

    picks_data = None
    picks_gw = None
    for attempt in range(gw, 0, -1):
        resp = requests.get(f"{BASE_URL}/entry/{manager_id}/event/{attempt}/picks/", timeout=10)
        if resp.status_code == 200:
            picks_data = resp.json()
            picks_gw = attempt
            break
    if picks_data is None:
        return {"error": f"Could not fetch squad for Manager ID {manager_id}."}

    squad = []
    for pick in picks_data.get("picks", []):
        p_data = players_by_id.get(pick["element"])
        if not p_data:
            continue
        xp, note = _player_xp(p_data, fixture_lookup, event=gw, risk=risk)
        squad.append({
            "player_id": p_data["id"],
            "name": f"{p_data['first_name']} {p_data['second_name']}",
            "team_id": p_data["team"],
            "team": teams_by_id.get(p_data["team"], "?"),
            "position": POS_MAP.get(p_data["element_type"], "?"),
            "price": p_data["now_cost"] / 10.0,
            "xp": xp,
            "status": note,
            "is_captain": pick.get("is_captain", False),
            "is_vice_captain": pick.get("is_vice_captain", False),
            "multiplier": pick.get("multiplier", 1),
            "pick_position": pick.get("position", 0),
        })

    entry_url = f"{BASE_URL}/entry/{manager_id}/"
    entry_resp = requests.get(entry_url, timeout=10)
    entry_data = entry_resp.json() if entry_resp.status_code == 200 else {}

    return {
        "team_name": entry_data.get("name", "My Team"),
        "bank": picks_data.get("entry_history", {}).get("bank", 0) / 10.0,
        "team_value": picks_data.get("entry_history", {}).get("value", 1000) / 10.0,
        "gameweek_used": gw,
        "squad_gameweek": picks_gw,
        "squad": squad
    }

def suggest_transfers_for_custom_squad(
    squad: List[Dict[str, Any]], 
    bank: float, 
    free_transfers: int, 
    eval_chips: List[str] = [],
    event: Optional[int] = None, 
    risk: str = "balanced"
) -> Dict[str, Any]:
    bootstrap = _get_bootstrap()
    fixture_lookup = _build_fixture_lookup(bootstrap)
    teams_by_id = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    elements_by_id = {e["id"]: e for e in bootstrap["elements"]}
    
    if event is None:
        event = _next_gameweek(bootstrap)

    hit_cost = _risk_profile(risk)["hit_cost"]
    current_ids = [p["player_id"] for p in squad]
    pool = []
    seen = set()
    
    for pid in current_ids:
        e = elements_by_id.get(pid)
        if not e:
            continue
        pos = POS_MAP.get(e["element_type"])
        xp, note = _player_xp(e, fixture_lookup, event=event, risk=risk)
        pool.append(_pool_entry(e, teams_by_id, xp, note, pos))
        seen.add(pid)

    for e in bootstrap["elements"]:
        if e["id"] in seen:
            continue
        pos = POS_MAP.get(e["element_type"])
        if not pos:
            continue
        xp, note = _player_xp(e, fixture_lookup, event=event, risk=risk)
        if note in ("OUT", "Blank", "Injured", "Suspended", "Unavailable"):
            continue
        pool.append(_pool_entry(e, teams_by_id, xp, note, pos))
        seen.add(e["id"])

    current_cost = sum(elements_by_id[pid]["now_cost"] for pid in current_ids if pid in elements_by_id) / 10.0
    budget = current_cost + bank
    pool_by_id = {p["id"]: p for p in pool}

    def _get_moves(selected_ids, is_unlimited):
        if not selected_ids: 
            return [], 0, 0.0, 0.0
        selected_set = set(selected_ids)
        sold = [pid for pid in current_ids if pid not in selected_set]
        bought = [pid for pid in selected_ids if pid not in current_ids]
        mvs = []
        for o, i in zip(sold, bought):
            mvs.append({
                "out": pool_by_id[o],
                "in": pool_by_id[i],
                "xp_gain": round(pool_by_id[i]["xp"] - pool_by_id[o]["xp"], 2),
                "cost": round(pool_by_id[i]["price"] - pool_by_id[o]["price"], 2)
            })
        hits = 0 if is_unlimited else max(0, len(mvs) - free_transfers)
        tot_gain = sum(m["xp_gain"] for m in mvs)
        net_gain = round(tot_gain - hit_cost * hits, 2)
        cost_chg = round(sum(m["cost"] for m in mvs), 2)
        return mvs, hits, net_gain, cost_chg

    # ==============================================================
    # 1. Universe A: Standard Transfers Optimization (Takes Hit Penalty)
    # ==============================================================
    std_selected, _ = _solve_squad(
        pool, budget=budget, must_include_ids=set(current_ids),
        hit_config={"free_transfers": free_transfers, "hit_cost": hit_cost, "max_transfers": free_transfers + MAX_HIT_TRANSFERS}
    )
    std_moves, std_hits, std_net, std_cost = _get_moves(std_selected, False)
    if std_net <= 0:
        std_moves, std_hits, std_net, std_cost = [], 0, 0.0, 0.0

    # ==============================================================
    # 2. Universe B: Unlimited Transfers (For Wildcard / Free Hit)
    # ==============================================================
    unl_moves, unl_hits, unl_net, unl_cost = [], 0, 0.0, 0.0
    if any(c in eval_chips for c in ("Wildcard", "Free Hit")):
        unl_selected, _ = _solve_squad(
            pool, budget=budget, must_include_ids=set(current_ids),
            hit_config={"free_transfers": 15, "hit_cost": 0.0, "max_transfers": 15}
        )
        unl_moves, unl_hits, unl_net, unl_cost = _get_moves(unl_selected, True)

    # ==============================================================
    # 3. Project Universe A Squad (For BB and TC Eval)
    # ==============================================================
    sold_ids = [m["out"]["id"] for m in std_moves]
    bought_ids = [m["in"]["id"] for m in std_moves]
    std_squad = [p for p in squad if p["player_id"] not in sold_ids]
    
    current_out_statuses = sum(1 for p in squad if p.get("status") in ("Injured", "Suspended", "Unavailable", "OUT"))
    
    for in_id in bought_ids:
        fpl_p = elements_by_id[in_id]
        pos = POS_MAP.get(fpl_p["element_type"])
        xp, note = _player_xp(fpl_p, fixture_lookup, event=event, risk=risk)
        std_squad.append({
            "player_id": fpl_p["id"],
            "name": f"{fpl_p['first_name']} {fpl_p['second_name']}",
            "team_id": fpl_p["team"],
            "team": teams_by_id.get(fpl_p["team"], "?"),
            "position": pos,
            "price": fpl_p["now_cost"] / 10.0,
            "xp": xp,
            "status": note,
            "is_captain": False
        })
        
    std_best_xi = select_starting_xi(std_squad)

    # ==============================================================
    # 4. Ultra-Strict Chip Scoring & Ranking
    # ==============================================================
    chip_scores = {}
    if "Wildcard" in eval_chips:
        chip_scores["Wildcard"] = round(unl_net - std_net, 2)
    if "Free Hit" in eval_chips:
        chip_scores["Free Hit"] = round(unl_net - std_net, 2)
    if "Bench Boost" in eval_chips:
        chip_scores["Bench Boost"] = round(sum(p['xp'] for p in std_best_xi['bench']), 2)
    if "Triple Captain" in eval_chips:
        cap = std_best_xi.get('captain')
        chip_scores["Triple Captain"] = cap.get('xp', 0.0) if cap else 0.0

    ranked_chips = sorted(chip_scores.items(), key=lambda x: x[1], reverse=True)
    
    # Ultra-Strict Compelling Reason Thresholds
    THRESHOLDS = {"Wildcard": 20.0, "Free Hit": 18.0, "Bench Boost": 15.0, "Triple Captain": 10.0}
    
    chip_advice_list = []
    if len(eval_chips) > 1:
        chip_advice_list.append("⚠️ <b>Official FPL Rule:</b> You may only activate 1 chip per Gameweek. The system has ranked your selections below based on mathematical scarcity:")

    recommended_chip = "None (Hold Chips)"
    best_chip_gain = 0.0

    for chip_name, score in ranked_chips:
        threshold = THRESHOLDS.get(chip_name, 99.0)
        passed_threshold = score >= threshold
        
        # Wildcard exception: Lower xP barrier if squad is ravaged by injuries
        if chip_name == "Wildcard" and not passed_threshold:
            if score >= 10.0 and current_out_statuses >= 4:
                passed_threshold = True

        if passed_threshold and recommended_chip == "None (Hold Chips)":
            recommended_chip = chip_name
            best_chip_gain = score
            if chip_name in ("Wildcard", "Free Hit"):
                crisis_msg = f" Your squad has {current_out_statuses} flagged players and a reset yields <b>+{score:.1f} xP</b>." if current_out_statuses >= 4 and chip_name == "Wildcard" else f" Yields a massive <b>+{score:.1f} xP</b> over standard transfers."
                chip_advice_list.append(f"🏆 <b>{chip_name}</b>: Strongly Recommended.{crisis_msg}")
            elif chip_name == "Bench Boost":
                chip_advice_list.append(f"🏆 <b>{chip_name}</b>: Strongly Recommended. Your optimized bench provides a massive <b>+{score:.1f} xP</b>.")
            elif chip_name == "Triple Captain":
                cap_name = cap['name'] if cap else "Captain"
                chip_advice_list.append(f"🏆 <b>{chip_name}</b>: Recommended. <b>{cap_name}</b> has an elite ceiling ({score} xP ➞ <b>{score*3:.1f} xP</b>).")
        else:
            # Failed threshold or lost to a better chip
            if passed_threshold:
                chip_advice_list.append(f"❌ <b>{chip_name}</b>: Hold. Yields +{score:.1f} xP, but FPL limits 1 chip/wk. <b>{recommended_chip}</b> is mathematically superior right now.")
            else:
                chip_advice_list.append(f"❌ <b>{chip_name}</b>: Hold. Only projects <b>+{score:.1f} xP</b>. Save this scarce asset for a compelling Double/Blank Gameweek (requires +{threshold:.1f} xP).")

    # Generate Standard Advice
    n = len(std_moves)
    if n == 0:
        advice = "Hold — no transfers mathematically improve your expected points (xP) after penalties."
    elif std_hits == 0:
        advice = f"Make {n} free transfer(s) — no points hit."
    else:
        advice = f"Make {n} transfer(s), taking {std_hits} hit(s) (-{int(hit_cost * std_hits)} pts) for a net +{std_net:.1f} xP."

    return {
        "transfers": std_moves,
        "standard_transfers": std_moves,
        "wildcard_transfers": unl_moves,
        "hits": std_hits,
        "net_gain": std_net,
        "cost_change": std_cost,
        "hit_advice": advice,
        "chip_evaluations": chip_advice_list,
        "recommended_chip": recommended_chip
    }

def rank_players_by_xp(position: str = None, max_price: float = None, limit: int = 20, event: Optional[int] = None, risk: str = "balanced") -> Dict[str, Any]:
    bootstrap = _get_bootstrap()
    fixture_lookup = _build_fixture_lookup(bootstrap)
    teams_by_id = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    if event is None:
        event = _next_gameweek(bootstrap)

    ranked = []
    for p in bootstrap["elements"]:
        pos = POS_MAP.get(p["element_type"])
        price = p["now_cost"] / 10.0
        if position and pos != position:
            continue
        if max_price and max_price > 0 and price > max_price:
            continue

        xp, note = _player_xp(p, fixture_lookup, event=event, risk=risk)
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

def _pid(p: Dict[str, Any]) -> Any:
    return p.get("player_id", p.get("id"))

def select_starting_xi(squad: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_pos = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in squad:
        by_pos.setdefault(p.get("position"), []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: x.get("xp", 0), reverse=True)

    best_xi, best_score, best_form = None, -1.0, None
    for d, m, f in VALID_FORMATIONS:
        if len(by_pos["GK"]) < 1 or len(by_pos["DEF"]) < d or len(by_pos["MID"]) < m or len(by_pos["FWD"]) < f:
            continue
        xi = by_pos["GK"][:1] + by_pos["DEF"][:d] + by_pos["MID"][:m] + by_pos["FWD"][:f]
        score = sum(p.get("xp", 0) for p in xi)
        if score > best_score:
            best_score, best_xi, best_form = score, xi, (d, m, f)

    if best_xi is None:
        return {"xi": [], "bench": [], "formation": None, "captain": None, "vice_captain": None, "total_xp": 0.0}

    captain = next((p for p in squad if p.get("is_captain")), None)
    vice = next((p for p in squad if p.get("is_vice_captain")), None)

    flagged = [p for p in (captain, vice) if p is not None]
    flagged_ids = {_pid(p) for p in flagged}
    for fp in flagged:
        if any(_pid(p) == _pid(fp) for p in best_xi):
            continue
        pos = fp.get("position")
        same_pos = [p for p in best_xi if p.get("position") == pos and _pid(p) not in flagged_ids]
        if not same_pos:
            same_pos = [p for p in best_xi if p.get("position") == pos]
        if not same_pos:
            continue
        weakest = min(same_pos, key=lambda x: x.get("xp", 0))
        best_xi = [fp if _pid(p) == _pid(weakest) else p for p in best_xi]

    if captain is None:
        captain = max(best_xi, key=lambda x: x.get("xp", 0))
    if vice is None or _pid(vice) == _pid(captain):
        others = sorted((p for p in best_xi if _pid(p) != _pid(captain)), key=lambda x: x.get("xp", 0), reverse=True)
        vice = others[0] if others else None

    xi_ids = {_pid(p) for p in best_xi}
    bench = [p for p in squad if _pid(p) not in xi_ids]
    bench_gk = [p for p in bench if p.get("position") == "GK"]
    bench_out = [p for p in bench if p.get("position") != "GK"]
    bench_out.sort(key=lambda x: x.get("xp", 0), reverse=True)

    pos_order = {"GK": 1, "DEF": 2, "MID": 3, "FWD": 4}
    xi_sorted = sorted(best_xi, key=lambda x: (pos_order.get(x.get("position"), 5), -x.get("xp", 0)))

    return {
        "xi": xi_sorted,
        "bench": bench_gk + bench_out,
        "formation": best_form,
        "captain": captain,
        "vice_captain": vice,
        "total_xp": round(sum(p.get("xp", 0) for p in best_xi), 2),
    }
