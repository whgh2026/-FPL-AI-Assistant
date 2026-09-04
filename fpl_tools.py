import math
import os
import json
import re
import time
import requests
from typing import Dict, Any, List, Tuple, Optional
import dateutil.parser

try:
    import streamlit as st
except ImportError:
    # Allow the CLI pipeline scripts (snapshot/ingest/auto-tune) to run without
    # Streamlit installed (e.g. in GitHub Actions).
    class _DummyStreamlit:
        @staticmethod
        def cache_data(*args, **kwargs):
            def decorator(func):
                return func
            return decorator
    st = _DummyStreamlit()

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
WILDCARD_SCARCITY_COST = 25.0
ROLL_TRANSFER_VALUE = 1.5

RISK_PROFILES = {
    "conservative": {"hit_cost": 8.0, "ow_weight": 0.8, "threat_weight": 0.0, "floor_weight": 1.0, "ft_friction": 2.0},
    "balanced":     {"hit_cost": 6.0, "ow_weight": 0.0, "threat_weight": 0.0, "floor_weight": 0.0, "ft_friction": 1.5},
    "aggressive":   {"hit_cost": 3.0, "ow_weight": -0.8, "threat_weight": 1.2, "floor_weight": -0.2, "ft_friction": 0.5},
    # Competitive modes: defend a lead (shield high-ownership assets) vs chase a
    # leader (hunt low-ownership high-xGI differentials).
    "rank_protecting": {"hit_cost": 4.0, "ow_weight": 0.0, "threat_weight": 0.0, "floor_weight": 0.5, "shield_weight": 0.8, "ft_friction": 2.0},
    "rank_chasing":    {"hit_cost": 3.0, "ow_weight": 0.0, "threat_weight": 0.0, "floor_weight": 0.0, "hunt_weight": 0.6, "ft_friction": 0.5},
}

# Display-label aliases so the UI can pass human-readable mode names.
_RISK_ALIASES = {
    "rank protecting (shield)": "rank_protecting",
    "rank protecting": "rank_protecting",
    "shield": "rank_protecting",
    "rank chasing (hunting)": "rank_chasing",
    "rank chasing": "rank_chasing",
    "hunting": "rank_chasing",
}

# Objective bonus for a cohesive budget-defender rotation pair (complementary
# easy fixtures across the 4-GW horizon).
ROTATION_PAIR_BONUS = 0.35
ROTATION_MAX_PRICE = 4.5
ROTATION_EASY_FDR = 2

# ======================================================================
# FUTURE COMMERCIAL ROADMAP — remaining optimization edges (next phase)
# ======================================================================
# 1. Price Change Predictor: the official tool reports a "velocity" that can
#    exceed 100% (a change is expected at the next overnight update). Wire the
#    velocity threshold into get_market_movers() so we act on near-certain rises
#    and falls rather than raw transfer volume alone.
# 2. Effective Ownership (EO): discount raw selected_by_percent against an
#    estimated dead-team baseline to recover TRUE competitive ownership — the
#    ownership that actually matters in a specific mini-league. This makes the
#    Rank Protecting / Rank Chasing modes sharper.
# 3. Rotation pairing can be upgraded from a static FDR-complement bonus to a
#    full 2-player rotation optimizer with budget-defender rotation constraints.
# 4. Auto-sub simulation can be made exact by modelling 0-minute outcomes per
#    starter and validating every bench permutation against formation minima.
# ======================================================================

POS_MAP = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
GOAL_PTS = {1: 10, 2: 6, 3: 5, 4: 4}
CS_PTS = {1: 4, 2: 4, 3: 1, 4: 0}
POS_COUNTS = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
# 2026/27 Defensive Contributions (CBIT/CBIRT): expected involvement rates and
# thresholds used to model the +2 point bonus (DEF: 10 CBIT, MID/FWD: 12 CBIRT).
DEFCON_BASE_PER90 = {2: 8.0, 3: 5.0, 4: 2.5}
DEFCON_THRESHOLD = {2: 10, 3: 12, 4: 12}
# Transfer friction: positional penalty applied per player sold, so the solver
# won't churn a goalkeeper (or, to a lesser extent, a defender) for a marginal gain.
TRANSFER_FRICTION = {"GK": 1.5, "DEF": 0.5, "MID": 0.1, "FWD": 0.1}
# Rolling 4-gameweek horizon weights for multi-week xP projection.
HORIZON_WEIGHTS = [1.0, 0.85, 0.70, 0.55]
OUT_STATUSES = {"i", "s", "u", "n"}

# Official FPL formation constraints: exactly 1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD,
# with 10 outfield players + 1 GK = 11 starters total.
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
    key = (risk or "balanced").lower().strip()
    key = _RISK_ALIASES.get(key, key)
    return RISK_PROFILES.get(key, RISK_PROFILES["balanced"])

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

# ------------------------------------------------------------------
# Live odds (The Odds API) integration
# ------------------------------------------------------------------
_ODDS_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}

# Normalised Odds-API club name -> FPL short_name (used to map markets to squads).
_CLUB_ALIASES = {
    "arsenal": "ARS",
    "aston villa": "AVL",
    "bournemouth": "BOU",
    "brentford": "BRE",
    "brighton": "BHA",
    "brighton and hove albion": "BHA",
    "chelsea": "CHE",
    "crystal palace": "CRY",
    "everton": "EVE",
    "fulham": "FUL",
    "ipswich": "IPS",
    "ipswich town": "IPS",
    "leicester": "LEI",
    "leicester city": "LEI",
    "liverpool": "LIV",
    "man city": "MCI",
    "manchester city": "MCI",
    "man utd": "MUN",
    "manchester united": "MUN",
    "manchester utd": "MUN",
    "newcastle": "NEW",
    "newcastle united": "NEW",
    "nottingham forest": "NFO",
    "southampton": "SOU",
    "tottenham": "TOT",
    "tottenham hotspur": "TOT",
    "spurs": "TOT",
    "west ham": "WHU",
    "west ham united": "WHU",
    "wolves": "WOL",
    "wolverhampton": "WOL",
    "wolverhampton wanderers": "WOL",
}


