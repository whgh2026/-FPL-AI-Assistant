"""Stage 7 gate: the page renders, and fails visibly.

app.py is a single ~2400-line Streamlit script whose entire body executes on
every rerun, so the only way to know it still works is to run it. These tests
drive tests/render_app.py in a subprocess -- module-level execution has to be
isolated, since it mutates fpl_tools' network accessors and Streamlit's global
state.

The second test is the one that matters. Before Stage 7, an FPL outage produced
a complete-looking page built on nothing: _build_fixture_lookup returned {} on
error, which makes every player project 0.0 as a "Blank", so a squad read as
fifteen worthless assets and the engine recommended selling all of them. A total
outage rendered as confident advice.
"""

import os
import re
import subprocess
import sys
import unittest

import fpl_tools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tests", "render_app.py")
PROBE = os.path.join(ROOT, "tests", "probe_ui.py")
COLD_RENDER_BUDGET_S = 3.0


def _run(*args):
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )


def _app_source():
    return open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()


def _field(stdout, key):
    for line in stdout.splitlines():
        if line.startswith(key):
            return line.split(":", 1)[1].strip()
    return ""


class RenderTest(unittest.TestCase):
    def test_page_renders_against_the_fixture(self):
        r = _run()
        self.assertEqual(r.returncode, 0, f"render failed:\n{r.stdout}\n{r.stderr[-2000:]}")
        self.assertEqual(_field(r.stdout, "status"), "ok")

    def test_cold_render_is_within_budget(self):
        r = _run()
        wall = float(_field(r.stdout, "wall time").rstrip("s"))
        self.assertLess(
            wall, COLD_RENDER_BUDGET_S,
            f"cold render took {wall:.2f}s against a {COLD_RENDER_BUDGET_S}s budget")

    def test_api_outage_degrades_visibly_and_does_not_crash(self):
        """Two failure modes, both unacceptable: a Streamlit traceback, or a
        confident page built on zeros. Neither may happen."""
        r = _run("--fail-api")
        self.assertEqual(
            r.returncode, 0,
            f"outage path failed or showed no error:\n{r.stdout}\n{r.stderr[-2000:]}")
        self.assertEqual(_field(r.stdout, "status"), "ok",
                         "the page crashed instead of degrading")
        self.assertIn("error", r.stdout,
                      "the page rendered with no visible error banner")


class ResilienceTest(unittest.TestCase):
    def test_fixture_loader_raises_rather_than_returning_empty(self):
        """{} is indistinguishable from 'nobody has a fixture', which the
        projection reads as a squad of worthless players."""
        import fpl_tools
        saved = fpl_tools._get_fixtures
        fpl_tools._get_fixtures = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            with self.assertRaises(fpl_tools.FixtureDataUnavailable):
                fpl_tools._build_fixture_lookup({"teams": [], "elements": []})
        finally:
            fpl_tools._get_fixtures = saved

    def test_headshots_make_no_network_call(self):
        """Was a blocking requests.head PER PLAYER: 15 on the pitch, 22 on the
        radar, 2 per transfer row, up to 2s each on a cold cache."""
        import inspect
        src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
        self.assertNotIn("requests.head(", src,
                         "a blocking HEAD request is back in the render path")

    def test_caveat_never_raises(self):
        """It called get_api_timestamp() unguarded, so an outage took the whole
        page down from a footnote."""
        src = _app_source()
        start = src.index("def get_caveat_html()")
        body = src[start:start + 1200]
        self.assertIn("except Exception", body)


class InformationArchitectureTest(unittest.TestCase):
    """The tabs are the top-level map of the app, so they carry the whole
    'disjointed and messy' complaint. Names have to say what is behind them."""

    def test_tabs_are_named_for_the_reader(self):
        src = _app_source()
        for label in ("🏟️ My Plan", "🗺️ Transfer Roadmap", "🗓️ Fixtures",
                      "📡 Players", "🩺 Model Health"):
            self.assertIn(label, src, f"tab {label!r} is missing")

    def test_old_module_shaped_tab_names_are_gone(self):
        """'Insights Lab' and 'Player Radar & Market' named the code, not the
        content -- neither told you which one held the fixture ticker."""
        src = _app_source()
        for dead in ("Quant Auto Transfer Planner", "🔬 Insights Lab",
                     "📡 Player Radar & Market"):
            self.assertNotIn(dead, src, f"{dead!r} survived the IA pass")

    def test_model_health_claim_has_somewhere_to_land(self):
        """The hero says you can check the model's homework. Before Stage 7d
        there was no page to check it on, which made the claim an overclaim of
        exactly the kind the copy pass exists to remove."""
        src = _app_source()
        self.assertIn("with tab_health:", src)
        self.assertIn("prediction_accuracy_by_gw", src)
        self.assertIn("<b>Model Health</b>", src,
                      "the hero no longer points the reader at the tab")

    def test_scorecard_is_rendered_with_the_final_squad(self):
        src = _app_source()
        self.assertIn("_scorecard_html(", src)
        self.assertIn("How your squad stacks up", src)

    def test_reasoning_renders_before_the_decision_buttons(self):
        """Streamlit renders in source order, so this ordering IS the layout.
        Accept/Hold used to come first, which asked the reader to commit their
        gameweek and only then let them scroll down to why."""
        src = _app_source()
        reasoning = src.index('"💡 Why we\'re telling you to do this"')
        accept = src.index('key="btn_accept_all"')
        hold = src.index('key="btn_fast_hold"')
        self.assertLess(reasoning, accept, "Accept renders above the reasoning")
        self.assertLess(reasoning, hold, "Hold renders above the reasoning")
        for panel in ('"🩺 Is your squad set up right?"', '"📊 Where you\'re exposed"'):
            self.assertLess(src.index(panel), accept,
                            f"{panel} renders below the decision")

    def test_transfer_reasoning_is_in_plain_english(self):
        """The panel explaining a transfer is exactly where jargon does the most
        damage: it is the one place a reader goes when they don't already
        understand the recommendation."""
        src = _app_source()
        start = src.index('"💡 Why we\'re telling you to do this"')
        block = src[start:src.index('key="btn_accept_all"')]
        # Comment lines are for whoever maintains this, not for the reader --
        # "# 4. Late fitness gating" is a fine label for a branch and a terrible
        # one for a person deciding their gameweek.
        panel = "\n".join(ln for ln in block.splitlines()
                          if not ln.lstrip().startswith("#"))
        for jargon in ("Expected Value Maximisation", "Hit amortisation",
                       "Transfer Conservation", "Goalkeeper structuring",
                       "Late fitness gating", "option value", "positional baseline"):
            self.assertNotIn(jargon, panel, f"{jargon!r} survived the copy pass")

    def test_helpers_compute_the_right_numbers(self):
        """Delegated to a subprocess: app.py has to be executed at module scope
        to get at its helpers, and that mutates fpl_tools and Streamlit globals."""
        r = subprocess.run([sys.executable, PROBE], cwd=ROOT,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0,
                         f"UI probes failed:\n{r.stdout}\n{r.stderr[-2000:]}")
        self.assertIn("all probes passed", r.stdout)


