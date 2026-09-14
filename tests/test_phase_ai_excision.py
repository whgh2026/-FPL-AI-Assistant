"""Tier 4 gate: the retired AI critique leaves nothing behind.

The DeepSeek "Final Boss" prompt surface was retired from the product, but the
entire block survived in app.py: two system prompts, a three-attempt retry
loop against a cost-sensitive endpoint, an API key read from the environment,
two session-state keys, and a `requests` import no other line in the file
used. A retired feature that still ships its API call is one config change
away from being live again.

These are source-level assertions because the defect IS the presence of code.
"""

import io
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _app():
    with io.open(os.path.join(ROOT, "app.py"), encoding="utf-8") as fh:
        return fh.read()


def _code_only(src):
    """Source with comment lines stripped. The excision is allowed to be
    DESCRIBED in a comment; it must not be present as code."""
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))


class FinalBossExcisionTest(unittest.TestCase):

    def test_no_third_party_model_endpoint_remains(self):
        code = _code_only(_app()).lower()
        for needle in ("deepseek", "api.deepseek.com", "chat/completions"):
            self.assertNotIn(needle, code, f"{needle!r} still present")

    def test_no_api_key_is_read(self):
        self.assertNotIn("DEEPSEEK_API_KEY", _code_only(_app()))

    def test_the_prompt_surface_is_gone(self):
        code = _code_only(_app())
        for needle in ("fb_system_prompt", "fb_ai_prompt", "fb_context",
                       "fb_lineup", "fb_moves"):
            self.assertNotIn(needle, code, f"{needle} survives")

    def test_the_session_keys_are_gone(self):
        code = _code_only(_app())
        for key in ("ai_response", "last_ai_prompt"):
            self.assertNotIn(key, code, f"session key {key!r} survives")

    def test_the_keys_that_existed_only_to_feed_it_are_gone(self):
        """`displayed_moves` / `displayed_chip` were written for the prompt and
        read nowhere else. Left behind they are state nothing consumes."""
        code = _code_only(_app())
        for key in ("displayed_moves", "displayed_chip"):
            self.assertNotIn(key, code, f"orphaned key {key!r} survives")

    def test_requests_is_no_longer_imported(self):
        """It was imported for the POST and used by no other line. This does
        NOT mean the dependency goes: fpl_tools reaches the FPL API with it,
        so requirements.txt is deliberately untouched.

        Checked via the AST rather than by substring. app.py still DESCRIBES a
        removed `requests.head` call in a docstring, and a text search cannot
        tell prose about old code from live code -- the same trap
        test_stage8_hygiene.py calls out for the CI workflow's `run:` lines."""
        import ast
        tree = ast.parse(_app())
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import) for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        self.assertNotIn("requests", imported, "app.py still imports requests")

        used = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "requests"
        ]
        self.assertEqual(used, [], "a live requests.* call survives in app.py")

    def test_the_dependency_itself_is_retained(self):
        """Guard against over-zealous cleanup: removing `requests` from
        requirements would take the whole engine down, since fpl_tools fetches
        every bootstrap and fixture list with it."""
        with io.open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as fh:
            reqs = fh.read()
        self.assertIn("requests", reqs)
        with io.open(os.path.join(ROOT, "fpl_tools.py"), encoding="utf-8") as fh:
            self.assertIn("import requests", fh.read())

    def test_the_plan_tab_still_renders_its_final_section(self):
        """The UI has to end somewhere deliberate. Captaincy is the last card
        in the plan tab now, and the roadmap tab follows it."""
        src = _app()
        self.assertIn("⭐ Captaincy", src)
        self.assertIn("with tab_roadmap:", src)
        self.assertLess(src.index("⭐ Captaincy"), src.index("with tab_roadmap:"),
                        "the captaincy card is no longer the plan tab's finale")


if __name__ == "__main__":
    unittest.main()