def _get_odds_api_key() -> Optional[str]:
    key = os.getenv("ODDS_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
        key = st.secrets.get("ODDS_API_KEY")
    except Exception:
        key = None
    return key or None


def _canonical_club(name: str) -> Optional[str]:
    if not name:
        return None
    n = name.lower().strip()
    n = re.sub(r"[^a-z ]", " ", n)
    n = " ".join(n.split())
    if n in _CLUB_ALIASES:
        return _CLUB_ALIASES[n]
    for alias, code in _CLUB_ALIASES.items():
        if alias in n:
            return code
    return None


def _fetch_market_win_probs(bootstrap: Optional[Dict[str, Any]] = None) -> Dict[int, Dict[int, float]]:
    """Return {team_id: {opponent_id: implied win probability}} for upcoming fixtures.

    Fetches live h2h odds from The Odds API and converts decimal prices into
    bookmaker-margin-adjusted implied win probabilities. Returns {} on failure or
    when ODDS_API_KEY is not configured.
    """
    global _ODDS_CACHE
    now = time.time()
    if _ODDS_CACHE["data"] is not None and now - _ODDS_CACHE["ts"] < 600:
        return _ODDS_CACHE["data"]

    result: Dict[int, Dict[int, float]] = {}
    key = _get_odds_api_key()
    if not key:
        _ODDS_CACHE["data"] = result
        _ODDS_CACHE["ts"] = now
        return result

    if bootstrap is None:
        bootstrap = _get_bootstrap()
    short_by_id = {t["id"]: t.get("short_name") for t in bootstrap.get("teams", [])}
    id_by_short = {v: k for k, v in short_by_id.items() if v}

    try:
        url = (
            "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
            f"?apiKey={key}&regions=uk,eu&markets=h2h"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        matches = resp.json()
    except Exception:
        _ODDS_CACHE["data"] = result
        _ODDS_CACHE["ts"] = now
        return result

    for m in matches:
        home = _canonical_club(m.get("home_team", ""))
        away = _canonical_club(m.get("away_team", ""))
        h_id = id_by_short.get(home) if home else None
        a_id = id_by_short.get(away) if away else None
        if h_id is None or a_id is None:
            continue

        h_prices: List[float] = []
        a_prices: List[float] = []
        for bm in m.get("bookmakers", []):
            for mk in bm.get("markets", []):
                if mk.get("key") != "h2h":
                    continue
                for out in mk.get("outcomes", []):
                    price = _to_float(out.get("price"))
                    if price <= 0:
                        continue
                    code = _canonical_club(out.get("name", ""))
                    if code == home:
                        h_prices.append(price)
                    elif code == away:
                        a_prices.append(price)

        if not h_prices or not a_prices:
            continue
        h_avg = sum(h_prices) / len(h_prices)
        a_avg = sum(a_prices) / len(a_prices)
        h_imp = 1.0 / h_avg
        a_imp = 1.0 / a_avg
        total = h_imp + a_imp
        if total <= 0:
            continue
        result.setdefault(h_id, {})[a_id] = h_imp / total
        result.setdefault(a_id, {})[h_id] = a_imp / total

    _ODDS_CACHE["data"] = result
    _ODDS_CACHE["ts"] = now
    return result


def _build_fixture_lookup(bootstrap: Optional[Dict[str, Any]] = None) -> Dict[int, List[Dict[str, Any]]]:
    if bootstrap is None:
        bootstrap = _get_bootstrap()
    teams = {t["id"]: t for t in bootstrap.get("teams", [])}

    try:
        fixtures = _get_fixtures()
    except Exception:
        return {}

    win_probs = _fetch_market_win_probs(bootstrap)

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
            "win_prob": win_probs.get(h, {}).get(a),
        }
        away_fx = {
            "event": event,
            "is_home": False,
            "opponent": h,
            "difficulty": f.get("team_a_difficulty") or 3,
            "opp_strength_def": _team_strength(teams.get(h, {}), away=False, which="def"),
            "opp_strength_att": _team_strength(teams.get(h, {}), away=False, which="att"),
            "win_prob": win_probs.get(a, {}).get(h),
        }
        lookup.setdefault(h, []).append(home_fx)
        lookup.setdefault(a, []).append(away_fx)

    for t in lookup:
        lookup[t].sort(key=lambda x: x["event"] if x["event"] is not None else 999)
    return lookup


def _fixture_traffic_lights(team_id: int, fixture_lookup: Dict[int, List[Dict[str, Any]]], start_event: int, n: int = 4) -> str:
    """Return a 4-GW traffic-light string, e.g. '[🟢 🟡 🔴 🟢]'.

    Uses market-implied win probability when available, falling back to FDR.
    """
    fixtures = fixture_lookup.get(team_id, [])
    lights: List[str] = []
    for i in range(n):
        ev = start_event + i
        fx = next((x for x in fixtures if x.get("event") == ev), None)
        if fx is None:
            lights.append("⚪")
            continue
        wp = fx.get("win_prob")
        if wp is not None:
            if wp > 0.5:
                lights.append("🟢")
            elif wp >= 0.3:
                lights.append("🟡")
            else:
                lights.append("🔴")
        else:
            fdr = fx.get("difficulty") or 3
            if fdr <= 2:
                lights.append("🟢")
            elif fdr == 3:
                lights.append("🟡")
            else:
                lights.append("🔴")
    return "[" + " ".join(lights) + "]"

def _expected_minute_fraction(p: Dict[str, Any], status: str) -> float:
    # Availability haircut using the official FPL API flag (chance_of_playing_*).
    # None/100 -> retain 100%; 75 -> 0.75; 50 -> 0.50; 25 -> 0.25; 0 -> 0.0.
    # (Injured/suspended/unavailable statuses are already zeroed upstream.)
    chance = p.get("chance_of_playing_next_round")
    if chance is None:
        chance = p.get("chance_of_playing_this_round")
    if chance is not None:
        frac = _to_float(chance) / 100.0
    else:
        frac = 1.0
    return max(0.0, min(1.0, frac))

def _risk_adjust(p: Dict[str, Any], xp: float, risk: str) -> float:
    if xp <= 0:
        return xp

    # Community transfer momentum (applies to every risk profile): net transfers-in
    # flag bandwagons (price rises, fixture form) while net transfers-out flag
    # injury exits / price drops. Kept deliberately small.
    tin = _to_float(p.get("transfers_in_event"))
    tout = _to_float(p.get("transfers_out_event"))
    # Goalkeeper crowd momentum is noisy — nerf it to a tighter band so keeper xP
    # isn't swung around by bandwagon transfer volume.
    is_gk = p.get("element_type") == 1 or p.get("position") == "GK"
    cap = 0.1 if is_gk else 0.3
    momentum = max(-cap, min(cap, (tin - tout) / 100000.0))
    xp = max(0.0, xp + momentum)

    prof = _risk_profile(risk)
    ow = _to_float(p.get("selected_by_percent"))
    adj = 0.0

    if prof.get("ow_weight", 0.0) != 0.0:
        ow_score = max(-1.0, min(1.0, (ow - 15.0) / 20.0))
        adj += prof["ow_weight"] * ow_score

    if prof.get("threat_weight", 0.0) != 0.0:
        threat_score = max(0.0, min(1.0, _to_float(p.get("threat")) / 300.0))
        adj += prof["threat_weight"] * threat_score

    if prof.get("floor_weight", 0.0) != 0.0:
        floor = _expected_minute_fraction(p, p.get("status", "a"))
        adj += prof["floor_weight"] * (floor - 0.7)

    # Rank Protecting (Shield): overweight high-ownership assets (>30%) to
    # minimise rank volatility when defending a mini-league lead.
    if prof.get("shield_weight", 0.0) != 0.0:
        shield = max(0.0, min(1.0, (ow - 30.0) / 40.0))
        adj += prof["shield_weight"] * shield

    # Rank Chasing (Hunting): penalise template ownership and overweight
    # low-ownership (<12%) high-xGI differentials to maximise upside when chasing.
    if prof.get("hunt_weight", 0.0) != 0.0:
        xgi = _to_float(p.get("expected_goals_per_90")) + _to_float(p.get("expected_assists_per_90"))
        template_penalty = min(1.0, ow / 50.0)
        low_ow_boost = max(0.0, (12.0 - ow) / 12.0) * min(xgi / 0.6, 1.0)
        adj += prof["hunt_weight"] * (low_ow_boost - template_penalty)

    return max(0.0, xp + adj)

def _poisson_survival(threshold: int, lam: float) -> float:
    """P(X >= threshold) for X ~ Poisson(lam), via the lower tail CDF."""
    if lam <= 0.0 or threshold <= 0:
        return 0.0
    term = math.exp(-lam)
    cdf = term
    for i in range(1, threshold):
        term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def _defcon_expected_pts(p: Dict[str, Any], emin: float, pos_id: int) -> float:
    """Expected points from the 2026/27 Defensive Contribution bonus.

    DEF: +2 points for reaching 10 CBIT actions in a match.
    MID/FWD: +2 points for reaching 12 CBIRT actions in a match.
    """
    if pos_id not in DEFCON_THRESHOLD:
        return 0.0
    frac = emin / 90.0
    if frac <= 0.0:
        return 0.0
    base = DEFCON_BASE_PER90.get(pos_id, 0.0)
    # Influence (ICC) captures tackles/interceptions/clearances, so we nudge the
    # involvement rate up for defensively busy roles.
    influence = _to_float(p.get("influence"))
    rate = base * (0.6 + 0.4 * min(influence / 60.0, 2.0))
    p_meet = _poisson_survival(DEFCON_THRESHOLD[pos_id], rate * frac)
    return 2.0 * p_meet


_WEIGHTS_CACHE: Optional[Dict[str, float]] = None


def _load_weights() -> Dict[str, float]:
    """Load tunable weights from weights.json (defaulting to 1.0 on any miss)."""
    global _WEIGHTS_CACHE
    if _WEIGHTS_CACHE is not None:
        return _WEIGHTS_CACHE
    defaults = {"global_xP_modifier": 1.0, "home_advantage": 1.0, "clean_sheet_confidence": 1.0}
    weights = dict(defaults)
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weights.json")
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for k in defaults:
                try:
                    weights[k] = float(data.get(k, defaults[k]))
                except (TypeError, ValueError):
                    weights[k] = defaults[k]
    except Exception:
        pass
    _WEIGHTS_CACHE = weights
    return weights


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

    # Live market scaling: blend bookmaker-implied win probability with static FDR.
    win_prob = f.get("win_prob")
    if win_prob is not None:
        market_att = 0.6 + 0.8 * win_prob
        market_def = 1.4 - 0.8 * win_prob
        def_adj *= market_att
        att_adj *= market_def

    _w = _load_weights()
    home_adv = _w["home_advantage"]
    venue_att = (1.0 + 0.08 * home_adv) if f.get("is_home") else (1.0 - 0.05 * home_adv)
    venue_def = (1.0 - 0.05 * home_adv) if f.get("is_home") else (1.0 + 0.05 * home_adv)

    frac = emin / 90.0

    xg = xg90 * frac * def_adj * venue_att
    xa = xa90 * frac * def_adj * venue_att
    attack_pts = xg * GOAL_PTS.get(pos_id, 4) + xa * 3.0

    xgc = xgc90 * frac * att_adj * venue_def
    p_cs = math.exp(-xgc) if xgc < 10 else 0.0
    cs_pts = CS_PTS.get(pos_id, 0) * p_cs * min(emin / 60.0, 1.0) * _w["clean_sheet_confidence"]

    conceded_pts = -0.5 * xgc if pos_id in (1, 2) else 0.0
    saves_pts = saves90 * frac / 3.0 if pos_id == 1 else 0.0

    minutes_pts = 2.0 if emin >= 60 else (1.0 if emin > 0 else 0.0)

    # 2026/27 BPS recalibration:
    #  - being tackled no longer costs BPS (helps attacking mids / wing-backs)
    #  - CBI converted at 1 BPS per 3 actions (slightly lowers centre-back bonus)
    #  - GK saves now carry a much higher BPS weight
    if pos_id == 1:
        bonus_pts = min(2.0, (0.40 + 0.18 * saves90) * frac)
    elif pos_id == 2:
        bonus_pts = 0.34 * frac
    else:
        bonus_pts = 0.72 * frac

    defcon_pts = _defcon_expected_pts(p, emin, pos_id)
    card_pts = -0.12 if pos_id in (2, 3) else -0.05

    return max(minutes_pts + attack_pts + cs_pts + conceded_pts + saves_pts + bonus_pts + defcon_pts + card_pts, 0.0)

def _player_xp_raw(p: Dict[str, Any], fixture_lookup: Dict[int, List[Dict[str, Any]]], event: Optional[int] = None) -> Tuple[float, str]:
    """Raw expected points for a single gameweek, before risk adjustment."""
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

    # Strict minutes gating: block assets with zero minutes played this season
    # (e.g. backup goalkeepers) so the solver never buys a zero-projection player.
    # Skipped during GW1, when no player has accumulated minutes yet.
    if _to_float(p.get("minutes", 0)) <= 0 and (event is None or event > 1):
        return 0.0, "No minutes"

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
        # Availability haircut applied to BOTH the model projection and FPL's own
        # projection, so doubtful assets never display an unadjusted baseline.
        xp = (1.0 - EP_BLEND) * our_total + EP_BLEND * ep_next * min_frac
    else:
        xp = our_total

    return max(xp, 0.0), note


def _player_xp(p: Dict[str, Any], fixture_lookup: Dict[int, List[Dict[str, Any]]], event: Optional[int] = None, risk: str = "balanced") -> Tuple[float, str]:
    raw, note = _player_xp_raw(p, fixture_lookup, event)
    gmod = _load_weights()["global_xP_modifier"]
    return round(max(_risk_adjust(p, raw, risk), 0.0) * gmod, 2), note


def _player_xp_horizon(p: Dict[str, Any], fixture_lookup: Dict[int, List[Dict[str, Any]]], start_event: int, risk: str = "balanced", n: int = 4) -> Tuple[float, str]:
    """Multi-gameweek expected points with geometric decay over the horizon.

    xP_horizon = 1.0*xP(GW) + 0.85*xP(GW+1) + 0.70*xP(GW+2) + 0.55*xP(GW+3).
    Risk adjustment is applied once to the weighted total (it is a single-GW
    market signal). Blank gameweeks contribute 0 without flagging the player.
    """
    status = p.get("status", "a")
    if status == "i":
        return 0.0, "Injured"
    elif status == "s":
        return 0.0, "Suspended"
    elif status in ("u", "n"):
        return 0.0, "Unavailable"

    # Strict minutes gating (backup-GK fix): zero projection for zero-minute assets.
    # Skipped during GW1, when no player has accumulated minutes yet.
    if _to_float(p.get("minutes", 0)) <= 0 and start_event > 1:
        return 0.0, "No minutes"

    weights = HORIZON_WEIGHTS[:n] if n <= len(HORIZON_WEIGHTS) else HORIZON_WEIGHTS
    total = 0.0
    for i, w in enumerate(weights):
        raw, _ = _player_xp_raw(p, fixture_lookup, start_event + i)
        total += w * raw

    note = "Available"
    chance_val = p.get("chance_of_playing_next_round")
    if status == "d":
        note = f"{chance_val}% Chance" if chance_val is not None else "Doubtful"
    elif chance_val is not None and chance_val < 100:
        note = f"{chance_val}% Chance"

    gmod = _load_weights()["global_xP_modifier"]
    return round(max(_risk_adjust(p, total, risk), 0.0) * gmod, 2), note

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

def _player_fdr_list(p: Dict[str, Any], fixture_lookup: Dict[int, List[Dict[str, Any]]], start_event: int, n: int = 4) -> List[float]:
    """Fixture Difficulty Rating for the next n gameweeks (5 = blank/hard)."""
    team_id = p.get("team")
    fx = fixture_lookup.get(team_id, [])
    out = []
    for i in range(n):
        ev = start_event + i
        f = next((x for x in fx if x.get("event") == ev), None)
        out.append(float(f.get("difficulty", 3)) if f else 5.0)
    return out


def _transfer_rationale(out_entry: Dict[str, Any], in_entry: Dict[str, Any],
                        out_e: Dict[str, Any], in_e: Dict[str, Any]) -> str:
    """Plain-English explanation for why this transfer is recommended."""
    reasons = []

    # Base mathematical reason (always applies).
    reasons.append("Offers a superior expected points forecast based on underlying data and upcoming fixtures.")

    # Flagged / injured outgoing player
    chance = out_e.get("chance_of_playing_this_round")
    if chance is None:
        chance = out_e.get("chance_of_playing_next_round")
    status = out_e.get("status", "a")
    if (chance is not None and _to_float(chance) < 75.0) or status in ("d", "i", "s"):
        reasons.append("Replaces a flagged/injured player with a guaranteed starter.")

    # Downgrade to free up budget
    out_sell = _to_float(out_entry.get("sell_price", out_entry.get("price")))
    in_cost = _to_float(in_entry.get("price"))
    if out_sell - in_cost >= 1.0:
        reasons.append("Releases £1.0m+ into your bank for future premium upgrades.")

    # Heavy net market momentum on the incoming player
    net = _to_float(in_e.get("transfers_in_event")) - _to_float(in_e.get("transfers_out_event"))
    if net > 100000.0:
        reasons.append("Capitalises on heavy market momentum to catch a likely overnight price rise.")

    return "💡 " + " | ".join(reasons)


def _pool_entry(e: Dict[str, Any], teams_by_id: Dict[int, str], xp: float, note: str, pos: str,
                selling_price: Optional[float] = None, xp_gw: Optional[float] = None,
                fdr: Optional[List[float]] = None) -> Dict[str, Any]:
    entry = {
        "id": e["id"],
        "name": f"{e['first_name']} {e['second_name']}",
        "team_id": e["team"],
        "team": teams_by_id.get(e["team"], "?"),
        "position": pos,
        "price": e["now_cost"] / 10.0,
        "xp": xp,
        "status": note,
    }
    if selling_price is not None:
        entry["sell_price"] = selling_price
    if xp_gw is not None:
        entry["xp_gw"] = xp_gw
    if fdr is not None:
        entry["fdr"] = fdr
    return entry

def _solve_squad(
    pool: List[Dict[str, Any]],
    budget: float,
    must_include_ids: Optional[set] = None,
    hit_config: Optional[Dict[str, Any]] = None,
    bench_boost: bool = False,
    scarcity_cost: float = 0.0,
    roll_value: float = 0.0,
) -> Tuple[Optional[List[int]], Optional[float]]:
    if not HAS_PULP:
        return None, None

    by_id = {p["id"]: p for p in pool}
    ids = list(by_id.keys())
    x = pulp.LpVariable.dicts("x", ids, cat="Binary")

    prob = pulp.LpProblem("fpl_squad", pulp.LpMaximize)

    # Budget: total purchasing power = Bank + sum(selling_price of current squad).
    # Kept current players are charged at their selling price (the opportunity cost
    # of not cashing them in); incoming players are charged at their now_cost.
    if must_include_ids is not None:
        prob += pulp.lpSum(
            (by_id[pid].get("sell_price", by_id[pid]["price"]) if pid in must_include_ids else by_id[pid]["price"]) * x[pid]
            for pid in ids
        ) <= budget, "budget"
    else:
        prob += pulp.lpSum(by_id[pid]["price"] * x[pid] for pid in ids) <= budget, "budget"

    for pos, cnt in POS_COUNTS.items():
        prob += pulp.lpSum(x[pid] for pid in ids if by_id[pid]["position"] == pos) == cnt, f"pos_{pos}"

    for t in {by_id[pid]["team_id"] for pid in ids}:
        prob += pulp.lpSum(x[pid] for pid in ids if by_id[pid]["team_id"] == t) <= 3, f"team_{t}"

    # Objective. Back-up goalkeepers are heavily discounted (x0.1) so the solver
    # treats the second GK as a budget enabler rather than a premium bench-warmer.
    # The discount is removed when Bench Boost is active (every bench player scores).
    gk_ids = [pid for pid in ids if by_id[pid]["position"] == "GK"]
    xp_expr = pulp.lpSum(by_id[pid]["xp"] * x[pid] for pid in ids if by_id[pid]["position"] != "GK")

    if bench_boost or not gk_ids:
        xp_expr += pulp.lpSum(by_id[pid]["xp"] * x[pid] for pid in gk_ids)
    else:
        # Binary role assignment: exactly one keeper starts (1.0x xP) and exactly
        # one keeper is the bench option (0.1x xP) — so the backup is budget fodder.
        gk_start = pulp.LpVariable.dicts("gk_start", gk_ids, cat="Binary")
        gk_bench = pulp.LpVariable.dicts("gk_bench", gk_ids, cat="Binary")
        for pid in gk_ids:
            # A selected keeper is either the starter or the bench option.
            prob += gk_start[pid] + gk_bench[pid] == x[pid], f"gk_role_{pid}"
        prob += pulp.lpSum(gk_start[pid] for pid in gk_ids) == 1, "one_start_gk"
        prob += pulp.lpSum(gk_bench[pid] for pid in gk_ids) == 1, "one_bench_gk"
        xp_expr += pulp.lpSum(by_id[pid]["xp"] * gk_start[pid] for pid in gk_ids)
        xp_expr += 0.1 * pulp.lpSum(by_id[pid]["xp"] * gk_bench[pid] for pid in gk_ids)

    # Positional transfer friction: subtract a penalty for every player sold
    # ((1 - x[pid]) == 1 for outgoing players). This stops the solver burning a
    # transfer/hit on a GK (1.5) or DEF (0.5) unless the xP uplift is substantial.
    if must_include_ids is not None:
        friction = pulp.lpSum(
            TRANSFER_FRICTION.get(by_id[pid]["position"], 0.0) * (1 - x[pid])
            for pid in must_include_ids if pid in by_id
        )
        xp_expr = xp_expr - friction

    # Automated budget rotation pairing: reward complementary budget-defender
    # pairs (<= £4.5m) whose fixtures alternate easy (FDR 1-2) across the horizon,
    # enabling a clean rotational bench. Linearised pairwise bonus via z = x_a & x_b.
    budget_def_ids = [pid for pid in ids if by_id[pid]["position"] == "DEF" and by_id[pid]["price"] <= ROTATION_MAX_PRICE + 1e-9]
    rotation_bonus = 0.0
    for a in range(len(budget_def_ids)):
        for b in range(a + 1, len(budget_def_ids)):
            pa, pb = budget_def_ids[a], budget_def_ids[b]
            fa, fb = by_id[pa].get("fdr"), by_id[pb].get("fdr")
            if not fa or not fb or len(fa) < 4 or len(fb) < 4:
                continue
            if all(min(fa[i], fb[i]) <= ROTATION_EASY_FDR for i in range(4)):
                z = pulp.LpVariable(f"rot_{pa}_{pb}", cat="Binary")
                prob += z <= x[pa], f"rot_a_{pa}_{pb}"
                prob += z <= x[pb], f"rot_b_{pa}_{pb}"
                prob += z >= x[pa] + x[pb] - 1, f"rot_ge_{pa}_{pb}"
                rotation_bonus += ROTATION_PAIR_BONUS * z
    xp_expr = xp_expr + rotation_bonus

    if must_include_ids is not None and hit_config is not None:
        transfers = pulp.lpSum((1 - x[pid]) for pid in must_include_ids if pid in by_id)
        max_t = hit_config.get("max_transfers")
        if max_t is not None:
            prob += transfers <= max_t, "max_transfers"
        hits = pulp.LpVariable("hits", lowBound=0, cat="Integer")
        prob += hits >= transfers - hit_config["free_transfers"], "hits_lb"
        # Transfer friction: each free transfer used costs ft_friction xP, so the
        # solver values holding FTs. Hits stack the -4 penalty on top.
        ft_friction = hit_config.get("ft_friction", 0.0)
        obj = xp_expr - hit_config["hit_cost"] * hits - ft_friction * (transfers - hits)

        # Wildcard scarcity: activating the chip (any transfer) incurs a fixed
        # full-season opportunity cost, so the solver holds it unless the rebuilt
        # squad decisively outscores the current squad over the horizon.
        if scarcity_cost > 0 and max_t is not None:
            chip_used = pulp.LpVariable("chip_used", cat="Binary")
            prob += transfers <= max_t * chip_used, "chip_used_force"
            obj = obj - scarcity_cost * chip_used

        # Rolling value: banking the free transfer (0 transfers) earns a small
        # bonus, favouring internal bench rotation over lateral tinkering.
        if roll_value > 0 and max_t is not None:
            roll = pulp.LpVariable("roll_ft", cat="Binary")
            prob += roll <= 1 - transfers / max_t, "roll_ub"
            prob += roll >= 1 - transfers, "roll_lb"
            obj = obj + roll_value * roll

        prob.setObjective(obj)
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
            "selling_price": _to_float(pick.get("selling_price", p_data["now_cost"])) / 10.0,
            "purchase_price": _to_float(pick.get("purchase_price", p_data["now_cost"])) / 10.0,
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
        "selling_value": round(sum(p.get("selling_price", p.get("price", 0.0)) for p in squad), 2),
        "gameweek_used": gw,
        "squad_gameweek": picks_gw,
        "squad": squad
    }