class PerformanceTest(unittest.TestCase):
    """C33/C34. Nothing here changes an answer; they change how many times the
    app pays to compute the same one."""

    def test_fixture_lookup_is_memoised(self):
        """Called from 50+ sites, and each call re-walked and re-sorted every
        fixture, refetched the odds and re-read the ratings."""
        import fpl_tools
        from tests import harness
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            first = fpl_tools._build_fixture_lookup(bootstrap)
            self.assertIs(fpl_tools._build_fixture_lookup(bootstrap), first,
                          "the lookup is rebuilt on every call")

    def test_clearing_the_ratings_also_clears_the_lookup(self):
        """The lookup embeds the ratings, so a stale lookup would serve the old
        fit through a different door."""
        import fpl_tools
        from tests import harness
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            first = fpl_tools._build_fixture_lookup(bootstrap)
            fpl_tools._clear_rating_caches()
            self.assertIsNot(fpl_tools._build_fixture_lookup(bootstrap), first)

    def test_manager_endpoints_go_through_the_cache(self):
        """score_my_squad walked back up to 38 gameweeks looking for picks, one
        uncached request each -- and that slow path was the COMMON one, since it
        only triggers for a manager who has not set a team yet. get_free_transfers
        made three uncached calls per invocation, on every rerun."""
        src = open(os.path.join(ROOT, "fpl_tools.py"), encoding="utf-8").read()
        for fn in ("def score_my_squad", "def get_free_transfers"):
            start = src.index(fn)
            body = src[start:start + 2600]
            self.assertNotIn("requests.get(f\"{BASE_URL}/entry/", body,
                             f"{fn} still makes an uncached manager request")

    def test_the_picks_walk_is_bounded_by_the_managers_first_season(self):
        src = open(os.path.join(ROOT, "fpl_tools.py"), encoding="utf-8").read()
        start = src.index("def score_my_squad")
        body = src[start:start + 2600]
        self.assertIn("floor_gw", body,
                      "the picks walk still runs to gameweek 1 unconditionally")


class StructuralHealthCopyTest(unittest.TestCase):
    """Part E. The expander header was renamed in the copy pass but the four
    labels inside it were not, so the panel opened onto the jargon it was
    supposed to have replaced."""

    def test_labels_are_in_plain_english(self):
        """Scans only the strings the READER sees. The docstring and the
        numbered comments still use the internal names, which is right: they
        describe the branch to whoever maintains it."""
        src = open(os.path.join(ROOT, "fpl_tools.py"), encoding="utf-8").read()
        start = src.index("def _squad_structural_health")
        body = src[start:src.index("return checks", start)]
        shown = "\n".join(
            ln for ln in body.splitlines()
            if not ln.lstrip().startswith("#")
            and ('"label"' in ln or '"detail"' in ln or ln.lstrip().startswith(
                ('f"', '"', "f'", "'", "+ ", "if ", "else")))
        )
        for jargon in ("Stranded bench capital", "Enabler efficiency",
                       "Formation optionality", "Price-point pivot liquidity",
                       "Deadlock:"):
            self.assertNotIn(jargon, shown, f"{jargon!r} is still shown to the reader")

    def test_checks_carry_a_stable_key_for_lookup(self):
        """Copy must never be an identifier. Matching on the label is what let a
        wording change break unrelated logic."""
        import fpl_tools
        from tests import harness
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            squad = harness.build_squad(bootstrap, "balanced")
            checks = fpl_tools._squad_structural_health(
                harness.squad_as_manager_input(bootstrap, squad), 1.0)
            keys = {c.get("key") for c in checks}
            self.assertEqual(
                keys, {"bench_capital", "enabler_efficiency",
                       "formation_optionality", "pivot_liquidity"})
            for c in checks:
                self.assertTrue(c.get("label"), "a check has no display label")


class MethodologyLeakTest(unittest.TestCase):
    """No user-visible copy should name the underlying statistical technique --
    not because it's wrong to say, but because the deployed app is the one
    fully public surface (the repo itself is confirmed private), and naming
    the exact method for free is a bigger giveaway than any UI wording."""

    def test_no_named_technique_survives_in_visible_copy(self):
        src = _app_source()
        for jargon in ("Poisson", "DEFCON", "Dixon-Coles", "Dixon–Coles"):
            for line in src.splitlines():
                stripped = line.lstrip()
                if jargon in line and not (stripped.startswith("#") or '"""' in line
                                           or stripped.startswith("'''")):
                    self.fail(f"{jargon!r} in live copy: {line.strip()!r}")

    def test_the_engine_description_states_what_it_delivers(self):
        """Removing jargon must not leave a content-free sentence -- the
        replacement still has to say what the model actually does."""
        src = _app_source()
        start = src.index('"quant": (')
        desc = src[start:src.index(")", start)]
        for must_have in ("forecasts points", "fixture difficulty",
                          "checks itself against real results"):
            self.assertIn(must_have, desc, f"{must_have!r} missing from the engine description")

    def test_odds_pending_line_matches_its_own_sibling(self):
        """Same metric, two branches (odds live vs pending) -- the pending
        branch used to tack '(Dixon-Coles)' onto a label the live branch
        states plainly. Both must now read the same way."""
        src = _app_source()
        self.assertIn('f"Market Odds Pending · Opp. defence: {opp_def:.1f}/5"', src)


