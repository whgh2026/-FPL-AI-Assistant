"""Tier 5 gate: every declared cron reaches a step, and the opt-in is scoped.

The Wednesday auto-tune cron was declared in `on.schedule` but never named in
the step's `if:`, which only matched workflow_dispatch. A scheduled run
therefore fired the workflow, matched no step, and reported success -- so
auto-tune had effectively never run on a schedule while looking like it did.

The env var is set at the step, not defaulted ON in scripts/auto_tune.py. The
job writes weights.json into the repo and auto-commits it, so the opt-in
belongs at the one call site that is meant to do that.
"""

import os
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "fpl_logger.yml")


def _workflow():
    with open(WF, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _steps():
    return _workflow()["jobs"]["run_pipeline"]["steps"]


class CronCoverageTest(unittest.TestCase):

    def test_every_declared_cron_is_matched_by_some_step(self):
        """The defect, generalised. A cron nobody listens for is a scheduled
        no-op that reports success -- the worst possible failure shape."""
        wf = _workflow()
        # PyYAML parses the bare key `on:` as the boolean True.
        trigger = wf.get("on") or wf.get(True)
        crons = [c["cron"] for c in trigger["schedule"]]
        conditions = " ".join(str(s.get("if", "")) for s in _steps())
        for cron in crons:
            self.assertIn(cron, conditions,
                          f"cron {cron!r} is declared but no step listens for it")

    def test_the_autotune_step_matches_both_triggers(self):
        step = next(s for s in _steps() if "Auto-Tune" in s["name"])
        self.assertIn("workflow_dispatch", step["if"])
        self.assertIn("'0 10 * * 3'", step["if"])


class AutoTuneOptInTest(unittest.TestCase):

    def test_the_enable_flag_is_scoped_to_the_autotune_step(self):
        for step in _steps():
            env = step.get("env") or {}
            if "Auto-Tune" in step["name"]:
                self.assertEqual(env.get("FPL_AUTOTUNE_ENABLED"), "1")
            else:
                self.assertNotIn("FPL_AUTOTUNE_ENABLED", env,
                                 f"{step['name']!r} leaks the auto-tune opt-in")

    def test_the_script_still_defaults_to_disabled(self):
        """The whole point of setting it at the call site. A stray
        `python scripts/auto_tune.py` must not rewrite production weights."""
        with open(os.path.join(ROOT, "scripts", "auto_tune.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('"FPL_AUTOTUNE_ENABLED", "0"', src,
                      "auto_tune.py no longer defaults to disabled")

    def test_the_job_that_commits_weights_is_the_one_holding_the_flag(self):
        step = next(s for s in _steps() if "Auto-Tune" in s["name"])
        self.assertIn("weights.json", step["run"])
        self.assertIn("git commit", step["run"])


if __name__ == "__main__":
    unittest.main()