@st.cache_data(ttl=300, show_spinner=False)
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
    ft_friction = _risk_profile(risk).get("ft_friction", 1.5)
    current_ids = [p["player_id"] for p in squad]
    sell_by_id = {p["player_id"]: p.get("selling_price", p.get("price", 0.0)) for p in squad}
    pool = []
    seen = set()
    
    for pid in current_ids:
        e = elements_by_id.get(pid)
        if not e:
            continue
        pos = POS_MAP.get(e["element_type"])
        # Multi-GW horizon xP drives the solver; keep the single-GW xP alongside
        # for final lineup/captaincy decisions (which are per-gameweek).
        xp, note = _player_xp_horizon(e, fixture_lookup, event, risk=risk)
        xp_gw, _ = _player_xp(e, fixture_lookup, event=event, risk=risk)
        fdr = _player_fdr_list(e, fixture_lookup, event)
        pool.append(_pool_entry(e, teams_by_id, xp, note, pos,
                                selling_price=sell_by_id.get(pid), xp_gw=xp_gw, fdr=fdr))
        seen.add(pid)

    for e in bootstrap["elements"]:
        if e["id"] in seen:
            continue
        pos = POS_MAP.get(e["element_type"])
        if not pos:
            continue
        xp, note = _player_xp_horizon(e, fixture_lookup, event, risk=risk)
        if note in ("OUT", "Blank", "Injured", "Suspended", "Unavailable", "No minutes"):
            continue
        xp_gw, _ = _player_xp(e, fixture_lookup, event=event, risk=risk)
        fdr = _player_fdr_list(e, fixture_lookup, event)
        pool.append(_pool_entry(e, teams_by_id, xp, note, pos, xp_gw=xp_gw, fdr=fdr))
        seen.add(e["id"])

    # Total purchasing power = Bank + sum(selling price of the current squad).
    total_sell = sum(
        sell_by_id.get(pid, elements_by_id[pid]["now_cost"] / 10.0)
        for pid in current_ids if pid in elements_by_id
    )
    budget = bank + total_sell
    pool_by_id = {p["id"]: p for p in pool}

    def _get_moves(selected_ids, is_unlimited):
        if not selected_ids: 
            return [], 0, 0.0, 0.0
        selected_set = set(selected_ids)
        sold = [pid for pid in current_ids if pid not in selected_set]
        bought = [pid for pid in selected_ids if pid not in current_ids]
        mvs = []
        
        # Positional pairing logic to avoid UI cross-positional mismatch
        for pos in ["GK", "DEF", "MID", "FWD"]:
            sold_pos = sorted((p for p in sold if pool_by_id[p]["position"] == pos), key=lambda pid: pool_by_id[pid]["xp"])
            bought_pos = sorted((p for p in bought if pool_by_id[p]["position"] == pos), key=lambda pid: pool_by_id[pid]["xp"])
            for o, i in zip(sold_pos, bought_pos):
                mvs.append({
                    "out": pool_by_id[o],
                    "in": pool_by_id[i],
                    "xp_gain": round(pool_by_id[i]["xp"] - pool_by_id[o]["xp"], 2),
                    "cost": round(pool_by_id[i]["price"] - pool_by_id[o].get("sell_price", pool_by_id[o]["price"]), 2),
                    "rationale": _transfer_rationale(
                        pool_by_id[o], pool_by_id[i],
                        elements_by_id.get(o, {}), elements_by_id.get(i, {}),
                    ),
                })
                
        hits = 0 if is_unlimited else max(0, len(mvs) - free_transfers)
        # Value the free transfers spent: each must clear ft_friction xP.
        free_used = 0 if is_unlimited else (len(mvs) - hits)
        friction_penalty = ft_friction * free_used
        tot_gain = sum(m["xp_gain"] for m in mvs)
        net_gain = round(tot_gain - hit_cost * hits - friction_penalty, 2)
        cost_chg = round(sum(m["cost"] for m in mvs), 2)
        return mvs, hits, net_gain, cost_chg

    # ==============================================================
    # 1. Universe A: Standard Transfers Optimization (Takes Hit Penalty)
    # ==============================================================
    std_selected, _ = _solve_squad(
        pool, budget=budget, must_include_ids=set(current_ids),
        hit_config={"free_transfers": free_transfers, "hit_cost": hit_cost, "max_transfers": free_transfers + MAX_HIT_TRANSFERS, "ft_friction": ft_friction},
        bench_boost=False,
        roll_value=ROLL_TRANSFER_VALUE,
    )
    std_moves, std_hits, std_net, std_cost = _get_moves(std_selected, False)
    # Buffer the hold strategy: net_gain already subtracts FT friction, so a
    # non-positive net gain means holding (banking the FT) is the better play.
    if std_net <= 0:
        std_moves, std_hits, std_net, std_cost = [], 0, 0.0, 0.0

    # Roll Transfer decision: if no move clears the hit penalty / threshold over the
    # 4-GW horizon, bank the free transfer (up to the 5-transfer cap).
    roll_transfer = len(std_moves) == 0 and free_transfers < 5
    projected_ft = min(free_transfers + 1, 5) if roll_transfer else free_transfers

    # ==============================================================
    # 2. Universe B: Unlimited Transfers (For Wildcard / Free Hit)
    # ==============================================================
    unl_moves, unl_hits, unl_net, unl_cost = [], 0, 0.0, 0.0
    if any(c in eval_chips for c in ("Wildcard", "Free Hit")):
        unl_selected, _ = _solve_squad(
            pool, budget=budget, must_include_ids=set(current_ids),
            hit_config={"free_transfers": 15, "hit_cost": 0.0, "max_transfers": 15, "ft_friction": 0.0},
            bench_boost=("Bench Boost" in eval_chips)
        )
        unl_moves, unl_hits, unl_net, unl_cost = _get_moves(unl_selected, True)

    # Wildcard is a full-season chip: re-solve with a scarcity penalty so it is
    # only deployed when the rebuilt squad decisively outscores the current one.
    wc_moves, wc_hits, wc_net, wc_cost = [], 0, 0.0, 0.0
    if "Wildcard" in eval_chips:
        wc_selected, _ = _solve_squad(
            pool, budget=budget, must_include_ids=set(current_ids),
            hit_config={"free_transfers": 15, "hit_cost": 0.0, "max_transfers": 15, "ft_friction": 0.0},
            bench_boost=("Bench Boost" in eval_chips),
            scarcity_cost=WILDCARD_SCARCITY_COST,
        )
        wc_moves, wc_hits, wc_net, wc_cost = _get_moves(wc_selected, True)

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
        chip_scores["Wildcard"] = round(wc_net - std_net, 2)
    if "Free Hit" in eval_chips:
        chip_scores["Free Hit"] = round(unl_net - std_net, 2)
    if "Bench Boost" in eval_chips:
        chip_scores["Bench Boost"] = round(sum(p['xp'] for p in std_best_xi['bench']), 2)
    if "Triple Captain" in eval_chips:
        cap = std_best_xi.get('captain')
        chip_scores["Triple Captain"] = cap.get('xp', 0.0) if cap else 0.0

    ranked_chips = sorted(chip_scores.items(), key=lambda x: x[1], reverse=True)
    
    # Ultra-Strict Compelling Reason Thresholds
    THRESHOLDS = {"Wildcard": 25.0, "Free Hit": 18.0, "Bench Boost": 15.0, "Triple Captain": 10.0}
    
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
            if score >= 15.0 and current_out_statuses >= 3:
                passed_threshold = True

        if passed_threshold and recommended_chip == "None (Hold Chips)":
            recommended_chip = chip_name
            best_chip_gain = score
            if chip_name in ("Wildcard", "Free Hit"):
                crisis_msg = f" Your squad has {current_out_statuses} flagged players and a reset yields <b>+{score:.1f} xP</b>." if current_out_statuses >= 3 and chip_name == "Wildcard" else f" Yields a massive <b>+{score:.1f} xP</b> over standard transfers."
                chip_advice_list.append(f"🏆 <b>{chip_name}</b>: Strongly Recommended.{crisis_msg}")
            elif chip_name == "Bench Boost":
                chip_advice_list.append(f"🏆 <b>{chip_name}</b>: Strongly Recommended. Your optimised bench provides a massive <b>+{score:.1f} xP</b>.")
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
        if roll_transfer:
            advice = f"🔄 Roll Transfer — no move clears the 4-GW hit threshold. Bank your free transfer (you'll carry {projected_ft} into next GW)."
        else:
            advice = "Hold — no transfers mathematically improve your expected points (xP) after penalties."
    elif std_hits == 0:
        advice = f"Make {n} free transfer(s) — no points hit (4-GW horizon)."
    else:
        advice = f"Make {n} transfer(s), taking {std_hits} hit(s) (-{int(hit_cost * std_hits)} pts) for a net +{std_net:.1f} xP over the 4-GW horizon."

    return {
        "transfers": std_moves,
        "standard_transfers": std_moves,
        "wildcard_transfers": unl_moves,
        "hits": std_hits,
        "net_gain": std_net,
        "cost_change": std_cost,
        "hit_advice": advice,
        "chip_evaluations": chip_advice_list,
        "recommended_chip": recommended_chip,
        "roll_transfer": roll_transfer,
        "projected_ft": projected_ft,
        "horizon": 4
    }

