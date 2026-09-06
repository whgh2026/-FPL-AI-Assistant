"""Stage 7 gate: the screenshot importer resolves the right players, or says
it couldn't.

This path takes a photo of someone's team and turns it into fifteen player ids
that every downstream number is then computed from. A wrong id here is not a
rounding error -- it silently analyses a squad the user does not own, and
nothing later in the pipeline can detect it. The old matcher took the FIRST
bootstrap element whose full name merely CONTAINED the OCR'd string, so "Son"
resolved to whichever of Jackson, Wilson, Robertson or Son happened to sit
earliest in the file.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import squad_override as so


def el(pid, first, second, web, etype=3):
    return {"id": pid, "first_name": first, "second_name": second,
            "web_name": web, "element_type": etype}


# The substring trap, in the order that used to make it bite: the decoys come
# first, so a first-hit matcher never reaches the real player.
BOOTSTRAP = {"elements": [
    el(1, "Nathaniel", "Jackson", "Jackson", 4),
    el(2, "Harry", "Wilson", "Wilson", 3),
    el(3, "Andrew", "Robertson", "Robertson", 2),
    el(4, "Heung-min", "Son", "Son", 3),
    el(5, "Mohamed", "Salah", "M.Salah", 3),
    el(6, "Bruno", "Fernandes", "B.Fernandes", 3),
    el(7, "Bruno", "Guimaraes", "Bruno G.", 3),
    el(8, "David", "Raya", "Raya", 1),
]}


def match(name, pos=""):
    got, miss = so.match_players_to_fpl(BOOTSTRAP, [{"name": name, "position": pos}])
    return (got[0]["player_id"] if got else None), miss


class NameMatchingTest(unittest.TestCase):
    def test_exact_surname_beats_every_substring_decoy(self):
        """The bug, stated as a test: three decoys contain 'son' and all three
        sort ahead of the player whose name IS 'Son'."""
        pid, miss = match("Son")
        self.assertEqual(pid, 4, f"resolved to element {pid}, not Son")
        self.assertEqual(miss, [])

    def test_web_name_match(self):
        self.assertEqual(match("Raya")[0], 8)

    def test_full_name_match(self):
        self.assertEqual(match("Mohamed Salah")[0], 5)

    def test_initial_and_surname(self):
        """The form FPL's own UI prints."""
        self.assertEqual(match("M. Salah")[0], 5)
        self.assertEqual(match("H. Son")[0], 4)

    def test_initial_form_does_not_match_a_different_first_name(self):
        pid, miss = match("Z. Salah")
        self.assertNotEqual(pid, 5, "matched Salah on a first initial that isn't his")

    def test_accents_are_stripped(self):
        self.assertEqual(match("Guimarães")[0], 7)

    def test_fuzzy_rescues_a_plausible_ocr_slip(self):
        """OCR reads off a screenshot, so a dropped letter is routine."""
        self.assertEqual(match("Robertsen")[0], 3)

    def test_nonsense_is_reported_rather_than_guessed(self):
        """The important half. An unmatched name has to surface as unmatched --
        resolving it to the nearest thing in the file is how a user ends up
        analysing somebody else's squad."""
        pid, miss = match("Zzzzqqqq")
        self.assertIsNone(pid)
        self.assertEqual(miss, ["Zzzzqqqq"])

    def test_position_breaks_a_tie_but_cannot_create_a_match(self):
        pid, miss = match("Zzzzqqqq", "MID")
        self.assertIsNone(pid, "a position hint conjured a match out of nothing")

    def test_a_player_is_not_matched_twice(self):
        """Fifteen distinct players. If two OCR lines both resolve to Salah, the
        second is a failure to report, not a duplicate to return."""
        got, miss = so.match_players_to_fpl(
            BOOTSTRAP, [{"name": "Salah"}, {"name": "Salah"}])
        ids = [g["player_id"] for g in got]
        self.assertEqual(len(set(ids)), len(ids), f"duplicate ids returned: {ids}")

    def test_position_comes_from_the_bootstrap_not_the_ocr(self):
        """The screenshot's position label is a hint for matching; FPL's own
        element_type is the truth."""
        got, _ = so.match_players_to_fpl(BOOTSTRAP, [{"name": "Raya", "position": "FWD"}])
        self.assertEqual(got[0]["position"], "GK")

    def test_empty_and_missing_names_do_not_crash(self):
        got, miss = so.match_players_to_fpl(BOOTSTRAP, [{"name": ""}, {}])
        self.assertEqual(got, [])
        self.assertEqual(len(miss), 2)


class FenceStrippingTest(unittest.TestCase):
    """The old code ran .replace('json\\n', '') over the whole payload -- a
    global substitution on a document containing player names."""

    def test_strips_a_json_fence(self):
        self.assertEqual(so._strip_code_fence('```json\n[{"a": 1}]\n```'), '[{"a": 1}]')

    def test_strips_a_bare_fence(self):
        self.assertEqual(so._strip_code_fence('```\n[1]\n```'), '[1]')

    def test_leaves_unfenced_json_alone(self):
        self.assertEqual(so._strip_code_fence('[{"a": 1}]'), '[{"a": 1}]')

    def test_does_not_touch_the_middle_of_the_payload(self):
        """A name containing the fence's language tag must survive intact."""
        payload = '```json\n[{"name": "json\\nweird"}]\n```'
        self.assertIn('json\\nweird', so._strip_code_fence(payload))

    def test_handles_empty_input(self):
        self.assertEqual(so._strip_code_fence(""), "")
        self.assertEqual(so._strip_code_fence(None), "")


class DeadCodeTest(unittest.TestCase):
    def test_gemini_summary_is_gone(self):
        """50 lines, never imported, and containing no LLM call despite the
        name -- the actual Gemini call lives in squad_override.py."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertFalse(os.path.exists(os.path.join(root, "gemini_summary.py")))


if __name__ == "__main__":
    unittest.main()
