"""Stage 8 gate: the deploy is reproducible and the start command is in the repo.

None of this changes a recommendation. It changes whether the recommendations
keep arriving after a dependency releases a new major, and whether anyone other
than the person holding the Railway dashboard can redeploy this thing.
"""

import ast
import os
import re
import sys
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

    def test_python_version_supports_the_pinned_numpy_floor(self):
        """fpl_logger.yml pinned Python 3.10 while requirements.txt's numpy
        floor (raised to >=2.4 for Stage 8a) needs >=3.11 -- pip on 3.10
        can't see that from the error alone, it just reports "no matching
        distribution" for numpy<3,>=2.4 and the whole install step dies
        before any of the four scheduled jobs (archive/snapshot/ingest/
        autotune) get a chance to run. tests.yml already runs 3.11 against
        this exact requirements.txt; both workflows must specify the same
        version so this cannot drift apart again."""
        wf_versions = {}
        for name in ("fpl_logger.yml", "tests.yml"):
            path = os.path.join(ROOT, ".github", "workflows", name)
            with open(path, encoding="utf-8") as fh:
                m = re.search(r'python-version:\s*[\'"]?([\d.]+)[\'"]?', fh.read())
            self.assertIsNotNone(m, f"{name}: no python-version found")
            wf_versions[name] = m.group(1)

        self.assertEqual(wf_versions["fpl_logger.yml"], wf_versions["tests.yml"],
                         f"workflow Python versions drifted apart: {wf_versions}")
        numpy_floor = next(ln for ln in _requirements() if ln.startswith("numpy"))
        floor_version = re.search(r'>=(\d+\.\d+)', numpy_floor).group(1)
        self.assertGreaterEqual(tuple(map(int, wf_versions["fpl_logger.yml"].split("."))),
                                (3, 11),
                                f"numpy{numpy_floor[len('numpy'):]} (floor {floor_version}) "
                                f"needs Python >=3.11; fpl_logger.yml pins "
                                f"{wf_versions['fpl_logger.yml']}")

    def test_scipy_is_actually_available_via_that_file(self):
        """The concrete failure this fixes: requirements.txt must carry the
        dependency the workflow now installs wholesale."""
        names = [ln.split("[")[0].split(">")[0].split("=")[0].split("<")[0].strip().lower()
                for ln in _requirements()]
        self.assertIn("scipy", names)


# Distribution name -> the module name it actually installs, where the two
# differ. Without this a declared dependency reads as undeclared.
_IMPORT_NAME = {
    "pyyaml": "yaml",
    "pillow": "PIL",
    "psycopg2-binary": "psycopg2",
    "python-dateutil": "dateutil",
    "google-genai": "google",
}


def _declared_modules():
    """Importable module names requirements.txt provides."""
    out = set()
    for line in _requirements():
        dist = re.split(r"[<>=~\[]", line, 1)[0].strip().lower()
        out.add(_IMPORT_NAME.get(dist, dist))
    return out


def _tests_workflow_install():
    """The tests workflow's `pip install` command, as a token list."""
    path = os.path.join(ROOT, ".github", "workflows", "tests.yml")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped.startswith("run:") and "pip install" in stripped:
                return stripped[len("run:"):].strip().split()
    return []


def _local_modules():
    """Module names resolvable from the repo itself.

    tests/ and scripts/ both prepend their own directory to sys.path, so a
    bare `import backtest` is a LOCAL import of scripts/backtest.py, not a
    third-party package. Missing this is what makes a naive version of this
    check produce five false positives.
    """
    out = {"tests", "scripts"}
    for sub in ("", "scripts", "tests", os.path.join("tests", "fixtures")):
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        out |= {f[:-3] for f in os.listdir(d) if f.endswith(".py")}
    return out


def _top_level_imports(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            # level > 0 is an explicit relative import, always local.
            mods.add(node.module.split(".")[0])
    return mods


class TestsWorkflowDependencyTest(unittest.TestCase):
    """CIWorkflowDependencyTest above guards fpl_logger.yml. tests.yml is the
    job that actually runs the suite and had no equivalent guard, which is how
    an undeclared PyYAML import shipped: the suite went green locally, because
    PyYAML sits in the dev container's base image (pip reports it as
    Required-by: conan), and would have died at COLLECTION on a clean runner --
    taking all 600-odd tests with it, not just the five that import yaml.

    The comment above tests.yml's own install step describes the identical
    failure one layer down: a hand-typed list that omitted streamlit, green
    locally for the same reason, red on every push, and nobody had opened the
    Actions tab.
    """

    def test_the_test_job_installs_from_requirements_txt(self):
        self.assertIn("-r", _tests_workflow_install())
        self.assertIn("requirements.txt", _tests_workflow_install())

    def test_anything_installed_alongside_it_is_test_tooling(self):
        """Extras are allowed -- pytest is not an app dependency and has no
        business in requirements.txt -- but they are the same drift risk the
        hand-maintained list was, so the permitted set is explicit."""
        tokens = _tests_workflow_install()
        extras = [t for t in tokens[tokens.index("install") + 1:]
                  if t not in ("-r", "requirements.txt")]
        self.assertTrue(set(extras) <= {"pytest"},
                        f"unexpected packages installed outside requirements.txt: {extras}")


class ImportDeclarationTest(unittest.TestCase):
    """Every third-party module the repo imports must be installable from
    requirements.txt (plus whatever tests.yml installs alongside it).

    This is the check that would have caught the PyYAML miss before CI did.
    A dependency satisfied only by the dev container's base image is invisible
    until a clean runner tries it, and by then it is a collection error.
    """

    def test_every_third_party_import_is_declared(self):
        allowed = (_declared_modules() | _local_modules()
                   | set(sys.stdlib_module_names)
                   | {t for t in _tests_workflow_install()
                      if t not in ("pip", "install", "-r", "requirements.txt")})
        undeclared = {}
        for sub in ("", "scripts", "tests"):
            d = os.path.join(ROOT, sub)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(d, name)
                for mod in _top_level_imports(path) - allowed:
                    undeclared.setdefault(mod, []).append(os.path.join(sub, name))
        self.assertEqual(
            undeclared, {},
            "third-party imports with no entry in requirements.txt: "
            + "; ".join(f"{m} <- {', '.join(f)}" for m, f in sorted(undeclared.items())))


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