def get_market_movers(threshold: float = 50000) -> Dict[str, List[Dict[str, Any]]]:
    """Detect players near a nightly price rise/fall from bootstrap transfer flows.

    A player with net transfer-in >= threshold is a likely price rise; a player
    with net transfer-out >= threshold is a likely price fall.
    """
    bootstrap = _get_bootstrap()
    teams_by_id = {t["id"]: t["short_name"] for t in bootstrap.get("teams", [])}
    risers: List[Dict[str, Any]] = []
    fallers: List[Dict[str, Any]] = []
    for e in bootstrap.get("elements", []):
        tin = _to_float(e.get("transfers_in_event"))
        tout = _to_float(e.get("transfers_out_event"))
        net = tin - tout
        if net >= threshold or net <= -threshold:
            pos = POS_MAP.get(e.get("element_type"), "?")
            item = {
                "id": e["id"],
                "name": f"{e['first_name']} {e['second_name']}",
                "team": teams_by_id.get(e["team"], "?"),
                "position": pos,
                "price": e["now_cost"] / 10.0,
                "transfers_in": tin,
                "transfers_out": tout,
                "net": net,
            }
            if net <= -threshold:
                fallers.append(item)
            else:
                risers.append(item)
    risers.sort(key=lambda x: x["net"], reverse=True)
    fallers.sort(key=lambda x: x["net"])
    return {"risers": risers[:15], "fallers": fallers[:15]}


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
        status = p.get("status", "a")
        if status == "u":
            continue  # left the Premier League — never surface these in rankings
        if position and pos != position:
            continue
        if max_price and max_price > 0 and price > max_price:
            continue

        xp, note = _player_xp(p, fixture_lookup, event=event, risk=risk)

        # Hazard icon so unadjusted baselines never mislead the user.
        chance = p.get("chance_of_playing_next_round")
        hazard = ""
        if status in ("i", "s", "u", "n"):
            hazard = "🔴"
        elif status == "d" or (chance is not None and _to_float(chance) < 100):
            hazard = "⚠️"

        ranked.append({
            "name": f"{p['first_name']} {p['second_name']}",
            "position": pos,
            "team": teams_by_id.get(p["team"], "?"),
            "price": price,
            "xp": xp,
            "status": note,
            "hazard": hazard,
        })

    ranked.sort(key=lambda x: x["xp"], reverse=True)
    return {"players": ranked[:limit]}