class RankExposureCopyTest(unittest.TestCase):
    """'Where you're exposed' is the one place EO turns into a signed number a
    manager has to interpret unaided -- Short and Inverted mean opposite things
    and look similar on the page.

    A first pass added a two-paragraph glossary above the list explaining each
    flag in full. That duplicated what each row's own subtitle already says,
    which is the wrong place to carry the detail twice: the intro should orient
    the reader in one line, and each row should be self-explanatory on its own
    without needing the caption re-opened above it.
    """

    def test_intro_is_one_concise_sentence(self):
        """Not a glossary -- one sentence pointing at the rows, which carry the
        specifics. A caption that re-grew into paragraphs would be the same
        duplication this pass removed, just re-added."""
        src = _app_source()
        start = src.index('"📊 Where you\'re exposed"')
        nearby = src[start:start + 500]
        self.assertIn("unowned or non-captained players heavily backed", nearby)
        self.assertNotIn("**Inverted**", nearby,
                         "the per-term glossary is back in the intro")
        self.assertNotIn("**Short**", nearby,
                         "the per-term glossary is back in the intro")
        self.assertNotIn("\\n\\n", nearby, "the intro is back to multiple paragraphs")

    def test_short_row_detail_carries_its_own_inline_explainer(self):
        """Mirrors the Inverted row's existing '(rank drops when they score)'
        -- each row should be self-explanatory without opening the caption
        above it."""
        src = _app_source()
        start = src.index('"🔻 Short"')
        row = src[start:start + 300]
        self.assertIn("rival ownership", row)
        self.assertIn("your rank drops by", row)
        self.assertIn("pts per point scored", row)

    def test_short_sign_convention_matches_the_stated_example(self):
        """-0.89 pts/point at 89% EO, not +0.89 -- the explainer's own example
        has to match what _rank_exposure actually returns for an unowned
        player, or the copy would be teaching the wrong sign."""
        import fpl_tools
        per_pt = fpl_tools._rank_exposure(0, 89.0, 1.0)
        self.assertAlmostEqual(per_pt, -0.89, places=6)


class ImageFallbackTest(unittest.TestCase):
    """Bare <img src=...> tags have no CSS-only fallback for a failed load --
    Streamlit strips onerror under unsafe_allow_html, so a 404 painted the
    browser's own broken-image [?] icon. Transfer Surges and Radar Shortlists
    were the two sites still using one; Desktop Pitch and the Player Modal
    already used the safe div-background pattern via _headshot_img."""

    def test_no_bare_img_headshot_tags_remain(self):
        src = _app_source()
        self.assertNotIn('<img class="headshot"', src,
                         "a bare <img> headshot survived -- it has no CSS "
                         "fallback and paints the browser's broken-image icon")

    def test_surges_and_radar_use_the_safe_tile_helper(self):
        src = _app_source()
        self.assertIn("def _headshot_tile(", src)
        # Both former bare-<img> sites now route through the same helper.
        self.assertEqual(src.count('{_headshot_tile(r["photo"])}'), 2,
                         "expected both Transfer Surges and Radar Shortlists "
                         "to use _headshot_tile")

    def test_dead_network_url_helper_is_gone(self):
        """_headshot_url only ever fed the two unsafe <img> sites -- once both
        are gone, keeping it around invites a future call site to reintroduce
        the same bug."""
        src = _app_source()
        self.assertNotIn("def _headshot_url(", src)

    def test_mobile_pitch_hides_headshots_entirely(self):
        """Confirms mobile pitch view stays compact with no headshots, rather
        than trying (and failing) to cram photos into a narrow layout."""
        css = open(os.path.join(ROOT, "static", "app.css"), encoding="utf-8").read()
        m = re.search(r"@media \(max-width: 768px\) \{.*?\n\s*\}", css, re.DOTALL)
        self.assertIsNotNone(m, "mobile media query not found")
        self.assertIn(".pitch-player .photo-frame", m.group(0))
        self.assertIn("display: none", m.group(0))


