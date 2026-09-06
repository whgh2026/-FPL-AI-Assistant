"""Load app.py against the fixture, then exercise its helpers directly.

Separate from render_app.py on purpose. That harness answers "does the page
execute"; this one answers "do the helpers compute the right numbers". Both have
to run app.py at module scope -- which mutates fpl_tools' network accessors and
Streamlit's global state -- so both run as subprocesses, and neither is imported
by a test process that other tests share.

Prints one PASS/FAIL line per check and exits non-zero if any failed.
"""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import render_app

FAILURES = []


def check(name, condition, detail=""):
    ok = bool(condition)
    if not ok:
        FAILURES.append(f"{name}: {detail}")
    print(f"{'PASS' if ok else 'FAIL'} {name}{(' -- ' + detail) if detail and not ok else ''}")


def load_app():
    render_app._install_fixture()
    spec = importlib.util.spec_from_file_location("fplapp_probe", "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    app = load_app()

    # ---- set-piece duty: only first choice counts -------------------------
    check("set_piece_first_choice_only",
          app._set_piece_note({"penalties_order": 1,
                               "direct_freekicks_order": 2,
                               "corners_and_indirect_freekicks_order": None}) == "pens",
          repr(app._set_piece_note({"penalties_order": 1, "direct_freekicks_order": 2})))
    check("set_piece_multiple_duties",
          app._set_piece_note({"penalties_order": 1,
                               "corners_and_indirect_freekicks_order": 1}) == "pens, corners")
    check("set_piece_none", app._set_piece_note({}) == "")
    check("set_piece_missing_element", app._set_piece_note(None) == "")

    # ---- scorecard arithmetic ---------------------------------------------
    # A nailed starter, a rotation risk, and a bench of known cost. The
    # scorecard must separate "what it scores" from "how it is built".
    #
    # Both carry a real team id, and that matters: _minute_distribution derives
    # games from the team's fixtures played, so a player with no team falls back
    # to games = max(starts, 1) and one start in one game reads as nailed on. The
    # fixture has three matches played, which is what makes 1 start a rotation
    # risk and 3 starts a regular.
    team_id = 1
    nailed = {"id": 901, "team": team_id, "minutes": 270, "starts": 3,
              "status": "a", "element_type": 3,
              "chance_of_playing_next_round": None, "penalties_order": 1}
    risk = {"id": 902, "team": team_id, "minutes": 120, "starts": 1,
            "status": "a", "element_type": 3,
            "chance_of_playing_next_round": None}
    pool = {901: nailed, 902: risk}
    starters = [{"player_id": 901, "xp": 6.0, "price": 9.0, "web_name": "Nailed"},
                {"player_id": 902, "xp": 2.0, "price": 5.5, "web_name": "Risky"}]
    bench = [{"player_id": 903, "xp": 0.5, "price": 4.0},
             {"player_id": 904, "xp": 0.5, "price": 4.5}]

    card = app._squad_scorecard(starters, bench, pool)
    check("scorecard_xi_points", abs(card["xi_points"] - 8.0) < 1e-6, str(card["xi_points"]))
    check("scorecard_bench_cost", abs(card["bench_cost"] - 8.5) < 1e-6, str(card["bench_cost"]))
    check("scorecard_of", card["of"] == 2, str(card["of"]))
    check("scorecard_nailed_excludes_rotation_risk",
          card["nailed"] == 1,
          f"expected 1 of 2 nailed, got {card['nailed']}")
    check("scorecard_takers_found", len(card["takers"]) == 1, str(card["takers"]))
    check("scorecard_taker_duty", card["takers"] and card["takers"][0][1] == "pens")

    # An unknown player id must not crash or silently count as nailed.
    orphan = app._squad_scorecard([{"player_id": 999, "xp": 1.0, "price": 5.0}], [], {})
    check("scorecard_unknown_player_not_nailed", orphan["nailed"] == 0, str(orphan))

    # ---- scorecard rendering ----------------------------------------------
    html = app._scorecard_html(card, delta_xi=1.4)
    for label in ("Starting XI points", "Money on the bench",
                  "Who's nailed on", "Set-piece takers"):
        check(f"scorecard_label_{label.split()[0].lower()}", label in html)
    check("scorecard_delta_shown", "+1.4 vs your original XI" in html)
    check("scorecard_no_jargon",
          not any(j in html for j in ("xP", "P50", "Bench Cost", "Minutes Certainty")),
          "a technical label survived into the scorecard")

    empty = app._scorecard_html(app._squad_scorecard([], [], {}))
    check("scorecard_empty_squad_renders", "Set-piece takers" in empty)
    check("scorecard_no_takers_copy", "Nobody in your XI is on dead balls." in empty)

    # ---- bench-cost verdict bands -----------------------------------------
    def verdict(cost):
        c = dict(card, bench_cost=cost)
        h = app._scorecard_html(c)
        for word in ("sensible", "a bit rich", "dead money"):
            if word in h:
                return word
        return "?"

    check("bench_band_ok", verdict(15.0) == "sensible", verdict(15.0))
    check("bench_band_boundary", verdict(16.0) == "sensible", verdict(16.0))
    check("bench_band_rich", verdict(18.0) == "a bit rich", verdict(18.0))
    check("bench_band_dead", verdict(22.0) == "dead money", verdict(22.0))

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall probes passed")


if __name__ == "__main__":
    main()
