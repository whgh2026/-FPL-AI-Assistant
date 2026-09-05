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

try:
    import numpy as _np
    HAS_NUMPY = True
except ImportError:
    _np = None
    HAS_NUMPY = False

# Decaying transfer tax (anti-whipsaw). Lives in db.py (persistent ledger); fall
# back to a no-op if the module is unavailable so the solver stays importable.
try:
    from db import calculate_decaying_tax, save_team_ratings
except ImportError:
    def calculate_decaying_tax(current_gw, purchase_gw, status="a"):
        return 0.0

    def save_team_ratings(ratings, gameweek=None):
        return False

BASE_URL = "https://fantasy.premierleague.com/api"

# ------------------------------------------------------------------
# Configuration / constants
# ------------------------------------------------------------------
EP_BLEND = 0.5                 
HIT_COST = 4.0                 
MAX_HIT_TRANSFERS = 3          
PRIOR_MINUTES = 270.0          
WILDCARD_SCARCITY_COST = 55.0
ROLL_TRANSFER_VALUE = 1.5
TIGHTROPE_DISCOUNT = 0.85   # 15% haircut on multi-week xP for a player one card from a ban

# Phase C — game theory, effective ownership, and two-set chip scheduling.
PHASE2_START_GW = 26            # GW26+ switches from pure EV to Blocker/Divergence
CHIP_SET1_EXPIRY_GW = 19        # Set 1 chips expire at the GW19 deadline (2 Jan 2027 13:30 GMT)
CHIPS = ["Wildcard", "Free Hit", "Bench Boost", "Triple Captain"]
CAP_LOCK_BONUS = 2.0            # blocker reward for captaining the consensus (highest-EO) asset

# Phase D — stochastic optimisation & multi-week planning.
SAA_SCENARIOS = 500             # Monte Carlo scenarios per solve
PLAN_HORIZON = 6                # multi-GW transfer planning window
PLAN_WEIGHTS = [1.0, 0.85, 0.7, 0.55, 0.45, 0.35]  # decay over the planning horizon

# Phase 1 in-memory upgrades — value of rolled FTs (diminishing marginal curve),
# cash-reserve liquidity, and minutes-floor hit-hurdle scaling.
ROLLED_FT_SHAPE         = [1.0, 0.8, 0.55, 0.25, 0.05]  # convex-down marginal value of the 1st..5th banked FT
LIQUIDITY_BONUS_PER_05M = 0.2                          # xP per £0.5m held unspent in the bank
HIT_FLOOR_PENALTY       = 0.5                          # φ: surcharge on low minutes-floor acquisitions