class StrategyChipScenarioCardsTest(unittest.TestCase):
    """Strategy Mode used to live in the sidebar, then gained a synced mobile
    duplicate inside Step 3 (Streamlit forbids two widgets sharing one key, so
    that took a pair of on_change callbacks to keep them consistent). Both
    were replaced by a single design: one canonical control, living in Step
    3's own "Strategy & Risk Mode" card, side by side with "Chip Scenario
    Lab" -- nothing left to duplicate or drift out of sync with itself."""

    def test_sidebar_no_longer_offers_a_strategy_selector(self):
        src = _app_source()
        sidebar = src.index("with st.sidebar:")
        how_it_thinks = src.index('"🧠 How it thinks"', sidebar)
        sidebar_body = src[sidebar:how_it_thinks]
        self.assertNotIn("How should we play it", sidebar_body)
        self.assertNotIn('key="risk"', sidebar_body)
        self.assertNotIn('key="rival_id_input"', sidebar_body,
                         "the rival-shadow input is strategy-dependent -- it "
                         "should have moved to Step 3 with the strategy radio, "
                         "not been left behind orphaned in the sidebar")

    def test_rival_shadow_input_moved_with_the_strategy_radio(self):
        """Deleting the sidebar's strategy radio without relocating its
        dependent rival-ID input would silently disable rival-shadowing for
        Conservative/Shield forever -- rival_id_input would never be set."""
        src = _app_source()
        step3 = src.index("Step 3: Transfer Planner")
        container = src.index("with st.container(border=True):", step3)
        risk_widget = src.index('key="risk"', container)
        rival_input = src.index('key="rival_id_input"', container)
        self.assertGreater(rival_input, risk_widget,
                           "rival_id_input should render just after the "
                           "strategy radio in Step 3's left column")

    def test_only_one_widget_owns_the_risk_key(self):
        """Streamlit raises at runtime if two widgets share a key -- this is
        the regression test for that, and the whole point of 'one canonical
        control' rather than a synced pair."""
        src = _app_source()
        self.assertEqual(src.count('key="risk"'), 1,
                         "more than one widget claims key=\"risk\" -- Streamlit "
                         "will crash with a duplicate-key error at runtime")
        for dead in ("risk_inline", "_sync_risk_to_inline", "_sync_risk_from_inline"):
            self.assertNotIn(dead, src, f"{dead!r} survived the consolidation to one control")

    def test_strategy_and_chip_are_twin_columns_in_step_3(self):
        src = _app_source()
        step3 = src.index("Step 3: Transfer Planner")
        container = src.index("with st.container(border=True):", step3)
        columns_call = src.index("st.columns([1, 1])", container)
        # The literal header markdown, not just the substring -- a comment
        # naming "Strategy & Risk Mode" sits above the columns() call itself.
        strategy_header = src.index('"##### 🎯 Strategy & Risk Mode"', container)
        risk_widget = src.index('key="risk"', container)
        chip_widget = src.index('key="confirmed_chip_radio"', container)
        # The columns container has to exist before either card's content,
        # and the strategy card's own widget before the chip card's.
        self.assertLess(columns_call, strategy_header)
        self.assertLess(columns_call, risk_widget)
        self.assertLess(risk_widget, chip_widget)

    def test_strategy_card_shows_every_mode_description_plus_the_selected_one(self):
        """Two distinct requirements: the full glossary (one description per
        option, via Streamlit's own per-option captions) AND a restatement of
        specifically the selected mode directly under the picker."""
        src = _app_source()
        self.assertIn("RISK_DESCRIPTIONS", src)
        self.assertIn("captions=RISK_DESCRIPTIONS", src)
        expected_descriptions = [
            "Chase the most points. No thumb on the scale.",
            "Play it safe. Own what your rivals own, and only move for a clear upgrade.",
            "Go hunting. Back differentials and take a hit for a big enough gain.",
            "Protect a lead. Mirror the players your rivals own so their good weeks can't hurt you.",
            "Close a gap. Target players almost nobody else has.",
        ]
        for desc in expected_descriptions:
            self.assertIn(desc, src, f"mode description {desc!r} missing")
        start = src.index('key="risk"')
        nearby = src[start:start + 400]
        self.assertIn("st.info(f\"**{risk_label}:**", nearby,
                      "no restatement of the selected mode's own description "
                      "directly under the picker")

    def test_both_widgets_invalidate_the_transfer_cache_on_change(self):
        """Changing Strategy Mode or the Chip Scenario must never leave the
        cards, pitch or projected xP showing a plan computed for the old
        setting."""
        src = _app_source()
        risk_start = src.index('key="risk"')
        risk_block = src[risk_start:risk_start + 300]
        self.assertIn("on_change=clear_transfer_cache", risk_block)
        chip_start = src.index('key="confirmed_chip_radio"')
        chip_block = src[chip_start:chip_start + 200]
        self.assertIn("on_change=clear_transfer_cache", chip_block)


class ChipCopyTest(unittest.TestCase):
    """The old copy ('Confirm Active Chip' / 'Select which chip you will
    actively play this Gameweek (Only 1 allowed):') was administrative-sounding
    and didn't say what confirming a chip actually does to the plan above it."""

    def test_old_chip_copy_is_gone(self):
        src = _app_source()
        for dead in ("Confirm Active Chip",
                     "Select which chip you will actively play this Gameweek"):
            self.assertNotIn(dead, src, f"{dead!r} survived the chip copy pass")

    def test_new_chip_header_and_caption_present(self):
        # Adjacent string literals in app.py -- checked separately since the
        # raw source still has quotes/indentation between them.
        src = _app_source()
        self.assertIn("Chip Scenario Lab (Set 1", src)
        self.assertIn("Simulate playing a chip this week to see how your lineup and", src)
        self.assertIn("projected points shift. Leave blank for standard rolling", src)
        self.assertIn("transfer strategy.", src)


class SectionCaptionTest(unittest.TestCase):
    """Plain-English captions directly underneath the major Step 3 card
    titles, so a reader doesn't need to already know what each card is doing
    to make sense of the number inside it."""

    def test_how_it_could_go_caption(self):
        src = _app_source()
        start = src.index('"🎲 How it could go"')
        block = src[max(0, start - 500):start]
        self.assertIn("realistic ceiling", block)
        self.assertIn("expected baseline", block)
        # 500, not a round headline number that overstates it -- SAA_SCENARIOS
        # is the actual number of Monte Carlo draws the solver runs.
        self.assertIn("500 gameweek outcomes", block)
        self.assertIn(f"{fpl_tools.SAA_SCENARIOS}", block,
                      "the caption's simulation count must track SAA_SCENARIOS, "
                      "not a hardcoded figure that can drift from the real one")

    def test_chip_scenario_lab_caption_still_present(self):
        """The chip card's own copy pass (a separate round) already covers
        this -- pinned here too since it's now also the literal home for the
        'Scenario Comparison' framing this class exists to lock down."""
        src = _app_source()
        start = src.index('"🎟️ Active Chip Analysis & Recommendations"')
        block = src[max(0, start - 500):start]
        self.assertIn("Compares competing strategy paths", block)
        self.assertIn("highest net expected points", block)

    def test_rolling_transfer_plan_caption(self):
        src = _app_source()
        start = src.index('"🗓️ The next few weeks"')
        block = src[max(0, start - 400):start]
        self.assertIn("mathematically optimal gameweek-by-gameweek transfer", block)
        self.assertIn("bank free transfers", block)
        self.assertIn("carries forward", block)

    def test_bookies_signal_description_is_plain_english(self):
        """Step 1's Gaffer's Positional Diagnostic legend, 'Bookies' column --
        must explain de-vigging in plain terms rather than assume the reader
        already knows what an overround or a devigged price is."""
        src = _app_source()
        start = src.index('"Bookies", "')
        block = src[start:start + 400]
        self.assertIn("built-in profit margin", block)
        self.assertIn("true, unbiased probabilities", block)
        self.assertNotIn("expected goals, assists, and clean sheet probabilities.\"", block,
                         "the old, jargon-light-but-imprecise copy survived alongside it")


