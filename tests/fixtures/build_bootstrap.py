"""Generate a deterministic synthetic `bootstrap-static` + fixture list.

Why synthetic rather than a frozen live snapshot: the golden-file harness needs
byte-level reproducibility and must run in CI without network access. A synthetic
pool also lets us plant the specific edge cases the legality gate has to catch
(squads whose highest-xP eleven is an illegal formation), which a real snapshot
would only contain by luck.

Run `python tests/fixtures/build_bootstrap.py` to regenerate
`bootstrap_synthetic.json` and `fixtures_synthetic.json`. Both are committed so
tests never depend on this script running.

SCALE NOTE — the team blocks below deliberately mirror the real FPL API:
`strength` is on a 1-5 scale while `strength_overall_home/away` are on a
~1000-1400 scale. `_team_attack_def_ratings` blends the *latter* against
Dixon-Coles ratings that live on a [1,5] scale, which is the Stage 4 clamp bug.
This premise could not be verified against the live API from the build
environment (the egress proxy denies fantasy.premierleague.com), so it must be
confirmed against a real bootstrap before Stage 4 ships.
"""

import json
import math
import os
import random

SEED = 20262027
N_TEAMS = 20
PLAYERS_PER_TEAM = 18          # 2 GK, 6 DEF, 6 MID, 4 FWD
GWS = 12                       # fixtures generated for events 1..12
CURRENT_GW = 4                 # events 1..3 finished, 4 is next

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

TEAM_NAMES = [
    ("Arsenal", "ARS"), ("Aston Villa", "AVL"), ("Bournemouth", "BOU"),
    ("Brentford", "BRE"), ("Brighton", "BHA"), ("Burnley", "BUR"),
    ("Chelsea", "CHE"), ("Crystal Palace", "CRY"), ("Everton", "EVE"),
    ("Fulham", "FUL"), ("Leeds", "LEE"), ("Liverpool", "LIV"),
    ("Man City", "MCI"), ("Man Utd", "MUN"), ("Newcastle", "NEW"),
    ("Nott'm Forest", "NFO"), ("Sunderland", "SUN"), ("Spurs", "TOT"),
    ("West Ham", "WHU"), ("Wolves", "WOL"),
]

POS_SLOTS = [(1, 2), (2, 6), (3, 6), (4, 4)]   # element_type -> count per team


def _strength_block(rank: int) -> dict:
    """FPL-shaped strength fields. rank 0 = strongest, 19 = weakest."""
    # 1-5 ordinal, as the real API's `strength` field
    ordinal = 5 - int(rank / 5)
    # ~1000-1400 continuous, as the real API's `strength_overall_*` fields
    overall = 1400 - int(rank * (400 / (N_TEAMS - 1)))
    return {
        "strength": max(1, min(5, ordinal)),
        "strength_overall_home": overall + 30,
        "strength_overall_away": overall - 30,
        "strength_attack_home": overall + 20,
        "strength_attack_away": overall - 20,
        "strength_defence_home": overall + 25,
        "strength_defence_away": overall - 25,
    }


def build_teams():
    teams = []
    for rank, (name, short) in enumerate(TEAM_NAMES):
        t = {
            "id": rank + 1,
            "code": 100 + rank,
            "name": name,
            "short_name": short,
            "played": CURRENT_GW - 1,
            # latent true strengths, used to drive results and player rates
            "_att": round(0.45 - rank * 0.045, 4),
            "_def": round(0.45 - rank * 0.045, 4),
        }
        t.update(_strength_block(rank))
        teams.append(t)
    return teams


