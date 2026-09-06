"""Execute app.py top-to-bottom against the synthetic fixture, offline.

Not a unit test: a smoke harness. app.py is a single 2400-line Streamlit script
whose whole body runs on every rerun, so the only way to know it still executes
is to execute it. The build environment cannot reach the FPL API, so the network
accessors are patched to the Stage 0 fixture first.

    python tests/render_app.py            # normal run
    python tests/render_app.py --fail-api # every FPL call raises, to check the
                                          # UI degrades visibly instead of
                                          # rendering an empty page

Reports the widgets and markdown blocks Streamlit was asked to emit, plus wall
time, so the "cold render < 3s" gate can be measured.
"""

import importlib.util
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fpl_tools
from tests import harness

FAIL_API = "--fail-api" in sys.argv


def _install_fixture():
    bootstrap, fixtures = harness.load_synthetic()
    if FAIL_API:
        def _boom(*a, **k):
            raise RuntimeError("simulated FPL API outage")
        fpl_tools._get_bootstrap = _boom
        fpl_tools._get_fixtures = _boom
        fpl_tools._get_all_fixtures = _boom
    else:
        fpl_tools._get_bootstrap = lambda: bootstrap
        fpl_tools._get_fixtures = lambda: fixtures
        fpl_tools._get_all_fixtures = lambda: fixtures
    fpl_tools._fetch_market_win_probs = lambda bootstrap=None: {}
    harness.reset_caches()


class _Counter:
    """Counts what the script asked Streamlit to render."""

    def __init__(self):
        self.calls = {}

    def wrap(self, st, names):
        for name in names:
            original = getattr(st, name, None)
            if original is None:
                continue
            setattr(st, name, self._make(name, original))

    def _make(self, name, original):
        def _wrapped(*a, **k):
            self.calls[name] = self.calls.get(name, 0) + 1
            return original(*a, **k)
        return _wrapped


def main():
    _install_fixture()

    import streamlit as st
    counter = _Counter()
    counter.wrap(st, ["markdown", "metric", "button", "selectbox", "text_input",
                      "number_input", "checkbox", "radio", "caption", "expander",
                      "tabs", "error", "warning", "info", "file_uploader"])

    start = time.time()
    spec = importlib.util.spec_from_file_location("fplapp", "app.py")
    module = importlib.util.module_from_spec(spec)
    status = "ok"
    try:
        spec.loader.exec_module(module)
    except Exception as exc:                       # noqa: BLE001 - report, don't mask
        status = f"{type(exc).__name__}: {exc}"
    elapsed = time.time() - start

    print(f"mode      : {'API FAILURE' if FAIL_API else 'synthetic fixture'}")
    print(f"status    : {status}")
    print(f"wall time : {elapsed:.2f}s")
    print("rendered  :")
    for name, n in sorted(counter.calls.items(), key=lambda kv: -kv[1]):
        print(f"    {name:<16} {n}")

    if status != "ok":
        sys.exit(1)
    if FAIL_API and not (counter.calls.get("error") or counter.calls.get("warning")):
        print("\nFAIL: the API was down and the page raised no visible error.")
        sys.exit(1)


if __name__ == "__main__":
    main()
