"""Golden-file harness: load the synthetic bootstrap into fpl_tools offline.

Patches the network accessors and clears every module-level cache, so a test run
is hermetic and reproducible. Also builds the standard squad set used by the
legality gate.

Squad archetypes are chosen so that an *unconstrained* solver would pick an
illegal eleven for several of them -- that is the whole point of the gate. See
`SQUAD_SPECS` for which trap each one plants.
"""

import contextlib
import json
import os

import fpl_tools

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Every module-level cache in fpl_tools. Missing one silently leaks state
# between tests, which is exactly the class of flakiness golden files exist to
# eliminate, so this list is asserted complete in test_stage0_harness.py.
_CACHE_RESET = {
    "_CACHE": dict,
    "_LEAGUE_AVG": lambda: None,
    "_LEAGUE_AVG_TS": float,
    "_TEAM_RATINGS_CACHE": lambda: None,
    "_TEAM_RATINGS_TS": float,
    "_TEAM_PLAYED": lambda: None,
    "_TEAM_PLAYED_TS": float,
    "_WEIGHTS_CACHE": lambda: None,
    "_WEIGHTS_STAMP": lambda: (0.0, 0.0),
    "_DC_RAW": dict,
    "_FIXTURE_LOOKUP_CACHE": lambda: None,
    "_FIXTURE_LOOKUP_TS": float,
    "_RECENT_CACHE": dict,
    "_RECENT_CACHE_TS": dict,
    "_EO_CACHE": dict,
    "_EO_CACHE_TS": float,
    "_LIVE_CACHE": dict,
    "_LIVE_CACHE_TS": float,
    "_LIVE_CACHE_GW": lambda: None,
    "_CALENDAR_CACHE": lambda: None,
    "_CALENDAR_CACHE_TS": float,
}


def load_synthetic():
    with open(os.path.join(FIXTURE_DIR, "bootstrap_synthetic.json")) as fh:
        bootstrap = json.load(fh)
    with open(os.path.join(FIXTURE_DIR, "fixtures_synthetic.json")) as fh:
        fixtures = json.load(fh)
    return bootstrap, fixtures


def reset_caches():
    for name, factory in _CACHE_RESET.items():
        if hasattr(fpl_tools, name):
            setattr(fpl_tools, name, factory())
    fpl_tools._ODDS_CACHE = {"ts": 0.0, "data": None}
    # No FPL API in the build environment, so leaving the recency prefetch on
    # would mean a few hundred doomed HTTP attempts per solve. The suite
    # therefore exercises the SEASON minutes path; the recency path is tested by
    # populating _RECENT_CACHE directly, which is also the only way to control
    # what history a player has.
    fpl_tools.RECENT_MINUTES_ENABLED = False


@contextlib.contextmanager
def synthetic_world(profile="deterministic"):
    """Run a block against the synthetic bootstrap with no network access."""
    bootstrap, fixtures = load_synthetic()
    saved = {
        "_get_bootstrap": fpl_tools._get_bootstrap,
        "_get_fixtures": fpl_tools._get_fixtures,
        "_get_all_fixtures": fpl_tools._get_all_fixtures,
        "_fetch_market_win_probs": fpl_tools._fetch_market_win_probs,
        "profile": fpl_tools.get_solver_profile(),
    }
    fpl_tools._get_bootstrap = lambda: bootstrap
    fpl_tools._get_fixtures = lambda: fixtures
    fpl_tools._get_all_fixtures = lambda: fixtures
    # No odds in the fixture: exercise the static-ratings path deterministically.
    fpl_tools._fetch_market_win_probs = lambda bootstrap=None: {}
    fpl_tools.set_solver_profile(profile)
    reset_caches()
    try:
        yield bootstrap, fixtures
    finally:
        for k in ("_get_bootstrap", "_get_fixtures", "_get_all_fixtures", "_fetch_market_win_probs"):
            setattr(fpl_tools, k, saved[k])
        fpl_tools.set_solver_profile(saved["profile"])
        reset_caches()


# ---------------------------------------------------------------------------
# Squad construction
# ---------------------------------------------------------------------------

# (name, description, selection strategy). The "trap" squads are the ones whose
# highest-xP eleven is an illegal formation.
SQUAD_SPECS = [
    ("balanced",          "mid-table pool, no trap"),
    ("zero_fwd_trap",     "three worthless forwards -> best XI wants 0 FWD"),
    ("two_def_trap",      "three worthless defenders -> best XI wants 2 DEF"),
    ("premium_heavy",      "top assets, tight bank"),
    ("budget_heavy",       "cheap squad, large bank"),
    ("flagged_keeper",     "starting GK flagged out"),
    ("flagged_outfield",   "two starters doubtful"),
    ("all_nailed",         "every player a nailed starter"),
    ("rotation_risk",      "bench-fodder heavy"),
    ("blank_gw_exposed",   "loaded with GW9 blankers"),
    ("double_gw_exposed",  "loaded with GW10 doublers"),
    ("three_per_club_tight", "at the 3-per-club limit on four clubs"),
    ("broke",              "zero bank"),
    ("rich",               "large bank"),
    ("mixed_value",        "spread across price tiers"),
    ("def_heavy_value",    "strong cheap defenders"),
    ("fwd_heavy_value",    "strong cheap forwards"),
    ("top_six_stack",      "stacked on the strongest clubs"),
    ("bottom_six_stack",   "stacked on the weakest clubs"),
    ("wildcard_shape",     "structurally awkward, needs a rebuild"),
]