class TransferRoadmapTabTest(unittest.TestCase):
    """The Gantt timeline moved out of Step 3 into its own top-level tab --
    elevated, not duplicated: it must exist exactly once, not survive in both
    places."""

    def test_roadmap_tab_exists_right_after_my_plan(self):
        src = _app_source()
        tabs_call = src.index("st.tabs(")
        my_plan = src.index('"🏟️ My Plan"', tabs_call)
        roadmap = src.index('"🗺️ Transfer Roadmap"', tabs_call)
        fixtures = src.index('"🗓️ Fixtures"', tabs_call)
        self.assertLess(my_plan, roadmap, "Transfer Roadmap must come after My Plan")
        self.assertLess(roadmap, fixtures, "Transfer Roadmap must come before Fixtures")

    def test_gantt_chart_no_longer_renders_inside_step_3(self):
        """It used to render nested inside Step 3's 'next few weeks' card --
        that was the whole complaint this tab exists to fix."""
        src = _app_source()
        step3 = src.index("Step 3: Transfer Planner")
        tab_roadmap_start = src.index("with tab_roadmap:")
        step3_block = src[step3:tab_roadmap_start]
        self.assertNotIn("_transfer_gantt_figure(", step3_block,
                         "the Gantt chart still renders inside Step 3 as well as its own tab")

    def test_gantt_chart_renders_exactly_once(self):
        src = _app_source()
        self.assertEqual(src.count("_transfer_gantt_figure(gantt)"), 1)

    def test_roadmap_tab_carries_the_required_caveat_banner(self):
        src = _app_source()
        start = src.index("with tab_roadmap:")
        block = src[start:start + 2000]
        self.assertIn("🗺️ The Rolling Transfer Roadmap", block)
        self.assertIn("A roadmap, not a contract", block)
        self.assertIn("Dynamic recalculation", block)
        self.assertIn("re-solves before every deadline", block)

    def test_roadmap_tab_degrades_gracefully_with_no_plan_yet(self):
        """Before Step 2 has ever run, override_analysis doesn't exist --
        the tab must say so rather than crash reaching into session_state."""
        src = _app_source()
        start = src.index("with tab_roadmap:")
        block = src[start:start + 2000]
        self.assertIn('"override_analysis" not in st.session_state', block)

class FixtureRunTest(unittest.TestCase):
    """'The move' card's side-by-side fixture-horizon traffic lights: each of
    the Transfer Out / Transfer In player badges gets its own 4-fixture track
    (official FDR badge, opponent + venue, that gameweek's xP) underneath."""

    def test_fixture_run_wired_into_both_transfer_cards(self):
        src = _app_source()
        start = src.index("def _transfer_pair_html(")
        end = src.index("\ndef ", start + 1)
        block = src[start:end]
        self.assertEqual(
            block.count("_player_fixture_run_html("), 2,
            "expected one fixture-run call for the Transfer Out card and one "
            "for the Transfer In card")

    def test_fixture_run_html_reads_the_official_fdr_not_the_internal_ease_score(self):
        src = _app_source()
        start = src.index("def _player_fixture_run_html(")
        end = src.index("\ndef ", start + 1)
        block = src[start:end]
        self.assertIn("fpl_tools._player_fixture_run(", block,
                      "must reuse fpl_tools' own per-gameweek breakdown rather "
                      "than reinventing it against the internal ease score")

    def test_fdr_badge_bands_match_the_specified_colours(self):
        """Green 1-2 (favourable), amber 3 (moderate), red 4-5 (difficult) --
        the exact bands given, not this app's own inverted internal scale."""
        src = _app_source()
        start = src.index("_FX_RUN_FDR_COLOR = {")
        end = src.index("}", start)
        table = src[start:end]
        self.assertIn("1: \"var(--pos)\"", table.replace("'", '"'))
        self.assertIn("2: \"var(--pos)\"", table.replace("'", '"'))
        self.assertIn("3: \"var(--warn)\"", table.replace("'", '"'))
        self.assertIn("4: \"var(--neg)\"", table.replace("'", '"'))
        self.assertIn("5: \"var(--neg)\"", table.replace("'", '"'))

    def test_move_card_caption_is_present_and_exact(self):
        # Checked as separate literal fragments rather than one concatenated
        # string: the source wraps the caption across adjacent string
        # literals, so the raw file text never contains it as one substring.
        src = _app_source()
        start = src.index('_card(transfer_html, "⚙️ The move")')
        block = src[start:start + 500]
        self.assertIn("st.caption(", block)
        self.assertIn(
            "Fixtures colored by FDR (Fixture Difficulty Rating): 🟢 Favourable,",
            block)
        self.assertIn(
            "🟡 Moderate, 🔴 Difficult. Values indicate projected points for that",
            block)
        self.assertIn("specific fixture.", block)