def build_players(teams, rng):
    """One pool with planted archetypes.

    Every team gets a `premium_def` and a `dud_fwd` so the golden squads can
    construct pools whose unconstrained best eleven is an illegal formation.
    """
    elements = []
    pid = 0
    for t in teams:
        tier = 1.0 + t["_att"]           # ~1.45 strongest .. ~0.6 weakest
        for etype, count in POS_SLOTS:
            for j in range(count):
                pid += 1
                # j == 0 is the team's best asset in that position
                quality = max(0.25, 1.0 - 0.16 * j) * tier
                nailed = j < (2 if etype != 1 else 1)

                if etype == 1:      # GK
                    price, xg, xa, saves = 45 + int(10 * quality), 0.0, 0.02, 2.6
                elif etype == 2:    # DEF - deliberately generous, see docstring
                    price, xg, xa, saves = 40 + int(25 * quality), 0.11 * quality, 0.13 * quality, 0.0
                elif etype == 3:    # MID
                    price, xg, xa, saves = 45 + int(85 * quality), 0.33 * quality, 0.26 * quality, 0.0
                else:               # FWD - deliberately thin, see docstring
                    price, xg, xa, saves = 45 + int(60 * quality), 0.30 * quality, 0.14 * quality, 0.0

                minutes = int((260 if nailed else 95) * quality) + rng.randint(0, 40)
                starts = max(0, min(CURRENT_GW - 1, int(minutes / 90)))

                elements.append({
                    "id": pid,
                    "code": 200000 + pid,
                    "first_name": f"P{pid}",
                    "second_name": f"{t['short_name']}{etype}{j}",
                    "web_name": f"{t['short_name']}{etype}{j}",
                    "photo": f"{200000 + pid}.jpg",
                    "element_type": etype,
                    "team": t["id"],
                    "now_cost": price,
                    "minutes": minutes,
                    "starts": starts,
                    "status": "a",
                    "chance_of_playing_next_round": None,
                    "chance_of_playing_this_round": None,
                    "ep_next": round(1.6 + 2.4 * quality, 1),
                    "expected_goals_per_90": round(xg, 3),
                    "expected_assists_per_90": round(xa, 3),
                    "expected_goals_conceded_per_90": round(1.55 - 0.55 * t["_def"] * 2, 3),
                    "saves_per_90": round(saves, 2),
                    "influence": round(120 * quality, 1),
                    # Real per-player defensive counts, so the DefCon model has
                    # per-player signal to work with rather than a positional
                    # constant. Spread deliberately WIDE within a position: the
                    # bug being guarded against gave every defender an identical
                    # +1.36, so a fixture where they genuinely differ is the only
                    # way to prove the fix.
                    "clearances_blocks_interceptions": int(
                        minutes / 90.0 * (2.0 + 9.0 * (j / 5.0) if etype == 2 else 1.0 + 2.0 * (j / 5.0))),
                    "tackles": int(minutes / 90.0 * (1.0 + 3.0 * ((j + etype) % 4) / 3.0)),
                    "recoveries": int(minutes / 90.0 * (2.0 + 7.0 * ((j + 1) % 5) / 4.0)),
                    "threat": round(180 * quality, 1),
                    # BPS and cards are ACCUMULATIONS, so they must scale with
                    # minutes as well as quality. "220 * quality" did not: a
                    # 100-minute fringe player carried the same season BPS as a
                    # 900-minute regular, which reads as ~180 BPS per 90 -- about
                    # five times anything real, and enough to make a per-90 rate
                    # meaningless. Per-90 BPS realistically spans ~14 (a squad
                    # filler) to ~38 (an elite defender or midfielder).
                    "bps": round(minutes / 90.0 * (14.0 + 24.0 * quality), 1),
                    "selected_by_percent": str(round(1.0 + 28.0 * quality ** 3, 1)),
                    "transfers_in_event": int(40000 * quality),
                    "transfers_out_event": int(15000 * (1.4 - quality)),
                    # Same reasoning: booking rates run ~0.1-0.35 per 90 and
                    # are highest for defenders and midfielders. randint(0, 3)
                    # regardless of minutes let a 100-minute player show 2.7
                    # yellows per 90, which is not a rate football produces.
                    "yellow_cards": int(round(minutes / 90.0 * (
                        (0.06 + 0.14 * ((j + etype) % 4) / 3.0)
                        * (1.5 if etype in (2, 3) else 0.6)))),
                    "red_cards": 1 if (pid % 47 == 0 and minutes > 300) else 0,
                    "goals_scored": 0,
                    "assists": 0,
                    "points_per_game": str(round(1.5 + 3.0 * quality, 1)),
                })
    return elements