SMOKE_SQUADS = [
    "balanced", "zero_fwd_trap", "two_def_trap", "flagged_keeper", "wildcard_shape",
]


def active_squads():
    """Squad names for the current scope.

    20 squads x 4 solve paths is 80 MILP solves; at the deterministic profile's
    60s ceiling that is a worst case no one wants blocking a pull request. So
    per-commit CI runs the 5-squad smoke set (both traps included, so the Stage 1
    gate keeps its teeth) and the nightly job runs all 20.

    FPL_GOLDEN_SCOPE=full forces the complete set locally.
    """
    if os.environ.get("FPL_GOLDEN_SCOPE", "full").lower() == "smoke":
        return [(n, d) for n, d in SQUAD_SPECS if n in SMOKE_SQUADS]
    return list(SQUAD_SPECS)


def _by_pos(bootstrap):
    out = {1: [], 2: [], 3: [], 4: []}
    for e in bootstrap["elements"]:
        out[e["element_type"]].append(e)
    for k in out:
        out[k].sort(key=lambda e: (-e["now_cost"], e["id"]))
    return out


def _pick(pool, want, club_count, exclude, reverse=False):
    """Take `want` players respecting the 3-per-club rule."""
    chosen = []
    seq = list(reversed(pool)) if reverse else pool
    for e in seq:
        if len(chosen) == want:
            break
        if e["id"] in exclude:
            continue
        if club_count.get(e["team"], 0) >= 3:
            continue
        chosen.append(e)
        exclude.add(e["id"])
        club_count[e["team"]] = club_count.get(e["team"], 0) + 1
    if len(chosen) < want:
        raise RuntimeError(f"could not fill {want} players (got {len(chosen)})")
    return chosen


def build_squad(bootstrap, name):
    """Return a 15-man squad (2/5/5/3) as a list of bootstrap elements."""
    pos = _by_pos(bootstrap)
    exclude, clubs = set(), {}
    cheap = name in ("budget_heavy", "broke", "rich", "bottom_six_stack")

    # Trap squads deliberately fill one position with the worst available assets
    # so the unconstrained best-XI drops below the legal minimum for it.
    weak_fwd = name == "zero_fwd_trap"
    weak_def = name == "two_def_trap"

    gk = _pick(pos[1], 2, clubs, exclude, reverse=cheap)
    dfn = _pick(pos[2], 5, clubs, exclude, reverse=cheap or weak_def)
    mid = _pick(pos[3], 5, clubs, exclude, reverse=cheap)
    fwd = _pick(pos[4], 3, clubs, exclude, reverse=cheap or weak_fwd)
    return gk + dfn + mid + fwd


def squad_as_manager_input(bootstrap, squad):
    """Shape a fixture squad the way suggest_transfers_for_custom_squad wants."""
    teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    out = []
    for e in squad:
        price = e["now_cost"] / 10.0
        out.append({
            "player_id": e["id"],
            "name": e["web_name"],
            "team": teams.get(e["team"], "?"),
            "position": fpl_tools.POS_MAP[e["element_type"]],
            "price": price,
            "purchase_price": price,
            "selling_price": price,
            "status": "Available",
            "xp": 0.0,
        })
    return out


def market_pool_entries(bootstrap, fixture_lookup, event, per_pos=None):
    """A wider candidate pool, so the solver has real freedom over the 15.

    A pool of exactly 2/5/5/3 leaves squad membership fully determined by the
    positional constraints, so any objective term acting on x[pid] is constant
    and provably cannot change the answer. Tests that need to observe squad
    selection (EO, blocker/divergence) must use this rather than a single squad.
    """
    per_pos = per_pos or {1: 6, 2: 12, 3: 12, 4: 8}
    by_pos = _by_pos(bootstrap)
    teams_by_id = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    entries = []
    for etype, want in per_pos.items():
        for e in by_pos[etype][:want]:
            xp, note = fpl_tools._player_xp_horizon(e, fixture_lookup, event)
            xp_gw, _ = fpl_tools._player_xp(e, fixture_lookup, event=event)
            entries.append(fpl_tools._pool_entry(
                e, teams_by_id, xp, note, fpl_tools.POS_MAP[etype],
                selling_price=e["now_cost"] / 10.0, xp_gw=xp_gw, event=event))
    return entries


def squad_to_pool_entries(bootstrap, squad, fixture_lookup, event, risk="balanced"):
    """Convert bootstrap elements into the pool-entry shape _solve_squad wants."""
    teams_by_id = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    entries = []
    for e in squad:
        xp, note = fpl_tools._player_xp_horizon(e, fixture_lookup, event)
        xp_gw, _ = fpl_tools._player_xp(e, fixture_lookup, event=event)
        entries.append(fpl_tools._pool_entry(
            e, teams_by_id, xp, note, fpl_tools.POS_MAP[e["element_type"]],
            selling_price=e["now_cost"] / 10.0, xp_gw=xp_gw, event=event,
        ))
    return entries