def _pid(p: Dict[str, Any]) -> Any:
    return p.get("player_id", p.get("id"))

def _next_gw_opponents() -> Dict[Any, set]:
    """Map team_id -> set of opponent team_ids for the upcoming gameweek."""
    try:
        bootstrap = _get_bootstrap()
        gw = _next_gameweek(bootstrap)
        opp: Dict[Any, set] = {}
        for tid, fx_list in _build_fixture_lookup(bootstrap).items():
            for f in fx_list:
                if f.get("event") == gw:
                    opp.setdefault(tid, set()).add(f.get("opponent"))
        return opp
    except Exception:
        return {}


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

    # Automated captaincy: (C) = highest-projected starter, (VC) = second-highest.
    captain = max(best_xi, key=lambda x: x.get("xp", 0))
    vice = max((p for p in best_xi if _pid(p) != _pid(captain)), key=lambda x: x.get("xp", 0), default=None)

    xi_ids = {_pid(p) for p in best_xi}
    bench = [p for p in squad if _pid(p) not in xi_ids]
    bench_gk = [p for p in bench if p.get("position") == "GK"]
    bench_out = [p for p in bench if p.get("position") != "GK"]
    bench_out.sort(key=lambda x: x.get("xp", 0), reverse=True)

    pos_order = {"GK": 1, "DEF": 2, "MID": 3, "FWD": 4}
    xi_sorted = sorted(best_xi, key=lambda x: (pos_order.get(x.get("position"), 5), -x.get("xp", 0)))

    # total_xp includes the captain's doubled score (×2); the UI adds one more
    # multiplier for Triple Captain.
    total_xp = round(sum(p.get("xp", 0) for p in best_xi) + captain.get("xp", 0), 2)

    return {
        "xi": xi_sorted,
        "bench": bench_gk + bench_out,  # Slot 1 = reserve GK; Slots 2-4 = outfield (descending xP)
        "formation": best_form,
        "captain": captain,
        "vice_captain": vice,
        "total_xp": total_xp,
    }