class MobileOverhaulStructureTest(unittest.TestCase):
    """Source-level wiring checks for the mobile-first overhaul: the parts
    that aren't expressible as a pure CSS regex (which function renders
    what, which markup carries which class)."""

    def test_pitch_player_uses_the_real_dot_renderer_not_the_emoji_string(self):
        src = _app_source()
        start = src.index("def _pitch_player_html(")
        end = src.index("\ndef ", start + 1)
        block = src[start:end]
        self.assertIn("_pitch_fixture_dots_html(", block)
        self.assertNotIn("_fixture_traffic_lights(", block,
                         "the pitch card must render real circular badges, "
                         "not the emoji string meant for text contexts")

    def test_badge_img_never_emits_a_bare_img_tag(self):
        """Same reasoning as _headshot_tile: a bare <img> has no CSS-only
        fallback under unsafe_allow_html (Streamlit strips onerror), so a
        404 or an unmapped team must fail onto a styled div, never a raw
        <img src=...> the browser can paint a broken-image icon for."""
        src = _app_source()
        start = src.index("def _badge_img(")
        end = src.index("\ndef ", start + 1)
        block = src[start:end]
        self.assertNotIn("<img", block)
        self.assertIn("badge-crest", block)

    def test_badge_img_falls_back_to_initials_when_no_crest_is_mapped(self):
        src = _app_source()
        start = src.index("def _badge_img(")
        end = src.index("\ndef ", start + 1)
        block = src[start:end]
        self.assertIn("short_name", block,
                      "the no-code branch must derive initials from the team's short_name")

    def test_transfer_pair_score_block_carries_the_mobile_hook_class(self):
        src = _app_source()
        start = src.index("def _transfer_pair_html(")
        end = src.index("\ndef ", start + 1)
        block = src[start:end]
        self.assertIn('class="tc-score"', block)

    def test_radar_shortlists_card_uses_the_pmc_grid(self):
        src = _app_source()
        start = src.index("Player Radar Shortlists")
        end = src.index("st.markdown(_card(grid,", start)
        block = src[start:end]
        self.assertIn('class="radar-card pmc"', block)
        self.assertIn('class="pmc-photo"', block)
        self.assertIn('class="pmc-info"', block)
        self.assertIn('class="pmc-xp"', block)


