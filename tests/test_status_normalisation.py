"""P0 remediation, Task 1: player status normalisation.

Two vocabularies for the same concept coexist in this codebase by design:

  * the OFFICIAL FPL status codes ('a', 'i', 's', 'u', 'n', 'd'), read
    straight off the bootstrap, which _player_xp_raw and _player_xp_horizon
    branch on;
  * a richer, human-readable NOTE vocabulary ("Injured", "Suspended",
    "50% Chance", "No minutes", "Blank", ...) that _pool_entry stamps onto
    every pool/squad entry as "status", used for display and for the
    solver's own out-status/dead-money checks (current_out_statuses,
    _NON_PLAYING_NOTES) further down the pipeline.

These are genuinely different domains -- "No minutes" and "Blank" describe a
minutes history and a fixture respectively, neither of which is an FPL status
flag at all -- so the note-domain checks are NOT touched here; forcing them
through a raw-code normaliser would silently break them (there is no raw
code for "the club has no fixture this gameweek"). What IS fixed is every
point that reads a RAW code and used to do so by direct string comparison:
a display string reaching one of those by accident used to fall through
silently to "available" instead of being recognised as out.
"""

import copy
import unittest

import fpl_tools
from tests import harness

EVENT = 4


class NormaliseStatusTest(unittest.TestCase):
    def test_raw_codes_pass_through_unchanged(self):
        for code in ("a", "i", "s", "u", "n", "d"):
            self.assertEqual(fpl_tools.normalise_status(code), code)

    def test_display_strings_map_to_the_right_code(self):
        cases = {
            "Available": "a",
            "Injured": "i",
            "Suspended": "s",
            "Unavailable": "u",
            "OUT": "u",
            "Doubtful": "d",
        }
        for text, expected in cases.items():
            self.assertEqual(fpl_tools.normalise_status(text), expected,
                             f"{text!r} should normalise to {expected!r}")

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(fpl_tools.normalise_status(" Injured "), "i")
        self.assertEqual(fpl_tools.normalise_status("INJURED"), "i")
        self.assertEqual(fpl_tools.normalise_status("I"), "i")

    def test_notes_that_are_not_status_flags_stay_available(self):
        """"No minutes" is a minutes-history note and "Blank" a fixture
        note -- neither is an FPL status flag, so a player carrying either
        must not be folded into an out-status count via this function."""
        self.assertEqual(fpl_tools.normalise_status("No minutes"), "a")
        self.assertEqual(fpl_tools.normalise_status("Blank"), "a")

    def test_unrecognised_and_missing_default_to_available(self):
        """Matches _player_xp_raw's own pre-existing default for a missing
        status -- this must not become a stricter (or looser) gate."""
        self.assertEqual(fpl_tools.normalise_status(None), "a")
        self.assertEqual(fpl_tools.normalise_status(""), "a")
        self.assertEqual(fpl_tools.normalise_status("some future FPL flag"), "a")

    def test_percentage_chance_notes_are_not_status_flags(self):
        """A "62% Chance" note is a _player_xp_horizon rationale string, not
        an official status code -- it must not collapse to a hard out."""
        self.assertEqual(fpl_tools.normalise_status("62% Chance"), "a")


class HardenedIngestionTest(unittest.TestCase):
    """The raw-code ingestion points now normalise on entry, so a display
    string reaching them behaves identically to the raw code it describes --
    where before it would have silently fallen through to "available"."""

    def _mid(self, bs):
        return copy.deepcopy(next(e for e in bs["elements"] if e["element_type"] == 3))

    def test_player_xp_raw_treats_the_display_string_the_same_as_the_code(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            base = self._mid(bs)
            coded, hoped = copy.deepcopy(base), copy.deepcopy(base)
            coded["status"] = "i"
            hoped["status"] = "Injured"     # a display string in the raw slot
            xp_code, note_code = fpl_tools._player_xp_raw(coded, lookup, event=EVENT)
            xp_note, note_note = fpl_tools._player_xp_raw(hoped, lookup, event=EVENT)
            self.assertEqual((xp_code, note_code), (0.0, "Injured"))
            self.assertEqual((xp_note, note_note), (0.0, "Injured"),
                             "a display-string status must still zero the projection")

    def test_player_xp_horizon_treats_the_display_string_the_same_as_the_code(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            base = self._mid(bs)
            coded, susp = copy.deepcopy(base), copy.deepcopy(base)
            coded["status"] = "s"
            susp["status"] = "Suspended"
            self.assertEqual(fpl_tools._player_xp_horizon(coded, lookup, EVENT),
                             fpl_tools._player_xp_horizon(susp, lookup, EVENT))

    def test_calibration_features_treats_the_display_string_the_same_as_the_code(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            base = self._mid(bs)
            coded, unav = copy.deepcopy(base), copy.deepcopy(base)
            coded["status"] = "u"
            unav["status"] = "Unavailable"
            zero = {"raw_total": 0.0, "xp_cameo": 0.0, "ep_w": 0.0, "ep_term": 0.0,
                    "cameo_mass": 0.0, "rotation_variance": 0.0}
            self.assertEqual(fpl_tools.calibration_features(coded, lookup, event=EVENT), zero)
            self.assertEqual(fpl_tools.calibration_features(unav, lookup, event=EVENT), zero)

    def test_minute_distribution_treats_the_display_string_the_same_as_the_code(self):
        p = {"element_type": 3, "team": 1, "starts": 5, "minutes": 450}
        self.assertEqual(fpl_tools._minute_distribution(p, "i"), (1.0, 0.0, 0.0))
        self.assertEqual(fpl_tools._minute_distribution(p, "Injured"), (1.0, 0.0, 0.0))

    def test_rank_players_by_xp_excludes_the_display_string_the_same_as_the_code(self):
        with harness.synthetic_world() as (bs, _fx):
            target = next(e for e in bs["elements"] if e["element_type"] == 3)
            target["first_name"], target["second_name"] = "Zdisplaystring", "Excludedtest"
            target["status"] = "Unavailable"     # display string, not the raw "u"
            ranked = fpl_tools.rank_players_by_xp(event=EVENT, limit=999)
            names = {r["name"] for r in ranked["players"]}
            self.assertNotIn("Zdisplaystring Excludedtest", names,
                             "a display-string 'Unavailable' status must be excluded "
                             "exactly like the raw 'u' code already is")


if __name__ == "__main__":
    unittest.main()
