"""Tier 4 gate: failures are visible, and an outage never widens permissions.

Four independent silent-failure modes in app.py, each of which produced a
plausible-looking screen:

  * db's writers return False on a failed write instead of raising, so the
    try/except around them never fired and a decision that never reached the
    database looked persisted.
  * `_transfers_stale = False` sat OUTSIDE the try that recalculates, so a
    failed re-solve still marked the plan fresh -- the warning showed once,
    then the stale plan was served as current forever.
  * A chip-ledger outage fell back to ALL_CHIPS, re-enabling chips already
    spent. Failing OPEN on a permissions question.
  * The free-transfer input was hardcoded to 1 even though the API-derived
    figure was already in session state.
"""

import ast
import io
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _app():
    with io.open(os.path.join(ROOT, "app.py"), encoding="utf-8") as fh:
        return fh.read()


class PersistenceReturnTest(unittest.TestCase):

    def test_every_writer_call_consumes_its_return_value(self):
        """A bare expression-statement call is a discarded boolean. Checked on
        the AST, so a call inside a condition or an assignment passes and a
        fire-and-forget one does not."""
        tree = ast.parse(_app())
        writers = {"log_decision", "save_chip_play", "save_plan", "log_squad_health"}
        discarded = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr):
                continue
            call = node.value
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                    and call.func.id in writers:
                discarded.append((call.func.id, node.lineno))
        self.assertEqual(discarded, [],
                         f"writer return values thrown away at {discarded}")

    def test_the_failure_is_surfaced_to_the_user(self):
        self.assertIn("Database write failed. Lineup is local-only.", _app())


class StaleFlagTest(unittest.TestCase):

    def test_the_flag_is_cleared_inside_the_try_not_after_it(self):
        """Structural, via the AST: the assignment must be a descendant of the
        Try's body, never a sibling that runs whatever happened."""
        tree = ast.parse(_app())

        def clears_flag(node):
            return (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Subscript)
                            and isinstance(t.slice, ast.Constant)
                            and t.slice.value == "_transfers_stale"
                            for t in node.targets)
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is False)

        clears = [n for n in ast.walk(tree) if clears_flag(n)]
        self.assertTrue(clears, "nothing clears _transfers_stale any more")
        for node in clears:
            in_try_body = any(
                any(c is node or node in list(ast.walk(c))
                    for c in getattr(t, "body", []))
                for t in ast.walk(tree) if isinstance(t, ast.Try))
            self.assertTrue(
                in_try_body,
                f"line {node.lineno}: the stale flag is cleared outside the try, "
                "so a failed re-solve still marks the plan fresh")


class ChipLedgerFailClosedTest(unittest.TestCase):

    def test_an_outage_does_not_fall_back_to_every_chip(self):
        self.assertNotIn(
            "available_chips(GW_ID) if chip_ledger else ALL_CHIPS", _app(),
            "the chip ledger still fails OPEN on an API outage")

    def test_it_distinguishes_no_chips_left_from_no_history(self):
        """The two mean opposite things to a manager deciding a gameweek."""
        src = _app()
        self.assertIn("Chip history unavailable", src)
        self.assertIn("does not mean you have no", src)

    def test_the_hold_option_survives_an_empty_chip_list(self):
        """available_now = [] must still leave a legal radio to render."""
        self.assertIn('chip_options = ["None (Hold Chips)"] + available_now', _app())


class FreeTransferDefaultTest(unittest.TestCase):

    def test_the_input_is_seeded_from_the_api_derived_figure(self):
        src = _app()
        self.assertIn(
            'value=int(st.session_state.get("api_free_transfers_default", 1))', src,
            "the free-transfer input is still hardcoded")


class DeadRiskProfileTest(unittest.TestCase):

    def test_the_empty_risk_profile_lookup_is_gone(self):
        """Five empty dicts behind a lookup that could only return {}. Its last
        caller left with the Final Boss block."""
        import fpl_tools
        self.assertFalse(hasattr(fpl_tools, "RISK_PROFILES"))
        self.assertFalse(hasattr(fpl_tools, "_risk_profile"))

    def test_the_alias_table_is_retained(self):
        """_RISK_ALIASES is a different thing and still has live callers --
        it normalises the UI's display labels onto the internal keys."""
        import fpl_tools
        self.assertTrue(hasattr(fpl_tools, "_RISK_ALIASES"))
        self.assertIn("rank_protecting", set(fpl_tools._RISK_ALIASES.values()))

    def test_risk_appetite_still_has_a_home(self):
        import fpl_tools
        self.assertIn("balanced", fpl_tools.HIT_HURDLE)
        self.assertIn("rank_protecting", fpl_tools.HIT_HURDLE)


if __name__ == "__main__":
    unittest.main()
