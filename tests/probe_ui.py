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

    # ---- headshot compositing: one image layer, never two -----------------
    # The official PL headshots are transparent-cutout PNGs. Layering a second
    # full image (the silhouette fallback) directly behind one paints that
    # silhouette straight through every transparent pixel of a photo that
    # loaded FINE -- the real photo visibly composited over its own fallback.
    # One layer cannot bleed through itself, so the fix is having only one.
    with_photo = app._headshot_style("12345")
    check("headshot_single_layer",
          with_photo.count("background-image:url(") == 1,
          f"{with_photo!r} -- a second image layer can bleed through a "
          "transparent real photo")
    check("headshot_no_silhouette_layered_behind",
          "Photo-Missing" not in with_photo,
          "the silhouette fallback is layered behind the real photo")

    no_photo = app._headshot_style("")
    check("headshot_missing_code_no_stacked_image",
          "url(" not in no_photo,
          f"{no_photo!r} -- with no code there is nothing to composite under, "
          "so the card surface (not a second image) must be the fallback")

    # ---- headshot tile: the safe replacement for the bare <img> sites ------
    # Transfer Surges and Radar Shortlists used to render <img src=...> with
    # no fallback at all -- a 404 painted the browser's own broken-image [?]
    # icon. _headshot_tile renders a div carrying the same style as
    # _headshot_style, so a missing photo falls back to the .headshot class's
    # own solid surface colour instead, exactly like every other photo site.
    tile = app._headshot_tile("12345")
    check("headshot_tile_is_not_an_img_tag",
          "<img" not in tile,
          f"{tile!r} -- a bare <img> has no CSS fallback for a failed load")
    check("headshot_tile_carries_the_headshot_class",
          'class="headshot"' in tile,
          f"{tile!r} -- must use the .headshot class to get its fallback surface")
    check("headshot_tile_no_photo_falls_back_cleanly",
          "url(" not in app._headshot_tile(""),
          "an empty photo code must not try to load a URL at all")

    # ---- public model label: display only, never the internal slug --------
    label = app._public_model_label("v7-minutes-recency")
    check("public_label_hides_the_descriptive_slug",
          "minutes-recency" not in label and "recency" not in label,
          f"{label!r} -- the internal changelog slug is still visible publicly")
    check("public_label_keeps_the_build_number",
          "7" in label,
          f"{label!r} -- lost the number the self-correction copy points at")
    check("public_label_handles_a_malformed_version",
          app._public_model_label("") and app._public_model_label(None),
          "an unversioned string crashed the label rather than degrading")
    check("model_version_itself_is_unchanged",
          app.fpl_tools.MODEL_VERSION.startswith("v") and "-" in app.fpl_tools.MODEL_VERSION,
          "the internal constant lost its descriptive-slug shape -- auto_tune "
          "and the version-boundary tests key off exactly that shape")

    # ---- rolling transfer horizon Gantt: figure built from real data -------
    gantt_squad = [{"name": "Held Player", "position": "MID"},
                   {"name": "Sold Player", "position": "DEF"}]
    gantt_schedule = [
        {"gw": 10, "buys": [], "sells": [], "chip": None, "hits": 0},
        {"gw": 11, "buys": ["Bought Player"], "sells": ["Sold Player"], "chip": None, "hits": 0},
        {"gw": 12, "buys": [], "sells": [], "chip": "Wildcard", "hits": 0},
    ]
    gantt = app.fpl_tools.build_transfer_gantt_data(
        gantt_squad, gantt_schedule,
        xp_lookup={"Held Player": 6.0, "Sold Player": 2.0, "Bought Player": 8.0})
    fig = app._transfer_gantt_figure(gantt)
    check("gantt_figure_is_a_plotly_figure",
          isinstance(fig, app.go.Figure),
          f"{type(fig)!r} -- _transfer_gantt_figure must return a go.Figure")
    bar_traces = [t for t in fig.data if t.type == "bar"]
    check("gantt_figure_has_one_bar_trace_per_tenure_segment",
          len(bar_traces) == len(gantt["bars"]),
          f"{len(bar_traces)} bar traces for {len(gantt['bars'])} segments")
    scatter_traces = [t for t in fig.data if t.type == "scatter"]
    check("gantt_figure_marks_the_captain",
          any(t.name == "Captain" for t in scatter_traces),
          "no Captain marker trace found")
    check("gantt_figure_xaxis_spans_the_horizon",
          fig.layout.xaxis.range == (gantt["start_gw"] - 0.5, gantt["end_gw"] + 0.5),
          f"{fig.layout.xaxis.range!r} vs expected horizon "
          f"({gantt['start_gw']}, {gantt['end_gw']})")

    empty_fig = app._transfer_gantt_figure({"start_gw": None, "end_gw": None,
                                            "bars": [], "chip_events": [], "captains": {}})
    check("gantt_figure_handles_an_empty_schedule_without_raising",
          isinstance(empty_fig, app.go.Figure))

    # ---- fixture-run track: circular FDR badge + opponent/venue + xP ------
    fixture_run = [
        {"gw": 5, "fdr": 2, "opponent": "EVE", "venue": "H", "xp": 3.9,
         "is_double": False, "is_blank": False},
        {"gw": 6, "fdr": 3, "opponent": "AVL", "venue": "H", "xp": 2.1,
         "is_double": False, "is_blank": False},
        {"gw": 7, "fdr": 5, "opponent": "MUN", "venue": "A", "xp": 1.4,
         "is_double": True, "is_blank": False},
        {"gw": 8, "fdr": None, "opponent": None, "venue": None, "xp": 0.0,
         "is_double": False, "is_blank": True},
    ]
    fx_html = app._fixture_run_html(fixture_run)
    check("fixture_run_html_four_cells",
          fx_html.count('class="fx-run-cell"') == 4, fx_html)
    check("fixture_run_html_favourable_fixture_is_green",
          'background:var(--pos);">2<' in fx_html, fx_html)
    check("fixture_run_html_moderate_fixture_is_amber",
          'background:var(--warn);">3<' in fx_html, fx_html)
    check("fixture_run_html_difficult_fixture_is_red",
          'background:var(--neg);">5<' in fx_html, fx_html)
    check("fixture_run_html_shows_opponent_and_venue",
          "EVE (H)" in fx_html and "MUN (A)" in fx_html, fx_html)
    check("fixture_run_html_shows_per_fixture_xp",
          "3.9 xP" in fx_html, fx_html)
    check("fixture_run_html_blank_gameweek_has_no_fdr_or_opponent",
          "Blank" in fx_html, fx_html)

    # ---- fixture-run wired end-to-end through _transfer_pair_html ---------
    # Real team ids from the synthetic fixture, not fabricated ones, so
    # _player_fixture_run actually finds fixtures to draw a track from.
    ctx = app._bootstrap_ctx()
    any_team_id = next(iter(ctx["teams_by_id"]))
    move_out = {"id": 901, "team_id": any_team_id, "team": "T1",
               "position": "MID", "price": 6.0}
    move_in = {"id": 902, "team_id": any_team_id, "team": "T1",
              "position": "MID", "price": 6.5}
    ctx["players_by_id"].setdefault(901, {"id": 901, "team": any_team_id,
                                          "element_type": 3, "minutes": 900,
                                          "starts": 10, "status": "a"})
    ctx["players_by_id"].setdefault(902, {"id": 902, "team": any_team_id,
                                          "element_type": 3, "minutes": 900,
                                          "starts": 10, "status": "a"})
    pair_html = app._transfer_pair_html(
        [{"out": move_out, "in": move_in, "xp_gain": 1.2, "cost": 0.5}])
    fx_run_count = pair_html.count('class="fx-run"')
    check("transfer_pair_html_renders_a_fixture_track_for_each_side",
          fx_run_count == 2,
          f"expected 2 fixture tracks (out + in), found {fx_run_count}")

    # ---- waterfall reconciliation: no incommensurate "total squad" row ----
    # The removed "projected_points" row carried the WHOLE squad's projected
    # points over the horizon (order ~100+ over a 4-GW window), while every
    # other row and "net" are single-digit move deltas. Comparing the two
    # meant "shown" always swamped "net" and the >20% residual warning fired
    # on essentially every recommended transfer, whatever the real components
    # reconciled to.
    check("waterfall_rows_drop_projected_points",
          "projected_points" not in dict(app.WATERFALL_ROWS),
          str(app.WATERFALL_ROWS))

    tracked_breakdown = {
        "points_hit": -4.0,
        "transfer_bar": -0.8,
        "banked_transfer_value": 0.65,
        "chip_cost": 0.0,
        "cash_optionality": 0.05,
    }
    tracked_breakdown["net"] = round(sum(tracked_breakdown.values()), 2)
    clean_html = app._waterfall_html(tracked_breakdown)
    check("waterfall_projected_points_label_absent",
          "Projected points" not in clean_html, clean_html)
    check("waterfall_tracked_terms_reconcile_without_warning",
          "wf-warn" not in clean_html,
          f"tracked terms summed to net cleanly but still fired the "
          f"residual warning: {clean_html!r}")

    # ---- pitch-view fixture dots: real circular elements, not emoji ------
    dots_html = app._pitch_fixture_dots_html(any_team_id)
    check("pitch_fixture_dots_renders_four_real_dots",
          dots_html.count('class="fx-dot ') == 4, dots_html)
    import re as _re
    band_classes = _re.findall(r'fx-dot fx-dot-(\w+)', dots_html)
    check("pitch_fixture_dots_uses_known_band_classes",
          len(band_classes) == 4 and
          all(b in ("green", "amber", "red", "double", "blank") for b in band_classes),
          f"{band_classes!r} from {dots_html!r}")
    check("pitch_fixture_dots_no_emoji_leaks_through",
          not any(e in dots_html for e in ("🟢", "🟡", "🔴", "🔵", "⚪")),
          dots_html)
    check("pitch_fixture_dots_handles_missing_team_id",
          app._pitch_fixture_dots_html(None) == '<div class="fx-dots"></div>')

    # ---- club-crest fallback: styled div, initials when no code mapped ----
    crest_html = app._badge_img(any_team_id)
    check("badge_img_is_not_a_bare_img_tag", "<img" not in crest_html, crest_html)
    check("badge_img_carries_the_crest_class", "badge-crest" in crest_html, crest_html)

    fake_team_id = "no-such-team-id"
    fallback_html = app._badge_img(fake_team_id)
    check("badge_img_falls_back_to_something_for_an_unmapped_team",
          fallback_html != "", "an unmapped team must not render nothing")
    check("badge_img_fallback_is_still_the_crest_class",
          "badge-crest" in fallback_html, fallback_html)

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall probes passed")


if __name__ == "__main__":
    main()
