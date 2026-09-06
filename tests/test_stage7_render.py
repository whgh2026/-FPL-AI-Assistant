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
COLD_RENDER_BUDGET_S = 3.0


def _run(*args):
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )


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
        src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
        start = src.index("def get_caveat_html()")
        body = src[start:start + 1200]
        self.assertIn("except Exception", body)


if __name__ == "__main__":
    unittest.main()
