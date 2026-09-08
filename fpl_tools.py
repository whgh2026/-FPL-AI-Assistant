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

try:
    from scipy.optimize import brentq as _brentq
    HAS_SCIPY = True
except ImportError:
    _brentq = None
    HAS_SCIPY = False

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


class FixtureDataUnavailable(RuntimeError):
    """Raised when the fixture list cannot be loaded.

    Its own type because the failure mode matters: without fixtures every
    projection silently becomes 0.0, so callers must show an error rather than
    an empty squad.
    """

# ------------------------------------------------------------------
# Configuration / constants
# ------------------------------------------------------------------
EP_BLEND = 0.5                 # max weight on FPL's own ep_next, at zero minutes
EP_BLEND_FADE_MINUTES = 600.0  # ...decaying to zero by here (~GW8 for a starter)
# The real FPL penalty, charged ONCE, in the gameweek it is paid. Never scaled
# by the horizon: the objective is sum(w_t * xP_t) with w_0 = 1.0, so a one-off
# cost belongs at w_0. Previously hit_cost was inflated per risk profile (6.5
# balanced, 8.0 conservative) and then multiplied AGAIN by HORIZON_SUM (3.1),
# charging up to 24.8 points for a -4 and making hits effectively impossible.
HIT_COST = 4.0
# Risk appetite is now an explicit, separately-named hurdle rather than a
# corrupted cost. Balanced demands a genuine +5.0 over the horizon to justify a
# -4, which is roughly where serious FPL modellers sit.
HIT_HURDLE = {"conservative": 2.5, "balanced": 1.0, "aggressive": 0.0,
              "rank_protecting": 2.0, "rank_chasing": 0.0}
MAX_HIT_TRANSFERS = 3          
PRIOR_MINUTES = 270.0          
# Weak Beta prior on the start rate: 2 pseudo-starts in 3 pseudo-games, i.e. a
# prior mean of 0.67. Deliberately ABOVE 0.5 -- a player with minutes on the
# clock is more likely than not a starter, so a symmetric prior would drag
# nailed players down. Light enough that real evidence dominates quickly:
# 10 starts from 10 games reads 0.92, while at GW4 a single rest moves 4/4 from
# 0.86 to 0.71 rather than 1.00 to 0.75.
PRIOR_STARTS = 2.0
PRIOR_GAMES = 3.0
WILDCARD_SCARCITY_COST = 55.0
TIGHTROPE_DISCOUNT = 0.85   # 15% haircut on multi-week xP for a player one card from a ban

# Positional "routes to points" transfer hurdle -- an ADDITIONAL, position-
# scaled bar layered on top of the generic HURDLE_BASE/HURDLE_SIGMA_WEIGHT
# search brake below, for positions whose ceiling is structurally capped.
# MID and FWD have several independent, largely continuous routes to points
# (goals, assists, open-play xG, BPS upside), so at multiplier 1.0 their bar
# is untouched -- bit-for-bit what HURDLE_BASE already produced. GKP returns
# are almost purely binary clean-sheet dependence, hard-capped around 6-8
# points a match, and carry the highest opportunity cost of the four squad
# slots, so a GKP transfer must clear a materially larger net gain before the
# solver will make it; DEF sits between the two.
#
# POSITIONAL_HURDLE_BASE_XP is NOT derived from HURDLE_BASE (a much smaller
# noise-damping constant, ~1-2 points per swap) -- it is calibrated directly
# against the requested GKP band: 4.4 * 1.75 = 7.7, inside the specified
# 7.5-8.0 xP minimum net gain. Outcome volatility (sigma) still adds on top
# per player exactly as it already does for every other position, so a
# volatile candidate faces a correspondingly higher real bar than this floor.
POSITIONAL_HURDLE_BASE_XP = 4.4
POSITIONAL_HURDLE_MULTIPLIER = {"GK": 1.75, "DEF": 1.25, "MID": 1.0, "FWD": 1.0}
# The elevated GKP bar, exposed on its own: this is "the elevated 1.75x GKP
# hurdle" the Active GK Lock below checks a prospective transfer against.
GKP_TRANSFER_HURDLE_XP = POSITIONAL_HURDLE_BASE_XP * POSITIONAL_HURDLE_MULTIPLIER["GK"]

# Variance dampener on a PROSPECTIVE goalkeeper signing's own clean-sheet xP
# (xCS * 4.0 pts * this dampener): a clean sheet is wiped out by a single
# added-time concession, so the raw P(CS) overstates how reliable that
# return actually is for a decision being made about to buy it. Applied only
# when evaluating a candidate as a transfer IN (suggest_transfers_for_
# custom_squad's incoming-candidate pool, and the multi-GW planner's SAA
# scenarios for the same candidates) -- never to a GK already owned, since
# discounting an owned player's own displayed xP would move numbers shown
# all over the app (My Plan, Model Health, the live tracker) for a decision
# this feature is not about.
GK_TRANSFER_IN_CS_DAMPENER = 0.85

# "60 projected minutes" for the Active GK Lock, expressed the same way the
# rest of the engine already measures playing time -- _expected_playing_
# fraction, not a raw minutes count FPL doesn't publish as a projection.
GK_LOCK_MIN_MINUTES_FLOOR = 60.0 / 90.0

# Phase C — game theory, effective ownership, and two-set chip scheduling.
CHIP_SET1_EXPIRY_GW = 19        # Set 1 chips expire at the GW19 deadline (2 Jan 2027 13:30 GMT)
CHIPS = ["Wildcard", "Free Hit", "Bench Boost", "Triple Captain"]
CAP_LOCK_BONUS = 2.0            # blocker reward for captaining the consensus (highest-EO) asset

# Phase D — stochastic optimisation & multi-week planning.
SAA_SCENARIOS = 500             # Monte Carlo scenarios per solve
PLAN_HORIZON = 6                # multi-GW transfer planning window
PLAN_WEIGHTS = [1.0, 0.85, 0.7, 0.55, 0.45, 0.35]  # decay over the planning horizon

# Phase E — auto-calibration, CVaR hedging, live tracking.
CVAR_STRESS_K = 50              # pooled downside scenarios for the CVaR tail
DIXON_COLES_DECAY_DEFAULT = 0.03  # reference decay for the calibration re-projection
# Plausible bounds on a team's expected goals in a single fixture. The most
# lopsided real Premier League matchup sits near 3.5; 5.0 leaves headroom while
# still bounding the tail.
LAMBDA_MIN = 0.15
LAMBDA_MAX = 5.0

# Bonus (C7). Expected bonus scales as (player bps90 / positional bps90) raised
# to BONUS_CONVEXITY, around the positional constant as prior. The exponent is
# above 1 because bonus is a rank-order tournament -- the top three BPS in a
# match take 3/2/1 and everyone else takes nothing -- so being 30% above
# average is worth appreciably more than 30% more bonus. BONUS_CAP is the
# maximum a single match can award, so no projection may exceed it.
#
# Both are uncalibrated constants standing in for a fit that needs real
# per-match BPS history; they are tagged for the backtester, like
# FT_OPTION_MARGINAL.
BONUS_CONVEXITY = 1.6
BONUS_CAP = 3.0

# Cards (C11). FPL's own scoring, not an estimate: a yellow is -1 and a red -3.
YELLOW_CARD_PTS = 1.0
RED_CARD_PTS = 3.0

# Stamp on every calibration row, so auto_tune only ever fits against a
# homogeneous population of predictions. It must be bumped by ANY stage that
# changes what _player_xp returns -- Stage 4 (Dixon-Coles centring and scale),
# Stage 2 (strategy terms leaving the forecast), Stage 5b (EP_BLEND taper,
# clean sheets, DefCon) and Stage 8 (team ratings no longer rounded to 2dp).
# One stamp spanning several would mix materially different predictions under a
# single label, which is exactly what versioning is for.
#
# v5 (continuous ratings) was small in effect -- max 0.005 points, 0.097%
# relative -- and bumped anyway, because the point of the stamp is that nobody
# has to adjudicate whether a change was "big enough". v6 is not small: bonus
# and cards stop being positional constants, so two players at the same
# position who previously carried identical values for both terms now differ by
# up to ~0.33 points of bonus and ~0.4 of card charge. Nor is v7: minutes are
# exponentially weighted toward recent matches and doubt now shifts a player
# from starting toward a cameo, and minutes multiply every other component.
MODEL_VERSION = "v7-minutes-recency"

# Phase 1 in-memory upgrades — value of rolled FTs (diminishing marginal curve),
# cash-reserve liquidity, and minutes-floor hit-hurdle scaling.
# Marginal value of the 1st..5th banked free transfer, concave by construction.
# Replaces ROLL_TRANSFER_VALUE * ROLLED_FT_SHAPE, which stacked a per-profile
# scalar on top of a shape and produced up to 3.98 points of banking reward.
FT_OPTION_MARGINAL = [1.30, 1.00, 0.65, 0.30, 0.05]

# Terminal cash is worth a little optionality -- it funds a later upgrade without
# a restructuring hit -- but only a little, and only ONCE. The previous
# LIQUIDITY_BONUS_PER_05M = 0.2 per GBP 0.5m was linear and uncapped, so
# downgrading a 12.0m midfielder to a 5.0m one ADDED 2.8 points to the objective:
# the solver was being paid to make the squad worse. Capped at 1.0m of credit,
# i.e. 0.05 points total. Decision utility, never shown as football points.
LIQUIDITY_PER_M    = 0.05
LIQUIDITY_CAP_M    = 1.0

# In-objective search brake. Removing the five stacked frictions leaves the MIP
# free to churn on noise (a +0.1 xP "upgrade" is inside the model's own error),
# so a separable linear hurdle stays -- separable because it must remain linear.
# Post-solve gating was the alternative and is worse: it leaves holes in
# transfer bundles and risks re-solve cycling inside the interactive budget.
HURDLE_BASE = 0.8
HURDLE_SIGMA_WEIGHT = 1.2

