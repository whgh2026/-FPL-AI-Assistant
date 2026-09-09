"""Stage 8 gate: the deploy is reproducible and the start command is in the repo.

None of this changes a recommendation. It changes whether the recommendations
keep arriving after a dependency releases a new major, and whether anyone other
than the person holding the Railway dashboard can redeploy this thing.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _requirements():
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as fh:
        return [ln.strip() for ln in fh
                if ln.strip() and not ln.strip().startswith("#")]


class DependencyPinTest(unittest.TestCase):
    def test_every_dependency_has_a_version_constraint(self):
        """Unpinned, the next deploy installs whatever PyPI serves that day."""
        for line in _requirements():
            self.assertRegex(
                line, r"[<>=~]",
                f"{line!r} is unpinned -- the next deploy could install anything")

    def test_every_dependency_caps_its_major_version(self):
        """A floor alone does not help: majors are where the breaks live."""
        for line in _requirements():
            self.assertIn("<", line, f"{line!r} has no upper bound")

    def test_pulp_is_held_below_4(self):
        """Not hypothetical. The suite already warns that PULP_CBC_CMD and
        direct LpVariable construction -- the two APIs the solver is built on --
        are removed in PuLP 4.0. Unpinned, the engine dies on a deploy that
        changed no code."""
        pulp = [l for l in _requirements() if l.lower().startswith("pulp")]
        self.assertEqual(len(pulp), 1, f"expected one pulp line, got {pulp}")
        self.assertRegex(pulp[0], r"<\s*4")

    def test_solver_api_still_exists_at_the_installed_version(self):
        """The cap is only worth having if the capped API is the one in use."""
        import pulp
        self.assertTrue(hasattr(pulp, "PULP_CBC_CMD"))
        self.assertLess(int(pulp.__version__.split(".")[0]), 4,
                        f"PuLP {pulp.__version__} is installed but the code "
                        "still uses the pre-4.0 solver API")

    def test_python_dotenv_is_gone(self):
        """It was installed and never imported, so .env files were silently not
        loaded -- worse than absent, because it looks like they would be."""
        for line in _requirements():
            self.assertNotIn("dotenv", line.lower())

    def test_nothing_imports_dotenv(self):
        """If anything ever does, the dependency has to come back with it."""
        import subprocess
        r = subprocess.run(["grep", "-rn", "dotenv", "--include=*.py", "."],
                           cwd=ROOT, capture_output=True, text=True)
        hits = [ln for ln in r.stdout.splitlines() if "tests/" not in ln]
        self.assertEqual(hits, [], f"dotenv is imported but not declared: {hits}")


class CIWorkflowDependencyTest(unittest.TestCase):
    """P0 remediation, Task 3 (D4/D8): the scheduled pipeline installed a
    hand-maintained package list that had already drifted from
    requirements.txt twice over -- carrying `pandas` (used nowhere, there
    only to silently supply python-dateutil) while missing `scipy`
    (fpl_tools._devig_power's power-method de-vig solver), so every scheduled
    job silently ran the degraded proportional-split fallback instead of the
    real one, with no error to notice it by."""

    def _workflow(self):
        path = os.path.join(ROOT, ".github", "workflows", "fpl_logger.yml")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_installs_from_requirements_txt(self):
        wf = self._workflow()
        self.assertIn("pip install -r requirements.txt", wf)

    def test_no_hand_maintained_package_list_survives(self):
        """The specific old command, or any equivalent hardcoded list, must
        be gone -- not just supplemented by the new one. Checked against the
        `run:` lines only, so this cannot be satisfied by prose describing
        the old command in a comment."""
        run_lines = [ln for ln in self._workflow().splitlines()
                    if ln.strip().startswith("run:")]
        for line in run_lines:
            if "pip install" in line:
                self.assertEqual(line.strip(), "run: pip install -r requirements.txt",
                                 f"a hand-maintained pip install line survives: {line!r}")

    def test_scipy_is_actually_available_via_that_file(self):
        """The concrete failure this fixes: requirements.txt must carry the
        dependency the workflow now installs wholesale."""
        names = [ln.split("[")[0].split(">")[0].split("=")[0].split("<")[0].strip().lower()
                for ln in _requirements()]
        self.assertIn("scipy", names)


class StartCommandTest(unittest.TestCase):
    def test_procfile_exists(self):
        """The start command lived only in the Railway dashboard, so the repo
        could not be deployed anywhere else -- and nobody could review it."""
        self.assertTrue(os.path.exists(os.path.join(ROOT, "Procfile")))

    def test_procfile_binds_the_platform_port_and_all_interfaces(self):
        """Streamlit defaults to 8501 on localhost. Both are wrong in a
        container: the platform assigns $PORT, and a process bound to localhost
        is unreachable from outside it."""
        with open(os.path.join(ROOT, "Procfile"), encoding="utf-8") as fh:
            proc = fh.read()
        self.assertIn("streamlit run app.py", proc)
        self.assertIn("--server.port=$PORT", proc)
        self.assertIn("--server.address=0.0.0.0", proc)
        self.assertIn("--server.headless=true", proc)

    def test_procfile_declares_a_web_process(self):
        with open(os.path.join(ROOT, "Procfile"), encoding="utf-8") as fh:
            self.assertTrue(re.match(r"^web:\s", fh.read()))


if __name__ == "__main__":
    unittest.main()