# Hit hurdle rate calibration — the -4 transfer cost scales with the active risk
# profile to prevent hyperactive churn. Defensive profiles demand a far larger
# expected gain before sanctioning a point hit than aggressive punt profiles.
#   Defensive (conservative): -8.0 xP  — massive expected gain required for any hit.
#   Balanced:                 -6.5 xP  — clear multi-gameweek upgrade to justify a -4.
#   Aggressive:               -4.0 xP  — raw mathematical cost, allows tactical punts.
RISK_PROFILES = {
    "conservative": {"hit_cost": 8.0, "ow_weight": 0.8, "threat_weight": 0.0, "floor_weight": 1.0, "ft_friction": 2.0, "roll_value": 2.0},
    "balanced":     {"hit_cost": 6.5, "ow_weight": 0.0, "threat_weight": 0.0, "floor_weight": 0.0, "ft_friction": 1.5, "roll_value": 1.5},
    "aggressive":   {"hit_cost": 4.0, "ow_weight": -0.8, "threat_weight": 1.2, "floor_weight": -0.2, "ft_friction": 0.5, "roll_value": 0.5},
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
ROTATION_EASY = 4.0   # continuous fixture-ease floor (6 - opp_strength_def) for a rotation pair

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
HORIZON_SUM = sum(HORIZON_WEIGHTS)   # ~3.1: scales the -4 hit to the 4-GW horizon
ROLL_HURDLE = 1.5                    # immediate-GW xP bar when holding exactly 1 FT
# Convex bench ordering (Λ): the reserve keeper (~5%) never comes on for partial
# cameos (which suppresses backup-GK churn); the outfield bench is two-tiered —
# the "12th man" (B1, ~30%) is the likeliest autosub, while B2/B3 are near-dead
# capital (~3.5%, the mean of 0.06/0.01).
BENCH_GK_WEIGHT = 0.05
BENCH_B1_WEIGHT = 0.30
BENCH_DEAD_WEIGHT = 0.035
# Market candidate shortlist entering the MIP: the manager's 15 plus the top
# assets per position by horizon xP, so the starter/bench/captain binaries
# (~135 pool entries) solve in well under a second.
POOL_SHORTLIST = {"GK": 20, "DEF": 35, "MID": 35, "FWD": 30}
CONSERVATIVE_OW_FLOOR = 5.0
CONSERVATIVE_OW_PENALTY = 1.0
AGGRESSIVE_OW_CEILING = 10.0
AGGRESSIVE_DIFF_BONUS = 0.5
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


def _get_all_fixtures() -> List[Dict[str, Any]]:
    """Full-season fixtures (results + schedule) for the Dixon-Coles fit."""
    try:
        return _cached_json(f"{BASE_URL}/fixtures/")
    except Exception:
        return []

def _to_float(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _selling_price(purchase: float, current: float) -> float:
    """FPL manager selling price in £m, using integer-tenths arithmetic.

    FPL prices are integer tenths of a million (105 == £10.5m). On a profit the
    manager keeps £0.1m for every full £0.2m of price rise, so the gain is
    rounded DOWN to the nearest £0.1m; on a loss the player sells at the current
    price. Integer division avoids floating-point rounding errors.
    """
    try:
        p = int(round(purchase * 10))
        c = int(round(current * 10))
    except (TypeError, ValueError):
        return float(current)
    if c > p:
        return (p + (c - p) // 2) / 10.0
    return c / 10.0


def _manager_sell_price(selling_price_raw: Any, purchase_price_raw: Any, now_cost_tenths: float) -> float:
    """Manager-specific selling price in £m.

    Prefer the API's `selling_price` (already the 50%-profit value); if it is
    missing, recompute it from the purchase price with integer-tenths arithmetic.
    """
    if selling_price_raw is not None:
        return _to_float(selling_price_raw) / 10.0
    purchase = purchase_price_raw if purchase_price_raw is not None else now_cost_tenths
    return _selling_price(_to_float(purchase) / 10.0, _to_float(now_cost_tenths) / 10.0)


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

def _weeks_ago(kickoff_time: Any, now: float) -> float:
    try:
        dt = dateutil.parser.isoparse(str(kickoff_time))
        return max(0.0, (now - dt.timestamp()) / (7.0 * 86400.0))
    except Exception:
        return 0.0


def _tau_correction(x: float, y: float, lh: float, la: float, rho: float):
    """Dixon-Coles low-score correction and its partial derivatives.

    Returns (tau, d_tau/d_lh, d_tau/d_la, d_tau/d_rho). Corrects the 0-0, 1-0,
    0-1 and 1-1 scorelines which the independent-Poisson model over-predicts.
    """
    if x == 0 and y == 0:
        return 1.0 - lh * la * rho, -la * rho, -lh * rho, -lh * la
    if x == 1 and y == 0:
        return 1.0 + lh * rho, rho, 0.0, lh
    if x == 0 and y == 1:
        return 1.0 + la * rho, 0.0, rho, la
    if x == 1 and y == 1:
        return 1.0 - rho, 0.0, 0.0, -1.0
    return 1.0, 0.0, 0.0, 0.0


def _fit_dixon_coles(finished_fixtures, decay=0.03, tau=0.2, iterations=300, lr=0.1):
    """Time-decayed Dixon-Coles bivariate Poisson -> ({team_id: {'att','def'}}, gamma, rho).

    lambda_home = exp(att[h] - def[a] + gamma); lambda_away = exp(att[a] - def[h]).
    Attack strengths are centred (zero mean) for identifiability; defence absorbs
    the overall scoring level. Pure-Python gradient ascent (no scipy/numpy).
    """
    team_ids = sorted({f["team_h"] for f in finished_fixtures} | {f["team_a"] for f in finished_fixtures})
    if not team_ids:
        return {}, 0.25, 0.2
    idx = {t: i for i, t in enumerate(team_ids)}
    n = len(team_ids)
    att = [0.0] * n
    dfn = [0.0] * n
    gamma = 0.25
    rho = tau
    now = time.time()
    matches = []
    for f in finished_fixtures:
        h = idx.get(f.get("team_h"))
        a = idx.get(f.get("team_a"))
        if h is None or a is None:
            continue
        x = _to_float(f.get("team_h_score"))
        y = _to_float(f.get("team_a_score"))
        w = math.exp(-decay * _weeks_ago(f.get("kickoff_time"), now))
        matches.append((h, a, x, y, w))

    total_w = sum(w for _, _, _, _, w in matches) or 1.0
    for it in range(iterations):
        step = lr * (0.5 ** (it // 100))
        g_att = [0.0] * n
        g_dfn = [0.0] * n
        g_gamma = 0.0
        g_rho = 0.0
        for h, a, x, y, w in matches:
            lh = math.exp(max(-6.0, min(6.0, att[h] - dfn[a] + gamma)))
            la = math.exp(max(-6.0, min(6.0, att[a] - dfn[h])))
            g_att[h] += w * (x - lh)
            g_dfn[a] += w * (lh - x)
            g_att[a] += w * (y - la)
            g_dfn[h] += w * (la - y)
            g_gamma += w * (x - lh)
            tc, dtc_lh, dtc_la, dtc_rho = _tau_correction(x, y, lh, la, rho)
            if tc > 0.0:
                g_att[h] += w * (dtc_lh / tc) * lh
                g_dfn[a] += w * (dtc_lh / tc) * (-lh)
                g_att[a] += w * (dtc_la / tc) * la
                g_dfn[h] += w * (dtc_la / tc) * (-la)
                g_gamma += w * (dtc_lh / tc) * lh
                g_rho += w * (dtc_rho / tc)
        for i in range(n):
            att[i] += step * g_att[i] / total_w
            dfn[i] += step * g_dfn[i] / total_w
        gamma += step * g_gamma / total_w
        rho = max(-0.3, min(0.3, rho + step * g_rho / total_w))
        ca = sum(att) / n
        att = [v - ca for v in att]

    ratings = {tid: {"att": att[idx[tid]], "def": dfn[idx[tid]]} for tid in team_ids}
    return ratings, gamma, rho


_TEAM_RATINGS_CACHE: Optional[Dict[int, Dict[str, float]]] = None
_TEAM_RATINGS_TS: float = 0.0


def _team_attack_def_ratings() -> Dict[int, Dict[str, float]]:
    """Continuous Dixon-Coles attack/defence ratings per team on a [1,5]-ish scale.

    Blended with FPL overall strength early in the season (few finished fixtures)
    and shifted toward pure Dixon-Coles as results accrue. Returns
    {team_id: {'att','def','att_home','att_away','def_home','def_away'}}.
    """
    global _TEAM_RATINGS_CACHE, _TEAM_RATINGS_TS
    if _TEAM_RATINGS_CACHE is not None and (time.time() - _TEAM_RATINGS_TS) < 300:
        return _TEAM_RATINGS_CACHE

    bootstrap = _get_bootstrap()
    teams = {t["id"]: t for t in bootstrap.get("teams", [])}
    finished = [f for f in _get_all_fixtures() if f.get("finished")]
    w = _load_weights()
    scale = w.get("rating_scale", 2.0)

    dc, gamma, _rho = ({}, 0.25, 0.2)
    if len(finished) >= 5:
        dc, gamma, _rho = _fit_dixon_coles(
            finished, decay=w.get("dixon_coles_decay", 0.03), tau=w.get("dixon_coles_tau", 0.2)
        )
    dc_blend = min(1.0, len(finished) / 100.0) if dc else 0.0

    out = {}
    for tid, team in teams.items():
        ov_h = _to_float(team.get("strength_overall_home") or 0.0)
        ov_a = _to_float(team.get("strength_overall_away") or 0.0)
        fpl_overall = (ov_h + ov_a) / 2.0 if (ov_h and ov_a) else 3.0
        if dc and tid in dc:
            dc_att = 3.0 + dc[tid]["att"] * scale
            dc_def = 3.0 - dc[tid]["def"] * scale
        else:
            dc_att = dc_def = fpl_overall
        att = dc_blend * dc_att + (1.0 - dc_blend) * fpl_overall
        dfn = dc_blend * dc_def + (1.0 - dc_blend) * fpl_overall
        half = (gamma / 2.0) * scale
        out[tid] = {
            "att": round(att, 2),
            "def": round(dfn, 2),
            "att_home": round(min(5.0, max(1.0, att + half)), 2),
            "att_away": round(min(5.0, max(1.0, att - half)), 2),
            "def_home": round(min(5.0, max(1.0, dfn + half)), 2),
            "def_away": round(min(5.0, max(1.0, dfn - half)), 2),
        }
    _TEAM_RATINGS_CACHE = out
    _TEAM_RATINGS_TS = time.time()
    try:
        save_team_ratings(out, _next_gameweek(bootstrap))
    except Exception:
        pass
    return out

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
    ratings = _team_attack_def_ratings()

    lookup: Dict[int, List[Dict[str, Any]]] = {}
    for f in fixtures:
        h, a = f["team_h"], f["team_a"]
        event = f.get("event")
        rh = ratings.get(h, {})
        ra = ratings.get(a, {})
        home_fx = {
            "event": event,
            "is_home": True,
            "opponent": a,
            "kickoff_time": f.get("kickoff_time"),
            # Continuous Dixon-Coles matchup: the away side's attack/defence when
            # on the road (venue-adjusted). Higher def = tougher for our attackers.
            "opp_strength_def": ra.get("def_away", 3.0),
            "opp_strength_att": ra.get("att_away", 3.0),
            "win_prob": win_probs.get(h, {}).get(a),
        }
        away_fx = {
            "event": event,
            "is_home": False,
            "opponent": h,
            "kickoff_time": f.get("kickoff_time"),
            "opp_strength_def": rh.get("def_home", 3.0),
            "opp_strength_att": rh.get("att_home", 3.0),
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
            opp_def = fx.get("opp_strength_def") or 3
            if opp_def <= 2.0:
                lights.append("🟢")
            elif opp_def <= 3.2:
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


_TEAM_PLAYED: Optional[Dict[int, int]] = None
_TEAM_PLAYED_TS: float = 0.0


def _team_played_map() -> Dict[int, int]:
    global _TEAM_PLAYED, _TEAM_PLAYED_TS
    if _TEAM_PLAYED is not None and (time.time() - _TEAM_PLAYED_TS) < 300:
        return _TEAM_PLAYED
    bootstrap = _get_bootstrap()
    _TEAM_PLAYED = {t["id"]: int(t.get("played") or 0) for t in bootstrap.get("teams", [])}
    _TEAM_PLAYED_TS = time.time()
    return _TEAM_PLAYED


def _minute_distribution(p: Dict[str, Any], status: str) -> Tuple[float, float, float]:
    """Tri-state minutes distribution (P(M=0), P(1<=M<=59), P(M>=60)).

    Derived from season starts, minutes and the team's games played, scaled by the
    FPL availability flag. GKs are all-or-nothing (keepers never register cameos).
    """
    if status in OUT_STATUSES:
        return (1.0, 0.0, 0.0)
    chance = p.get("chance_of_playing_next_round")
    if chance is None:
        chance = p.get("chance_of_playing_this_round")
    avail = 1.0 if chance is None else max(0.0, min(1.0, _to_float(chance) / 100.0))

    is_gk = (p.get("element_type") == 1) or (p.get("position") == "GK")
    starts = _to_float(p.get("starts"))
    minutes = _to_float(p.get("minutes"))
    games = max(starts, float(_team_played_map().get(p.get("team"), 0)), 1.0)

    if is_gk:
        start_rate = min(1.0, starts / games) if starts > 0 else (1.0 if minutes >= 60 else 0.0)
        p_full = avail * start_rate
        p_cameo = 0.0
    else:
        if starts > 0:
            start_rate = min(1.0, starts / games)
        else:
            # Starts unreported: infer from minutes (treat as full-game starts) so a
            # 1000-minute player is a nailed starter, not a 100% cameo sub.
            start_rate = min(1.0, (minutes / 90.0) / games) if minutes > 0 else 0.0
        sub_minutes = max(0.0, minutes - start_rate * games * 90.0)
        sub_apps = sub_minutes / 30.0
        sub_rate = min(sub_apps / games, max(0.0, 1.0 - start_rate))
        p_full = avail * start_rate
        p_cameo = avail * sub_rate

    p0 = 1.0 - p_full - p_cameo
    total = p_full + p_cameo + p0
    if total > 0:
        p_full /= total
        p_cameo /= total
        p0 /= total
    return (max(0.0, p0), max(0.0, p_cameo), max(0.0, p_full))


def _expected_playing_fraction(p: Dict[str, Any], status: str) -> float:
    """Expected minutes/90 from the tri-state distribution."""
    _p0, p_cameo, p_full = _minute_distribution(p, status)
    return p_full + (30.0 / 90.0) * p_cameo

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
    defaults = {
        "global_xP_modifier": 1.0,
        "home_advantage": 1.0,
        "clean_sheet_confidence": 1.0,
        "autosub_ref": 1.8,
        "rotation_convexity": 0.4,
        "dixon_coles_decay": 0.03,
        "dixon_coles_tau": 0.2,
        "rating_scale": 2.0,
        "eo_tc_mass": 0.03,
        "eo_floor": 15.0,
        "eo_ceil": 10.0,
        "block_weight": 0.04,
        "diverge_weight": 0.05,
        "stack_weight": 0.3,
        "track_weight": 0.5,
        "saa_att_sigma": 0.25,
        "saa_def_sigma": 0.2,
        "saa_crisis_prob": 0.05,
        "term_equity": 0.1,
        "term_ft": 1.5,
        "term_dead": 0.5,
    }
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

    # Categorical minutes (the cameo guard): P(M=0) / P(1<=M<=59) / P(M>=60).
    p0, p_cameo, p_full = _minute_distribution(p, status)

    xp_full = sum(_xp_for_fixture(p, f, 90.0, pos_id) for f in target)
    xp_cameo = sum(_xp_for_fixture(p, f, 30.0, pos_id) for f in target)
    our_total = p_full * xp_full + p_cameo * xp_cameo

    _w = _load_weights()
    # A 1-59 cameo earns ~1pt but blocks an autosub worth ~autosub_ref: net negative.
    cameo_penalty = p_cameo * max(0.0, _w.get("autosub_ref", 1.8) - xp_cameo)
    # Rotation convexity discount: unpredictable starters carry extra variance.
    variance = p0 * (1.0 - p0) + p_cameo * (1.0 - p_cameo)
    rotation_penalty = _w.get("rotation_convexity", 0.4) * variance
    our_total = our_total - cameo_penalty - rotation_penalty

    ep_next = _to_float(p.get("ep_next"))
    if ep_next > 0:
        # Availability-adjusted minutes fraction applied to FPL's own projection,
        # so doubtful assets never display an unadjusted baseline.
        blend_frac = p_full + (30.0 / 90.0) * p_cameo
        xp = (1.0 - EP_BLEND) * our_total + EP_BLEND * ep_next * blend_frac
    else:
        xp = our_total

    return max(xp, 0.0), note


def _player_xp(p: Dict[str, Any], fixture_lookup: Dict[int, List[Dict[str, Any]]], event: Optional[int] = None, risk: str = "balanced") -> Tuple[float, str]:
    raw, note = _player_xp_raw(p, fixture_lookup, event)
    gmod = _load_weights()["global_xP_modifier"]
    return round(max(_risk_adjust(p, raw, risk), 0.0) * gmod, 2), note


def _is_on_tightrope(p: Dict[str, Any], event: Optional[int] = None) -> bool:
    """True when a player is one yellow card away from a PL suspension.

    Premier League accumulation rules:
      * 5 yellows in a club's first 19 matches  -> 1-match ban.
      * 10 yellows through match 32             -> 2-match ban.
    The current gameweek (`event`) is used as a proxy for the club's matches
    played, so the manager is flagged the week before the ban can trigger.
    """
    if event is None:
        return False
    try:
        yellows = int(p.get("yellow_cards") or 0)
    except (TypeError, ValueError):
        return False
    if event < 19:
        return yellows == 4
    if event < 32:
        return yellows == 9
    return False


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

    # Proactive suspension tightrope: a player one yellow card away from a ban
    # carries real multi-week downside, so haircut the horizon projection.
    if _is_on_tightrope(p, start_event):
        total *= TIGHTROPE_DISCOUNT

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
    """Continuous fixture-ease series for the next n gameweeks (5 = easiest, 0 = blank).

    Ease = 6 - opponent's Dixon-Coles defensive strength, replacing the official
    1-5 FDR so an elite attack against a leaky defence reads differently to the
    same attack against an elite defence.
    """
    team_id = p.get("team")
    fx = fixture_lookup.get(team_id, [])
    out = []
    for i in range(n):
        ev = start_event + i
        f = next((x for x in fx if x.get("event") == ev), None)
        out.append(6.0 - _to_float(f.get("opp_strength_def", 3.0)) if f else 0.0)
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
                fdr: Optional[List[float]] = None, event: Optional[int] = None,
                holding_map: Optional[Dict[Any, int]] = None,
                eo: Optional[float] = None) -> Dict[str, Any]:
    entry = {
        "id": e["id"],
        "name": f"{e['first_name']} {e['second_name']}",
        "team_id": e["team"],
        "team": teams_by_id.get(e["team"], "?"),
        "position": pos,
        "price": e["now_cost"] / 10.0,
        "xp": xp,
        "status": note,
        "on_yellow_card_tightrope": _is_on_tightrope(e, event),
        "minutes_floor": _expected_playing_fraction(e, e.get("status", "a")),
    }
    if selling_price is not None:
        entry["sell_price"] = selling_price
    if xp_gw is not None:
        entry["xp_gw"] = xp_gw
    if fdr is not None:
        entry["fdr"] = fdr
    if holding_map is not None:
        entry["purchase_gw"] = holding_map.get(e["id"])
    if eo is not None:
        entry["eo"] = eo
    return entry

def _solve_squad(
    pool: List[Dict[str, Any]],
    budget: float,
    must_include_ids: Optional[set] = None,
    hit_config: Optional[Dict[str, Any]] = None,
    bench_boost: bool = False,
    scarcity_cost: float = 0.0,
    roll_value: float = 0.0,
    holding_map: Optional[Dict[Any, int]] = None,
    current_gw: Optional[int] = None,
    eo_map: Optional[Dict[int, Dict[str, float]]] = None,
    mode: str = "ev",
    phase: int = 1,
    rival_ids: Optional[set] = None,
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
        spend = pulp.lpSum(
            (by_id[pid].get("sell_price", by_id[pid]["price"]) if pid in must_include_ids else by_id[pid]["price"]) * x[pid]
            for pid in ids
        )
    else:
        spend = pulp.lpSum(by_id[pid]["price"] * x[pid] for pid in ids)
    prob += spend <= budget, "budget"

    for pos, cnt in POS_COUNTS.items():
        prob += pulp.lpSum(x[pid] for pid in ids if by_id[pid]["position"] == pos) == cnt, f"pos_{pos}"

    for t in {by_id[pid]["team_id"] for pid in ids}:
        prob += pulp.lpSum(x[pid] for pid in ids if by_id[pid]["team_id"] == t) <= 3, f"team_{t}"

    # Objective. Back-up players are discounted to their autosub activation
    # probability: the reserve keeper ~5% (never comes on for partial cameos) and
    # the outfield bench is two-tier convex (B1 ~30%, B2/B3 ~3.5%). The discount
    # is removed under Bench Boost (every bench player scores) — but captaincy
    # (one starter scores double) applies in every gameweek.
    gk_ids = [pid for pid in ids if by_id[pid]["position"] == "GK"]
    outfield_ids = [pid for pid in ids if by_id[pid]["position"] != "GK"]

    if bench_boost or not gk_ids:
        # Bench Boost (or no keeper pool): every selected player counts fully.
        xp_expr = pulp.lpSum(by_id[pid]["xp"] * x[pid] for pid in ids)
        start = None
    else:
        # Role assignment: exactly 11 starters (1 GK + 10 outfield) at 1.0x, the
        # remaining 4 bench slots at their activation-probability weight. The
        # outfield bench is convex: the "12th man" (B1) is worth ~30% (likeliest
        # autosub) while B2/B3 are near-dead capital (~3.5%).
        start = pulp.LpVariable.dicts("start", ids, cat="Binary")
        for pid in ids:
            prob += start[pid] <= x[pid], f"start_le_x_{pid}"
        prob += pulp.lpSum(start[pid] for pid in ids) == 11, "eleven_starters"
        prob += pulp.lpSum(start[pid] for pid in gk_ids) == 1, "one_start_gk"
        xp_expr = pulp.lpSum(by_id[pid]["xp"] * start[pid] for pid in ids)
        # 12th-man binary: the single highest-value outfield bench slot.
        b1 = pulp.LpVariable.dicts("b1", outfield_ids, cat="Binary")
        for pid in outfield_ids:
            prob += b1[pid] <= x[pid] - start[pid], f"b1_le_bench_{pid}"
        prob += pulp.lpSum(b1[pid] for pid in outfield_ids) == 1, "one_b1"
        xp_expr += BENCH_B1_WEIGHT * pulp.lpSum(by_id[pid]["xp"] * b1[pid] for pid in outfield_ids)
        xp_expr += BENCH_DEAD_WEIGHT * pulp.lpSum(by_id[pid]["xp"] * (x[pid] - start[pid] - b1[pid]) for pid in outfield_ids)
        xp_expr += BENCH_GK_WEIGHT * pulp.lpSum(by_id[pid]["xp"] * (x[pid] - start[pid]) for pid in gk_ids)

    # Captaincy uplift: the armband doubles one starter's score every gameweek.
    # Modelled in the solver so an incoming armband-winner's xP is valued ~2x.
    captain = pulp.LpVariable.dicts("captain", ids, cat="Binary")
    prob += pulp.lpSum(captain[pid] for pid in ids) == 1, "one_captain"
    for pid in ids:
        if start is not None:
            prob += captain[pid] <= start[pid], f"captain_le_start_{pid}"
        else:
            prob += captain[pid] <= x[pid], f"captain_le_x_{pid}"
    xp_expr += pulp.lpSum(by_id[pid]["xp"] * captain[pid] for pid in ids)

    # Phase-2 game-theory objective: EO alignment (blocker) vs ceiling variance
    # (divergence). All terms are linear (constant-coefficient penalties/rewards,
    # pairwise stack binaries) so the MIP stays under 2s.
    if phase >= 2 and eo_map and mode in ("blocker", "divergence"):
        w = _load_weights()
        if mode == "blocker":
            eo_floor = w.get("eo_floor", 15.0)
            block_w = w.get("block_weight", 0.04)
            # Penalise selecting sub-floor EO differentials.
            xp_expr = xp_expr - block_w * pulp.lpSum(
                max(0.0, eo_floor - eo_map.get(pid, {}).get("eo", 0.0)) * x[pid]
                for pid in ids
            )
            # Lock the consensus captain (highest-EO selected player).
            if ids:
                consensus = max(ids, key=lambda pid: eo_map.get(pid, {}).get("eo", 0.0))
                xp_expr = xp_expr + CAP_LOCK_BONUS * captain[consensus]
            # Mirror a rival squad (tracking-error alignment) when provided.
            if rival_ids:
                track_w = w.get("track_weight", 0.5)
                xp_expr = xp_expr - track_w * pulp.lpSum(
                    (1 - x[pid]) for pid in rival_ids if pid in by_id
                )
        else:  # divergence
            eo_ceil = w.get("eo_ceil", 10.0)
            div_w = w.get("diverge_weight", 0.05)
            # Reward sub-ceiling EO differentials (high-ceiling low-EO punts).
            xp_expr = xp_expr + div_w * pulp.lpSum(
                max(0.0, eo_ceil - eo_map.get(pid, {}).get("eo", 0.0)) * x[pid]
                for pid in ids
            )
            # Correlated attacking-stack covariance bonus (top-3 attackers/club).
            stack_w = w.get("stack_weight", 0.3)
            attack_ids = [pid for pid in ids if by_id[pid]["position"] in ("MID", "FWD")]
            by_team: Dict[int, List[int]] = {}
            for pid in attack_ids:
                by_team.setdefault(by_id[pid]["team_id"], []).append(pid)
            for team_pids in by_team.values():
                team_pids = sorted(team_pids, key=lambda pid: by_id[pid]["xp"], reverse=True)[:3]
                for a in range(len(team_pids)):
                    for b in range(a + 1, len(team_pids)):
                        pa, pb = team_pids[a], team_pids[b]
                        z = pulp.LpVariable(f"stack_{pa}_{pb}", cat="Binary")
                        prob += z <= x[pa], f"stack_a_{pa}_{pb}"
                        prob += z <= x[pb], f"stack_b_{pa}_{pb}"
                        prob += z >= x[pa] + x[pb] - 1, f"stack_ge_{pa}_{pb}"
                        xp_expr = xp_expr + stack_w * z

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
            if all(min(fa[i], fb[i]) >= ROTATION_EASY for i in range(4)):
                z = pulp.LpVariable(f"rot_{pa}_{pb}", cat="Binary")
                prob += z <= x[pa], f"rot_a_{pa}_{pb}"
                prob += z <= x[pb], f"rot_b_{pa}_{pb}"
                prob += z >= x[pa] + x[pb] - 1, f"rot_ge_{pa}_{pb}"
                rotation_bonus += ROTATION_PAIR_BONUS * z
    xp_expr = xp_expr + rotation_bonus

    # Cash reserve liquidity: holding cash enables a 1-move upgrade later without a
    # restructuring hit, so reward residual budget rather than always maxing it out.
    xp_expr = xp_expr + LIQUIDITY_BONUS_PER_05M * (budget - spend) / 0.5

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

        # Minutes-floor hit-hurdle scaling: a rotational punt (low expected-minute
        # floor) faces a higher effective hurdle than a nailed starter, because the
        # -4 is guaranteed while the payoff is far more variable.
        incoming = [pid for pid in ids if pid not in must_include_ids]
        floor_risk = pulp.lpSum(
            max(0.0, 1.0 - by_id[pid].get("minutes_floor", 1.0)) * x[pid]
            for pid in incoming
        )
        obj = obj - HIT_FLOOR_PENALTY * hit_config["hit_cost"] * floor_risk

        # Wildcard scarcity: activating the chip (any transfer) incurs a fixed
        # full-season opportunity cost, so the solver holds it unless the rebuilt
        # squad decisively outscores the current squad over the horizon.
        if scarcity_cost > 0 and max_t is not None:
            chip_used = pulp.LpVariable("chip_used", cat="Binary")
            prob += transfers <= max_t * chip_used, "chip_used_force"
            obj = obj - scarcity_cost * chip_used

        # Value of a rolled transfer: banking free transfers holds strategic
        # optionality, so reward each unspent FT carried forward with a diminishing
        # marginal curve (the 1st banked FT is worth more than the 5th).
        free_transfers = hit_config.get("free_transfers", 0)
        if roll_value > 0 and max_t is not None and 0 < free_transfers <= 5:
            rolled = free_transfers - transfers + hits   # == max(0, F - T) at optimality
            y = pulp.LpVariable.dicts("roll_ft", range(1, 6), cat="Binary")
            prob += rolled == pulp.lpSum(y[k] for k in range(1, 6)), "roll_ft_sum"
            for k in range(2, 6):
                prob += y[k] <= y[k - 1], f"roll_ft_mono_{k}"
            obj = obj + roll_value * pulp.lpSum(ROLLED_FT_SHAPE[k - 1] * y[k] for k in range(1, 6))

        for pid in must_include_ids:
            if pid not in by_id:
                continue
            p = by_id[pid]
            purchase_gw = p.get("purchase_gw")
            if purchase_gw is None and holding_map is not None:
                purchase_gw = holding_map.get(pid)
            tax = calculate_decaying_tax(current_gw, purchase_gw, p.get("status", "a"))
            if tax > 0:
                obj = obj - tax * (1 - x[pid])

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
            "selling_price": _manager_sell_price(pick.get("selling_price"), pick.get("purchase_price"), p_data["now_cost"]),
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

def _ownership_adjust(e, xp, risk):
    """Tilt xP by ownership to make strategy modes mechanically distinct."""
    ow = _to_float(e.get("selected_by_percent"))
    if risk == "conservative" and ow < CONSERVATIVE_OW_FLOOR:
        return max(0.0, xp - CONSERVATIVE_OW_PENALTY)
    if risk == "aggressive" and ow < AGGRESSIVE_OW_CEILING:
        return xp + AGGRESSIVE_DIFF_BONUS
    return xp


_EO_CACHE: Optional[Dict[int, Dict[str, float]]] = None
_EO_CACHE_TS: float = 0.0


def _captain_distribution(elements, n: int = 10) -> Dict[int, float]:
    """Estimate captain % across players (sum ~100%) from ownership-weighted xP.

    FPL exposes no captain/TC percentages for free, so we model captaincy as the
    ownership-weighted xP share of the top-N candidates, normalised to ~100%.
    """
    candidates = []
    for e in elements:
        ow = _to_float(e.get("selected_by_percent"))
        if ow <= 0:
            continue
        xp = _to_float(e.get("ep_next")) or _to_float(e.get("points_per_game")) or 1.0
        candidates.append((e["id"], ow * max(xp, 1.0)))
    candidates.sort(key=lambda t: t[1], reverse=True)
    top = candidates[:n]
    total = sum(wt for _, wt in top) or 1.0
    return {pid: 100.0 * (wt / total) for pid, wt in top}


def _eo_map() -> Dict[int, Dict[str, float]]:
    """Effective ownership per player: ownership + captain + 2*tc (all in %)."""
    global _EO_CACHE, _EO_CACHE_TS
    if _EO_CACHE is not None and (time.time() - _EO_CACHE_TS) < 300:
        return _EO_CACHE
    bootstrap = _get_bootstrap()
    elements = bootstrap.get("elements", [])
    cap_dist = _captain_distribution(elements)
    w = _load_weights()
    tc_mass = w.get("eo_tc_mass", 0.03) * 100.0
    top_pid = max(cap_dist, key=cap_dist.get) if cap_dist else None
    out = {}
    for e in elements:
        pid = e["id"]
        ow = _to_float(e.get("selected_by_percent"))
        cap = cap_dist.get(pid, 0.0)
        tc = tc_mass if pid == top_pid else 0.0
        out[pid] = {
            "ownership": round(ow, 2),
            "captain": round(cap, 2),
            "tc": round(tc, 2),
            "eo": round(ow + cap + 2.0 * tc, 2),
        }
    _EO_CACHE = out
    _EO_CACHE_TS = time.time()
    return out


def _rank_exposure(multiplier: int, eo: float, points: float) -> float:
    """Δ_i = points * (m_i - EO_i/100): marginal rank PnL of player i scoring `points`."""
    return points * (float(multiplier) - eo / 100.0)


def _strategy_mode(risk: str) -> str:
    """Map a strategy label to a Phase-2 game-theory mode (ev / blocker / divergence)."""
    key = (risk or "balanced").lower().strip()
    key = _RISK_ALIASES.get(key, key)
    if key in ("conservative", "rank_protecting"):
        return "blocker"
    if key in ("aggressive", "rank_chasing"):
        return "divergence"
    return "ev"


def _chip_inventory(current_gw):
    """Which chips are playable in Set 1 (GW1-19) vs Set 2 (GW20-38)."""
    gw = int(current_gw or 1)
    set1 = CHIPS if gw <= CHIP_SET1_EXPIRY_GW else []
    set2 = CHIPS if gw > CHIP_SET1_EXPIRY_GW else []
    return {"set1": set1, "set2": set2, "expiry_gw": CHIP_SET1_EXPIRY_GW}


def _chip_reservation_threshold(chip: str, gw: int) -> float:
    """Declining reservation hurdle as the Set-1 deadline approaches."""
    base = {"Wildcard": 55.0, "Free Hit": 20.0, "Bench Boost": 15.0, "Triple Captain": 15.0}.get(chip, 99.0)
    gw = int(gw)
    if gw >= 18:
        return 0.0   # forced exercise: play on any positive gain
    if gw >= 15:
        return 10.0  # dropped hurdle
    return base


def get_played_chips(manager_id: str) -> List[str]:
    """Chip names the manager has already played (from FPL history)."""
    try:
        manager_id = _clean_manager_id(manager_id)
        resp = requests.get(f"{BASE_URL}/entry/{manager_id}/history/", timeout=10)
        resp.raise_for_status()
        chips = resp.json().get("chips") or []
        return [c.get("name") for c in chips if c.get("name")]
    except Exception:
        return []


def _generate_scenarios(player_ids, fixture_lookup, event, risk="balanced",
                        n=PLAN_HORIZON, S=SAA_SCENARIOS, seed=7):
    """Correlated SAA scenarios -> (saa_mean, matrix).

    Outcomes are coupled through shared team attack/defence latent shocks and a
    shared rotation-crisis minutes shock (never independent player draws).
    Returns:
      saa_mean: {pid: [mean xP over GW t=0..n-1]}
      matrix:   {pid: [n arrays, each length S]}  (for floor/ceiling quantiles)
    """
    bootstrap = _get_bootstrap()
    elements = {e["id"]: e for e in bootstrap.get("elements", [])}
    ids = [pid for pid in player_ids if pid in elements]

    base = {}
    p0 = {}
    team_of = {}
    pos_of = {}
    for pid in ids:
        e = elements[pid]
        row = []
        for t in range(n):
            xp, _ = _player_xp(e, fixture_lookup, event=event + t, risk=risk)
            row.append(xp)
        base[pid] = row
        _p0, _pc, _pf = _minute_distribution(e, e.get("status", "a"))
        p0[pid] = _p0
        team_of[pid] = e.get("team")
        pos_of[pid] = POS_MAP.get(e.get("element_type"), "MID")

    if not HAS_NUMPY or not ids:
        saa_mean = {pid: [round(v, 2) for v in base[pid]] for pid in ids}
        matrix = {pid: [[v] for v in base[pid]] for pid in ids}
        return saa_mean, matrix

    w = _load_weights()
    sig_att = w.get("saa_att_sigma", 0.25)
    sig_def = w.get("saa_def_sigma", 0.2)
    crisis_p = w.get("saa_crisis_prob", 0.05)

    teams = sorted({team_of[pid] for pid in ids})
    tidx = {t: i for i, t in enumerate(teams)}
    T = len(teams)
    rng = _np.random.default_rng(seed)

    # Shared team latent shocks: shape (S, T, n), mean-1 log-normals.
    att_shock = rng.lognormal(mean=-0.5 * sig_att ** 2, sigma=sig_att, size=(S, T, n))
    def_shock = rng.lognormal(mean=-0.5 * sig_def ** 2, sigma=sig_def, size=(S, T, n))
    crisis = (rng.random(size=(S, T, n)) < crisis_p).astype(float) * rng.beta(1.0, 3.0, size=(S, T, n))

    att_share = {"GK": 0.1, "DEF": 0.45, "MID": 0.85, "FWD": 0.9}

    saa_mean = {}
    matrix = {}
    for pid in ids:
        ti = tidx[team_of[pid]]
        ashare = att_share.get(pos_of[pid], 0.8)
        shock = ashare * att_shock[:, ti, :] + (1.0 - ashare) * def_shock[:, ti, :]  # (S, n)
        min_factor = 1.0 - p0[pid] * crisis[:, ti, :]                                # (S, n)
        xp_s = _np.asarray(base[pid], dtype=float)[None, :] * shock * min_factor      # (S, n)
        matrix[pid] = xp_s
        saa_mean[pid] = [round(float(xp_s[:, t].mean()), 2) for t in range(n)]

    return saa_mean, matrix


def _scenario_distribution(selected_ids, matrix, weights=None, n=PLAN_HORIZON):
    """P5/P50/P95 of a selected squad's horizon points across scenarios."""
    sel = [pid for pid in selected_ids if pid in matrix]
    if not sel:
        return {"p5": 0.0, "p50": 0.0, "p95": 0.0, "mean": 0.0}
    S = len(matrix[sel[0]][0])
    w = weights or PLAN_WEIGHTS[:n]
    if HAS_NUMPY:
        totals = _np.zeros(S)
        for pid in sel:
            m = matrix[pid]
            for t in range(min(n, len(m))):
                totals = totals + w[t] * _np.asarray(m[t])
        p5 = float(_np.percentile(totals, 5))
        p50 = float(_np.percentile(totals, 50))
        p95 = float(_np.percentile(totals, 95))
        mean = float(totals.mean())
    else:
        totals = [0.0] * S
        for pid in sel:
            m = matrix[pid]
            for t in range(min(n, len(m))):
                wt = w[t]
                for s in range(S):
                    totals[s] += wt * m[t][s]
        ts = sorted(totals)
        p5 = ts[int(0.05 * (S - 1))]
        p50 = ts[int(0.5 * (S - 1))]
        p95 = ts[int(0.95 * (S - 1))]
        mean = sum(totals) / S
    return {"p5": round(p5, 2), "p50": round(p50, 2), "p95": round(p95, 2), "mean": round(mean, 2)}


def _planner_shortlist(pool, current_ids, saa_mean):
    """Downsample the pool to ~40 players for the multi-GW planner."""
    cur = [p for p in pool if p["id"] in set(current_ids)]
    incoming = [p for p in pool if p["id"] not in set(current_ids)]

    def horizon_xp(p):
        m = saa_mean.get(p["id"], [p.get("xp", 0.0)])
        return sum(PLAN_WEIGHTS[t] * (m[t] if t < len(m) else p.get("xp", 0.0))
                   for t in range(len(PLAN_WEIGHTS)))

    incoming.sort(key=horizon_xp, reverse=True)
    per_pos = {"GK": 4, "DEF": 6, "MID": 6, "FWD": 4}
    picked = []
    for pos, k in per_pos.items():
        picked += [p for p in incoming if p["position"] == pos][:k]
    picked_ids = {p["id"] for p in picked} | {p["id"] for p in cur}
    for p in incoming:
        if len(picked_ids) >= 40:
            break
        if p["id"] not in picked_ids:
            picked.append(p)
            picked_ids.add(p["id"])
    return cur + picked


def _plan_transfers_multi_gw(pool, budget, free_transfers, current_ids, saa_mean,
                             event, n=PLAN_HORIZON):
    """Multi-GW transfer scheduler: ownership + FT + budget over N GWs (Omega(f)).

    Spans the horizon so banking FTs now can fund a coupled structural pivot in a
    later gameweek without hits; a terminal value Psi(S_t+N) stops the solver from
    strip-mining the squad in the final week.
    """
    if not HAS_PULP:
        return []
    shortlist = _planner_shortlist(pool, current_ids, saa_mean)
    by_id = {p["id"]: p for p in shortlist}
    ids = [p["id"] for p in shortlist]
    cur_set = set(current_ids)
    T = n
    w = _load_weights()
    lam_eq = w.get("term_equity", 0.1)
    lam_ft = w.get("term_ft", 1.5)
    lam_dead = w.get("term_dead", 0.5)

    prob = pulp.LpProblem("multi_gw_plan", pulp.LpMaximize)
    x = pulp.LpVariable.dicts("mx", (ids, range(T)), cat="Binary")
    buy = pulp.LpVariable.dicts("mbuy", (ids, range(T)), cat="Binary")
    sell = pulp.LpVariable.dicts("msell", (ids, range(T)), cat="Binary")
    ft = [pulp.LpVariable(f"ft_{t}", lowBound=0, upBound=5, cat="Integer") for t in range(T + 1)]
    hits = [pulp.LpVariable(f"hit_{t}", lowBound=0, cat="Integer") for t in range(T)]
    bank = [pulp.LpVariable(f"bank_{t}", lowBound=0) for t in range(T + 1)]

    # Ownership linkage.
    for pid in ids:
        c0 = 1 if pid in cur_set else 0
        prob += x[pid][0] == c0 + buy[pid][0] - sell[pid][0], f"own0_{pid}"
        for t in range(1, T):
            prob += x[pid][t] == x[pid][t - 1] + buy[pid][t] - sell[pid][t], f"own_{pid}_{t}"
        for t in range(T):
            prob += buy[pid][t] + sell[pid][t] <= 1, f"no_churn_{pid}_{t}"

    # Squad structure per GW.
    for t in range(T):
        prob += pulp.lpSum(x[pid][t] for pid in ids) == 15, f"squad_{t}"
        for pos, cnt in POS_COUNTS.items():
            prob += pulp.lpSum(x[pid][t] for pid in ids if by_id[pid]["position"] == pos) == cnt, f"pos_{pos}_{t}"
        for tm in {by_id[pid]["team_id"] for pid in ids}:
            prob += pulp.lpSum(x[pid][t] for pid in ids if by_id[pid]["team_id"] == tm) <= 3, f"team_{tm}_{t}"

    # Transfers, FT, hits, budget linkage.
    transfers = [pulp.lpSum(buy[pid][t] for pid in ids) for t in range(T)]
    sell_value = [pulp.lpSum(sell[pid][t] * by_id[pid].get("sell_price", by_id[pid]["price"]) for pid in ids) for t in range(T)]
    buy_cost = [pulp.lpSum(buy[pid][t] * by_id[pid]["price"] for pid in ids) for t in range(T)]

    prob += ft[0] == min(5, int(free_transfers)), "ft0"
    prob += bank[0] == budget, "bank0"
    for t in range(T):
        prob += hits[t] >= transfers[t] - ft[t], f"hits_lb_{t}"
        prob += hits[t] <= transfers[t], f"hits_ub_{t}"
        prob += ft[t + 1] <= ft[t] - transfers[t] + 1 + hits[t], f"ft_next_{t}"
        prob += ft[t + 1] <= 5, f"ft_cap_{t}"
        prob += buy_cost[t] <= bank[t] + sell_value[t], f"budget_flow_{t}"
        prob += bank[t + 1] == bank[t] + sell_value[t] - buy_cost[t], f"bank_next_{t}"

    # Precompute per-player per-GW SAA xP.
    saa_xp = {}
    for pid in ids:
        m = saa_mean.get(pid)
        if m:
            saa_xp[pid] = [m[t] if t < len(m) else by_id[pid].get("xp", 0.0) for t in range(T)]
        else:
            saa_xp[pid] = [by_id[pid].get("xp", 0.0)] * T

    # Objective: horizon SAA points - hits + terminal value Psi.
    obj = 0.0
    for t in range(T):
        gw_w = PLAN_WEIGHTS[t] if t < len(PLAN_WEIGHTS) else 0.0
        pts = pulp.lpSum(x[pid][t] * saa_xp[pid][t] for pid in ids)
        obj += gw_w * (pts - 4.0 * hits[t])

    term_equity = pulp.lpSum(x[pid][T - 1] * by_id[pid].get("sell_price", by_id[pid]["price"]) for pid in ids)
    dead = pulp.lpSum(x[pid][T - 1] * (1.0 if (by_id[pid].get("status") in _NON_PLAYING_NOTES or by_id[pid].get("xp", 0.0) <= 0.1) else 0.0) for pid in ids)
    obj += lam_eq * term_equity + lam_ft * ft[T] - lam_dead * dead

    prob.setObjective(obj)
    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[prob.status] != "Optimal":
        return []

    schedule = []
    for t in range(T):
        buys = [by_id[pid]["name"] for pid in ids if buy[pid][t].varValue and buy[pid][t].varValue > 0.5]
        sells = [by_id[pid]["name"] for pid in ids if sell[pid][t].varValue and sell[pid][t].varValue > 0.5]
        schedule.append({
            "gw": event + t,
            "buys": buys,
            "sells": sells,
            "transfers": int(round(transfers[t].value() or 0.0)),
            "hits": int(round(hits[t].value() or 0.0)),
            "ft_after": int(round(ft[t + 1].value() or 0.0)),
            "bank_after": round(bank[t + 1].value() or 0.0, 1),
        })
    return schedule


@st.cache_data(ttl=300, show_spinner=False)
def suggest_transfers_for_custom_squad(
    squad: List[Dict[str, Any]], 
    bank: float, 
    free_transfers: int, 
    eval_chips: List[str] = [],
    event: Optional[int] = None,
    risk: str = "balanced",
    holding_map: Optional[Dict[Any, int]] = None,
    current_gw: Optional[int] = None,
    allow_hits: bool = False,
    rival_ids: Optional[set] = None,
) -> Dict[str, Any]:
    bootstrap = _get_bootstrap()
    fixture_lookup = _build_fixture_lookup(bootstrap)
    teams_by_id = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    elements_by_id = {e["id"]: e for e in bootstrap["elements"]}
    
    if event is None:
        event = _next_gameweek(bootstrap)

    if current_gw is None:
        current_gw = event

    mode = _strategy_mode(risk)
    phase = 2 if current_gw >= PHASE2_START_GW else 1
    eo_map = _eo_map() if phase >= 2 else None

    hit_cost = _risk_profile(risk)["hit_cost"]
    ft_friction = _risk_profile(risk).get("ft_friction", 1.5)
    roll_value = _risk_profile(risk).get("roll_value", ROLL_TRANSFER_VALUE)
    # Pay a hit against the IMMEDIATE gameweek xP, not diluted over the 4-GW sum.
    hit_cost_horizon = hit_cost * HORIZON_SUM
    roll_hurdle = {"conservative": 2.0, "aggressive": 0.8}.get(risk, ROLL_HURDLE)
    # Hard clamp: never take point hits for lateral moves by default.
    current_out_statuses = sum(1 for p in squad if p.get("status") in ("Injured", "Suspended", "Unavailable", "OUT"))
    fit_count = len(squad) - current_out_statuses
    if free_transfers == 0 and not allow_hits:
        max_transfers = 0
    elif allow_hits or fit_count < 11:
        max_transfers = free_transfers + MAX_HIT_TRANSFERS
    else:
        max_transfers = free_transfers
    current_ids = [p["player_id"] for p in squad]
    sell_by_id = {}
    for p in squad:
        sp = p.get("selling_price")
        sell_by_id[p["player_id"]] = sp if sp is not None else _selling_price(
            p.get("purchase_price", p.get("price", 0.0)), p.get("price", 0.0)
        )
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
        xp = _ownership_adjust(e, xp, risk)
        xp_gw, _ = _player_xp(e, fixture_lookup, event=event, risk=risk)
        fdr = _player_fdr_list(e, fixture_lookup, event)
        pool.append(_pool_entry(e, teams_by_id, xp, note, pos,
                                selling_price=sell_by_id.get(pid), xp_gw=xp_gw, fdr=fdr, event=event,
                                holding_map=holding_map,
                                eo=(eo_map.get(pid, {}).get("eo") if eo_map else None)))
        seen.add(pid)

    # Candidate shortlist: the full market (~700 players) is dominated by ~400
    # irrelevant non-starters. Restrict the MIP to the manager's 15 plus the top
    # assets per position by horizon xP, so the starter/bench/captain binaries
    # solve in well under a second.
    incoming_by_pos = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for e in bootstrap["elements"]:
        if e["id"] in seen:
            continue
        pos = POS_MAP.get(e["element_type"])
        if not pos:
            continue
        xp, note = _player_xp_horizon(e, fixture_lookup, event, risk=risk)
        if note in ("OUT", "Blank", "Injured", "Suspended", "Unavailable", "No minutes"):
            continue
        incoming_by_pos[pos].append((_ownership_adjust(e, xp, risk), note, e))
    for pos, entries in incoming_by_pos.items():
        entries.sort(key=lambda t: t[0], reverse=True)
        for xp, note, e in entries[:POOL_SHORTLIST.get(pos, 30)]:
            xp_gw, _ = _player_xp(e, fixture_lookup, event=event, risk=risk)
            fdr = _player_fdr_list(e, fixture_lookup, event)
            pool.append(_pool_entry(e, teams_by_id, xp, note, pos, xp_gw=xp_gw, fdr=fdr, event=event,
                                    holding_map=holding_map,
                                    eo=(eo_map.get(e["id"], {}).get("eo") if eo_map else None)))
            seen.add(e["id"])

    # Total purchasing power = Bank + sum(selling price of the current squad).
    total_sell = sum(
        sell_by_id.get(pid, elements_by_id[pid]["now_cost"] / 10.0)
        for pid in current_ids if pid in elements_by_id
    )
    budget = bank + total_sell
    pool_by_id = {p["id"]: p for p in pool}

    # Phase D SAA: correlated scenarios + SAA-mean horizon xP + multi-GW plan.
    saa_mean = {}
    scenario_matrix = {}
    multi_gw_plan = []
    try:
        pool_ids = [p["id"] for p in pool]
        saa_mean, scenario_matrix = _generate_scenarios(pool_ids, fixture_lookup, event, risk=risk)
        for p in pool:
            m = saa_mean.get(p["id"])
            if m:
                p["xp"] = round(sum(HORIZON_WEIGHTS[t] * (m[t] if t < len(m) else 0.0)
                                    for t in range(len(HORIZON_WEIGHTS))), 2)
        multi_gw_plan = _plan_transfers_multi_gw(pool, budget, free_transfers, current_ids, saa_mean, event)
    except Exception:
        saa_mean, scenario_matrix, multi_gw_plan = {}, {}, []

    def _get_moves(selected_ids, is_unlimited):
        if not selected_ids: 
            return [], 0, 0.0, 0.0, 0.0
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
        cost_chg = round(sum(m["cost"] for m in mvs), 2)
        # Pure xP net gain (drives the UI display, advice, and chip comparisons).
        net_gain = round(tot_gain - hit_cost_horizon * hits - friction_penalty, 2)
        # Virtual cash-reserve optionality: +0.2 xP per £0.5m released into the bank.
        # Used only for the hold-buffer decision, never surfaced as raw xP.
        liquidity_bonus = LIQUIDITY_BONUS_PER_05M * (-cost_chg) / 0.5
        decision_net = round(net_gain + liquidity_bonus, 2)
        return mvs, hits, net_gain, cost_chg, decision_net

    # ==============================================================
    # 1. Universe A: Standard Transfers Optimization (Takes Hit Penalty)
    # ==============================================================
    std_selected, _ = _solve_squad(
        pool, budget=budget, must_include_ids=set(current_ids),
        hit_config={"free_transfers": free_transfers, "hit_cost": hit_cost_horizon, "max_transfers": max_transfers, "ft_friction": ft_friction},
        bench_boost=False,
        roll_value=roll_value,
        holding_map=holding_map, current_gw=current_gw,
        eo_map=eo_map, mode=mode, phase=phase, rival_ids=rival_ids,
    )
    std_moves, std_hits, std_net, std_cost, std_decision = _get_moves(std_selected, False)
    # Buffer the hold strategy: the decision net (which includes the virtual
    # cash-reserve optionality) must be positive, else holding is the better play.
    if std_decision <= 0:
        std_moves, std_hits, std_net, std_cost = [], 0, 0.0, 0.0

    # Roll Transfer decision: if no move clears the hit penalty / threshold over the
    # 4-GW horizon, bank the free transfer (up to the 5-transfer cap).
    # Roll hurdle: with a single FT, the top move must clear a 1.5 xP bar in the
    # IMMEDIATE gameweek -- otherwise bank the transfer instead of chasing a
    # multi-GW projection.
    if free_transfers == 1 and std_moves:
        gw_gain = sum(m["in"].get("xp_gw", m["in"]["xp"]) - m["out"].get("xp_gw", m["out"]["xp"]) for m in std_moves)
        if gw_gain < roll_hurdle:
            std_moves, std_hits, std_net, std_cost = [], 0, 0.0, 0.0

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
            bench_boost=("Bench Boost" in eval_chips),
            holding_map=holding_map, current_gw=current_gw,
            eo_map=eo_map, mode=mode, phase=phase, rival_ids=rival_ids,
        )
        unl_moves, unl_hits, unl_net, unl_cost, _ = _get_moves(unl_selected, True)

    # Wildcard is a full-season chip: re-solve with a scarcity penalty so it is
    # only deployed when the rebuilt squad decisively outscores the current one.
    wc_moves, wc_hits, wc_net, wc_cost = [], 0, 0.0, 0.0
    if "Wildcard" in eval_chips:
        wc_selected, _ = _solve_squad(
            pool, budget=budget, must_include_ids=set(current_ids),
            hit_config={"free_transfers": 15, "hit_cost": 0.0, "max_transfers": 15, "ft_friction": 0.0},
            bench_boost=("Bench Boost" in eval_chips),
            scarcity_cost=WILDCARD_SCARCITY_COST,
            holding_map=holding_map, current_gw=current_gw,
            eo_map=eo_map, mode=mode, phase=phase, rival_ids=rival_ids,
        )
        wc_moves, wc_hits, wc_net, wc_cost, _ = _get_moves(wc_selected, True)

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
            "on_yellow_card_tightrope": _is_on_tightrope(fpl_p, event),
            "is_captain": False
        })
        
    std_best_xi = select_starting_xi(std_squad)

    # Scenario distribution of the final recommended squad (floor vs ceiling).
    scenario_dist = _scenario_distribution(
        [p["player_id"] for p in std_squad], scenario_matrix, weights=HORIZON_WEIGHTS, n=4
    ) if scenario_matrix else {}

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
    
    # Dynamic reservation thresholds (decline toward the Set-1 GW19 deadline).
    chip_advice_list = []
    if len(eval_chips) > 1:
        chip_advice_list.append("⚠️ <b>Official FPL Rule:</b> You may only activate 1 chip per Gameweek. The system has ranked your selections below based on mathematical scarcity:")
    if current_gw >= 17:
        chip_advice_list.append(f"⏳ <b>Set 1 chip deadline:</b> first-half chips expire at the GW{CHIP_SET1_EXPIRY_GW} deadline — play any positive-gain chip now or lose the asset.")

    recommended_chip = "None (Hold Chips)"
    best_chip_gain = 0.0

    for chip_name, score in ranked_chips:
        threshold = _chip_reservation_threshold(chip_name, current_gw)
        passed_threshold = score >= threshold
        
        # Wildcard exception: Lower xP barrier if squad is ravaged by injuries
        if chip_name == "Wildcard" and not passed_threshold:
            if score >= 25.0 and current_out_statuses >= 4:
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
            if chip_name == "Wildcard":
                chip_advice_list.append(f"❌ <b>Wildcard</b>: Hold. A complete reset yields +{score:.1f} xP, which does not justify burning a season-long strategic asset. Elite managers preserve the Wildcard for major fixture swings (GW6–8) or late-season Blank/Double Gameweek navigation.")
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
        "scenario_distribution": scenario_dist,
        "multi_gw_plan": multi_gw_plan,
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


def get_regression_candidates(min_minutes: int = 360, threshold: float = 1.5) -> Dict[str, Any]:
    """Regression-to-the-mean candidates from over/under-performance vs xG+xA.

    R_i = (goals + assists - (xG + xA)) / sqrt(xG + xA + 1). A player far above
    +threshold is over-performing (SELL-HIGH); far below -threshold is
    under-performing (BUY-LOW). Season-to-date totals from the bootstrap.
    """
    bootstrap = _get_bootstrap()
    teams_by_id = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    out: Dict[str, List[Dict[str, Any]]] = {"sell_high": [], "buy_low": []}
    for e in bootstrap.get("elements", []):
        if _to_float(e.get("minutes")) < min_minutes:
            continue
        goals = _to_float(e.get("goals_scored"))
        assists = _to_float(e.get("assists"))
        xg = _to_float(e.get("expected_goals"))
        xa = _to_float(e.get("expected_assists"))
        residual = (goals + assists - xg - xa) / math.sqrt(xg + xa + 1.0)
        row = {
            "id": e["id"],
            "name": f"{e.get('first_name', '')} {e.get('second_name', '')}".strip(),
            "team": teams_by_id.get(e.get("team"), "?"),
            "position": POS_MAP.get(e.get("element_type"), "?"),
            "price": _to_float(e.get("now_cost")) / 10.0,
            "residual": round(residual, 2),
            "goals": int(round(goals)),
            "assists": int(round(assists)),
            "xg": round(xg, 2),
            "xa": round(xa, 2),
            "minutes": int(_to_float(e.get("minutes"))),
        }
        if residual >= threshold:
            out["sell_high"].append(row)
        elif residual <= -threshold:
            out["buy_low"].append(row)
    out["sell_high"].sort(key=lambda r: r["residual"], reverse=True)
    out["buy_low"].sort(key=lambda r: r["residual"])
    return out


def _lin_slope(series: List[float]) -> float:
    """Least-squares slope of a series against its index (per-GW trend)."""
    n = len(series)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = (n - 1) / 2.0
    my = sum(series) / n
    num = sum((xs[i] - mx) * (series[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def _fixture_swing_scores(fixture_lookup, start_event, n=6):
    """Rank clubs by the forward slope of their fixture difficulty.

    Attackers face the opponent's defence (att_ease = 6 - opp_def); defenders face
    the opponent's attack (def_ease = 6 - opp_att). A positive slope = improving
    fixtures (prime entry window); negative = deteriorating (exit window).
    """
    bootstrap = _get_bootstrap()
    teams = {t["id"]: t for t in bootstrap.get("teams", [])}
    rows = []
    for tid, team in teams.items():
        fx = fixture_lookup.get(tid, [])
        att_ease: List[float] = []
        def_ease: List[float] = []
        for i in range(n):
            ev = start_event + i
            f = next((x for x in fx if x.get("event") == ev), None)
            if f is None:
                att_ease.append(3.0)
                def_ease.append(3.0)
            else:
                att_ease.append(6.0 - _to_float(f.get("opp_strength_def", 3.0)))
                def_ease.append(6.0 - _to_float(f.get("opp_strength_att", 3.0)))
        rows.append({
            "team_id": tid,
            "name": team.get("name") or team.get("short_name", "?"),
            "att_slope": round(_lin_slope(att_ease), 3),
            "def_slope": round(_lin_slope(def_ease), 3),
            "att_ease": [round(v, 1) for v in att_ease],
            "def_ease": [round(v, 1) for v in def_ease],
        })
    rows.sort(key=lambda r: r["att_slope"], reverse=True)
    return rows


_NON_PLAYING_NOTES = ("Injured", "Suspended", "Unavailable", "No minutes", "OUT", "Blank")


def _squad_structural_health(squad, bank):
    """Audit squad structure: stranded capital, enabler efficiency, formation
    optionality, and price-point pivot liquidity.

    Returns a list of {label, ok, detail} for rendering in the UI.
    """
    checks = []

    xi = select_starting_xi(squad)
    bench = xi.get("bench") or []
    # bench[0] = reserve GK, bench[1..3] = outfield B1/B2/B3 (descending xP)

    # 1. Stranded bench capital
    bench_cost = sum(_to_float(p.get("price", 0.0)) for p in bench)
    stranded = bench_cost > 15.0
    checks.append({
        "label": "Stranded bench capital",
        "ok": not stranded,
        "detail": f"Bench costs £{bench_cost:.1f}m {'(> £15.0m)' if stranded else '(≤ £15.0m)'}.",
    })

    # 2. Enabler efficiency: non-playing outfield assets > £4.0m on B2/B3
    dead = []
    for slot_p in bench[2:4]:
        if slot_p.get("position") == "GK":
            continue
        non_playing = slot_p.get("status") in _NON_PLAYING_NOTES or _to_float(slot_p.get("xp", 0.0)) <= 0.1
        if non_playing and _to_float(slot_p.get("price", 0.0)) > 4.0:
            dead.append(f"{slot_p.get('name', '?')} (£{_to_float(slot_p.get('price', 0.0)):.1f}m)")
    checks.append({
        "label": "Enabler efficiency (B2/B3)",
        "ok": not dead,
        "detail": ("Non-playing >£4.0m enablers: " + "; ".join(dead)) if dead else "No overpriced dead assets on bench slots 2/3.",
    })

    # 3. Formation optionality: formations within 5% of peak XI xP
    by_pos = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in squad:
        by_pos.setdefault(p.get("position"), []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: _to_float(x.get("xp", 0.0)), reverse=True)
    form_xp = []
    for d, m, f in VALID_FORMATIONS:
        if len(by_pos["GK"]) < 1 or len(by_pos["DEF"]) < d or len(by_pos["MID"]) < m or len(by_pos["FWD"]) < f:
            continue
        xi_players = by_pos["GK"][:1] + by_pos["DEF"][:d] + by_pos["MID"][:m] + by_pos["FWD"][:f]
        form_xp.append(sum(_to_float(x.get("xp", 0.0)) for x in xi_players))
    peak = max(form_xp) if form_xp else 0.0
    within = sum(1 for v in form_xp if v >= 0.95 * peak) if peak > 0 else 0
    optionality_ok = within >= 3
    checks.append({
        "label": "Formation optionality",
        "ok": optionality_ok,
        "detail": f"{within} formation(s) within 5% of peak xP ({'flexible' if optionality_ok else 'inflexible'}).",
    })

    # 4. Price-point liquidity: can the squad fund a one-transfer 3-5-2 <-> 3-4-3 pivot?
    def _cheapest(pos):
        ps = [p for p in squad if p.get("position") == pos]
        return min((_to_float(p.get("price", 0.0)) for p in ps), default=0.0)

    cheapest_fwd = _cheapest("FWD")
    cheapest_mid = _cheapest("MID")
    cheapest_def = _cheapest("DEF")
    mid_to_fwd = cheapest_fwd - cheapest_mid
    def_to_mid = cheapest_mid - cheapest_def
    deadlock = (mid_to_fwd > 0 and bank < mid_to_fwd) or (def_to_mid > 0 and bank < def_to_mid)
    checks.append({
        "label": "Price-point pivot liquidity",
        "ok": not deadlock,
        "detail": (
            f"Bank £{bank:.1f}m; MID→FWD gap £{max(0.0, mid_to_fwd):.1f}m, DEF→MID gap £{max(0.0, def_to_mid):.1f}m."
            if not deadlock else
            f"Deadlock: bank £{bank:.1f}m cannot fund the cheapest formation-pivot swap."
        ),
    })

    return checks