class StylesheetTest(unittest.TestCase):
    def test_no_hardcoded_hex_outside_root(self):
        """The Stage 7 gate. One #fff had survived on .pc .pos."""
        path = os.path.join(ROOT, "static", "app.css")
        offenders, in_root = [], False
        for n, line in enumerate(open(path, encoding="utf-8"), 1):
            if line.startswith(":root"):
                in_root = True
            elif in_root and line.startswith("}"):
                in_root = False
            elif not in_root and re.search(r"#[0-9a-fA-F]{3,8}\b", line):
                offenders.append(f"{n}: {line.strip()}")
        self.assertEqual(offenders, [], "hardcoded hex outside :root")

    def _css(self):
        return open(os.path.join(ROOT, "static", "app.css"), encoding="utf-8").read()

    def test_the_page_itself_cannot_scroll_sideways(self):
        """Only one media query existed before this, scoped to the pitch view
        alone -- everything else that ran wide on a phone had nothing to stop
        it pushing the whole page frame sideways, which is what "content cut
        off outside the frame" actually was: real content shoved past the
        viewport edge with no scrollbar to reach it."""
        css = self._css()
        m = re.search(r"\.block-container\s*\{[^}]*\}", css)
        self.assertIsNotNone(m, ".block-container rule not found")
        self.assertIn("overflow-x: hidden", m.group(0))

    def test_wide_tables_scroll_within_themselves(self):
        """The objective waterfall and the Model Health week-by-week table
        share .wf-row: a label plus three fixed min-width numeric columns that
        can exceed a phone's content width on their own. Clipped by the page
        safety net alone, the right-hand columns (bias, rank correlation)
        would simply vanish; scrollable, they stay reachable."""
        css = self._css()
        m = re.search(r"\.wf\s*\{[^}]*\}", css)
        self.assertIsNotNone(m, ".wf rule not found")
        self.assertIn("overflow-x:auto", m.group(0).replace(" ", ""))

    def test_transfer_pair_cards_can_shrink_below_their_text(self):
        """flex:1 alone does not let a flex item shrink below its CONTENT's
        natural width -- min-width:0 is what permits that, and without it two
        name+meta cards plus an arrow plus a fixed xP/cost block had nothing
        left to give on a narrow screen."""
        css = self._css()
        m = re.search(r"\.transfer-card\s*\{[^}]*\}", css)
        self.assertIsNotNone(m, ".transfer-card rule not found")
        self.assertIn("min-width: 0", m.group(0))

    def test_transfer_pair_text_ellipsises_instead_of_forcing_width(self):
        css = self._css()
        for cls in (".tc-name", ".tc-meta"):
            m = re.search(re.escape(cls) + r"\s*\{[^}]*\}", css)
            self.assertIsNotNone(m, f"{cls} rule not found")
            self.assertIn("text-overflow: ellipsis", m.group(0))

    def test_positional_diagnostic_grid_can_reflow(self):
        """Was grid-template-columns: repeat(5, 1fr) -- a fixed column count
        with no minmax floor, so on a phone each of the five GK-through-Bench
        cards was forced narrower than its own 132px label plus a bar plus a
        value can hold. A fixed track count has nowhere to reflow to, so it
        overflowed instead: MID/FWD/Bench cut off, text truncated."""
        css = self._css()
        m = re.search(r"\.signal-grid\s*\{[^}]*\}", css)
        self.assertIsNotNone(m, ".signal-grid rule not found")
        rule = m.group(0)
        self.assertNotRegex(rule, r"repeat\(\s*5\s*,",
                            "still a fixed 5-column grid with nowhere to reflow to")
        self.assertIn("auto-fit", rule)
        self.assertIn("minmax(", rule)

    def test_page_config_requests_an_expanded_sidebar(self):
        src = _app_source()
        m = re.search(r"st\.set_page_config\([^)]*\)", src, re.DOTALL)
        self.assertIsNotNone(m, "st.set_page_config(...) not found")
        self.assertIn('initial_sidebar_state="expanded"', m.group(0))

    def test_desktop_sidebar_is_pinned_open(self):
        """A misclick on Streamlit's own collapse arrow could hide the
        Strategy & Chip controls on desktop with no obvious way back. Scoped
        to >=992px so mobile/tablet keep Streamlit's own collapse-to-
        hamburger behaviour -- a narrow viewport still needs that space back."""
        css = self._css()
        m = re.search(r"@media \(min-width:\s*992px\)\s*\{.*?\n\}", css, re.DOTALL)
        self.assertIsNotNone(m, "no >=992px desktop media query found")
        block = m.group(0)
        self.assertIn('[data-testid="stSidebarCollapseButton"]', block)
        self.assertIn("display: none !important", block)
        self.assertIn("min-width: 300px !important", block)
        self.assertIn("max-width: 320px !important", block)
        self.assertIn("transform: none !important", block)
        self.assertIn("visibility: visible !important", block)

    def test_mobile_sidebar_collapse_is_not_touched_by_the_desktop_pin(self):
        """The >=992px pin must not leak into the existing <=768px mobile
        block -- narrow viewports still get Streamlit's own collapse."""
        css = self._css()
        mobile = re.search(r"@media \(max-width: 768px\) \{.*?\n\s*\}", css, re.DOTALL)
        self.assertIsNotNone(mobile)
        self.assertNotIn("stSidebarCollapseButton", mobile.group(0))

    def test_no_card_grows_its_own_vertical_scrollbar(self):
        """Streamlit gives some of its own container primitives (a bordered
        container, an expander body, a metric row) a default max-height once
        content is tall enough -- a second, nested scroll region inside a
        page that already scrolls, e.g. under 'How your squad stacks up' in
        Step 4. The page scrolls; no card should."""
        css = self._css()
        for selector in ('div[data-testid="stExpander"]',
                         'div[data-testid="stVerticalBlockBorderWrapper"]'):
            self.assertIn(selector, css, f"{selector} rule not found")
        m = re.search(
            r'\[data-testid="stVerticalBlock"\][^{]*?,\s*'
            r'\.stCard,\s*'
            r'div\[data-testid="stExpander"\],\s*'
            r'div\[data-testid="stVerticalBlockBorderWrapper"\]\s*\{([^}]*)\}',
            css, re.DOTALL)
        self.assertIsNotNone(m, "combined nested-scrollbar override rule not found")
        rule = m.group(1)
        self.assertIn("overflow-y: visible !important", rule)
        self.assertIn("max-height: none !important", rule)

    def test_no_container_is_given_a_fixed_pixel_height(self):
        """st.container(height=...) is exactly the API that creates a
        scrollable sub-region -- none of this app's cards should opt into
        one."""
        src = _app_source()
        self.assertNotIn("st.container(height=", src)

    def test_fixture_run_badges_are_circular_not_square(self):
        """.fdr-cell (the pre-existing rotation-matrix badge) is a rounded
        square -- this feature asked for circular badges specifically, so it
        needs its own class rather than reusing that one."""
        css = self._css()
        m = re.search(r"\.fx-run-badge\s*\{[^}]*\}", css)
        self.assertIsNotNone(m, ".fx-run-badge rule not found")
        self.assertIn("border-radius: 50%", m.group(0))

    def test_fixture_run_track_sits_under_the_card_and_can_shrink(self):
        css = self._css()
        for cls in (".fx-run", ".fx-run-cell", ".fx-run-opp"):
            m = re.search(re.escape(cls) + r"\s*\{[^}]*\}", css)
            self.assertIsNotNone(m, f"{cls} rule not found")
        cell = re.search(r"\.fx-run-cell\s*\{[^}]*\}", css).group(0)
        self.assertIn("min-width: 0", cell,
                      "without min-width:0 four cells plus a headshot header "
                      "cannot shrink to fit a phone-width transfer card")

    def _media_block(self, css, max_width_px):
        m = re.search(r"@media \(max-width:\s*" + str(max_width_px) + r"px\)\s*\{(.*?)\n\}",
                      css, re.DOTALL)
        self.assertIsNotNone(m, f"no max-width:{max_width_px}px media query found")
        return m.group(1)

    def test_narrow_phone_media_query_exists_and_follows_the_768px_block(self):
        """Both match below 600px and both use !important on shared
        selectors -- the 600px block must come AFTER the 768px one in the
        file so its rules win the tie by source order."""
        css = self._css()
        idx_768 = css.index("@media (max-width: 768px)")
        idx_600 = css.index("@media (max-width: 600px)")
        self.assertLess(idx_768, idx_600)

    def test_pitch_row_cards_wrap_into_a_staggered_layout_below_600px(self):
        block = self._media_block(self._css(), 600)
        m = re.search(r"\.pitch-row-cards\s*\{([^}]*)\}", block)
        self.assertIsNotNone(m, ".pitch-row-cards rule not found in the 600px block")
        rule = m.group(1)
        self.assertIn("flex-wrap: wrap", rule)
        self.assertIn("justify-content: center", rule)
        self.assertIn("gap: 6px 4px", rule)

    def test_pitch_player_gets_a_bounded_surface_below_600px(self):
        block = self._media_block(self._css(), 600)
        m = re.search(r"\.pitch-player\s*\{([^}]*)\}", block)
        self.assertIsNotNone(m, ".pitch-player rule not found in the 600px block")
        rule = m.group(1)
        self.assertIn("rgba(15, 23, 42, 0.7)", rule)
        self.assertIn("rgba(255, 255, 255, 0.08)", rule)
        self.assertIn("border-radius: 6px", rule)

    def test_pitch_player_text_is_tuned_down_below_600px(self):
        block = self._media_block(self._css(), 600)
        nm = re.search(r"\.pitch-player \.nm\s*\{([^}]*)\}", block)
        meta = re.search(r"\.pitch-player \.meta\s*\{([^}]*)\}", block)
        self.assertIsNotNone(nm, ".pitch-player .nm rule not found in the 600px block")
        self.assertIsNotNone(meta, ".pitch-player .meta rule not found in the 600px block")
        self.assertIn("0.70rem", nm.group(1))
        self.assertIn("0.65rem", meta.group(1))

    def test_fixture_dots_are_scaled_down_below_600px(self):
        """The pitch card's fixture indicator is now real circular <span>
        elements (fx-dot), not emoji glyphs -- rendered via
        _pitch_fixture_dots_html so mobile CSS can size them directly with
        width/height, exactly as requested (7px x 7px, margin: 0 1px)."""
        block = self._media_block(self._css(), 600)
        m = re.search(r"\.pitch-player \.fx-dot\s*\{([^}]*)\}", block)
        self.assertIsNotNone(m, ".pitch-player .fx-dot rule not found in the 600px block")
        rule = m.group(1)
        self.assertIn("width: 7px", rule)
        self.assertIn("height: 7px", rule)
        self.assertIn("margin: 0 1px", rule)

    def test_desktop_pitch_player_rule_is_unreachable_below_600px_only(self):
        """Requirement 4: >=600px must be untouched. The base (non-media)
        .pitch-player rule -- the one desktop actually renders under -- must
        not itself be edited to carry the new mobile-only styling; that
        styling must live only inside the 600px (or narrower) media query."""
        css = self._css()
        base_rule_end = css.index("@media (max-width: 768px)")
        base_css = css[:base_rule_end]
        # The desktop-scoped .pitch-player rule(s) must not carry the new
        # bounded-card background -- that would leak onto every viewport.
        for m in re.finditer(r"\.pitch-player\s*\{([^}]*)\}", base_css):
            self.assertNotIn("rgba(15, 23, 42, 0.7)", m.group(1))

    def test_transfer_pair_stacks_vertically_below_600px(self):
        block = self._media_block(self._css(), 600)
        pair = re.search(r"\.transfer-pair\s*\{([^}]*)\}", block)
        card = re.search(r"\.transfer-card\s*\{([^}]*)\}", block)
        self.assertIsNotNone(pair, ".transfer-pair rule not found in the 600px block")
        self.assertIsNotNone(card, ".transfer-card rule not found in the 600px block")
        self.assertIn("flex-direction: column", pair.group(1))
        self.assertIn("width: 100%", card.group(1))

    def test_transfer_score_block_goes_full_width_and_centres_below_600px(self):
        """The trailing +xP/cost block is right-aligned at a fixed min-width
        on desktop (it sits at the end of a horizontal row) -- stacked
        below the two cards it has to drop that and centre itself instead,
        or it reads as oddly pinned to the right of an otherwise full-width
        column."""
        block = self._media_block(self._css(), 600)
        m = re.search(r"\.tc-score\s*\{([^}]*)\}", block)
        self.assertIsNotNone(m, ".tc-score rule not found in the 600px block")
        rule = m.group(1)
        self.assertIn("width: 100%", rule)
        self.assertIn("text-align: center", rule)

    def test_fixture_run_row_is_explicit_row_with_space_between(self):
        """Belt-and-suspenders on top of the existing flex:1 cells -- the
        literal layout requested for the 4-fixture track."""
        css = self._css()
        m = re.search(r"\.fx-run\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m, ".fx-run rule not found")
        rule = m.group(0)
        self.assertIn("flex-direction: row", rule)
        self.assertIn("justify-content: space-between", rule)

    def test_player_market_card_grid_exists_and_reflows_on_mobile(self):
        """.pmc: the unified grid for player-market/list cards (Radar
        Shortlists). Desktop keeps today's visual arrangement (photo
        spanning two rows, info/xP stacked beside it) expressed as grid;
        mobile re-flows the SAME three pieces into the requested single-row
        48px/1fr/auto, with no markup difference between breakpoints."""
        css = self._css()
        base = re.search(r"(?<!\.)\.pmc\s*\{([^}]*)\}", css)
        self.assertIsNotNone(base, "base .pmc grid rule not found")
        self.assertIn("display: grid", base.group(1))
        block = self._media_block(css, 600)
        mobile = re.search(r"\.pmc\s*\{([^}]*)\}", block)
        self.assertIsNotNone(mobile, ".pmc rule not found in the 600px block")
        rule = mobile.group(1)
        self.assertIn("48px", rule)
        self.assertIn("1fr", rule)
        self.assertIn("auto", rule)

    def test_badge_crest_fallback_is_circular(self):
        css = self._css()
        m = re.search(r"\.badge-crest\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m, ".badge-crest rule not found")
        self.assertIn("border-radius: 50%", m.group(1))

    def test_fx_dot_is_a_real_sized_circle_by_default(self):
        """The base (desktop) rule must exist too, not just the mobile
        override -- .fx-dot has to be a real element at every breakpoint
        for the mobile rule to have anything to resize."""
        css = self._css()
        base_rule_end = css.index("@media (max-width: 768px)")
        m = re.search(r"(?<!-)\.fx-dot\s*\{([^}]*)\}", css[:base_rule_end])
        self.assertIsNotNone(m, "base .fx-dot rule not found before the first media query")
        rule = m.group(1)
        self.assertIn("border-radius: 50%", rule)
        self.assertIn("width:", rule)


class ModelHealthDataTest(unittest.TestCase):
    def test_accuracy_query_returns_empty_rather_than_raising(self):
        """No database in CI, and none on a fresh deploy either. The tab has to
        say so, not take the page down."""
        import db
        self.assertEqual(db.prediction_accuracy_by_gw("v4-xp-overhaul"), [])

    def test_accuracy_query_filters_by_model_version(self):
        """Rows from before a MODEL_VERSION bump are different predictions under
        the same column names; mixing them would report a discontinuity as error."""
        import inspect
        import db
        src = inspect.getsource(db.prediction_accuracy_by_gw)
        self.assertIn("model_version = %s", src)
        self.assertIn("actual_points IS NOT NULL", src)


if __name__ == "__main__":
    unittest.main()