def build_events():
    events = []
    for gw in range(1, GWS + 1):
        events.append({
            "id": gw,
            "name": f"Gameweek {gw}",
            "deadline_time": f"2026-{8 + (gw // 5):02d}-{1 + (gw % 27):02d}T17:30:00Z",
            "finished": gw < CURRENT_GW,
            "is_current": gw == CURRENT_GW - 1,
            "is_next": gw == CURRENT_GW,
        })
    return events


def build_fixtures(teams, rng):
    """Round-robin style schedule with a planted blank and double gameweek.

    GW9 is a blank for the first 8 teams; GW10 is a double for those same teams,
    so the BGW/DGW detectors and the fixture consumers have something to bite on.
    """
    fixtures = []
    fid = 0
    ids = [t["id"] for t in teams]
    latent = {t["id"]: t for t in teams}

    for gw in range(1, GWS + 1):
        rot = ids[1:]
        shift = (gw - 1) % (N_TEAMS - 1)
        rot = rot[shift:] + rot[:shift]
        pairs = [(ids[0], rot[0])]
        for k in range(1, N_TEAMS // 2):
            pairs.append((rot[k], rot[N_TEAMS - 1 - k]))

        for (h, a) in pairs:
            # planted blank gameweek
            if gw == 9 and (h <= 8 or a <= 8):
                continue
            fid += 1
            finished = gw < CURRENT_GW
            hs = as_ = None
            if finished:
                lh = math.exp(0.35 + latent[h]["_att"] - latent[a]["_def"] + 0.25)
                la = math.exp(0.35 + latent[a]["_att"] - latent[h]["_def"])
                hs, as_ = _poisson(lh, rng), _poisson(la, rng)
            fixtures.append({
                "id": fid, "event": gw, "team_h": h, "team_a": a,
                "team_h_score": hs, "team_a_score": as_,
                "finished": finished,
                "kickoff_time": f"2026-{8 + (gw // 5):02d}-{2 + (gw % 26):02d}T14:00:00Z",
            })

        # planted double gameweek: replay the blanked pairings in GW10
        if gw == 10:
            for (h, a) in pairs:
                if h <= 8 or a <= 8:
                    fid += 1
                    fixtures.append({
                        "id": fid, "event": 10, "team_h": a, "team_a": h,
                        "team_h_score": None, "team_a_score": None,
                        "finished": False,
                        "kickoff_time": "2026-10-14T19:00:00Z",
                    })
    return fixtures


def _poisson(lam, rng):
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def main():
    rng = random.Random(SEED)
    teams = build_teams()
    elements = build_players(teams, rng)
    bootstrap = {
        "events": build_events(),
        "teams": [{k: v for k, v in t.items() if not k.startswith("_")} for t in teams],
        "elements": elements,
        "element_types": [
            {"id": 1, "singular_name_short": "GKP"},
            {"id": 2, "singular_name_short": "DEF"},
            {"id": 3, "singular_name_short": "MID"},
            {"id": 4, "singular_name_short": "FWD"},
        ],
    }
    fixtures = build_fixtures(teams, rng)

    with open(os.path.join(OUT_DIR, "bootstrap_synthetic.json"), "w") as fh:
        json.dump(bootstrap, fh, indent=1, sort_keys=True)
    with open(os.path.join(OUT_DIR, "fixtures_synthetic.json"), "w") as fh:
        json.dump(fixtures, fh, indent=1, sort_keys=True)

    print(f"teams={len(teams)} elements={len(elements)} fixtures={len(fixtures)}")
    print(f"finished fixtures={sum(1 for f in fixtures if f['finished'])}")


if __name__ == "__main__":
    main()