# Hit hurdle rate calibration — the -4 transfer cost scales with the active risk
# profile to prevent hyperactive churn. Defensive profiles demand a far larger
# expected gain before sanctioning a point hit than aggressive punt profiles.
#   Defensive (conservative): -8.0 xP  — massive expected gain required for any hit.
#   Balanced:                 -6.5 xP  — clear multi-gameweek upgrade to justify a -4.
#   Aggressive:               -4.0 xP  — raw mathematical cost, allows tactical punts.
RISK_PROFILES = {
    "conservative": {},
    "balanced":     {},
    "aggressive":   {},
    # Competitive modes: defend a lead (shield high-ownership assets) vs chase a
    # leader (hunt low-ownership high-xGI differentials).
    "rank_protecting": {},
    "rank_chasing":    {},
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
# Rolling 4-gameweek horizon weights for multi-week xP projection.
HORIZON_WEIGHTS = [1.0, 0.85, 0.70, 0.55]
HORIZON_SUM = sum(HORIZON_WEIGHTS)   # ~3.1. NOT a hit multiplier: see HIT_COST.
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

# ------------------------------------------------------------------
# Solver profiles
# ------------------------------------------------------------------
# CBC is only deterministic single-threaded, and a wall-clock timeLimit returns
# whatever incumbent the timer happens to catch. Two profiles, because the two
# callers want different contracts:
#
#   "deterministic" - tests and golden files. Optimality must be *proven*; a
#       timeout is a hard failure, never a silently-compared incumbent.
#   "interactive"   - the live app. Latency is the contract; an unproven gap is
#       acceptable but must be reported so the UI never claims "optimal".
#
# threads=1 in both: multi-threaded branch-and-bound is non-deterministic by
# design and would make golden files flap on a loaded runner.
SOLVER_PROFILES = {
    "deterministic": {"threads": 1, "timeLimit": 60.0, "gapRel": 0.0},
    "interactive":   {"threads": 1, "timeLimit": 0.8, "gapRel": 0.01},
}
_SOLVER_PROFILE = os.environ.get("FPL_SOLVER_PROFILE", "interactive")

# Diagnostics from the most recent _solve_squad call. Read by tests (to assert
# formation legality on the XI the MIP actually chose) and by the UI (to decide
# between "optimal" and "best plan found in the time available").
_LAST_SOLVE: Dict[str, Any] = {}


def set_solver_profile(name: str) -> None:
    """Select a solver profile. Raises on an unknown name rather than silently
    falling back, so a typo in CI can't quietly disable determinism."""
    global _SOLVER_PROFILE
    if name not in SOLVER_PROFILES:
        raise ValueError(f"unknown solver profile {name!r}; expected one of {sorted(SOLVER_PROFILES)}")
    _SOLVER_PROFILE = name


def get_solver_profile() -> str:
    return _SOLVER_PROFILE


def _make_solver():
    return pulp.PULP_CBC_CMD(msg=0, **SOLVER_PROFILES[_SOLVER_PROFILE])

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
    # bps90 / yc90 / rc90 join the per-90 averages here so C7 (bonus) and C11
    # (cards) can shrink a player toward their POSITION's rate, exactly as the
    # attacking rates already do. Both were flat constants: every defender got
    # the same 0.34 bonus and the same -0.12 card charge as every other
    # defender, so neither term could ever distinguish two players.
    keys = ("xg", "xa", "xgc", "saves", "bps90", "yc90", "rc90")
    sums = {pos: {k: 0.0 for k in keys} | {"n": 0} for pos in POS_COUNTS}
    for e in bootstrap.get("elements", []):
        pos = POS_MAP.get(e["element_type"])
        if not pos:
            continue
        mins = _to_float(e.get("minutes"))
        if mins < 180:
            continue
        per90 = 90.0 / mins
        sums[pos]["xg"] += min(_to_float(e.get("expected_goals_per_90")), 5.0)
        sums[pos]["xa"] += min(_to_float(e.get("expected_assists_per_90")), 5.0)
        sums[pos]["xgc"] += min(_to_float(e.get("expected_goals_conceded_per_90")), 5.0)
        sums[pos]["saves"] += min(_to_float(e.get("saves_per_90")), 12.0)
        sums[pos]["bps90"] += min(_to_float(e.get("bps")) * per90, 120.0)
        sums[pos]["yc90"] += min(_to_float(e.get("yellow_cards")) * per90, 1.0)
        sums[pos]["rc90"] += min(_to_float(e.get("red_cards")) * per90, 0.5)
        sums[pos]["n"] += 1

    _LEAGUE_AVG = {
        pos: {k: sums[pos][k] / (sums[pos]["n"] or 1) for k in keys}
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


def _fit_dixon_coles(finished_fixtures, decay=0.03, tau=-0.10, iterations=300, lr=0.1):
    """Time-decayed Dixon-Coles bivariate Poisson.

    Returns ({team_id: {'att','def'}}, gamma, rho, mu) with

        lambda_home = exp(mu + att[h] - def[a] + gamma)
        lambda_away = exp(mu + att[a] - def[h])

    Both att and def are centred to zero mean every iteration; the league scoring
    level lives in the explicit intercept `mu`. Previously only att was centred
    and def absorbed the level, which left mean(def) at roughly -log(1.4) ~ -0.33.
    That is benign inside the fit, but `_team_attack_def_ratings` maps def onto a
    [1,5] scale as `3.0 +/- def*scale`, so the uncentred mean shifted every team's
    defensive rating by ~0.66 and, once the mapping sign was corrected, would have
    inflated league-wide attacker xG by roughly 28% against neutral. Centring is
    what makes the sign fix safe.

    `rho` also now defaults negative (~-0.10, the usual sign for football) rather
    than +0.20, and its gradient is normalised by the weight of the matches that
    actually contribute to it. Only 0-0, 1-0, 0-1 and 1-1 have a non-zero
    d(tau)/d(rho); dividing by the weight of *all* matches diluted the step by
    roughly 1/0.35, which is why rho barely moved from its initial value.

    Pure-Python gradient ascent (no scipy/numpy).
    """
    team_ids = sorted({f["team_h"] for f in finished_fixtures} | {f["team_a"] for f in finished_fixtures})
    if not team_ids:
        return {}, 0.25, -0.10, math.log(1.40)
    idx = {t: i for i, t in enumerate(team_ids)}
    n = len(team_ids)
    att = [0.0] * n
    dfn = [0.0] * n
    gamma = 0.25
    rho = tau
    mu = math.log(1.40)          # league goals per team per match, refined by the fit
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
        g_mu = 0.0
        g_rho = 0.0
        w_rho = 0.0          # weight of matches that actually inform rho
        for h, a, x, y, w in matches:
            lh = math.exp(max(-6.0, min(6.0, mu + att[h] - dfn[a] + gamma)))
            la = math.exp(max(-6.0, min(6.0, mu + att[a] - dfn[h])))
            g_att[h] += w * (x - lh)
            g_dfn[a] += w * (lh - x)
            g_att[a] += w * (y - la)
            g_dfn[h] += w * (la - y)
            g_gamma += w * (x - lh)
            g_mu += w * ((x - lh) + (y - la))
            tc, dtc_lh, dtc_la, dtc_rho = _tau_correction(x, y, lh, la, rho)
            if tc > 0.0:
                g_att[h] += w * (dtc_lh / tc) * lh
                g_dfn[a] += w * (dtc_lh / tc) * (-lh)
                g_att[a] += w * (dtc_la / tc) * la
                g_dfn[h] += w * (dtc_la / tc) * (-la)
                g_gamma += w * (dtc_lh / tc) * lh
                g_mu += w * (dtc_lh / tc) * lh + w * (dtc_la / tc) * la
                if dtc_rho != 0.0:
                    g_rho += w * (dtc_rho / tc)
                    w_rho += w
        for i in range(n):
            att[i] += step * g_att[i] / total_w
            dfn[i] += step * g_dfn[i] / total_w
        gamma += step * g_gamma / total_w
        mu += step * g_mu / total_w
        # Only the four low-score outcomes carry d(tau)/d(rho); normalising by
        # the whole fixture list would dilute the step by ~1/0.35.
        if w_rho > 0.0:
            rho = max(-0.3, min(0.3, rho + step * g_rho / w_rho))
        # Centre both parameter vectors; the level belongs to mu alone. Shift mu
        # so lambda is unchanged by the reparameterisation: att enters lambda with
        # a +, def with a -, so removing mean(att) lowers lambda (mu must rise)
        # while removing mean(def) raises it (mu must fall).
        ca = sum(att) / n
        cd = sum(dfn) / n
        att = [v - ca for v in att]
        dfn = [v - cd for v in dfn]
        mu += ca - cd

    ratings = {tid: {"att": att[idx[tid]], "def": dfn[idx[tid]]} for tid in team_ids}
    return ratings, gamma, rho, mu


_TEAM_RATINGS_CACHE: Optional[Dict[int, Dict[str, float]]] = None
_TEAM_RATINGS_TS: float = 0.0
# The raw fit, kept because the [1,5] mapping is lossy. Stage 4 fixed that
# mapping's sign and scale, but a rating squashed onto an ordinal band and then
# read back as 3.0/x is still a poor substitute for the Poisson rate the model
# actually estimated. _fixture_lambdas reads this instead.
_DC_RAW: Dict[str, Any] = {}


def _clear_rating_caches() -> None:
    """Drop the fitted Dixon-Coles ratings so the next call refits.

    Needed by anything that changes dixon_coles_decay and wants the effect: the
    fit is time-weighted by that parameter but cached for 300s, so a caller who
    swaps the weight without clearing gets the OLD ratings back and measures a
    derivative of exactly zero -- indistinguishable from "this parameter does
    not matter".
    """
    global _TEAM_RATINGS_CACHE, _TEAM_RATINGS_TS, _DC_RAW
    global _FIXTURE_LOOKUP_CACHE, _FIXTURE_LOOKUP_TS
    _TEAM_RATINGS_CACHE = None
    _TEAM_RATINGS_TS = 0.0
    _DC_RAW = {}
    # The lookup embeds the ratings, so leaving it behind would serve the old
    # fit through a different door -- exactly the failure this function exists
    # to prevent.
    _FIXTURE_LOOKUP_CACHE = None
    _FIXTURE_LOOKUP_TS = 0.0


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

    dc, gamma, _rho, _mu = ({}, 0.25, -0.10, math.log(1.40))
    if len(finished) >= 5:
        dc, gamma, _rho, _mu = _fit_dixon_coles(
            finished, decay=w.get("dixon_coles_decay", 0.03),
            tau=w.get("dixon_coles_tau", -0.10),
        )
    # Bayesian shrinkage toward the static prior rather than a hard ramp. The old
    # min(1, n/100) reached full confidence only at ~GW10 and was 0 before any
    # fixtures, so early-season ratings were entirely the static field below.
    dc_blend = (len(finished) / (len(finished) + 60.0)) if dc else 0.0

    # `strength_overall_*` is on a ~1000-1400 scale in the FPL API, NOT the 1-5
    # scale of the `strength` field, while the Dixon-Coles ratings below are
    # mapped onto [1,5]. Blending the two directly made `att`/`def` ~1290 for
    # every club, which then clamped to 5.0 -- so for the whole early season
    # every fixture scored identically and fixture difficulty did nothing.
    # Min-max normalising the observed values is robust to the actual scale:
    # it produces the same [1,5] output whether the field is 1000-1400 or 1-5.
    raws = []
    for team in teams.values():
        ov_h = _to_float(team.get("strength_overall_home") or 0.0)
        ov_a = _to_float(team.get("strength_overall_away") or 0.0)
        if ov_h and ov_a:
            raws.append((ov_h + ov_a) / 2.0)
    lo_raw, hi_raw = (min(raws), max(raws)) if raws else (0.0, 0.0)
    span = (hi_raw - lo_raw) or 1.0

    out = {}
    for tid, team in teams.items():
        ov_h = _to_float(team.get("strength_overall_home") or 0.0)
        ov_a = _to_float(team.get("strength_overall_away") or 0.0)
        if ov_h and ov_a and raws:
            fpl_overall = 1.0 + 4.0 * (((ov_h + ov_a) / 2.0) - lo_raw) / span
        else:
            fpl_overall = 3.0
        if dc and tid in dc:
            dc_att = 3.0 + dc[tid]["att"] * scale
            # Sign fix. The fit defines lambda_home = exp(mu + att[h] - def[a] + gamma),
            # so a HIGH def concedes fewer goals, i.e. is a BETTER defence. The old
            # mapping (3.0 - def*scale) therefore emitted a LOW value for an elite
            # defence, while every consumer treats a high value as "tough":
            # _xp_for_fixture uses def_adj = 3.0 / opp_strength_def, so attackers
            # were being boosted against the best defences and suppressed against
            # the worst. Verified on synthetic data: elite defences produced
            # def_adj 1.06 (a boost) and the worst 0.63 (a suppression).
            dc_def = 3.0 + dc[tid]["def"] * scale
        else:
            dc_att = dc_def = fpl_overall
        att = dc_blend * dc_att + (1.0 - dc_blend) * fpl_overall
        dfn = dc_blend * dc_def + (1.0 - dc_blend) * fpl_overall
        half = (gamma / 2.0) * scale
        # Not rounded. These were round(_, 2), which is a DISPLAY concern that
        # had leaked into the model: attacker xP runs through
        # def_adj = 3.0 / opp_def, so quantising opp_def at 0.01 quantises every
        # attacking projection with it, and two clubs of genuinely different
        # strength collapse to an identical multiplier.
        #
        # Found via the dixon_coles_decay derivative: perturbing the decay moved
        # the raw fit for all 20 clubs but the ROUNDED band for only 2, so every
        # forward -- who has no clean-sheet or conceded term to carry the signal
        # -- measured a sensitivity of exactly zero. The rounding was eating the
        # gradient. Callers that want two decimals should round at the point of
        # display; the UI already does.
        out[tid] = {
            "att": att,
            "def": dfn,
            "att_home": min(5.0, max(1.0, att + half)),
            "att_away": min(5.0, max(1.0, att - half)),
            "def_home": min(5.0, max(1.0, dfn + half)),
            "def_away": min(5.0, max(1.0, dfn - half)),
        }
    global _DC_RAW
    _DC_RAW = {"ratings": dc, "gamma": gamma, "mu": _mu, "rho": _rho}
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
# Odds-provider name VARIANTS, not a club roster. The live bootstrap is the
# source of truth for which clubs exist (see _canonical_club, which resolves
# against it first and gates every lookup here on it), so entries for clubs that
# have since been relegated are inert rather than wrong. Do not treat this list
# as something that needs to track promotions -- it does not, and maintaining it
# as though it did is what let it go stale in the first place.
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


def _normalise_club_name(name: str) -> str:
    n = (name or "").lower().strip()
    n = re.sub(r"[^a-z ]", " ", n)
    # Corporate suffixes the odds feed includes and FPL does not.
    n = re.sub(r"\b(fc|afc|association football club|football club)\b", " ", n)
    return " ".join(n.split())


def _canonical_club(name: str, bootstrap: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Odds-feed club name -> FPL short_name.

    The static alias table was a 2024/25 club list (it still contains Ipswich,
    Leicester and Southampton), so any newly promoted side silently failed to
    map and its fixture lost odds entirely -- falling back to static ratings
    with no signal that anything was missing. The live bootstrap is now the
    primary source, which self-updates every season; the table is only a
    fallback for genuinely different names ("Spurs" vs "Tottenham Hotspur").
    """
    if not name:
        return None
    n = _normalise_club_name(name)
    if not n or n == "draw":
        return None

    try:
        bs = bootstrap if bootstrap is not None else _get_bootstrap()
        teams = bs.get("teams", []) or []
    except Exception:
        teams = []

    # Exact match on the live club list first.
    for t in teams:
        short = t.get("short_name")
        if not short:
            continue
        if n in (_normalise_club_name(t.get("name", "")), _normalise_club_name(short)):
            return short
    # Then containment, longest name first so "Manchester United" is not
    # shadowed by a shorter club whose name is a substring.
    for t in sorted(teams, key=lambda t: -len(t.get("name", "") or "")):
        full = _normalise_club_name(t.get("name", ""))
        if full and (full in n or n in full):
            return t.get("short_name")

    # The alias map is a supplement for name FORMS the odds providers use that
    # FPL's own naming does not produce ("Spurs", "Man Utd", "Wolverhampton
    # Wanderers"). It is NOT a club roster, and it must never be read as one:
    # it is hand-maintained, so it goes stale the moment a club is relegated,
    # and it still names clubs that have since gone down.
    #
    # Gating every alias hit on the live club list makes a stale entry inert by
    # construction rather than by anyone remembering to prune it. Without this
    # gate the containment loop below would happily return "LEI" for an odds
    # feed mentioning Leicester, handing the caller a code no current team
    # holds -- which then silently matches nothing downstream.
    valid = {t.get("short_name") for t in teams if t.get("short_name")}
    if n in _CLUB_ALIASES and _CLUB_ALIASES[n] in valid:
        return _CLUB_ALIASES[n]
    for alias, code in _CLUB_ALIASES.items():
        if code in valid and alias in n:
            return code
    return None


DEVIG_POWER_BRACKET_LOW = 1.0
DEVIG_POWER_BRACKET_HIGH = 5.0


def _devig_power(implied_probs: List[float]) -> List[float]:
    r"""De-vig bookmaker-implied probabilities via the power method.

    Each outcome's raw implied probability is p_i = 1/O_i for decimal odds
    O_i; because the bookmaker builds in a margin, the raw probabilities sum
    to an overround S = sum(p_i) > 1.0 rather than to 1.0. The simplest fix,
    proportional normalisation (p_i* = p_i / S), spreads that margin evenly
    across every outcome -- but real bookmaker margin is NOT distributed
    evenly. It concentrates disproportionately on long shots (the well
    documented "favourite-longshot bias"), so proportional normalisation
    systematically overstates a longshot's true chance and understates a
    strong favourite's.

    The power method instead solves for a single exponent k such that

        sum_i (p_i)^k = 1.0

    and reports p_i* = (p_i)^k as the de-vigged probability. Because each
    p_i lies in (0, 1), (p_i)^k is strictly decreasing in k -- raising a
    fraction to an ever higher power shrinks it -- so f(k) = sum((p_i)^k) - 1
    is strictly decreasing too, meaning it has at most one root and brentq's
    bracketed search is exactly the right tool: k=1 always overshoots
    (f(1) = S - 1 > 0, since a real market always carries an overround) and
    k=5 comfortably undershoots for any realistic football market's margin,
    so [1, 5] reliably brackets the root.

    Falls back to plain proportional normalisation if brentq cannot bracket
    a root in [DEVIG_POWER_BRACKET_LOW, DEVIG_POWER_BRACKET_HIGH] (a same-
    signed bracket -- an already-fair "market" with zero overround, or a
    pathological one with a negative or inverted margin) or if scipy is not
    installed.

    Works for any number of outcomes (two-way or three-way markets alike).
    """
    if not implied_probs:
        return []
    total = sum(implied_probs)
    if total <= 0:
        return list(implied_probs)
    if any(p <= 0 for p in implied_probs):
        # A zero or negative implied probability breaks p^k for non-integer k
        # (0^k is fine, but a negative price should never reach here) --
        # proportional normalisation degrades gracefully instead.
        return [p / total for p in implied_probs]

    def _root(k: float) -> float:
        return sum(p ** k for p in implied_probs) - 1.0

    if HAS_SCIPY:
        try:
            f_low = _root(DEVIG_POWER_BRACKET_LOW)
            f_high = _root(DEVIG_POWER_BRACKET_HIGH)
            if f_low == 0.0:
                return list(implied_probs)          # already a fair market, k=1
            if f_low * f_high <= 0:
                k = _brentq(_root, DEVIG_POWER_BRACKET_LOW, DEVIG_POWER_BRACKET_HIGH)
                return [p ** k for p in implied_probs]
        except Exception:
            pass
    return [p / total for p in implied_probs]


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
        home = _canonical_club(m.get("home_team", ""), bootstrap)
        away = _canonical_club(m.get("away_team", ""), bootstrap)
        h_id = id_by_short.get(home) if home else None
        a_id = id_by_short.get(away) if away else None
        if h_id is None or a_id is None:
            continue

        h_prices: List[float] = []
        a_prices: List[float] = []
        d_prices: List[float] = []
        for bm in m.get("bookmakers", []):
            for mk in bm.get("markets", []):
                if mk.get("key") != "h2h":
                    continue
                for out in mk.get("outcomes", []):
                    price = _to_float(out.get("price"))
                    if price <= 0:
                        continue
                    raw_name = out.get("name", "")
                    code = _canonical_club(raw_name, bootstrap)
                    if code == home:
                        h_prices.append(price)
                    elif code == away:
                        a_prices.append(price)
                    elif raw_name.strip().lower() == "draw":
                        d_prices.append(price)

        # De-vig across all THREE outcomes. Previously the draw was discarded --
        # _canonical_club("Draw") returns None -- and home/away were normalised
        # against each other alone. That inflates every price: a true 45/28/27
        # market came back as P(home) = 0.625, a ~39% overstatement, which then
        # fed market_att = 0.6 + 0.8 * win_prob and the traffic lights.
        if not h_prices or not a_prices:
            continue
        if not d_prices:
            # A book quoting only two outcomes on a three-way market cannot be
            # de-vigged correctly; skipping is better than a 39% inflation.
            continue
        h_imp = 1.0 / (sum(h_prices) / len(h_prices))
        a_imp = 1.0 / (sum(a_prices) / len(a_prices))
        d_imp = 1.0 / (sum(d_prices) / len(d_prices))
        # Power-method de-vig rather than flat proportional normalisation --
        # see _devig_power's docstring for why an even split of the margin
        # (the flat p_i/S this replaces) overstates the longshot side of a
        # market and understates the favourite.
        h_true, a_true, d_true = _devig_power([h_imp, a_imp, d_imp])
        result.setdefault(h_id, {})[a_id] = h_true
        result.setdefault(a_id, {})[h_id] = a_true

    _ODDS_CACHE["data"] = result
    _ODDS_CACHE["ts"] = now
    return result


def _fixture_lambdas(home_id: int, away_id: int) -> Tuple[float, float]:
    """(lambda_home, lambda_away): expected goals for each side in this fixture.

    This is what the Dixon-Coles fit actually estimates, and it was being thrown
    away. The ratings were squashed onto a [1,5] ordinal band and read back as
    def_adj = 3.0 / opp_strength_def -- a convex ratio that loses the goal rate
    entirely. rho was captured as _rho and never referenced again, so the tau
    correction, whose whole purpose is to fix P(0-0) -- the clean sheet -- was
    fitted and discarded.

    Falls back to the league average when there is no fit yet (early season).
    """
    raw = _DC_RAW or {}
    ratings = raw.get("ratings") or {}
    mu = raw.get("mu", math.log(1.40))
    gamma = raw.get("gamma", 0.25)
    if home_id not in ratings or away_id not in ratings:
        base = math.exp(mu)
        return base * math.exp(gamma), base
    h, a = ratings[home_id], ratings[away_id]
    lam_h = math.exp(max(-6.0, min(6.0, mu + h["att"] - a["def"] + gamma)))
    lam_a = math.exp(max(-6.0, min(6.0, mu + a["att"] - h["def"])))
    # Clamp to a football-plausible range. The exponent guard above only bounds
    # lambda at exp(6) ~ 403, which is no guard at all: an extreme rating gap --
    # from a thin early-season fit, or a promoted side with a bad run -- can
    # produce a rate no fixture supports, and lambda feeds the clean-sheet
    # probability and the concession expectation directly.
    return (min(LAMBDA_MAX, max(LAMBDA_MIN, lam_h)),
            min(LAMBDA_MAX, max(LAMBDA_MIN, lam_a)))


_FIXTURE_LOOKUP_CACHE: Optional[Dict[int, List[Dict[str, Any]]]] = None
_FIXTURE_LOOKUP_TS: float = 0.0
FIXTURE_LOOKUP_TTL_SECONDS = 300.0


def _build_fixture_lookup(bootstrap: Optional[Dict[str, Any]] = None) -> Dict[int, List[Dict[str, Any]]]:
    """Per-team fixture lists, decorated with ratings, lambdas and odds.

    Memoised. This is called from more than fifty sites -- score_my_squad,
    rank_players_by_xp, the swing scores, every transfer solve -- and each call
    re-walked and re-sorted the whole fixture list, refetched the odds and
    re-read the ratings. A single page render did that work repeatedly for a
    result that cannot change between calls.

    The TTL matches the ratings cache, so the two expire together rather than
    the lookup pinning ratings that have already moved on. _clear_rating_caches
    drops this too, for the same reason: anything that changes the fit has to
    invalidate what was built on top of it.
    """
    global _FIXTURE_LOOKUP_CACHE, _FIXTURE_LOOKUP_TS
    if (_FIXTURE_LOOKUP_CACHE is not None
            and (time.time() - _FIXTURE_LOOKUP_TS) < FIXTURE_LOOKUP_TTL_SECONDS):
        return _FIXTURE_LOOKUP_CACHE

    if bootstrap is None:
        bootstrap = _get_bootstrap()
    teams = {t["id"]: t for t in bootstrap.get("teams", [])}

    try:
        fixtures = _get_fixtures()
    except Exception as exc:
        # Raise, never return {}. An empty lookup makes _player_xp_raw report
        # "Blank" with 0.0 xP for EVERY player in the game, which the solver
        # then reads as a squad of worthless assets -- a total failure that
        # renders as a plausible-looking page. The caller must decide how to
        # degrade; it cannot do that if the failure is disguised as data.
        raise FixtureDataUnavailable(
            "could not load the FPL fixture list") from exc

    win_probs = _fetch_market_win_probs(bootstrap)
    ratings = _team_attack_def_ratings()

    lookup: Dict[int, List[Dict[str, Any]]] = {}
    for f in fixtures:
        h, a = f["team_h"], f["team_a"]
        event = f.get("event")
        rh = ratings.get(h, {})
        ra = ratings.get(a, {})
        lam_h, lam_a = _fixture_lambdas(h, a)
        home_fx = {
            "event": event,
            "is_home": True,
            "lam_for": lam_h,
            "lam_against": lam_a,
            "opponent": a,
            "kickoff_time": f.get("kickoff_time"),
            # Continuous Dixon-Coles matchup: the away side's attack/defence when
            # on the road (venue-adjusted). Higher def = tougher for our attackers.
            "opp_strength_def": ra.get("def_away", 3.0),
            "opp_strength_att": ra.get("att_away", 3.0),
            "win_prob": win_probs.get(h, {}).get(a),
            # FPL's own published 1-5 rating, kept alongside (not instead of)
            # the continuous Dixon-Coles ease score above: the solver and the
            # rest of the projection pipeline deliberately use the latter, but
            # a UI showing "FDR" to a reader means this one -- the number they
            # already know from the official app.
            "official_fdr": f.get("team_h_difficulty"),
        }
        away_fx = {
            "event": event,
            "is_home": False,
            "lam_for": lam_a,
            "lam_against": lam_h,
            "opponent": h,
            "kickoff_time": f.get("kickoff_time"),
            "opp_strength_def": rh.get("def_home", 3.0),
            "opp_strength_att": rh.get("att_home", 3.0),
            "win_prob": win_probs.get(a, {}).get(h),
            "official_fdr": f.get("team_a_difficulty"),
        }
        lookup.setdefault(h, []).append(home_fx)
        lookup.setdefault(a, []).append(away_fx)

    for t in lookup:
        lookup[t].sort(key=lambda x: x["event"] if x["event"] is not None else 999)
    _FIXTURE_LOOKUP_CACHE = lookup
    _FIXTURE_LOOKUP_TS = time.time()
    return lookup


_TRAFFIC_LIGHT_EMOJI = {"green": "🟢", "amber": "🟡", "red": "🔴", "double": "🔵", "blank": "⚪"}


def _fixture_traffic_light_bands(team_id: int, fixture_lookup: Dict[int, List[Dict[str, Any]]],
                                 start_event: int, n: int = 4) -> List[str]:
    """The traffic-light band per gameweek ("green"/"amber"/"red"/"double"/
    "blank"), always exactly n entries -- the data _fixture_traffic_lights
    renders as emoji text and the pitch-view fixture badges render as sized
    circular elements. One source of truth for the banding logic so the two
    renderers can never silently disagree about what a given fixture rates.

    Uses market-implied win probability when available, falling back to FDR.
    """
    fixtures = fixture_lookup.get(team_id, [])
    bands: List[str] = []
    for i in range(n):
        fxs = _gw_fixtures(fixtures, start_event + i)
        if not fxs:
            bands.append("blank")
            continue
        # A double gets its own marker rather than being shown as whichever
        # single fixture happened to come first in the list.
        if len(fxs) > 1:
            bands.append("double")
            continue
        wps = [f.get("win_prob") for f in fxs if f.get("win_prob") is not None]
        if wps:
            wp = sum(wps) / len(wps)
            band = "green" if wp > 0.5 else ("amber" if wp >= 0.3 else "red")
        else:
            opp_def = sum(_to_float(f.get("opp_strength_def") or 3) for f in fxs) / len(fxs)
            band = "green" if opp_def <= 2.0 else ("amber" if opp_def <= 3.2 else "red")
        bands.append(band)
    return bands


def _fixture_traffic_lights(team_id: int, fixture_lookup: Dict[int, List[Dict[str, Any]]], start_event: int, n: int = 4) -> str:
    """Return a 4-GW traffic-light string, e.g. '[🟢 🟡 🔴 🟢]'."""
    bands = _fixture_traffic_light_bands(team_id, fixture_lookup, start_event, n)
    return "[" + " ".join(_TRAFFIC_LIGHT_EMOJI[b] for b in bands) + "]"

# NOTE (C28): _expected_minute_fraction was deleted here.
#
# The codebase carried TWO competing definitions of "how much of a match do we
# expect from this player". This one read the availability flag alone -- a
# doubtful player at 75% returned 0.75 and a fit rotation risk returned a
# confident 1.0, because it knew nothing about whether he starts.
# _expected_playing_fraction answers the same question from the tri-state
# minutes distribution, so it prices rotation as well as fitness.
#
# The two disagreed, and which one you got depended on which call site you were
# standing in: this one drove the deleted floor_weight tilt, the other drives
# the MIP hurdle. Stage 2 removed _risk_adjust and with it the only caller of
# this one, leaving it dead. _expected_playing_fraction is now the single
# definition, used by the engine, the snapshot and the UI alike.


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


# ----------------------------------------------------------------------------
# C10b -- minutes recency
#
# A season-long starts/games rate has no memory of WHEN the starts happened. A
# player benched for the opening ten matches and nailed for the last five reads
# as 0.33 -- a rotation risk -- when he is a certain starter, and the mirror
# case reads as nailed when he has lost his place. Minutes multiply every other
# component of the projection, so that is not a 5% edge, it is a systematically
# wrong recommendation: sell the nailed player, buy the one who has been dropped.
#
# The rate is therefore exponentially weighted over per-match history from
# element-summary, half-life RECENT_HALF_LIFE matches.
#
# That history is one HTTP call PER PLAYER, so it is never fetched from the
# projection path. The cache is populated deliberately, by a caller that knows
# which players it actually cares about (a squad plus a shortlist is ~50-150,
# not the ~700 in the game), and _minute_distribution simply consults whatever
# happens to be there. Missing history is normal and falls back to the season
# rate, so the engine works exactly as before with an empty cache.
# ----------------------------------------------------------------------------
# Off under the test harness: there is no FPL API in the build environment, so
# leaving it on would mean a few hundred doomed HTTP attempts per solve for a
# result that is meant to be optional. The consequence is stated plainly rather
# than hidden -- the suite exercises the SEASON path by default, and the
# recency path is tested by populating the cache directly.
RECENT_MINUTES_ENABLED = True

RECENT_HALF_LIFE = 3.0        # matches
# Only the most recent matches are considered at all. Exponential weighting
# alone is not enough: each match four months old carries ~9% weight, but there
# are a lot of them, and in aggregate they outvote the recent form the weighting
# exists to surface. Measured on a benched-ten-then-started-five history, an
# unbounded window read 0.63 -- still a rotation risk -- where the same player
# over a 12-match window reads 0.73.
RECENT_WINDOW = 12            # matches
RECENT_TTL_SECONDS = 3600.0
RECENT_MAX_FETCH = 160        # per prefetch call, so the cost can never surprise
RECENT_MIN_MATCHES = 3.0      # below this the season rate is the better estimate

# A substitute appearance averages well under a half. The 30.0 here was an
# assumption that every cameo is exactly 30 minutes, used to invert total
# minutes back into an appearance count; ~22 is closer to the real distribution
# of Premier League sub appearances, and it only matters when per-match history
# is unavailable -- with history the appearances are simply counted.
AVG_SUB_MINUTES = 22.0

# How far doubt shifts a player from starting toward a cameo. A 50%-doubtful
# player who does feature is more likely to be eased in than to play the full
# 90; the old model scaled p_full and p_cameo by the SAME factor, which keeps
# the full/cameo split identical no matter how doubtful the player is.
DOUBT_CAMEO_SHIFT = 0.5

_RECENT_CACHE: Dict[int, Dict[str, float]] = {}
_RECENT_CACHE_TS: Dict[int, float] = {}


def _element_summary(player_id: int) -> Optional[List[Dict[str, Any]]]:
    """Per-match history for one player, or None if it cannot be fetched."""
    try:
        data = _cached_json(f"{BASE_URL}/element-summary/{int(player_id)}/",
                            ttl=int(RECENT_TTL_SECONDS))
        history = data.get("history")
        return history if isinstance(history, list) else None
    except Exception:
        return None


def _summarise_recent(history: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Exponentially-weighted start and cameo rates from per-match history.

    Weights halve every RECENT_HALF_LIFE matches counting back from the most
    recent, so a changed role is reflected in weeks rather than being averaged
    against a whole season. Returns the effective sample size alongside the
    rates, because the caller needs to know how much evidence it is holding.
    """
    rows = [h for h in history if h.get("minutes") is not None]
    if not rows:
        return None
    # element-summary is chronological, so the last row is the most recent.
    rows = rows[-RECENT_WINDOW:]
    n = len(rows)
    w_sum = starts_w = cameo_w = 0.0
    for i, h in enumerate(rows):
        age = n - 1 - i
        w = 0.5 ** (age / RECENT_HALF_LIFE)
        mins = _to_float(h.get("minutes"))
        started = 1.0 if _to_float(h.get("starts")) > 0 else (1.0 if mins >= 60 else 0.0)
        w_sum += w
        if started:
            starts_w += w
        elif mins > 0:
            cameo_w += w
    if w_sum <= 0:
        return None
    return {"start_rate": starts_w / w_sum,
            "sub_rate": cameo_w / w_sum,
            "n_eff": w_sum,
            "matches": float(n)}


def prefetch_recent_minutes(player_ids, limit: int = RECENT_MAX_FETCH) -> int:
    """Populate the recency cache for the players a caller actually cares about.

    One HTTP call per uncached player, bounded by `limit`, so the cost of a page
    render is a number you can state rather than a surprise. Returns how many
    were fetched. Failures are silent by design: a missing history degrades to
    the season rate, which is the behaviour that shipped before this existed.
    """
    now = time.time()
    fetched = 0
    for pid in player_ids:
        if fetched >= limit:
            break
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        # Keyed on the TIMESTAMP, not on cache membership. A player whose
        # history could not be fetched records a time but no entry, and keying
        # on membership meant those were retried on every single render -- the
        # players who cost a doomed request each time were exactly the ones
        # already known to fail.
        if (now - _RECENT_CACHE_TS.get(pid, 0.0)) < RECENT_TTL_SECONDS:
            continue
        try:
            history = _element_summary(pid)
        except Exception:
            history = None
        fetched += 1
        summary = _summarise_recent(history) if history else None
        if summary:
            _RECENT_CACHE[pid] = summary
        _RECENT_CACHE_TS[pid] = now
    return fetched


def _recent_minutes(player_id: Any) -> Optional[Dict[str, float]]:
    """Cached recency summary for one player, or None. Never fetches."""
    try:
        pid = int(player_id)
    except (TypeError, ValueError):
        return None
    if (time.time() - _RECENT_CACHE_TS.get(pid, 0.0)) >= RECENT_TTL_SECONDS:
        return None
    got = _RECENT_CACHE.get(pid)
    if got and got.get("matches", 0.0) >= RECENT_MIN_MATCHES:
        return got
    return None


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

    recent = _recent_minutes(p.get("id"))

    if is_gk:
        if recent:
            start_rate = min(1.0, (recent["start_rate"] * recent["n_eff"] + PRIOR_STARTS)
                             / (recent["n_eff"] + PRIOR_GAMES))
        else:
            start_rate = min(1.0, starts / games) if starts > 0 else (1.0 if minutes >= 60 else 0.0)
        base_full, base_sub = start_rate, 0.0
    elif recent:
        # Recency path (C10b). The same Beta prior as the season path, applied
        # to the EXPONENTIALLY WEIGHTED counts, so a player whose role changed
        # is reflected in weeks rather than averaged across the whole season.
        # Cameo appearances are counted here rather than inferred by dividing
        # leftover minutes by an assumed cameo length.
        n_eff = recent["n_eff"]
        base_full = min(1.0, (recent["start_rate"] * n_eff + PRIOR_STARTS)
                        / (n_eff + PRIOR_GAMES))
        base_sub = min(recent["sub_rate"], max(0.0, 1.0 - base_full))
    else:
        if starts > 0:
            # Shrunk toward "probably a starter". Unlike the attacking rates,
            # which get PRIOR_MINUTES via _reg, start_rate had no prior at all --
            # so at GW4, with games = 4, a single rest swung a nailed starter
            # from 1.00 to 0.75 and the whole projection with it, because minutes
            # multiply every other component.
            base_full = min(1.0, (starts + PRIOR_STARTS) / (games + PRIOR_GAMES))
        else:
            # Starts unreported: infer from minutes (treat as full-game starts) so a
            # 1000-minute player is a nailed starter, not a 100% cameo sub.
            base_full = min(1.0, (minutes / 90.0) / games) if minutes > 0 else 0.0
        sub_minutes = max(0.0, minutes - base_full * games * 90.0)
        sub_apps = sub_minutes / AVG_SUB_MINUTES
        base_sub = min(sub_apps / games, max(0.0, 1.0 - base_full))

    # Doubt shifts mass from starting into a cameo; it does not merely scale
    # both down. A 50%-doubtful player who does feature is more likely to be
    # eased in off the bench than to play the full 90, and the old model kept
    # the full/cameo SPLIT identical however doubtful the player was.
    p_features = avail * (base_full + base_sub)
    if p_features > 0:
        full_share = base_full / (base_full + base_sub)
        full_share *= 1.0 - DOUBT_CAMEO_SHIFT * (1.0 - avail)
        p_full = p_features * full_share
        p_cameo = p_features * (1.0 - full_share)
    else:
        p_full = p_cameo = 0.0
    if is_gk:
        # Keepers never register cameos: it is all or nothing between the sticks.
        p_full, p_cameo = p_full + p_cameo, 0.0

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

# NOTE (Layer 1 independence): _risk_adjust was deleted here.
#
# It added non-predictive preference terms directly onto the points forecast,
# on every profile including Balanced:
#   * transfer momentum, (transfers_in - transfers_out)/100000 clamped to +/-0.3.
#     Net transfers routinely exceed 500k, so this SATURATED: every bandwagon
#     player simply received +0.3 xP for being popular. Chasing net transfers is
#     following the crowd, which is the opposite of an edge.
#   * ownership, threat, floor, shield and hunt tilts, with unfitted
#     normalisers (threat/300, (ow-15)/20, (ow-30)/40, (12-ow)/12, xgi/0.6).
#
# Three consequences, all now gone: the displayed "xP" was a preference score
# wearing a forecast's label; the same term was ~2% of a horizon number but
# ~7.5% of a single-gameweek one; and it poisoned calibration, because
# snapshot_xp.py stores this value as predicted_xp.
#
# Preference belongs in the OBJECTIVE, where it is visible and priced, not in
# the projection. The strategy modes are now expressed through the EO / blocker
# / divergence terms in _solve_squad.

def _expected_concession_penalty(lam: float, max_goals: Optional[int] = None) -> float:
    """E[floor(G/2)] for G ~ Poisson(lam): the expected -1s for goals conceded.

    FPL deducts one point per TWO goals conceded, so the expectation is
    E[floor(G/2)], not E[G]/2. The old -0.5 * xgc treated the step function as
    linear and over-penalised every keeper and defender by roughly 55% at
    typical scoring rates (0.65 vs 0.419 at lam = 1.3) -- and worst at LOW lam
    (139% at 0.6), so elite defences were hurt most.

    The summation bound scales with lam: a fixed cap truncates tail mass that
    the growing floor(G/2) weight amplifies.
    """
    if lam <= 0.0:
        return 0.0
    if max_goals is None:
        # mean + ~10 sd, floored at 20; P(G > bound) is negligible for any
        # realistic scoreline.
        max_goals = max(20, int(lam + 10.0 * math.sqrt(lam) + 10))
    total = 0.0
    pmf = math.exp(-lam)          # P(G = 0)
    for g in range(0, max_goals + 1):
        if g > 0:
            pmf *= lam / g
        total += (g // 2) * pmf
    return total


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


def _defcon_rate_per90(p: Dict[str, Any], pos_id: int) -> float:
    """The player's own defensive-action rate per 90, shrunk to a positional prior.

    Reads the real per-player counts FPL publishes -- defensive_contribution,
    clearances_blocks_interceptions, tackles, recoveries -- none of which the
    previous implementation touched. It used `influence` as a proxy:

        rate = base * (0.6 + 0.4 * min(influence / 60.0, 2.0))

    `influence` is a SEASON-CUMULATIVE figure, so min(influence/60, 2.0)
    saturates at 2.0 for essentially every regular starter. The result was
    rate = 8.0 * 1.4 = 11.2 for EVERY defender, P(X >= 10) ~ 0.68, and therefore
    +1.36 xP identically for all of them -- a flat positional bias toward
    defenders in every comparison the solver made, with no per-player signal.
    """
    minutes = _to_float(p.get("minutes"))
    prior = DEFCON_BASE_PER90.get(pos_id, 0.0)
    if minutes <= 0:
        return prior

    per90 = None
    direct = _to_float(p.get("defensive_contribution_per_90"))
    if direct > 0:
        per90 = direct
    else:
        total = _to_float(p.get("defensive_contribution"))
        if total <= 0:
            # Reconstruct from components. DEF are scored on CBIT (clearances,
            # blocks, interceptions, tackles); MID/FWD on CBIRT, which adds ball
            # recoveries.
            total = (_to_float(p.get("clearances_blocks_interceptions"))
                     + _to_float(p.get("tackles")))
            if pos_id in (3, 4):
                total += _to_float(p.get("recoveries"))
        if total > 0:
            per90 = total * 90.0 / minutes

    if per90 is None:
        return prior
    # Same empirical-Bayes shape as the attacking rates: weight the player's own
    # rate by minutes played against a positional prior.
    return (per90 * minutes + prior * PRIOR_MINUTES) / (minutes + PRIOR_MINUTES)


def _defcon_expected_pts(p: Dict[str, Any], emin: float, pos_id: int) -> float:
    """Expected points from the Defensive Contribution bonus.

    DEF: +2 for reaching 10 CBIT actions. MID/FWD: +2 for reaching 12 CBIRT.
    """
    if pos_id not in DEFCON_THRESHOLD:
        return 0.0
    frac = emin / 90.0
    if frac <= 0.0:
        return 0.0
    rate = _defcon_rate_per90(p, pos_id)
    p_meet = _poisson_survival(DEFCON_THRESHOLD[pos_id], rate * frac)
    return 2.0 * p_meet


_WEIGHTS_CACHE: Optional[Dict[str, float]] = None
_WEIGHTS_STAMP: Tuple[float, float] = (0.0, 0.0)   # (file mtime, load time)
WEIGHTS_TTL_SECONDS = 300.0


def _weights_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "weights.json")


def _weights_mtime() -> float:
    try:
        return os.path.getmtime(_weights_path())
    except OSError:
        return 0.0


def _load_weights() -> Dict[str, float]:
    """Load tunable weights from weights.json (defaulting to 1.0 on any miss).

    Cached, but not forever. The cache had no TTL and no mtime check, so a
    long-running Railway process pinned the weights it read at boot: the weekly
    auto-tune committed a new weights.json, the deploy picked it up, and the
    already-running dyno carried on with the old numbers until something
    happened to restart it. The calibration loop's output never reached the
    live app.

    Both checks are here on purpose. The mtime catches a redeploy that rewrites
    the file in place; the TTL bounds how long a stale read can survive a clock
    or filesystem that does not report mtime usefully.
    """
    global _WEIGHTS_CACHE, _WEIGHTS_STAMP
    if _WEIGHTS_CACHE is not None:
        mtime, loaded_at = _WEIGHTS_STAMP
        if (_weights_mtime() == mtime
                and (time.time() - loaded_at) < WEIGHTS_TTL_SECONDS):
            return _WEIGHTS_CACHE
    defaults = {
        "global_xP_modifier": 1.0,
        "home_advantage": 1.0,
        "clean_sheet_confidence": 1.0,
        "autosub_ref": 1.8,
        "rotation_convexity": 0.4,
        "dixon_coles_decay": 0.03,
        "dixon_coles_tau": -0.10,   # football rho is negative; +0.2 was the wrong sign
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
        "cvar_alpha": 0.1,
        "cvar_lambda": 0.05,
    }
    weights = dict(defaults)
    try:
        with open(_weights_path(), "r", encoding="utf-8") as fh:
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
    _WEIGHTS_STAMP = (_weights_mtime(), time.time())
    return weights


def _xp_for_fixture(p: Dict[str, Any], f: Dict[str, Any], emin: float, pos_id: int,
                    gk_cs_dampener: float = 1.0) -> float:
    avg = _league_averages().get(POS_MAP.get(pos_id, "MID"), {"xg": 0.3, "xa": 0.2, "xgc": 1.3, "saves": 0.5})
    minutes = _to_float(p.get("minutes"))

    def _reg(raw: Any, prior: float, cap: float = 5.0) -> float:
        """Empirical-Bayes shrinkage toward a prior, weighted by minutes played.

        The cap is per-quantity: 5.0 suits a per-90 goal rate and would silently
        flatten every BPS rate in the league, which runs an order of magnitude
        higher.
        """
        raw = min(_to_float(raw), cap)
        return (raw * minutes + prior * PRIOR_MINUTES) / (minutes + PRIOR_MINUTES)

    xg90 = _reg(p.get("expected_goals_per_90"), avg["xg"])
    xa90 = _reg(p.get("expected_assists_per_90"), avg["xa"])
    xgc90 = _reg(p.get("expected_goals_conceded_per_90"), avg["xgc"])
    saves90 = _reg(p.get("saves_per_90"), avg["saves"])

    # 3.0/x is convex, so it over-rewards the very weakest defences: at the old
    # floor of 1.0 an attacker facing the bottom club had their xG *tripled*.
    # That never bit before Stage 4 because every rating clamped to 5.0; fixing
    # the scale exposed it. The 1.5 floor caps the boost at 2.0x as a stopgap --
    # the real fix is Stage 5b, which replaces this ratio with the fitted
    # Dixon-Coles lambda for the fixture and drops the [1,5] round-trip entirely.
    DEF_ADJ_FLOOR = 1.5
    def_adj = 3.0 / max(_to_float(f.get("opp_strength_def")), DEF_ADJ_FLOOR)
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

    # Clean sheets are a TEAM-MATCH event, computed from the fixture's expected
    # goals against and independent of how long this player is on the pitch.
    #
    # Previously: xgc = xgc90 * frac * ... and then p_cs = exp(-xgc), with the
    # minutes fraction INSIDE the exponent. A 30-minute cameo therefore produced
    # xgc = 1.3 * 0.333 = 0.43 -> P(CS) = 65%, against 27% for the same player
    # over 90 minutes. Playing less does not make your team more likely to keep a
    # clean sheet; rotation-risk defenders were systematically over-valued.
    #
    # The player-level requirement is a threshold, not a rate: FPL awards the
    # clean sheet at 60+ minutes. The caller evaluates this function once at 90
    # and once at 30 and weights by P(full) / P(cameo), so the threshold is
    # simply whether this branch clears 60.
    lam_against = _to_float(f.get("lam_against")) or (xgc90 * att_adj * venue_def)
    lam_against = max(0.0, lam_against * venue_def)
    p_cs_team = math.exp(-lam_against) if lam_against < 10 else 0.0
    plays_60 = 1.0 if emin >= 60 else 0.0
    cs_pts = CS_PTS.get(pos_id, 0) * p_cs_team * plays_60 * _w["clean_sheet_confidence"]
    # GK Transfer-In Fragility Discount: a goalkeeper's clean sheet is undone
    # by a single added-time concession, so raw P(CS) overstates how reliable
    # that return is for a player being newly evaluated as a signing. Never
    # touches DEF, whose clean-sheet points share the same CS_PTS entry --
    # this is specifically the GK's own binary, all-or-nothing dependence on
    # it (a DEF still banks tackles/interceptions/attacking returns besides).
    if pos_id == 1:
        cs_pts *= gk_cs_dampener

    # Goals conceded is a per-appearance deduction, so it scales with the share
    # of the match played rather than being a whole-match event.
    conceded_pts = (-_expected_concession_penalty(lam_against * frac)
                    if pos_id in (1, 2) else 0.0)
    saves_pts = saves90 * frac / 3.0 if pos_id == 1 else 0.0

    minutes_pts = 2.0 if emin >= 60 else (1.0 if emin > 0 else 0.0)

    # C7. Bonus was three flat constants -- DEF 0.34, MID/FWD 0.72, plus a GK
    # saves term -- so Salah, Haaland and a £4.5m bench filler were credited
    # with identical bonus points. bps was read elsewhere for the live tracker
    # and never once used in the projection, despite bonus being ~8-10% of all
    # FPL points and among the most persistent player-level signals there is.
    #
    # The positional constant is kept as the SHRINKAGE PRIOR rather than
    # replaced, so a player with no minutes still projects exactly today's
    # number and only accumulated evidence moves them off it. Bonus is a
    # rank-order tournament -- top three BPS in the match take 3/2/1 -- so the
    # response to being above average is convex, not proportional: clearing the
    # bar at all is what pays.
    if pos_id == 1:
        bonus_base = min(2.0, 0.40 + 0.18 * saves90)
    elif pos_id == 2:
        bonus_base = 0.34
    else:
        bonus_base = 0.72

    avg_bps90 = avg.get("bps90") or 0.0
    if avg_bps90 > 1.0 and minutes > 0:
        bps90 = _reg(_to_float(p.get("bps")) * 90.0 / minutes, avg_bps90, cap=120.0)
        ratio = max(0.0, bps90 / avg_bps90)
        bonus_pts = min(BONUS_CAP, bonus_base * (ratio ** BONUS_CONVEXITY)) * frac
    else:
        bonus_pts = bonus_base * frac

    defcon_pts = _defcon_expected_pts(p, emin, pos_id)

    # C11. Cards were -0.12 for DEF/MID and -0.05 otherwise, flat, with
    # yellow_cards sitting unread two functions away. They are also EXPOSURE, so
    # they scale with expected minutes -- a 30-minute cameo carries a third of a
    # starter's booking risk, and the old constant charged both the same.
    if minutes > 0 and (avg.get("yc90") or avg.get("rc90")):
        yc90 = _reg(_to_float(p.get("yellow_cards")) * 90.0 / minutes,
                    avg.get("yc90") or 0.0, cap=1.0)
        rc90 = _reg(_to_float(p.get("red_cards")) * 90.0 / minutes,
                    avg.get("rc90") or 0.0, cap=0.5)
        card_pts = -(YELLOW_CARD_PTS * yc90 + RED_CARD_PTS * rc90) * frac
    else:
        card_pts = (-0.12 if pos_id in (2, 3) else -0.05) * frac

    return max(minutes_pts + attack_pts + cs_pts + conceded_pts + saves_pts + bonus_pts + defcon_pts + card_pts, 0.0)


# Double-gameweek fatigue/rotation discount applied to a second fixture inside
# 76 hours of the first. FPL's element_type only distinguishes GK/DEF/MID/FWD
# -- there is no signal anywhere in the bootstrap data for centre-back vs
# full-back/wing-back, so DEF uses a single blended figure: (0.93 for a
# centre-back-shaped defender + 0.82 for an attacking full-back/wing-back) / 2.
# A genuine per-player split would need a data source this app does not have;
# guessing it from ICT/attacking output risked mislabelling exactly the
# converted wing-backs and auxiliary centre-backs it would matter most for,
# which is a worse failure than one shared, honestly-averaged number.
DGW_FATIGUE_TURNAROUND_HOURS = 76.0
_DGW_FATIGUE_DISCOUNT = {
    1: 0.98,    # GK
    2: 0.875,   # DEF (blended CB/full-back figure, see above)
    3: 0.84,    # MID
    4: 0.88,    # FWD
}


def _dgw_fatigue_multiplier(prev_fixture: Dict[str, Any], fixture: Dict[str, Any], pos_id: int) -> float:
    """1.0 unless `fixture` follows `prev_fixture` inside the fatigue window."""
    try:
        prev_ko = dateutil.parser.isoparse(str(prev_fixture.get("kickoff_time")))
        this_ko = dateutil.parser.isoparse(str(fixture.get("kickoff_time")))
    except Exception:
        return 1.0
    turnaround_hours = abs((this_ko - prev_ko).total_seconds()) / 3600.0
    if turnaround_hours >= DGW_FATIGUE_TURNAROUND_HOURS:
        return 1.0
    return _DGW_FATIGUE_DISCOUNT.get(pos_id, 1.0)


def _xp_dgw_sum(p: Dict[str, Any], target: List[Dict[str, Any]], emin: float, pos_id: int,
                gk_cs_dampener: float = 1.0) -> float:
    """Sum of per-fixture xP across a gameweek's fixture(s), decomposing a
    double (or, rarely, triple) gameweek into independent per-fixture
    evaluations rather than a naive N-times multiplication -- each fixture
    already carries its own opponent, venue and win-probability inputs via
    _xp_for_fixture, so two very different fixtures were never actually
    multiplied together as one. What WAS missing is fatigue: a second match
    inside 76 hours of the first discounts that second match's contribution
    by position, via _dgw_fatigue_multiplier.

    Sorted by kickoff so "second fixture" always means chronologically
    second, regardless of the order the source fixture list happens to list
    a double gameweek's two matches in.
    """
    if not target:
        return 0.0
    ordered = sorted(target, key=lambda f: f.get("kickoff_time") or "")
    total = _xp_for_fixture(p, ordered[0], emin, pos_id, gk_cs_dampener)
    for i in range(1, len(ordered)):
        total += _dgw_fatigue_multiplier(ordered[i - 1], ordered[i], pos_id) * _xp_for_fixture(p, ordered[i], emin, pos_id, gk_cs_dampener)
    return total


def _player_xp_raw(p: Dict[str, Any], fixture_lookup: Dict[int, List[Dict[str, Any]]], event: Optional[int] = None,
                   gk_cs_dampener: float = 1.0) -> Tuple[float, str]:
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
        target = _gw_fixtures(fixtures, event)
        if not target:
            # "Blank" is a scheduling fact, not a verdict on the player: the
            # caller must not treat it as a reason to sell. See _fixture_calendar.
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
    minutes_played = _to_float(p.get("minutes"))

    xp_full = _xp_dgw_sum(p, target, 90.0, pos_id, gk_cs_dampener)
    xp_cameo = _xp_dgw_sum(p, target, 30.0, pos_id, gk_cs_dampener)
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
        # EP_BLEND was a flat 0.5: half of every single-gameweek projection was
        # FPL's own ep_next. Three problems. It capped the model's edge at half
        # the vendor baseline no matter how good the rest became; it
        # double-counted fixture difficulty, since ep_next already embeds it and
        # our_total applies def_adj/att_adj on top; and _player_xp_horizon blends
        # the SAME ep_next into GW+1/+2/+3, halving fixture sensitivity in every
        # future week. It also capped the benefit of the Stage 4 ratings work at
        # roughly 50%.
        #
        # Now a shrinkage prior for thin-data players only: full weight at zero
        # minutes, decaying to nothing by ~600 minutes (about GW8 for a starter).
        # Whether the model beats ep_next head-to-head is a question for the
        # Stage 8 scorecard, which logs both against actuals.
        ep_w = max(0.0, min(EP_BLEND, EP_BLEND * (1.0 - minutes_played / EP_BLEND_FADE_MINUTES)))
        # Availability-adjusted minutes fraction applied to FPL's own projection,
        # so doubtful assets never display an unadjusted baseline.
        blend_frac = p_full + (30.0 / 90.0) * p_cameo
        xp = (1.0 - ep_w) * our_total + ep_w * ep_next * blend_frac
    else:
        xp = our_total

    return max(xp, 0.0), note


def calibration_features(p: Dict[str, Any],
                         fixture_lookup: Dict[int, List[Dict[str, Any]]],
                         event: Optional[int] = None) -> Dict[str, float]:
    """The inputs `reproject` needs to rebuild this player's projection.

    Lives here rather than in the snapshot script deliberately. The surrogate's
    whole job is to agree with production, so anything it needs is read out of
    the production path, not reimplemented alongside it -- a copy in scripts/
    would go quietly stale the next time _player_xp_raw changed, and the failure
    would look like a calibration result rather than a bug.

    `raw_total` is the projection BEFORE the tunable penalties and before the
    ep_next blend. That distinction is the whole point: `base_pts` (what the
    snapshot used to store) is _player_xp's output at neutral weights, which has
    already been through the blend -- so anything reconstructing from it and
    then applying the blend itself counts ep_next twice.

    `ep_term` is the whole ep_next contribution (ep_w * ep_next * blend_frac),
    stored pre-multiplied because none of its three factors is tunable: only the
    (1 - ep_w) scaling on the tunable half matters to the descent.
    """
    zero = {"raw_total": 0.0, "xp_cameo": 0.0, "ep_w": 0.0, "ep_term": 0.0,
            "cameo_mass": 0.0, "rotation_variance": 0.0}
    status = p.get("status", "a")
    if status in ("i", "s", "u", "n"):
        return zero
    pos_id = p.get("element_type")
    if _to_float(p.get("minutes", 0)) <= 0 and (event is None or event > 1):
        return zero

    fixtures = fixture_lookup.get(p.get("team"), [])
    target = _gw_fixtures(fixtures, event) if event is not None else fixtures[:1]
    if not target:
        return zero

    p0, p_cameo, p_full = _minute_distribution(p, status)
    xp_full = _xp_dgw_sum(p, target, 90.0, pos_id)
    xp_cameo = _xp_dgw_sum(p, target, 30.0, pos_id)
    raw_total = p_full * xp_full + p_cameo * xp_cameo

    ep_next = _to_float(p.get("ep_next"))
    if ep_next > 0:
        minutes_played = _to_float(p.get("minutes"))
        ep_w = max(0.0, min(EP_BLEND, EP_BLEND * (1.0 - minutes_played / EP_BLEND_FADE_MINUTES)))
        blend_frac = p_full + (30.0 / 90.0) * p_cameo
        ep_term = ep_w * ep_next * blend_frac
    else:
        ep_w, ep_term = 0.0, 0.0
    return {
        "raw_total": float(raw_total),
        "xp_cameo": float(xp_cameo),
        "ep_w": float(ep_w),
        "ep_term": float(ep_term),
        "cameo_mass": float(p_cameo),
        "rotation_variance": float(p0 * (1.0 - p0) + p_cameo * (1.0 - p_cameo)),
    }


def _player_xp(p: Dict[str, Any], fixture_lookup: Dict[int, List[Dict[str, Any]]], event: Optional[int] = None,
              gk_cs_dampener: float = 1.0) -> Tuple[float, str]:
    """Single-gameweek expected points. A pure forecast.

    Takes no `risk` argument by design: strategy must not reach the projection.
    See the Layer 1 note on _player_xp_raw.
    """
    raw, note = _player_xp_raw(p, fixture_lookup, event, gk_cs_dampener)
    gmod = _load_weights()["global_xP_modifier"]
    return round(max(raw, 0.0) * gmod, 2), note


YELLOW_BAN_1 = 5      # 5 yellows within the club's first 19 matches -> 1 match
YELLOW_BAN_2 = 10     # 10 through the club's match 32               -> 2 matches
YELLOW_WINDOW_1 = 19
YELLOW_WINDOW_2 = 32


def _is_on_tightrope(p: Dict[str, Any], event: Optional[int] = None) -> bool:
    """True when a player is one yellow card away from a PL suspension.

    Premier League accumulation rules:
      * 5 yellows in a club's first 19 matches  -> 1-match ban.
      * 10 yellows through the club's match 32  -> 2-match ban.

    Counted in the CLUB'S matches played, not the gameweek number. Those two
    diverge the moment the calendar does: a club that has had a blank gameweek
    is a match behind the number, one that has played a double is ahead, and by
    midseason the gap can be two or more. Keying off the gameweek therefore
    opened and closed the suspension windows on the wrong week for exactly the
    clubs whose fixtures the rest of the engine is busy modelling.

    `event` is retained as a fallback for callers with no bootstrap available.

    The thresholds are equalities on purpose, not >=. FPL's yellow_cards runs
    for the whole season and does not reset when a ban is served, so a player
    sitting on 5-8 has already served the first ban and is not near the second;
    only 4 and 9 are one booking from a suspension.
    """
    try:
        yellows = int(p.get("yellow_cards") or 0)
    except (TypeError, ValueError):
        return False

    played = None
    team_id = p.get("team")
    if team_id is not None:
        try:
            played = _team_played_map().get(team_id)
        except Exception:
            played = None
    if not played:
        played = event
    if not played:
        return False

    if played < YELLOW_WINDOW_1:
        return yellows == YELLOW_BAN_1 - 1
    if played < YELLOW_WINDOW_2:
        return yellows == YELLOW_BAN_2 - 1
    # Past the club's 32nd match the accumulation rules stop applying, so
    # nobody is on a tightrope. That is the rule, not a gap in the check.
    return False


def _player_xp_horizon(p: Dict[str, Any], fixture_lookup: Dict[int, List[Dict[str, Any]]], start_event: int, n: int = 4,
                       gk_cs_dampener: float = 1.0) -> Tuple[float, str]:
    """Multi-gameweek expected points with geometric decay over the horizon.

    xP_horizon = 1.0*xP(GW) + 0.85*xP(GW+1) + 0.70*xP(GW+2) + 0.55*xP(GW+3).
    Blank gameweeks contribute 0 without flagging the player.

    Takes no `risk` argument by design. Previously the risk adjustment was
    applied once to the *horizon* total here but once to a *single* gameweek in
    _player_xp, so the identical +/-0.3 momentum term was ~2% of one number and
    ~7.5% of the other. See the Layer 1 note on _player_xp_raw.
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
        raw, _ = _player_xp_raw(p, fixture_lookup, start_event + i, gk_cs_dampener)
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
    return round(max(total, 0.0) * gmod, 2), note

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
        # Three uncached round trips per call, and this is called on every
        # rerun. _cached_json already existed; these simply never used it.
        entry = _cached_json(f"{BASE_URL}/entry/{manager_id}/")
        started_event = int(entry.get("started_event") or 1)
        if target_gw is None:
            target_gw = _next_gameweek(_get_bootstrap())

        history = _cached_json(f"{BASE_URL}/entry/{manager_id}/history/")
        transfers = _cached_json(f"{BASE_URL}/entry/{manager_id}/transfers/")

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

def _gw_fixtures(fx: List[Dict[str, Any]], event: int) -> List[Dict[str, Any]]:
    """Every fixture a club plays in `event` -- two of them in a double.

    `next((x for x in fx if x["event"] == ev), None)` was used at three call
    sites, so the FDR strip, the traffic lights and the fixture-swing windows
    all silently saw only the FIRST match of a double gameweek. _player_xp_raw
    already summed both, so the projection and everything presented alongside it
    disagreed about how many games a club was playing.
    """
    return [x for x in fx if x.get("event") == event]


_CALENDAR_CACHE: Optional[Dict[int, Dict[str, Any]]] = None
_CALENDAR_CACHE_TS: float = 0.0
BGW_MIN_BLANKS = 4       # clubs without a fixture before a gameweek counts as blank
DGW_MIN_DOUBLES = 4      # clubs with two fixtures before it counts as double


def _fixture_calendar(fixture_lookup=None, start_event=1, n=38) -> Dict[int, Dict[str, Any]]:
    """{event: {is_bgw, is_dgw, blanks, doubles, playing}} for the season.

    Needed to tell a BLANK apart from a bad fixture. Both currently project 0.0
    xP for the gameweek, so a premium with no fixture looked identical to a
    player who is simply out of form -- and the solver would sell him. A blank
    is a scheduling artefact that resolves; poor form is a property of the
    player. Chip logic needs the same distinction to target a Free Hit.
    """
    global _CALENDAR_CACHE, _CALENDAR_CACHE_TS
    if _CALENDAR_CACHE is not None and (time.time() - _CALENDAR_CACHE_TS) < 300:
        return _CALENDAR_CACHE
    if fixture_lookup is None:
        fixture_lookup = _build_fixture_lookup()

    counts: Dict[int, Dict[int, int]] = {}
    for tid, fixtures in fixture_lookup.items():
        for f in fixtures:
            ev = f.get("event")
            if ev is None:
                continue
            counts.setdefault(ev, {})
            counts[ev][tid] = counts[ev].get(tid, 0) + 1

    n_teams = len(fixture_lookup) or 20
    out = {}
    for ev in range(start_event, start_event + n):
        per_team = counts.get(ev, {})
        playing = sum(1 for c in per_team.values() if c >= 1)
        blanks = n_teams - playing
        doubles = sum(1 for c in per_team.values() if c >= 2)
        out[ev] = {
            "event": ev,
            "blanks": blanks,
            "doubles": doubles,
            "playing": playing,
            "is_bgw": blanks >= BGW_MIN_BLANKS,
            "is_dgw": doubles >= DGW_MIN_DOUBLES,
        }
    _CALENDAR_CACHE = out
    _CALENDAR_CACHE_TS = time.time()
    return out


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
        fs = _gw_fixtures(fx, start_event + i)
        if not fs:
            out.append(0.0)          # blank
            continue
        # Mean ease across the gameweek's fixtures, then a bonus for a double:
        # two average games are worth more than one, which a mean alone loses.
        ease = sum(6.0 - _to_float(f.get("opp_strength_def", 3.0)) for f in fs) / len(fs)
        out.append(round(min(5.0, ease * (1.0 + 0.5 * (len(fs) - 1))), 2))
    return out


def _player_fixture_run(p: Dict[str, Any], fixture_lookup: Dict[int, List[Dict[str, Any]]],
                        teams_by_id: Dict[int, Dict[str, Any]], start_event: int, n: int = 4) -> List[Dict[str, Any]]:
    """Per-gameweek fixture breakdown for the transfer-recommendation UI:
    FPL's own official 1-5 FDR, opponent short name, venue, and single-
    gameweek projected xP, one entry per gameweek in the window.

    Deliberately reads official_fdr rather than _player_fdr_list's continuous
    Dixon-Coles ease score -- that score is what the solver and the rest of
    the projection pipeline actually use, and stays that way, but a UI field
    labelled "FDR" means the number a reader already knows from the official
    app, not this app's own internal one wearing the same name.

    Always exactly n entries, so a UI can lay out a fixed-width track without
    checking length first -- a blank gameweek still gets one, with
    fdr/opponent/venue all None and xp 0.0.
    """
    team_id = p.get("team")
    fixtures = fixture_lookup.get(team_id, [])
    out = []
    for i in range(n):
        gw = start_event + i
        fxs = _gw_fixtures(fixtures, gw)
        xp_gw, _note = _player_xp(p, fixture_lookup, event=gw)
        if not fxs:
            out.append({"gw": gw, "fdr": None, "opponent": None, "venue": None,
                       "xp": 0.0, "is_double": False, "is_blank": True})
            continue
        # A double gameweek shows its first fixture's opponent/FDR/venue --
        # xp is still the full gameweek total across both matches, via
        # _player_xp above, which already sums (and fatigue-discounts) both.
        f = fxs[0]
        out.append({
            "gw": gw,
            "fdr": f.get("official_fdr"),
            "opponent": teams_by_id.get(f.get("opponent"), {}).get("short_name", "?"),
            "venue": "H" if f.get("is_home") else "A",
            "xp": round(xp_gw, 1),
            "is_double": len(fxs) > 1,
            "is_blank": False,
        })
    return out


def _pair_by_price(sold, bought, pool_by_id):
    """Pair sold -> bought within a position, minimising total price distance.

    The MIP selects a SET of 15; there is no causal "A replaced B" in its output.
    The previous approach sorted both sides by xP and zipped them, which invented
    a mapping and then hung every per-row figure and rationale off the invention.
    Matching on price at least reflects how the money actually moved. n <= 5 per
    position, so exhaustive matching is trivially cheap.
    """
    import itertools
    if not sold or not bought:
        return []
    k = min(len(sold), len(bought))
    best, best_cost = None, None
    for s_perm in itertools.permutations(sold, k):
        for b_perm in itertools.permutations(bought, k):
            cost = sum(
                abs(pool_by_id[b]["price"]
                    - pool_by_id[s].get("sell_price", pool_by_id[s]["price"]))
                for s, b in zip(s_perm, b_perm))
            if best_cost is None or cost < best_cost:
                best_cost, best = cost, list(zip(s_perm, b_perm))
    return best or []


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


def _transfer_hurdle_bar(position: str, sigma: float, hurdle_scale: float = 1.0) -> float:
    """The per-leg objective penalty a single player (sell OR buy side of a
    transfer) contributes to the in-objective search hurdle -- a pure
    function so the positional scaling can be tested directly, independent
    of building and solving a full MILP.

    MID/FWD (multiplier 1.0) get bit-for-bit the pre-existing HURDLE_BASE.
    GKP and DEF substitute the explicit, much larger POSITIONAL_HURDLE_
    BASE_XP scaled by their own multiplier -- see that constant's docstring
    for why it is not simply HURDLE_BASE * multiplier. `base` is split across
    the two legs so a full swap costs `base` in total; sigma is charged per
    leg because each side carries its own outcome volatility.
    """
    pos_mult = POSITIONAL_HURDLE_MULTIPLIER.get(position, 1.0)
    base = POSITIONAL_HURDLE_BASE_XP * pos_mult if pos_mult != 1.0 else HURDLE_BASE
    return hurdle_scale * (base / 2.0 + HURDLE_SIGMA_WEIGHT * sigma)


def _locked_starting_gk(current_ids: List[Any], elements_by_id: Dict[Any, Dict[str, Any]],
                        pool_by_id: Dict[Any, Dict[str, Any]],
                        gk_hurdle_xp: float = GKP_TRANSFER_HURDLE_XP) -> Optional[Any]:
    """The currently-owned goalkeeper the standard transfer solve must not
    remove, or None if there is nothing to protect.

    "Current starting GK" (Task 1.3): an owned GK with no injury/suspension/
    doubt flag (raw status == 'a'), a fully-reported 100% chance of playing
    (or no doubt reported at all), and >=60 projected minutes -- via the same
    expected-minutes-fraction (`minutes_floor`, from _expected_playing_
    fraction) the rest of the engine already uses to gate minutes-sensitive
    decisions, since FPL does not publish a raw "projected minutes" field to
    threshold against directly. A squad carries two GKs; at most one of them
    is normally expected to actually start, so this never locks a backup.

    Returns None (nothing to lock) when:
      - no owned GK clears that health/certainty bar, e.g. the squad's
        "starter" is himself injured or rotation risk this week; or
      - the best alternative GK anywhere in the pool already clears the
        elevated GKP hurdle on its own -- condition (a) from the spec: the
        transfer is justified by the numbers even without a lock, so the
        lock has nothing left to add and the (already-elevated) in-objective
        hurdle is left to decide normally; or
      - the player was never a candidate at all, e.g. current_ids is empty
        or contains no GK -- condition (b) needs no separate override flag:
        a player the user has already removed via Step 2's override is
        simply not present in current_ids for this solve, so there is
        nothing here to protect.
    """
    candidates = []
    for pid in current_ids:
        e = elements_by_id.get(pid)
        entry = pool_by_id.get(pid)
        if not e or not entry or entry.get("position") != "GK":
            continue
        if e.get("status") != "a":
            continue
        chance = e.get("chance_of_playing_next_round")
        if chance is not None and chance < 100:
            continue
        if entry.get("minutes_floor", 0.0) < GK_LOCK_MIN_MINUTES_FLOOR:
            continue
        candidates.append(pid)
    if not candidates:
        return None
    # The expected case is exactly one qualifying starter; if more than one
    # somehow clears the bar, protect whichever projects the most minutes.
    protected = max(candidates, key=lambda pid: pool_by_id[pid].get("minutes_floor", 0.0))

    current_xp = pool_by_id[protected].get("xp", 0.0)
    owned_gk_ids = {pid for pid in current_ids if pool_by_id.get(pid, {}).get("position") == "GK"}
    best_alt = max(
        (entry.get("xp", 0.0) for pid, entry in pool_by_id.items()
         if entry.get("position") == "GK" and pid not in owned_gk_ids),
        default=0.0)
    if best_alt - current_xp >= gk_hurdle_xp:
        return None
    return protected


def _solve_squad(
    pool: List[Dict[str, Any]],
    budget: float,
    must_include_ids: Optional[set] = None,
    hit_config: Optional[Dict[str, Any]] = None,
    bench_boost: bool = False,
    scarcity_cost: float = 0.0,
    holding_map: Optional[Dict[Any, int]] = None,
    current_gw: Optional[int] = None,
    eo_map: Optional[Dict[int, Dict[str, float]]] = None,
    mode: str = "ev",
    phase: int = 1,
    rival_ids: Optional[set] = None,
    cvar_scenarios: Optional[Dict[int, List[float]]] = None,
    bench_cap: Optional[float] = None,
    locked_ids: Optional[set] = None,
) -> Tuple[Optional[List[int]], Optional[float], Dict[str, float]]:
    if not HAS_PULP:
        return None, None, {}

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

    # Active GK Lock: a hard veto on removing a specific, caller-identified
    # player (a healthy, nailed starting goalkeeper), on top of -- not instead
    # of -- the positional hurdle above. The caller (_locked_starting_gk) has
    # already checked both escape hatches before passing this in: it returns
    # nothing to lock when the best market alternative already clears the
    # elevated GKP hurdle on its own, or when the player in question is not in
    # this solve's must_include_ids at all (the user removed them via Step 2's
    # own override, which needs no separate flag -- they are simply no longer
    # part of the input). Not applied on Wildcard/Free Hit/unlimited solves,
    # which are deliberate full rebuilds -- see the call site.
    if locked_ids:
        for pid in locked_ids:
            if pid in by_id:
                prob += x[pid] == 1, f"gk_lock_{pid}"

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
        # Formation legality. Without these, "11 starters and 1 GK" is the only
        # shape constraint, so the solver is free to value an illegal XI (1-5-5-0,
        # 1-2-5-3) and then buy players to serve it. select_starting_xi afterwards
        # returns a *different*, legal, lower-scoring eleven -- meaning the squad
        # was optimised against a lineup the manager can never field.
        for pos, lo, hi in (("DEF", 3, 5), ("MID", 2, 5), ("FWD", 1, 3)):
            pos_start = pulp.lpSum(start[pid] for pid in ids if by_id[pid]["position"] == pos)
            prob += pos_start >= lo, f"formation_{pos}_min"
            prob += pos_start <= hi, f"formation_{pos}_max"
        xp_expr = pulp.lpSum(by_id[pid]["xp"] * start[pid] for pid in ids)
        # 12th-man binary: the single highest-value outfield bench slot.
        b1 = pulp.LpVariable.dicts("b1", outfield_ids, cat="Binary")
        for pid in outfield_ids:
            prob += b1[pid] <= x[pid] - start[pid], f"b1_le_bench_{pid}"
        prob += pulp.lpSum(b1[pid] for pid in outfield_ids) == 1, "one_b1"
        xp_expr += BENCH_B1_WEIGHT * pulp.lpSum(by_id[pid]["xp"] * b1[pid] for pid in outfield_ids)
        xp_expr += BENCH_DEAD_WEIGHT * pulp.lpSum(by_id[pid]["xp"] * (x[pid] - start[pid] - b1[pid]) for pid in outfield_ids)
        xp_expr += BENCH_GK_WEIGHT * pulp.lpSum(by_id[pid]["xp"] * (x[pid] - start[pid]) for pid in gk_ids)

        # Bench cost cap. Without it the planner is happy to build a flat,
        # expensive bench that scores nothing in a normal gameweek. It ramps
        # into a scheduled Bench Boost rather than being flat, because a hard
        # cap would make it impossible to prepare for the chip at all.
        if bench_cap is not None:
            prob += pulp.lpSum(
                by_id[pid]["price"] * (x[pid] - start[pid]) for pid in ids
            ) <= bench_cap, "bench_cap"

    # Captaincy uplift: the armband doubles one starter's score every gameweek.
    # Modelled in the solver so an incoming armband-winner's xP is valued ~2x.
    captain = pulp.LpVariable.dicts("captain", ids, cat="Binary")
    prob += pulp.lpSum(captain[pid] for pid in ids) == 1, "one_captain"
    for pid in ids:
        if start is not None:
            prob += captain[pid] <= start[pid], f"captain_le_start_{pid}"
        else:
            prob += captain[pid] <= x[pid], f"captain_le_x_{pid}"
    # Valued at the SINGLE-gameweek xP, not the horizon total. The armband is
    # re-chosen every week, so adding a full horizon-weighted duplicate
    # over-rewarded the best horizon asset by ~3.1 gameweeks of points and
    # systematically distorted which premium the solver bought. Falls back to
    # the horizon value only when xp_gw is absent from the pool entry.
    xp_expr += pulp.lpSum(
        by_id[pid].get("xp_gw", by_id[pid]["xp"]) * captain[pid] for pid in ids)

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
            # CVaR downside hedging (Rockafellar-Uryasev): maximise the lower
            # alpha-tail mean of the XI+captain points across the pooled stress
            # scenarios. Auxiliary variables are continuous -> stays <2s.
            if cvar_scenarios and start is not None:
                cvar_w = w.get("cvar_lambda", 0.05)
                alpha = max(1e-3, w.get("cvar_alpha", 0.1))
                ks = list(range(len(next(iter(cvar_scenarios.values()), [0.0]))))
                if ks:
                    zeta = pulp.LpVariable("cvar_zeta", cat="Continuous")
                    u = pulp.LpVariable.dicts("cvar_u", ks, lowBound=0, cat="Continuous")
                    for s in ks:
                        pi_s = pulp.lpSum(
                            (start[pid] + captain[pid]) * cvar_scenarios.get(pid, [0.0] * len(ks))[s]
                            for pid in ids
                        )
                        prob += u[s] >= zeta - pi_s, f"cvar_excess_{s}"
                    xp_expr = xp_expr + cvar_w * (zeta - (1.0 / (alpha * len(ks))) * pulp.lpSum(u[s] for s in ks))
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

    # Positional transfer friction (TRANSFER_FRICTION) was removed in Stage 3.
    # It was one of five overlapping anti-transfer penalties, and it also applied
    # on Wildcard and Free Hit solves -- which pass must_include_ids -- taxing a
    # full 15-player rebuild by ~6.3 points for the privilege. The one case it
    # was really guarding, churning the starting keeper, is expressed better as
    # part of the search hurdle than as a standing charge on every sale.

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

    # Terminal cash optionality, capped. See LIQUIDITY_PER_M: the previous form
    # was linear and uncapped, so freeing GBP 7m by downgrading a premium ADDED
    # 2.8 points to the objective. Linearised with an auxiliary variable bounded
    # by both the cap and the actual residual bank.
    liquidity_term = 0.0
    if LIQUIDITY_PER_M > 0:
        liq = pulp.LpVariable("liquidity", lowBound=0, upBound=LIQUIDITY_CAP_M)
        prob += liq <= budget - spend, "liquidity_le_bank"
        liquidity_term = LIQUIDITY_PER_M * liq
        xp_expr = xp_expr + liquidity_term

    # Components are tracked separately so the caller can render a waterfall that
    # actually reconciles with what the solver maximised. Previously the UI
    # recomputed a naive sum(xp_in - xp_out) that excluded captaincy, bench
    # weights, CVaR, stacking, EO and tax -- so the recommendation and its stated
    # justification could disagree, and the roll hurdle tested a different
    # quantity from the one being optimised.
    parts = {"squad_xp": xp_expr, "hit_cost": 0.0, "ft_option": 0.0,
             "hurdle": 0.0, "scarcity": 0.0, "liquidity": liquidity_term}

    if must_include_ids is not None and hit_config is not None:
        transfers = pulp.lpSum((1 - x[pid]) for pid in must_include_ids if pid in by_id)
        max_t = hit_config.get("max_transfers")
        if max_t is not None:
            prob += transfers <= max_t, "max_transfers"
        hits = pulp.LpVariable("hits", lowBound=0, cat="Integer")
        prob += hits >= transfers - hit_config["free_transfers"], "hits_lb"

        # The real -4, charged once, plus an explicit risk hurdle. Deleted with
        # it: ft_friction (a second charge per transfer) and HIT_FLOOR_PENALTY,
        # which was gated on hit_config["hit_cost"] rather than on `hits` and so
        # fired even at ZERO hits -- charging up to 4 points for a rotation-risk
        # signing made on a free transfer.
        hit_charge = hit_config.get("hit_cost", HIT_COST) * hits
        parts["hit_cost"] = -hit_charge
        obj = xp_expr - hit_charge

        # Separable search hurdle. Each transfer must clear a base bar plus a
        # penalty proportional to the incoming player's own outcome volatility,
        # so a volatile punt faces a higher bar than a nailed upgrade. Separable
        # (per-player, not per-pair) to keep the program linear.
        # Zeroed on Wildcard / Free Hit solves: a chip rebuild is a deliberate
        # 15-player reshape, and charging a churn brake per leg would tax it by
        # ~15 points for doing exactly what the chip is for. This is the same
        # defect TRANSFER_FRICTION had, so it is not being reintroduced here.
        # hurdle_scale multiplies the WHOLE bar, base and sigma alike. Zeroing
        # only the base would leave 1.2*sigma per leg, which on a 15-player
        # rebuild is ~30 legs of volatility premium -- still a large phantom tax
        # on a chip whose entire purpose is the reshape.
        hurdle_scale = float(hit_config.get("hurdle_scale", 1.0))
        hurdle_expr = 0.0
        for pid in ids if hurdle_scale > 0 else []:
            sigma = float(by_id[pid].get("sigma", 0.0) or 0.0)
            bar = _transfer_hurdle_bar(by_id[pid]["position"], sigma, hurdle_scale)
            if pid in must_include_ids:
                hurdle_expr = hurdle_expr + bar * (1 - x[pid])   # selling
            else:
                hurdle_expr = hurdle_expr + bar * x[pid]          # buying
        parts["hurdle"] = -hurdle_expr
        obj = obj - hurdle_expr

        # Wildcard scarcity: activating the chip incurs a fixed full-season
        # opportunity cost, so the solver holds it unless the rebuild decisively
        # outscores the current squad.
        if scarcity_cost > 0 and max_t is not None:
            chip_used = pulp.LpVariable("chip_used", cat="Binary")
            prob += transfers <= max_t * chip_used, "chip_used_force"
            parts["scarcity"] = -scarcity_cost * chip_used
            obj = obj - scarcity_cost * chip_used

        # Option value of banking a free transfer, on a concave marginal curve.
        free_transfers = hit_config.get("free_transfers", 0)
        if max_t is not None and 0 < free_transfers <= 5:
            rolled = free_transfers - transfers + hits   # == max(0, F - T) at optimality
            y = pulp.LpVariable.dicts("roll_ft", range(1, 6), cat="Binary")
            prob += rolled == pulp.lpSum(y[k] for k in range(1, 6)), "roll_ft_sum"
            for k in range(2, 6):
                prob += y[k] <= y[k - 1], f"roll_ft_mono_{k}"
            ft_option = pulp.lpSum(FT_OPTION_MARGINAL[k - 1] * y[k] for k in range(1, 6))
            parts["ft_option"] = ft_option
            obj = obj + ft_option

        # calculate_decaying_tax was removed here. It was a sixth anti-transfer
        # charge stacked on the other five. NOTE: the manager_transfer_ledger it
        # read from is NOT redundant -- it establishes each player's true
        # purchase price and therefore the selling price used by the budget
        # constraint above, which is the only way that constraint reflects the
        # manager's real liquidation capital.

        prob.setObjective(obj)
    else:
        prob.setObjective(xp_expr)

    global _LAST_SOLVE
    prob.solve(_make_solver())
    status = pulp.LpStatus[prob.status]
    selected = [pid for pid in ids if x[pid].varValue is not None and x[pid].varValue > 0.5]

    # Record what the MIP actually chose, so tests can assert on the XI (the
    # `start` binaries are otherwise invisible to callers) and the UI can tell
    # a proven optimum from a time-limited incumbent.
    _LAST_SOLVE = {
        "status": status,
        "proven_optimal": status == "Optimal",
        "profile": _SOLVER_PROFILE,
        "selected": selected,
        "xi": [pid for pid in ids if start is not None
               and start[pid].varValue is not None and start[pid].varValue > 0.5],
        "formation": None,
        # Component breakdown of the objective, so the UI can render a waterfall
        # that reconciles exactly with what was maximised.
        "components": {k: (pulp.value(v) if not isinstance(v, (int, float)) else float(v))
                       for k, v in parts.items()},
    }
    if _LAST_SOLVE["xi"]:
        _LAST_SOLVE["formation"] = {
            pos: sum(1 for pid in _LAST_SOLVE["xi"] if by_id[pid]["position"] == pos)
            for pos in ("GK", "DEF", "MID", "FWD")
        }

    components = _LAST_SOLVE["components"]
    if status == "Optimal":
        return selected, pulp.value(prob.objective), components

    # A time-limited incumbent is usable interactively but never in tests: the
    # deterministic profile must fail loudly rather than hand back a squad that
    # varies with machine load.
    #
    # "Not Solved" is the only non-optimal status that carries a usable
    # incumbent. Infeasible / Unbounded / Undefined leave STALE variable values
    # from an earlier relaxation, and those values can look superficially
    # plausible: an infeasible solve was observed returning 15 selected players
    # with a TEN-man starting XI, because the previous check only counted the
    # squad. Validate the solution itself, not just its length.
    if _SOLVER_PROFILE == "interactive" and status == "Not Solved" and _is_valid_squad(selected, by_id, start):
        return selected, pulp.value(prob.objective), components
    return None, None, {}


def _is_valid_squad(selected, by_id, start) -> bool:
    """Structural check on a solver result before it is trusted."""
    if not selected or len(selected) != sum(POS_COUNTS.values()):
        return False
    counts = {}
    for pid in selected:
        counts[by_id[pid]["position"]] = counts.get(by_id[pid]["position"], 0) + 1
    if counts != POS_COUNTS:
        return False
    clubs = {}
    for pid in selected:
        clubs[by_id[pid]["team_id"]] = clubs.get(by_id[pid]["team_id"], 0) + 1
    if clubs and max(clubs.values()) > 3:
        return False
    if start is not None:
        xi = [pid for pid in selected
              if start[pid].varValue is not None and start[pid].varValue > 0.5]
        if len(xi) != 11:
            return False
        form = {}
        for pid in xi:
            form[by_id[pid]["position"]] = form.get(by_id[pid]["position"], 0) + 1
        if form.get("GK", 0) != 1:
            return False
        for pos, lo, hi in (("DEF", 3, 5), ("MID", 2, 5), ("FWD", 1, 3)):
            if not lo <= form.get(pos, 0) <= hi:
                return False
    return True

def score_my_squad(manager_id: str, gw: int, risk: str = "balanced") -> Dict[str, Any]:
    manager_id = _clean_manager_id(manager_id)
    bootstrap = _get_bootstrap()
    players_by_id = {p["id"]: p for p in bootstrap["elements"]}
    teams_by_id = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    fixture_lookup = _build_fixture_lookup(bootstrap)

    # Walk BACK from the requested gameweek to find the manager's most recent
    # picks. A manager with no picks for the current gameweek used to trigger up
    # to 38 sequential uncached HTTP round trips -- the exact case this loop
    # exists to serve, so the slow path was the common one for anyone who had
    # not yet set a team. Two changes: the manager's first season entry bounds
    # the walk (there are no picks before it), and each probe is cached, so a
    # rerun costs nothing.
    picks_data = None
    picks_gw = None
    try:
        entry = _cached_json(f"{BASE_URL}/entry/{manager_id}/")
        floor_gw = max(1, int(entry.get("started_event") or 1))
    except Exception:
        floor_gw = 1
    for attempt in range(gw, floor_gw - 1, -1):
        try:
            picks_data = _cached_json(f"{BASE_URL}/entry/{manager_id}/event/{attempt}/picks/")
            picks_gw = attempt
            break
        except Exception:
            continue           # no picks for that gameweek; try the one before
    if picks_data is None:
        return {"error": f"Could not fetch squad for Manager ID {manager_id}."}

    squad = []
    for pick in picks_data.get("picks", []):
        p_data = players_by_id.get(pick["element"])
        if not p_data:
            continue
        xp, note = _player_xp(p_data, fixture_lookup, event=gw)
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

# NOTE (Layer 1 independence): _ownership_adjust was deleted here.
#
# It tilted pool xP by ownership "to make strategy modes mechanically distinct",
# double-counting the ow_weight term that _risk_adjust was already applying. It
# also compared risk == "conservative" against the raw string, bypassing
# _RISK_ALIASES, so it silently did nothing for "rank protecting (shield)".

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


CHIP_RESERVATION_BASE = {"Wildcard": 12.0, "Free Hit": 6.0,
                         "Bench Boost": 5.0, "Triple Captain": 4.0}
CHIP_SET2_EXPIRY_GW = 38
# Wildcard timing prior. Fixture swings guide it, but a Set-1 wildcard has to
# last until GW19, so a strong three-week swing at GW4 is a trap: it buys a
# squad built for September and leaves it stale through autumn. The envelope
# penalises firing outside the window where the payback historically lands.
BENCH_CAP_STANDARD = 16.0
# T-2 and T-1 before a scheduled Bench Boost, then uncapped in the week itself.
BENCH_CAP_RAMP = {2: 19.0, 1: 22.0, 0: None}
WILDCARD_WINDOW = (7, 14)
WILDCARD_ENVELOPE_PENALTY = 6.0


def _bench_cap_for(current_gw, bench_boost_gw=None) -> Optional[float]:
    """Bench budget for this gameweek, ramping into a planned Bench Boost."""
    if bench_boost_gw is None:
        return BENCH_CAP_STANDARD
    weeks_out = int(bench_boost_gw) - int(current_gw)
    if weeks_out in BENCH_CAP_RAMP:
        return BENCH_CAP_RAMP[weeks_out]
    return BENCH_CAP_STANDARD


# The API returns chip names in its own vocabulary; the UI and this module use
# display names. Normalising in one place stops a played-chip lookup silently
# missing because it compared "3xc" against "Triple Captain".
_CHIP_API_NAMES = {
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
    "free_hit": "Free Hit",
    "bboost": "Bench Boost",
    "benchboost": "Bench Boost",
    "3xc": "Triple Captain",
    "triplecaptain": "Triple Captain",
}


def normalise_chip_name(name) -> Optional[str]:
    """API or display chip name -> canonical display name."""
    if not name:
        return None
    key = str(name).strip().lower().replace(" ", "").replace("-", "")
    if key in _CHIP_API_NAMES:
        return _CHIP_API_NAMES[key]
    for chip in CHIPS:
        if chip.lower().replace(" ", "") == key:
            return chip
    return None


def _chip_set_bounds(gw: int) -> Tuple[int, int]:
    """(first, last) gameweek of the chip set `gw` falls in."""
    gw = int(gw or 1)
    if gw <= CHIP_SET1_EXPIRY_GW:
        return 1, CHIP_SET1_EXPIRY_GW
    return CHIP_SET1_EXPIRY_GW + 1, CHIP_SET2_EXPIRY_GW


def _chip_reservation_threshold(chip: str, gw: int) -> float:
    """Reservation value of holding `chip`, decaying toward its set's deadline.

    Two defects in the previous three-step version:

    * It returned 0.0 for EVERY gw >= 18 -- including GW20-38, where the second
      chip set lives. From GW18 onward the top-ranked chip therefore cleared its
      threshold every single week for the rest of the season, so the engine
      recommended playing a chip continuously for more than half the campaign.
    * The bases were on a different scale from the scores they gated. Triple
      Captain scored one captain's single-gameweek xP, ~6-9, against a threshold
      of 15.0 -- so it could not be recommended before GW15 no matter how good
      the fixture was.

    Now a smooth decay to zero at the set deadline, restarting for Set 2, with
    per-chip bases on the same one-week scale as the (now commensurate) scores.
    """
    gw = int(gw)
    first, last = _chip_set_bounds(gw)
    base = CHIP_RESERVATION_BASE.get(chip, 99.0)
    span = max(1, last - first)
    remaining = max(0.0, min(1.0, (last - gw) / span))
    # eta > 1 decays slowly at first, then collapses near the deadline: hold the
    # option while it still has time to pay, then use it rather than lose it.
    eta = 2.0 if chip == "Wildcard" else 1.5
    return round(base * (remaining ** eta), 2)


def _wildcard_timing_penalty(gw: int, swing_scores=None) -> float:
    """Extra reservation applied to a wildcard fired outside its window.

    Purely dynamic timing (fire wherever the fixture swing peaks) has a known
    failure mode: a good three-week swing early buys a squad that then has to
    survive the rest of the half. The window is an empirical prior, not a hard
    band -- a large enough swing still clears it.
    """
    gw = int(gw)
    lo, hi = WILDCARD_WINDOW
    if lo <= gw <= hi or gw > CHIP_SET1_EXPIRY_GW:
        return 0.0
    distance = (lo - gw) if gw < lo else (gw - hi)
    return round(WILDCARD_ENVELOPE_PENALTY * min(1.0, distance / 4.0), 2)


def get_played_chips(manager_id: str) -> List[str]:
    """Chip names the manager has already played (from FPL history)."""
    try:
        manager_id = _clean_manager_id(manager_id)
        resp = requests.get(f"{BASE_URL}/entry/{manager_id}/history/", timeout=10)
        resp.raise_for_status()
        chips = resp.json().get("chips") or []
        # Normalised to display names. The API returns its own vocabulary --
        # "wildcard", "freehit", "bboost", "3xc" -- which was being compared
        # directly against "Wildcard", "Free Hit", "Bench Boost", "Triple
        # Captain". Nothing ever matched, so a played chip stayed on the
        # available list and could be recommended a second time.
        out = []
        for c in chips:
            name = normalise_chip_name(c.get("name"))
            if name and name not in out:
                out.append(name)
        return out
    except Exception:
        return []


def _generate_scenarios(player_ids, fixture_lookup, event, risk="balanced",
                        n=PLAN_HORIZON, S=SAA_SCENARIOS, seed=7,
                        gk_transfer_in_ids: Optional[set] = None):
    """Correlated SAA scenarios -> (saa_mean, matrix).

    Outcomes are coupled through shared team attack/defence latent shocks and a
    shared rotation-crisis minutes shock (never independent player draws).

    `gk_transfer_in_ids`: player ids to evaluate as prospective transfers IN
    for the GK Transfer-In Fragility Discount (Task 1.2) -- the multi-GW
    planner (_plan_transfers_multi_gw) shares this same scenario base, so the
    discount has to be threaded in here too, not just in the single-GW pool
    build. A no-op for anyone not at GK; see GK_TRANSFER_IN_CS_DAMPENER.

    Returns:
      saa_mean: {pid: [mean xP over GW t=0..n-1]}
      matrix:   {pid: ndarray of shape (S, n)} -- SAMPLES down the rows,
                gameweeks across the columns. This is the standard Monte Carlo
                orientation and lets a horizon total be a single contiguous
                matrix-vector product. The docstring previously advertised the
                transpose, [n arrays of length S], and both consumers believed
                it -- which is how "500 sims" came to mean six.
    """
    bootstrap = _get_bootstrap()
    elements = {e["id"]: e for e in bootstrap.get("elements", [])}
    ids = [pid for pid in player_ids if pid in elements]
    gk_transfer_in_ids = gk_transfer_in_ids or set()

    base = {}
    p0 = {}
    team_of = {}
    pos_of = {}
    for pid in ids:
        e = elements[pid]
        dampener = GK_TRANSFER_IN_CS_DAMPENER if pid in gk_transfer_in_ids else 1.0
        row = []
        for t in range(n):
            xp, _ = _player_xp(e, fixture_lookup, event=event + t, gk_cs_dampener=dampener)
            row.append(xp)
        base[pid] = row
        _p0, _pc, _pf = _minute_distribution(e, e.get("status", "a"))
        p0[pid] = _p0
        team_of[pid] = e.get("team")
        pos_of[pid] = POS_MAP.get(e.get("element_type"), "MID")

    if not HAS_NUMPY or not ids:
        # Degenerate single-scenario fallback, shaped (1, n) to match the numpy
        # path. It previously built [[v] for v in base] -- shape (n, 1), the
        # opposite convention -- so the two code paths disagreed about the
        # layout, which is why the mismatch survived so long.
        saa_mean = {pid: [round(v, 2) for v in base[pid]] for pid in ids}
        matrix = {pid: [list(base[pid])] for pid in ids}
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
        assert xp_s.shape == (S, n), f"scenario matrix must be (S, n), got {xp_s.shape}"
        matrix[pid] = xp_s
        saa_mean[pid] = [round(float(xp_s[:, t].mean()), 2) for t in range(n)]

    return saa_mean, matrix


def _scenario_sigmas(matrix, weights=None, n=PLAN_HORIZON):
    """{pid: sd of horizon points across scenarios}, for the Stage 3 hurdle.

    Computed DIRECTLY from the (S, n) matrix. It must not route through
    _scenario_distribution, which until Stage 5 indexes the array as
    [gameweek][scenario] and so collapses 500 scenarios to 6 -- a sigma derived
    from six gameweek slots would be meaningless. Keeping this separate also
    means the hurdle is correct even though Stage 3 lands before Stage 5.
    """
    if not matrix or not HAS_NUMPY:
        return {}
    out = {}
    for pid, m in matrix.items():
        arr = _np.asarray(m, dtype=float)
        if arr.ndim != 2:
            continue
        S, n_gw = arr.shape
        k = min(n, n_gw)
        w = _np.asarray((weights or PLAN_WEIGHTS)[:k], dtype=float)
        out[pid] = float((arr[:, :k] @ w).std())
    return out


def _horizon_totals(matrix, pids, weights=None, n=PLAN_HORIZON):
    """{pid: array of length S} -- each player's horizon points per scenario.

    One place that knows the matrix layout is (S, n): samples down the rows,
    gameweeks across the columns, which is the standard Monte Carlo orientation
    and what _generate_scenarios has always produced. Both former consumers
    indexed it as [gameweek][scenario] instead, so `S` resolved to 6 and the
    loop ran over the first six SCENARIOS rather than the gameweeks -- the
    "500 sims" in the UI were six.
    """
    out = {}
    for pid in pids:
        m = matrix.get(pid)
        if m is None:
            continue
        arr = _np.asarray(m, dtype=float)
        if arr.ndim != 2:
            continue
        k = min(n, arr.shape[1])
        w = _np.asarray((weights or PLAN_WEIGHTS)[:k], dtype=float)
        out[pid] = arr[:, :k] @ w          # (S, k) @ (k,) -> (S,)
    return out


def _scenario_distribution(selected_ids, matrix, weights=None, n=PLAN_HORIZON,
                           multipliers=None):
    """Floor / Expected / Ceiling of a squad's horizon points across scenarios.

    `multipliers` optionally weights each player's contribution (2 for the
    captain, 0 for the bench), so the spread describes what actually scores
    rather than all fifteen.
    """
    sel = [pid for pid in selected_ids if pid in matrix]
    if not sel or not HAS_NUMPY:
        return {"p5": 0.0, "p50": 0.0, "p95": 0.0, "mean": 0.0, "scenarios": 0}

    horizons = _horizon_totals(matrix, sel, weights, n)
    if not horizons:
        return {"p5": 0.0, "p50": 0.0, "p95": 0.0, "mean": 0.0, "scenarios": 0}

    mult = multipliers or {}
    totals = _np.zeros(len(next(iter(horizons.values()))))
    for pid, h in horizons.items():
        totals = totals + float(mult.get(pid, 1.0)) * h

    p5, p50, p95 = (float(v) for v in _np.percentile(totals, [5, 50, 95]))
    # Percentiles are monotone by construction; assert it so a future layout
    # regression surfaces here rather than as a plausible-looking wrong number.
    assert p5 <= p50 <= p95, f"non-monotone quantiles: {p5}, {p50}, {p95}"
    return {"p5": round(p5, 2), "p50": round(p50, 2), "p95": round(p95, 2),
            "mean": round(float(totals.mean()), 2), "scenarios": int(totals.size)}


def _select_stress_scenarios(matrix, K=CVAR_STRESS_K, selected_ids=None):
    """The K worst scenarios -> {pid: [horizon xP in each of those scenarios]}.

    Pooling bounds the CVaR auxiliary variables so the MIP stays inside its
    solve-time budget.

    Two fixes. The layout bug above meant argsort ran over a length-6 vector, so
    `K=50` could only ever return 6 indices and the CVaR term was fed six
    gameweek slots dressed as scenarios. And the tail was ranked by the
    POOL-WIDE total -- every candidate the solver might consider -- rather than
    by the squad being optimised, so it was not the tail of the portfolio the
    objective cares about. `selected_ids` narrows it to the current squad.
    """
    if not matrix or not HAS_NUMPY:
        return {}
    horizons = _horizon_totals(matrix, list(matrix.keys()), n=len(HORIZON_WEIGHTS))
    if not horizons:
        return {}

    rank_over = [pid for pid in (selected_ids or horizons.keys()) if pid in horizons]
    if not rank_over:
        rank_over = list(horizons.keys())
    totals = _np.zeros(len(next(iter(horizons.values()))))
    for pid in rank_over:
        totals = totals + horizons[pid]

    idx = _np.argsort(totals)[:K]
    return {pid: [float(h[s]) for s in idx] for pid, h in horizons.items()}


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


# Big-M constants for _plan_transfers_multi_gw's conditional (chip-gated)
# constraints. Each is sized to be safely non-binding on the "irrelevant"
# branch: FT_BIGM absorbs up to 15 transfers in one week (a full wildcard
# rebuild), PTS_BIGM exceeds any plausible 15-man squad's single-week total,
# and BUDGET_BIGM exceeds any plausible 15-man squad's total price.
_PLAN_FT_BIGM = 16.0
_PLAN_PTS_BIGM = 1000.0
_PLAN_BUDGET_BIGM = 1000.0


def _ft_state_machine_constraints(prob, ft_t, ft_next, u_t, chip_t, hits_t,
                                  big_m=_PLAN_FT_BIGM, prefix=""):
    """Retained free-transfer state machine, linking one week's bank (ft_t)
    and transfer count (u_t) to the next week's bank (ft_next) and this
    week's hit count (hits_t), gated by chip_t (1 iff Wildcard or Free Hit
    was played this week).

    Factored out of _plan_transfers_multi_gw so a test can drive u_t/ft_t/
    chip_t directly with fixed values -- solved for ft_next/hits_t only --
    rather than depending on the horizon optimiser electing to reach a
    particular week's state, which the test can't reliably dictate. This is
    the ONE place the formula is written, so a future change to it cannot
    silently drift out of sync with what the regression tests check.

    Upper bounds only, deliberately -- an earlier version of this also
    sandwiched ft_next between matching LOWER bounds, reasoning that would be
    more robust than depending on the objective to pull ft_next up to an
    upper bound. It was wrong, not just redundant: those lower bounds
    targeted the UNCLAMPED ft_t - u_t + 1 (or ft_t + 1), and made the whole
    horizon flatly INFEASIBLE the moment any perfectly ordinary week reached
    ft_t == 5 with u_t == 0 (target 6, lower-bounded above the ft_next <= 5
    cap) or took a hit at ft_t == 1 (target -1, lower-bounded below the
    ft_next >= ft_t - u_t + 1 - M upper bound once hits_t is folded in --
    see below). Caught by the isolated tests in tests/test_phased.py, which
    is exactly why this is tested at that level rather than only end-to-end.

    Upper-bound-only is both correct and simple here because `ft` appears in
    the horizon objective ONLY positively (the terminal ft[T] bonus; nothing
    anywhere rewards a LOWER ft), so maximisation pulls ft_next up to
    whichever bound below is tightest -- there is never a reason for the
    solver to leave it lower, so no separate lower bound is needed to pin it:

      - chip_t == 0: settles at ft_t - u_t + 1, and the "+ hits_t" term is
        what keeps that correct at the 1-floor with no separate floor
        constraint -- hits_t is pinned by minimisation (a hit costs 4.0 in
        the real objective) to exactly max(0, u_t - ft_t), so the bound
        collapses to exactly 1 the instant a hit is taken, rather than going
        negative.
      - chip_t == 1: settles at ft_t + 1, as if u_t were zero -- a
        Wildcard/Free Hit spends no banked transfers at all.

    `ft_t`, `u_t` and `chip_t` may be LpVariables/expressions (the real
    solve) or plain numbers (a test fixing them to drive a specific
    scenario) -- PuLP accepts either on either side of a constraint. A test
    that fixes them still needs SOME preference for a higher ft_next and a
    lower hits_t in its own objective -- with none at all, upper bounds alone
    leave ft_next and hits_t free to sit anywhere feasible below them, same
    as they would without the pull of the real horizon objective.
    """
    prob += ft_next <= 5, f"{prefix}ft_ub_cap"
    prob += ft_next <= ft_t + 1, f"{prefix}ft_ub_chip"
    prob += ft_next <= (ft_t - u_t) + 1 + hits_t + big_m * chip_t, f"{prefix}ft_ub_normal"
    # A chip week pays no hit, however many transfers it makes.
    prob += hits_t >= u_t - ft_t - big_m * chip_t, f"{prefix}hits_lb"
    prob += hits_t >= 0, f"{prefix}hits_nonneg"


def _plan_transfers_multi_gw(pool, budget, free_transfers, current_ids, saa_mean,
                             event, n=PLAN_HORIZON, bank_cash=None):
    """Multi-GW transfer scheduler: ownership + FT + budget over N GWs (Omega(f)).

    Spans the horizon so banking FTs now can fund a coupled structural pivot in a
    later gameweek without hits; a terminal value Psi(S_t+N) stops the solver from
    strip-mining the squad in the final week.

    Wildcard and Free Hit are both representable within the horizon (each at
    most once, mirroring "one chip per set"):

    * Wildcard behaves like any other week's transfers, except the FT/hits
      bookkeeping below waives the hit cost and does not touch the banked FT
      count -- exactly like the real chip. The squad change is permanent, so
      it flows through the ordinary x/buy/sell ownership chain.
    * Free Hit gets its own one-off squad (the `y` variables): a fresh 15 that
      only has to be legal and affordable for that single week, entirely
      decoupled from the ownership chain. `x` is frozen (buy/sell forced to 0)
      for every player during a Free Hit week, so the PERSISTENT squad carries
      forward unchanged and next week's schedule correctly shows the real
      squad resuming -- not the one-off XI the chip produced. That is the
      behaviour a naive "give this week free transfers" encoding would get
      wrong: the temporary squad would otherwise permanently overwrite the
      real one, one week early, in every week displayed after it.

    This cannot know whether a chip has already been spent this season -- the
    caller does not currently pass that in (eval_chips is always the full set
    in every existing call site) -- so, like the rest of the app, this can
    suggest a chip regardless of whether it is still available. Not a new
    limitation: the single-GW solver has the same gap.
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
    # The Free Hit one-off squad: a second, independent ownership indicator per
    # week, used for that week's points/budget only when fh[t] fires.
    y = pulp.LpVariable.dicts("mfh", (ids, range(T)), cat="Binary")
    ft = [pulp.LpVariable(f"ft_{t}", lowBound=0, upBound=5, cat="Integer") for t in range(T + 1)]
    hits = [pulp.LpVariable(f"hit_{t}", lowBound=0, cat="Integer") for t in range(T)]
    bank = [pulp.LpVariable(f"bank_{t}", lowBound=0) for t in range(T + 1)]
    wc = pulp.LpVariable.dicts("wc", range(T), cat="Binary")
    fh = pulp.LpVariable.dicts("fh", range(T), cat="Binary")
    pts_t = [pulp.LpVariable(f"pts_{t}", lowBound=0) for t in range(T)]

    def _prev_x(pid, t):
        """x[pid][t-1], or the real starting squad's indicator at t == 0."""
        return x[pid][t - 1] if t >= 1 else (1 if pid in cur_set else 0)

    # Ownership linkage.
    for pid in ids:
        c0 = 1 if pid in cur_set else 0
        prob += x[pid][0] == c0 + buy[pid][0] - sell[pid][0], f"own0_{pid}"
        for t in range(1, T):
            prob += x[pid][t] == x[pid][t - 1] + buy[pid][t] - sell[pid][t], f"own_{pid}_{t}"
        for t in range(T):
            prob += buy[pid][t] + sell[pid][t] <= 1, f"no_churn_{pid}_{t}"
            # Free Hit freezes the persistent squad: no buy or sell against it
            # in a week the chip is played, so next week resumes from exactly
            # where the REAL squad was, not from the one-off Free Hit XI.
            prob += buy[pid][t] <= 1 - fh[t], f"fh_freeze_buy_{pid}_{t}"
            prob += sell[pid][t] <= 1 - fh[t], f"fh_freeze_sell_{pid}_{t}"

    # Squad structure per GW -- for both the persistent squad (x) and the
    # Free Hit one-off squad (y). y's shape constraints hold unconditionally
    # (some legal 15 always exists, e.g. y == x, so this never blocks
    # feasibility in a week Free Hit isn't played); only the objective and
    # budget constraints below are gated on fh[t].
    for t in range(T):
        prob += pulp.lpSum(x[pid][t] for pid in ids) == 15, f"squad_{t}"
        prob += pulp.lpSum(y[pid][t] for pid in ids) == 15, f"fh_squad_{t}"
        for pos, cnt in POS_COUNTS.items():
            prob += pulp.lpSum(x[pid][t] for pid in ids if by_id[pid]["position"] == pos) == cnt, f"pos_{pos}_{t}"
            prob += pulp.lpSum(y[pid][t] for pid in ids if by_id[pid]["position"] == pos) == cnt, f"fh_pos_{pos}_{t}"
        for tm in {by_id[pid]["team_id"] for pid in ids}:
            prob += pulp.lpSum(x[pid][t] for pid in ids if by_id[pid]["team_id"] == tm) <= 3, f"team_{tm}_{t}"
            prob += pulp.lpSum(y[pid][t] for pid in ids if by_id[pid]["team_id"] == tm) <= 3, f"fh_team_{tm}_{t}"

    # Transfers, FT, hits, budget linkage.
    transfers = [pulp.lpSum(buy[pid][t] for pid in ids) for t in range(T)]
    sell_value = [pulp.lpSum(sell[pid][t] * by_id[pid].get("sell_price", by_id[pid]["price"]) for pid in ids) for t in range(T)]
    buy_cost = [pulp.lpSum(buy[pid][t] * by_id[pid]["price"] for pid in ids) for t in range(T)]

    prob += ft[0] == min(5, int(free_transfers)), "ft0"
    # Opening bank is ACTUAL cash, not total purchasing power. `budget` is
    # bank + sum(selling_price of the current squad); the flow constraint below
    # then adds sell_value[t] again on every sale, so seeding bank[0] with
    # `budget` counted every held player's equity twice and handed the planner
    # an imaginary war chest.
    prob += bank[0] == (budget if bank_cash is None else float(bank_cash)), "bank0"
    for t in range(T):
        # At most one chip per week, and (mirroring "one chip per set") at
        # most one Wildcard and one Free Hit across the whole horizon.
        prob += wc[t] + fh[t] <= 1, f"chip_excl_{t}"
        chip_t = wc[t] + fh[t]

        _ft_state_machine_constraints(
            prob, ft[t], ft[t + 1], transfers[t], chip_t, hits[t], prefix=f"gw{t}_")

        # Free Hit's one-off squad must fit the value available going INTO
        # that week -- current bank plus the persistent squad's sell value --
        # not the persistent budget flow, which is untouched (frozen) this
        # week. Non-binding whenever fh[t] == 0.
        fh_budget_t = bank[t] + pulp.lpSum(_prev_x(pid, t) * by_id[pid].get("sell_price", by_id[pid]["price"]) for pid in ids)
        prob += pulp.lpSum(y[pid][t] * by_id[pid]["price"] for pid in ids) <= fh_budget_t + _PLAN_BUDGET_BIGM * (1 - fh[t]), f"fh_budget_{t}"

        prob += buy_cost[t] <= bank[t] + sell_value[t], f"budget_flow_{t}"
        prob += bank[t + 1] == bank[t] + sell_value[t] - buy_cost[t], f"bank_next_{t}"

    prob += pulp.lpSum(wc[t] for t in range(T)) <= 1, "wc_once"
    prob += pulp.lpSum(fh[t] for t in range(T)) <= 1, "fh_once"

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
        pts_x = pulp.lpSum(x[pid][t] * saa_xp[pid][t] for pid in ids)
        pts_y = pulp.lpSum(y[pid][t] * saa_xp[pid][t] for pid in ids)
        # pts_t[t] is the week's SCORED points: the persistent squad's, unless
        # Free Hit is active, in which case the one-off squad's. Maximisation
        # settles pts_t at whichever bound is tight -- the other is relaxed
        # by _PLAN_PTS_BIGM and so never binds.
        prob += pts_t[t] <= pts_x + _PLAN_PTS_BIGM * fh[t], f"pts_sel_x_{t}"
        prob += pts_t[t] <= pts_y + _PLAN_PTS_BIGM * (1 - fh[t]), f"pts_sel_y_{t}"
        obj += gw_w * (pts_t[t] - 4.0 * hits[t])

    term_equity = pulp.lpSum(x[pid][T - 1] * by_id[pid].get("sell_price", by_id[pid]["price"]) for pid in ids)
    dead = pulp.lpSum(x[pid][T - 1] * (1.0 if (by_id[pid].get("status") in _NON_PLAYING_NOTES or by_id[pid].get("xp", 0.0) <= 0.1) else 0.0) for pid in ids)
    obj += lam_eq * term_equity + lam_ft * ft[T] - lam_dead * dead

    prob.setObjective(obj)
    prob.solve(_make_solver())
    if pulp.LpStatus[prob.status] != "Optimal":
        return []

    schedule = []
    for t in range(T):
        chip_played = ("Wildcard" if (wc[t].varValue or 0) > 0.5
                       else "Free Hit" if (fh[t].varValue or 0) > 0.5 else None)
        if chip_played == "Free Hit":
            # The persistent chain is frozen this week (buy/sell forced to 0),
            # so the one-off `y` squad against the PRE-chip squad is what
            # actually changed on the pitch -- that is what the schedule must
            # show, not the (empty) frozen buy/sell pair. _prev_x returns
            # either an LpVariable (t >= 1) or a plain int (t == 0); normalise
            # both to a "currently owned" id set.
            prev_ids = set()
            for pid in ids:
                prev = _prev_x(pid, t)
                owned = prev.varValue if hasattr(prev, "varValue") else prev
                if owned and owned > 0.5:
                    prev_ids.add(pid)
            fh_ids = {pid for pid in ids if y[pid][t].varValue and y[pid][t].varValue > 0.5}
            buys = [by_id[pid]["name"] for pid in fh_ids - prev_ids]
            sells = [by_id[pid]["name"] for pid in prev_ids - fh_ids]
            n_transfers = 0   # Free Hit: unlimited and free, not a transfer count.
        else:
            buys = [by_id[pid]["name"] for pid in ids if buy[pid][t].varValue and buy[pid][t].varValue > 0.5]
            sells = [by_id[pid]["name"] for pid in ids if sell[pid][t].varValue and sell[pid][t].varValue > 0.5]
            n_transfers = int(round(transfers[t].value() or 0.0))
        schedule.append({
            "gw": event + t,
            "buys": buys,
            "sells": sells,
            "chip": chip_played,
            "transfers": n_transfers,
            "hits": int(round(hits[t].value() or 0.0)),
            "ft_after": int(round(ft[t + 1].value() or 0.0)),
            "bank_after": round(bank[t + 1].value() or 0.0, 1),
        })
    return schedule


def build_transfer_gantt_data(
    initial_squad: List[Dict[str, Any]],
    schedule: List[Dict[str, Any]],
    xp_lookup: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Reshape _plan_transfers_multi_gw's week-by-week buy/sell diff list into
    per-player tenure bars a Gantt chart can plot directly, plus chip-
    activation markers and a simple captain/vice-captain call per gameweek.

    Pure and Streamlit/Plotly-independent on purpose, so the transform itself
    is unit-testable without rendering anything -- app.py's job is only to
    hand this its inputs and draw the result.

    `initial_squad` is the persistent squad BEFORE schedule[0]'s gameweek:
    [{"name": str, "position": str}, ...] (position is cosmetic -- used only
    to colour/group bars; "?" if omitted). `schedule` is exactly what
    _plan_transfers_multi_gw returns. `xp_lookup` is an optional {name: xp}
    map for choosing captain/vice-captain; omit it and every week's captain
    call is simply None rather than a guess.

    Returns:
      {
        "start_gw": int, "end_gw": int,
        "bars": [{"name", "position", "start_gw", "end_gw",
                  "entered_via": "initial"|"buy", "left_via": "sold"|"horizon_end"}],
        "chip_events": [{"gw", "chip"}],
        "captains": {gw: {"captain": name_or_None, "vice_captain": name_or_None}},
      }

    A player transferred out and later bought back within the same horizon
    gets two separate bar segments, not one that pauses -- each with its own
    start_gw/end_gw, exactly like a real Gantt chart shows a resource that
    leaves and returns.

    Free Hit freezes the persistent squad for that one week -- exactly as
    _plan_transfers_multi_gw's own solve does -- so its buys/sells describe a
    one-off XI, never a change to who is held going forward; tenure bars do
    not react to them, but that week's captain/vice-captain IS drawn from the
    one-off squad, since that is who is actually selected to play.
    """
    xp_lookup = xp_lookup or {}
    if not schedule:
        return {"start_gw": None, "end_gw": None, "bars": [], "chip_events": [], "captains": {}}

    start_gw = schedule[0]["gw"]
    end_gw = schedule[-1]["gw"]

    position_by_name: Dict[str, str] = {}
    open_bars: Dict[str, Dict[str, Any]] = {}
    for p in initial_squad:
        name = p.get("name")
        if not name:
            continue
        position_by_name[name] = p.get("position", "?")
        open_bars[name] = {"start_gw": start_gw, "entered_via": "initial"}
    held = set(open_bars)

    bars: List[Dict[str, Any]] = []
    chip_events: List[Dict[str, Any]] = []
    captains: Dict[int, Dict[str, Optional[str]]] = {}

    def _close_bar(name: str, end: int, left_via: str) -> None:
        seg = open_bars.pop(name)
        bars.append({
            "name": name,
            "position": position_by_name.get(name, "?"),
            "start_gw": seg["start_gw"],
            "end_gw": end,
            "entered_via": seg["entered_via"],
            "left_via": left_via,
        })

    for week in schedule:
        gw = week["gw"]
        chip = week.get("chip")
        if chip:
            chip_events.append({"gw": gw, "chip": chip})

        if chip != "Free Hit":
            for name in week.get("sells", []):
                if name in held:
                    held.discard(name)
                    _close_bar(name, gw - 1, "sold")
            for name in week.get("buys", []):
                if name not in held:
                    held.add(name)
                    position_by_name.setdefault(name, "?")
                    open_bars[name] = {"start_gw": gw, "entered_via": "buy"}
            pool_this_week = held
        else:
            # Free Hit's one-off XI: the persistent squad minus this week's
            # (temporary) sells, plus this week's (temporary) buys -- `held`
            # itself is untouched above, so it still reflects the squad going
            # INTO this week.
            pool_this_week = (held - set(week.get("sells", []))) | set(week.get("buys", []))

        ranked = sorted((n for n in pool_this_week if n in xp_lookup),
                        key=lambda n: xp_lookup[n], reverse=True)
        captains[gw] = {
            "captain": ranked[0] if ranked else None,
            "vice_captain": ranked[1] if len(ranked) > 1 else None,
        }

    for name in list(held):
        _close_bar(name, end_gw, "horizon_end")

    bars.sort(key=lambda b: (b["start_gw"], b["name"]))
    return {
        "start_gw": start_gw,
        "end_gw": end_gw,
        "bars": bars,
        "chip_events": chip_events,
        "captains": captains,
    }


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
    bench_boost_gw: Optional[int] = None,
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

    # Rank-aware logic is driven by the STRATEGY the manager picked, not by the
    # calendar. This was gated behind current_gw >= PHASE2_START_GW (26), so for
    # gameweeks 1-25 the EO, blocker, divergence, stack and CVaR terms were all
    # inert and "Protect my lead" produced a byte-identical squad to "Balanced".
    # With the strategy tilts now removed from Layer 1 (see _risk_adjust's
    # deletion note), the objective is the ONLY place strategy can express
    # itself -- so gating it here would leave the selector doing nothing at all.
    mode = _strategy_mode(risk)
    phase = 2 if mode in ("blocker", "divergence") else 1
    eo_map = _eo_map() if phase >= 2 else None

    # The real -4 plus an explicit, separately-named risk hurdle. Was
    # hit_cost (6.5 balanced / 8.0 conservative) * HORIZON_SUM (3.1), charging
    # 20.2 to 24.8 points for a one-off -4 and making hits effectively
    # impossible at any risk setting. The comment above this line used to claim
    # the opposite of what the code did.
    risk_key = _RISK_ALIASES.get((risk or "balanced").lower().strip(),
                                 (risk or "balanced").lower().strip())
    hit_charge = HIT_COST + HIT_HURDLE.get(risk_key, HIT_HURDLE["balanced"])
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
    # Minutes recency (C10b), fetched BEFORE any projection is computed so the
    # squad and the market are estimated on the same footing. Asymmetry would be
    # its own bias: accurate minutes for the fifteen you own and season averages
    # for everyone else systematically tilts every buy/sell comparison in one
    # direction, which is not obviously better than being wrong about both.
    #
    # One HTTP call per uncached player, bounded, cached for an hour, and the
    # whole thing degrades to the season rate if the network is unavailable.
    # It sits here rather than in _minute_distribution because this is the one
    # place that knows the full set of players about to be evaluated.
    if RECENT_MINUTES_ENABLED:
        try:
            prefetch_recent_minutes(
                list(current_ids)
                + [e["id"] for e in elements_by_id.values()
                   if _to_float(e.get("minutes")) > 0][:RECENT_MAX_FETCH])
        except Exception:
            pass

    pool = []
    seen = set()

    for pid in current_ids:
        e = elements_by_id.get(pid)
        if not e:
            continue
        pos = POS_MAP.get(e["element_type"])
        # Multi-GW horizon xP drives the solver; keep the single-GW xP alongside
        # for final lineup/captaincy decisions (which are per-gameweek).
        xp, note = _player_xp_horizon(e, fixture_lookup, event)
        xp_gw, _ = _player_xp(e, fixture_lookup, event=event)
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
        # GK Transfer-In Fragility Discount (Task 1.2): applied only here, to
        # players NOT already in the squad -- this loop IS "evaluating
        # prospective transfers in". A no-op for every other position; see
        # GK_TRANSFER_IN_CS_DAMPENER's docstring.
        dampener = GK_TRANSFER_IN_CS_DAMPENER if pos == "GK" else 1.0
        xp, note = _player_xp_horizon(e, fixture_lookup, event, gk_cs_dampener=dampener)
        if note in ("OUT", "Blank", "Injured", "Suspended", "Unavailable", "No minutes"):
            continue
        incoming_by_pos[pos].append((xp, note, e))
    for pos, entries in incoming_by_pos.items():
        entries.sort(key=lambda t: t[0], reverse=True)
        dampener = GK_TRANSFER_IN_CS_DAMPENER if pos == "GK" else 1.0
        for xp, note, e in entries[:POOL_SHORTLIST.get(pos, 30)]:
            xp_gw, _ = _player_xp(e, fixture_lookup, event=event, gk_cs_dampener=dampener)
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
    stress_scenarios = {}
    multi_gw_plan = []
    try:
        pool_ids = [p["id"] for p in pool]
        # Everyone in the pool who is not currently owned is, by definition, a
        # prospective transfer in -- gk_transfer_in_ids only actually changes
        # anything for the GK ones (see _generate_scenarios).
        gk_transfer_in_ids = set(pool_ids) - set(current_ids)
        saa_mean, scenario_matrix = _generate_scenarios(
            pool_ids, fixture_lookup, event, risk=risk, gk_transfer_in_ids=gk_transfer_in_ids)
        stress_scenarios = _select_stress_scenarios(scenario_matrix, selected_ids=current_ids)
        # Per-player outcome volatility for the Stage 3 search hurdle. Read
        # straight off the (S, n) matrix -- deliberately NOT via
        # _scenario_distribution, which mis-indexes the scenario axis until
        # Stage 5 and would yield a sigma computed from six gameweek slots.
        sigmas = _scenario_sigmas(scenario_matrix)
        for p in pool:
            m = saa_mean.get(p["id"])
            if m:
                horizon = sum(HORIZON_WEIGHTS[t] * (m[t] if t < len(m) else 0.0)
                              for t in range(len(HORIZON_WEIGHTS)))
                # C12. This overwrite REPLACES the horizon xP that
                # _player_xp_horizon produced, and the SAA mean is rebuilt from
                # single-gameweek projections that never saw the suspension
                # haircut. So a player one booking from a ban had his 15%
                # discount silently reverted for the entire solve -- the one
                # place it was meant to change a decision.
                if p.get("on_yellow_card_tightrope"):
                    horizon *= TIGHTROPE_DISCOUNT
                p["xp"] = round(horizon, 2)
            if p["id"] in sigmas:
                p["sigma"] = sigmas[p["id"]]
        multi_gw_plan = _plan_transfers_multi_gw(pool, budget, free_transfers, current_ids,
                                                saa_mean, event, bank_cash=bank)
    except Exception:
        saa_mean, scenario_matrix, stress_scenarios, multi_gw_plan = {}, {}, {}, []

    def _get_moves(selected_ids, is_unlimited, parts=None):
        """Pair the solver's chosen 15 against the current squad, and report the
        objective decomposition that produced it.

        Two changes from the previous version:

        * Pairing minimises total price distance within a position, rather than
          sorting both sides by xP and zipping them. The MIP picks a SET; "who
          replaced whom" is a presentational choice, and the old zip invented a
          mapping that every downstream artefact (per-row xp_gain, rationale,
          the +X badge) was then built on.

        * net_gain comes from the solver's own components instead of a
          recomputed sum(xp_in - xp_out) minus two of the eight terms. The old
          figure excluded captaincy, bench weights, CVaR, stacking, EO and tax,
          so the number shown to the user was not the quantity being maximised
          and could rank moves differently from the solver that chose them.
        """
        if not selected_ids:
            return [], 0, 0.0, 0.0, {}
        selected_set = set(selected_ids)
        sold = [pid for pid in current_ids if pid not in selected_set]
        bought = [pid for pid in selected_ids if pid not in current_ids]
        mvs = []

        for pos in ["GK", "DEF", "MID", "FWD"]:
            sold_pos = [p for p in sold if pool_by_id[p]["position"] == pos]
            bought_pos = [p for p in bought if pool_by_id[p]["position"] == pos]
            for o, i in _pair_by_price(sold_pos, bought_pos, pool_by_id):
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
        cost_chg = round(sum(m["cost"] for m in mvs), 2)

        # Waterfall, straight from the objective. Keys are display-ready.
        parts = parts or {}
        breakdown = {
            "projected_points": round(float(parts.get("squad_xp", 0.0)), 2),
            "points_hit": round(float(parts.get("hit_cost", 0.0)), 2),
            "transfer_bar": round(float(parts.get("hurdle", 0.0)), 2),
            "banked_transfer_value": round(float(parts.get("ft_option", 0.0)), 2),
            "chip_cost": round(float(parts.get("scarcity", 0.0)), 2),
            "cash_optionality": round(float(parts.get("liquidity", 0.0)), 2),
        }
        # Net gain relative to holding: the objective delta the moves bought.
        net_gain = round(sum(m["xp_gain"] for m in mvs)
                         + breakdown["points_hit"] + breakdown["transfer_bar"], 2)
        breakdown["net"] = net_gain
        return mvs, hits, net_gain, cost_chg, breakdown

    # ==============================================================
    # 1. Universe A: Standard Transfers Optimization (Takes Hit Penalty)
    # ==============================================================
    # Active GK Lock (Task 1.3): only on the standard, limited-transfer
    # solve. Never on the chip solves below -- Wildcard, Free Hit and a
    # Bench Boost rebuild are deliberate full reshapes the user has
    # explicitly asked for, where locking one player back in would be a
    # surprising and unwanted constraint on a chip whose entire point is
    # reconsidering the whole 15.
    locked_gk_id = _locked_starting_gk(current_ids, elements_by_id, pool_by_id)
    std_selected, _std_obj, std_parts = _solve_squad(
        pool, budget=budget, must_include_ids=set(current_ids),
        hit_config={"free_transfers": free_transfers, "hit_cost": hit_charge, "max_transfers": max_transfers},
        bench_boost=False,
        # No bench cap on the standard weekly solve. The fifteen are fixed here,
        # so the ONLY way to satisfy a bench-cost cap is to change who STARTS --
        # which forces price into a lineup decision that should be made on
        # expected points. Measured on the fixture: a GBP 16m cap cut the XI by
        # 12.7 points, RAISED bench cost (it benched cheap players to start
        # expensive ones), and where the squad could not comply at all it made
        # the program infeasible. The cap belongs on squad CONSTRUCTION, which is
        # what the chip solves below do.
        holding_map=holding_map, current_gw=current_gw,
        eo_map=eo_map, mode=mode, phase=phase, rival_ids=rival_ids,
        cvar_scenarios=stress_scenarios,
        locked_ids=({locked_gk_id} if locked_gk_id else None),
    )
    std_moves, std_hits, std_net, std_cost, std_breakdown = _get_moves(std_selected, False, std_parts)
    # The two post-solve vetoes were removed here.
    #
    #   1. `if std_decision <= 0: hold` re-tested the solve against a recomputed
    #      figure that ALSO re-added the liquidity bonus the objective had
    #      already counted -- so cash optionality was charged twice, once inside
    #      the MIP and once against its own output.
    #   2. The roll hurdle discarded the entire plan whenever the IMMEDIATE
    #      gameweek gain fell under 1.5, binning genuinely good multi-week moves,
    #      and it looked up `risk` without going through _RISK_ALIASES so it
    #      silently fell back to the default for "rank protecting (shield)".
    #
    # Both existed to stop churn on noise. That job now belongs to the separable
    # in-objective hurdle, which the solver optimises against rather than having
    # its answer overturned afterwards -- no holes left in transfer bundles, and
    # no risk of re-solve cycling inside the interactive time budget.

    roll_transfer = len(std_moves) == 0 and free_transfers < 5
    projected_ft = min(free_transfers + 1, 5) if roll_transfer else free_transfers

    # ==============================================================
    # 2. Universe B: Unlimited Transfers (For Wildcard / Free Hit)
    # ==============================================================
    unl_moves, unl_hits, unl_net, unl_cost = [], 0, 0.0, 0.0
    if any(c in eval_chips for c in ("Wildcard", "Free Hit")):
        unl_selected, _unl_obj, unl_parts = _solve_squad(
            pool, budget=budget, must_include_ids=set(current_ids),
            hit_config={"free_transfers": 15, "hit_cost": 0.0, "max_transfers": 15,
                        "hurdle_scale": 0.0},
            bench_boost=("Bench Boost" in eval_chips),
            bench_cap=_bench_cap_for(current_gw, bench_boost_gw),
            holding_map=holding_map, current_gw=current_gw,
            eo_map=eo_map, mode=mode, phase=phase, rival_ids=rival_ids,
            cvar_scenarios=stress_scenarios,
        )
        unl_moves, unl_hits, unl_net, unl_cost, _unl_bd = _get_moves(unl_selected, True, unl_parts)

    # Free Hit lasts exactly ONE gameweek -- the squad reverts afterwards -- so
    # it must be solved and scored on the single gameweek. It was previously
    # sharing the unlimited horizon solve, valuing a one-week squad over four
    # weeks and inflating it ~3x against Bench Boost and Triple Captain, which
    # are correctly scored over one.
    fh_moves, fh_hits, fh_net, fh_cost = [], 0, 0.0, 0.0
    fh_squad_ids = []
    if "Free Hit" in eval_chips:
        fh_pool = []
        for entry in pool:
            e = dict(entry)
            e["xp"] = e.get("xp_gw", e["xp"])     # one week, not the horizon
            fh_pool.append(e)
        fh_selected, _fh_obj, fh_parts = _solve_squad(
            fh_pool, budget=budget, must_include_ids=set(current_ids),
            hit_config={"free_transfers": 15, "hit_cost": 0.0, "max_transfers": 15,
                        "hurdle_scale": 0.0},
            bench_boost=False,
            holding_map=holding_map, current_gw=current_gw,
            eo_map=eo_map, mode=mode, phase=phase, rival_ids=rival_ids,
        )
        fh_squad_ids = fh_selected or []
        if fh_selected:
            fh_by_id = {e["id"]: e for e in fh_pool}
            fh_xi = sorted((fh_by_id[i]["xp"] for i in fh_selected), reverse=True)[:11]
            cur_xi = sorted((fh_by_id[i]["xp"] for i in current_ids if i in fh_by_id),
                            reverse=True)[:11]
            fh_net = round(sum(fh_xi) - sum(cur_xi), 2)

    # Wildcard is a full-season chip: re-solve with a scarcity penalty so it is
    # only deployed when the rebuilt squad decisively outscores the current one.
    wc_moves, wc_hits, wc_net, wc_cost = [], 0, 0.0, 0.0
    if "Wildcard" in eval_chips:
        wc_selected, _wc_obj, wc_parts = _solve_squad(
            pool, budget=budget, must_include_ids=set(current_ids),
            hit_config={"free_transfers": 15, "hit_cost": 0.0, "max_transfers": 15,
                        "hurdle_scale": 0.0},
            bench_boost=("Bench Boost" in eval_chips),
            bench_cap=_bench_cap_for(current_gw, bench_boost_gw),
            scarcity_cost=WILDCARD_SCARCITY_COST,
            holding_map=holding_map, current_gw=current_gw,
            eo_map=eo_map, mode=mode, phase=phase, rival_ids=rival_ids,
            cvar_scenarios=stress_scenarios,
        )
        wc_moves, wc_hits, wc_net, wc_cost, _wc_bd = _get_moves(wc_selected, True, wc_parts)

    # ==============================================================
    # 3. Project Universe A Squad (For BB and TC Eval)
    # ==============================================================
    sold_ids = [m["out"]["id"] for m in std_moves]
    bought_ids = [m["in"]["id"] for m in std_moves]
    # Held players keep the caller's dict, but their xP is RECOMPUTED here.
    # select_starting_xi ranks on "xp", and the Bench Boost and Triple Captain
    # scores are read off the resulting bench and captain -- so if a caller
    # omitted or stale-filled that field, both chips silently scored 0.0 and the
    # lineup was chosen on arbitrary values. The engine should not depend on its
    # caller to supply its own projections.
    std_squad = []
    for p in squad:
        if p["player_id"] in sold_ids:
            continue
        held = dict(p)
        fpl_p = elements_by_id.get(p["player_id"])
        if fpl_p:
            xp_h, note_h = _player_xp(fpl_p, fixture_lookup, event=event)
            held["xp"] = xp_h
            held.setdefault("status", note_h)
        std_squad.append(held)
    
    current_out_statuses = sum(1 for p in squad if p.get("status") in ("Injured", "Suspended", "Unavailable", "OUT"))
    
    for in_id in bought_ids:
        fpl_p = elements_by_id[in_id]
        pos = POS_MAP.get(fpl_p["element_type"])
        xp, note = _player_xp(fpl_p, fixture_lookup, event=event)
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

    # Floor / Expected / Ceiling for what actually SCORES: the starting eleven at
    # 1x and the captain at 2x. Spreading over all fifteen counted four bench
    # players who contribute nothing in a normal gameweek, which flattened the
    # distribution and made the range look narrower than the week really is.
    _xi_ids = {_pid(p) for p in (std_best_xi.get("xi") or [])}
    _cap = _pid(std_best_xi.get("captain") or {})
    _mult = {pid: (2.0 if pid == _cap else 1.0) for pid in _xi_ids}
    scenario_dist = _scenario_distribution(
        list(_xi_ids) or [p["player_id"] for p in std_squad],
        scenario_matrix, weights=HORIZON_WEIGHTS, n=4, multipliers=_mult,
    ) if scenario_matrix else {}

    # ==============================================================
    # 4. Ultra-Strict Chip Scoring & Ranking
    # ==============================================================
    # Every chip scored as the TOTAL extra points playing it now delivers, over
    # the window it actually applies to. Previously Wildcard and Free Hit were
    # measured over the 4-gameweek horizon while Bench Boost and Triple Captain
    # were measured over one, so the first two were ~3x inflated by construction
    # and almost always outranked the others.
    #
    # Wildcard legitimately keeps the horizon: the squad it builds persists.
    # That is a real difference in what the chip buys, not a unit mismatch.
    chip_scores = {}
    if "Wildcard" in eval_chips:
        chip_scores["Wildcard"] = round(wc_net - std_net, 2)
    if "Free Hit" in eval_chips:
        chip_scores["Free Hit"] = round(fh_net, 2)
    if "Bench Boost" in eval_chips:
        chip_scores["Bench Boost"] = round(sum(p.get("xp", 0.0) for p in std_best_xi["bench"]), 2)
    if "Triple Captain" in eval_chips:
        cap = std_best_xi.get("captain")
        chip_scores["Triple Captain"] = round(cap.get("xp", 0.0) if cap else 0.0, 2)

    # Free Hit is also the blank-gameweek escape hatch, which is its dominant
    # modern use -- rescuing a week where much of the squad has no fixture. The
    # calendar detector built in Stage 5 is exactly this trigger.
    fh_emergency = False
    try:
        cal = _fixture_calendar(fixture_lookup, event, 1).get(event, {})
        playing = sum(1 for p in squad
                      if _gw_fixtures(fixture_lookup.get(
                          elements_by_id.get(p["player_id"], {}).get("team"), []), event))
        fh_emergency = bool(cal.get("is_bgw")) and playing <= 8
    except Exception:
        fh_emergency = False

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
        if chip_name == "Wildcard":
            # Empirical timing prior: a Set-1 wildcard must last to GW19, so a
            # strong early swing is a trap. Additive, not a hard band -- a large
            # enough gain still clears it.
            threshold += _wildcard_timing_penalty(current_gw)
        if chip_name == "Free Hit" and fh_emergency:
            # A blank gameweek that guts the squad is what the chip is for.
            threshold = 0.0
        passed_threshold = score >= threshold

        # Wildcard exception: lower the barrier if the squad is injury-ravaged.
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
        advice = f"Make {n} transfer(s), taking {std_hits} hit(s) (-{int(HIT_COST * std_hits)} pts) for a net +{std_net:.1f} xP over the 4-GW horizon."

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
        "horizon": 4,
        "breakdown": std_breakdown,
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

        xp, note = _player_xp(p, fixture_lookup, event=event)

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
            fs = _gw_fixtures(fx, start_event + i)
            if not fs:
                att_ease.append(3.0)
                def_ease.append(3.0)
            else:
                scale = 1.0 + 0.5 * (len(fs) - 1)      # doubles count for more
                att_ease.append(min(5.0, scale * sum(
                    6.0 - _to_float(f.get("opp_strength_def", 3.0)) for f in fs) / len(fs)))
                def_ease.append(min(5.0, scale * sum(
                    6.0 - _to_float(f.get("opp_strength_att", 3.0)) for f in fs) / len(fs)))
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


# Each check carries a stable `key` alongside its display `label`. Callers and
# tests match on the key; only the UI reads the label. Matching on the label
# meant the Part E copy pass silently broke logic that had nothing to do with
# wording -- which is a good reason for copy never to be an identifier.
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
        "key": "bench_capital",
        "label": "Money sat on your bench",
        "ok": not stranded,
        "detail": (f"£{bench_cost:.1f}m of your budget is sitting on the bench — that's "
                   f"money not scoring you points."
                   if stranded else
                   f"£{bench_cost:.1f}m on the bench. That's sensible."),
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
        "key": "enabler_efficiency",
        "label": "Dead weight in your last two bench slots",
        "ok": not dead,
        "detail": ("You're paying over £4.0m for players who aren't playing: "
                   + "; ".join(dead)
                   if dead else
                   "Your last two bench slots are cheap, which is exactly what you want."),
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
        "key": "formation_optionality",
        "label": "Can you change shape?",
        "ok": optionality_ok,
        "detail": (f"You can line up {within} different ways without losing much — "
                   f"handy when someone gets injured."
                   if optionality_ok else
                   f"Only {within} formation really works for this squad. One injury "
                   f"and you're stuck."),
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
        "key": "pivot_liquidity",
        "label": "Can you afford to switch formation?",
        "ok": not deadlock,
        "detail": (
            f"£{bank:.1f}m in the bank covers a swap either way "
            f"(you'd need £{max(0.0, mid_to_fwd):.1f}m to go MID→FWD, "
            f"£{max(0.0, def_to_mid):.1f}m for DEF→MID)."
            if not deadlock else
            f"You're stuck: £{bank:.1f}m isn't enough to switch shape, even with "
            f"your cheapest swap."
        ),
    })

    return checks


PRED_FLOOR = 0.05      # a projection below this is not a real forecast
CALIBRATION_DAMPING = 0.25


def reproject(row, weights):
    """Re-run one stored prediction under a candidate set of weights.

    This has to be _player_xp_raw's arithmetic exactly, or coordinate descent
    optimises something the app does not compute. The previous surrogate was
    wrong in two ways that pulled in the same direction:

      * The cameo penalty is CLAMPED in production --
        `p_cameo * max(0, autosub_ref - xp_cameo)` -- so once a player's cameo
        projection exceeds autosub_ref the penalty is zero and its gradient with
        it. The surrogate modelled it unclamped as `autosub_ref * cameo_mass`,
        which is non-zero for every player with any cameo mass at all, so the
        descent saw a gradient where production has none.
      * Penalties are applied BEFORE the ep_next blend, so a unit change in a
        penalty moves the final projection by only (1 - ep_w). The surrogate
        assumed 1.0, over-attributing by up to 2x when ep_w was at its old flat
        0.5. Stage 5b made ep_w per-player, so it now has to be stored per row.

    Rows written before those columns existed fall back to the old behaviour
    (unclamped, unblended, off base_pts) rather than being dropped -- with a
    NULL xp_cameo the clamp cannot bind, which is exactly what the old surrogate
    assumed.
    """
    gmod = weights.get("global_xP_modifier", 1.0)
    autosub_ref = weights.get("autosub_ref", 1.8)
    rot = weights.get("rotation_convexity", 0.4)
    decay = weights.get("dixon_coles_decay", 0.03)

    cameo = _to_float(row.get("cameo_mass", 0.0))
    var = _to_float(row.get("rotation_variance", 0.0))
    dc = _to_float(row.get("dc_sensitivity", 0.0))

    # raw_total is pre-penalty AND pre-blend. base_pts is _player_xp at neutral
    # weights, so it has already been through the blend -- reconstructing from
    # it and then blending again applies ep_next twice. Legacy rows have only
    # base_pts, and get the old unblended arithmetic to match.
    raw = row.get("raw_total")
    legacy = raw is None
    base = _to_float(row.get("base_pts", row.get("predicted_xp", 0.0))) if legacy else _to_float(raw)

    xp_cameo = row.get("xp_cameo")
    if xp_cameo is None:
        cameo_penalty = autosub_ref * cameo
    else:
        cameo_penalty = cameo * max(0.0, autosub_ref - _to_float(xp_cameo))

    our_total = (base
                 - cameo_penalty
                 - rot * var
                 + (decay - DIXON_COLES_DECAY_DEFAULT) * dc)

    ep_w = row.get("ep_w")
    if legacy or ep_w is None:
        pred = gmod * our_total
    else:
        ep_w = max(0.0, min(1.0, _to_float(ep_w)))
        pred = gmod * ((1.0 - ep_w) * our_total + _to_float(row.get("ep_term", 0.0)))
    # Production clamps at zero (_player_xp_raw returns max(xp, 0.0)).
    return max(pred, 0.0)


def evaluate_calibration(rows, weights):
    """RMSE + count-data Poisson deviance of re-projected predictions vs actuals."""
    if not rows:
        return {"rmse": 0.0, "deviance": 0.0, "n": 0}
    sq = 0.0
    dev = 0.0
    n = 0
    for r in rows:
        pred = reproject(r, weights)
        actual = _to_float(r.get("actual_points", 0.0))
        sq += (pred - actual) ** 2
        # The deviance term needs a MEANINGFUL floor, not an epsilon. At the old
        # max(pred, 1e-6) a single zero-or-negative prediction against a 12-point
        # haul contributed 2*(0 + 12*13.8) = +331 to a metric whose typical row
        # is order 1 -- so one such row outweighed several hundred good ones and
        # the descent chased it. FPL points are not Poisson anyway (bonus, cards,
        # -1 per two conceded), so this term is a shape prior on count-like
        # error, not a likelihood; flooring it costs nothing it was entitled to.
        safe = max(pred, PRED_FLOOR)
        dev += 2.0 * (safe - actual * math.log(safe))
        n += 1
    return {"rmse": round(math.sqrt(sq / n), 4), "deviance": round(dev / n, 4), "n": n}


def calibrate_weights(rows, weights, damping=CALIBRATION_DAMPING):
    """Damped coordinate descent over the 4 targeted parameters.

    Minimises a blended RMSE + Poisson-deviance metric. Returns an updated dict.

    On damping: the probe is +/-10%, so a run moves a weight by at most
    `damping * 0.10` of its value. At the shipped 0.05 that was 0.5% per run --
    about 53 weekly runs to move a weight 30%, against a 38-gameweek season, so
    weights.json could not meaningfully change within a season no matter what
    the data said. Worse, the convergence test passed damping=1.0, twenty times
    the shipped value, so the production setting was never exercised. At 0.25 a
    weight moves 2.5% per run and reaches 30% in about fourteen weeks, which is
    responsive within a season while still needing a persistent signal rather
    than one freak gameweek.
    """
    if not rows:
        return dict(weights)

    def metric(w):
        e = evaluate_calibration(rows, w)
        return e["rmse"] + 0.3 * e["deviance"]

    bounds = {
        "global_xP_modifier": (0.5, 2.0),
        "autosub_ref": (0.5, 3.0),
        "rotation_convexity": (0.0, 1.5),
        "dixon_coles_decay": (0.0, 0.15),
    }
    new = dict(weights)
    for name, (lo, hi) in bounds.items():
        best_val = new[name]
        best_metric = metric(new)
        for frac in (-0.1, 0.1):
            cand = max(lo, min(hi, new[name] * (1.0 + frac)))
            trial = dict(new)
            trial[name] = cand
            m = metric(trial)
            if m < best_metric:
                best_metric, best_val = m, cand
        new[name] = round(new[name] + damping * (best_val - new[name]), 6)
    return new


_LIVE_CACHE: Dict[int, Dict[str, Any]] = {}
_LIVE_CACHE_TS: float = 0.0
_LIVE_CACHE_GW: Optional[int] = None


def get_live_event(gw):
    """Live per-player stats -> {pid: {minutes, total_points, bonus, bps, played}}."""
    global _LIVE_CACHE, _LIVE_CACHE_TS, _LIVE_CACHE_GW
    # The gameweek is part of the key. It was not, so any two different
    # gameweeks requested inside the 60s window returned the FIRST one's data --
    # silently serving stale scores to the live H2H tracker.
    if _LIVE_CACHE and _LIVE_CACHE_GW == gw and (time.time() - _LIVE_CACHE_TS) < 60:
        return _LIVE_CACHE
    try:
        resp = requests.get(f"{BASE_URL}/event/{gw}/live/", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}
    out = {}
    for e in data.get("elements", []):
        st = e.get("stats", {})
        out[e["id"]] = {
            "minutes": _to_float(st.get("minutes")),
            "total_points": _to_float(st.get("total_points")),
            "bonus": _to_float(st.get("bonus")),
            "bps": _to_float(st.get("bps")),
            "played": bool(st.get("played")),
        }
    _LIVE_CACHE = out
    _LIVE_CACHE_TS = time.time()
    _LIVE_CACHE_GW = gw
    return out


def compute_h2h(my_squad, rival_squad, live):
    """Head-to-head live comparison of two active XIs.

    Returns {my_rows, rival_rows, my_total, rival_total, margin} factoring the
    captain/TC multiplier, live BPS (provisional bonus) and remaining minutes.
    """
    def _resolve(squad):
        starters = [p for p in squad if (p.get("multiplier", 1) or 1) >= 1]
        rows = []
        for p in starters:
            pid = p.get("player_id")
            lv = live.get(pid, {})
            mult = int(p.get("multiplier", 1) or 1)
            pts = lv.get("total_points", 0.0) * mult
            mins = lv.get("minutes", 0.0)
            played = lv.get("played", False)
            in_progress = (not played) or (0 < mins < 90)
            rows.append({
                "pid": pid,
                "name": p.get("name", "?"),
                "multiplier": mult,
                "points": round(pts, 1),
                "minutes": mins,
                "in_progress": in_progress,
                "bps": lv.get("bps", 0.0),
                "bonus": lv.get("bonus", 0.0),
            })
        return rows

    my_rows = _resolve(my_squad)
    rival_rows = _resolve(rival_squad)
    my_total = sum(r["points"] for r in my_rows)
    rival_total = sum(r["points"] for r in rival_rows)
    return {
        "my_rows": my_rows,
        "rival_rows": rival_rows,
        "my_total": round(my_total, 1),
        "rival_total": round(rival_total, 1),
        "margin": round(my_total - rival_total, 1),
    }
