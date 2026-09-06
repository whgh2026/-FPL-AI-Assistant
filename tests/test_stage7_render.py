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
        for label in ("🏟️ My Plan", "🗓️ Fixtures", "📡 Players", "🩺 Model Health"):
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


class MobileStrategySelectorTest(unittest.TestCase):
    """Mobile browsers collapse Streamlit's sidebar behind a hamburger icon,
    hiding the one control (Strategy Mode) that governs the whole plan.
    Step 3 needs its own copy of it, kept in sync with the sidebar."""

    def test_inline_selector_renders_at_the_top_of_step_3(self):
        src = _app_source()
        step3 = src.index("Step 3: Transfer Planner")
        container = src.index("with st.container(border=True):", step3)
        inline_widget = src.index('key="risk_inline"', container)
        override_analysis = src.index('ov = st.session_state["override_analysis"]', container)
        self.assertLess(inline_widget, override_analysis,
                        "the inline Strategy Mode selector must render before "
                        "the rest of Step 3, not after")

    def test_inline_selector_carries_the_required_copy(self):
        # Two adjacent string literals in app.py -- checked separately since
        # the raw source (unlike the evaluated string) still has the closing
        # and opening quotes between them.
        src = _app_source()
        self.assertIn("Choose your risk profile. Switching dynamically recalibrates", src)
        self.assertIn("transfer targets and projected upside across the multi-week planner.", src)

    def test_inline_and_sidebar_selectors_share_one_options_list(self):
        """Two separately-keyed widgets (Streamlit forbids sharing one key)
        drifting to different option lists would silently break the sync."""
        src = _app_source()
        self.assertIn("RISK_OPTIONS", src)
        self.assertEqual(src.count("RISK_OPTIONS,"), 2,
                         "expected both the sidebar and inline radios to use "
                         "the shared RISK_OPTIONS list")

    def test_selectors_are_synced_both_ways(self):
        src = _app_source()
        self.assertIn("def _sync_risk_to_inline():", src)
        self.assertIn("def _sync_risk_from_inline():", src)
        self.assertIn('on_change=_sync_risk_to_inline', src)
        self.assertIn('on_change=_sync_risk_from_inline', src)


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
