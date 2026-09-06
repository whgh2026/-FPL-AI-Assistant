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
