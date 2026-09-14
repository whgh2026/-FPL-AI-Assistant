"""Tier 7 gate: the audit-derived items that reproduced against real source.

Every claim in this file was verified before being patched. The ones that did
NOT reproduce are recorded in UNVERIFIED_CLAIMS.md rather than "fixed".
"""

import unittest

import fpl_tools
from tests import harness


class BlankCountTest(unittest.TestCase):
    """`n_teams = len(fixture_lookup) or 20`. The lookup is keyed by team and
    only carries clubs with at least one fixture in the loaded range, so a club
    blanking across the WHOLE range has no key -- it silently left the
    denominator. `blanks = n_teams - playing` therefore under-reported exactly
    the gameweeks with the most blanks, and that figure gates the Free Hit
    emergency override: the worse the blank week, the more it was understated.
    """

    def test_a_club_absent_from_the_lookup_still_counts_as_blanking(self):
        with harness.synthetic_world() as (bs, fx):
            n_clubs = len(bs["teams"])
            # A lookup holding only two clubs, each with one fixture in GW5.
            partial = {
                1: [{"event": 5, "is_home": True, "opponent": 2}],
                2: [{"event": 5, "is_home": False, "opponent": 1}],
            }
            cal = fpl_tools._fixture_calendar(partial, start_event=5, n=1)
        self.assertEqual(cal[5]["playing"], 2)
        self.assertEqual(
            cal[5]["blanks"], n_clubs - 2,
            "clubs missing from the lookup were dropped from the denominator")

    def test_the_denominator_is_the_real_club_count(self):
        with harness.synthetic_world() as (bs, fx):
            n_clubs = len(bs["teams"])
            lookup = fpl_tools._build_fixture_lookup(bs, fixtures_override=fx)
            cal = fpl_tools._fixture_calendar(lookup, start_event=1, n=1)
        self.assertEqual(cal[1]["playing"] + cal[1]["blanks"], n_clubs)


class EpNextHorizonTest(unittest.TestCase):
    """FPL publishes ep_next for the NEXT gameweek only -- one number, not a
    series. _player_xp_horizon called _player_xp_raw once per week and every
    one of those blended the SAME ep_next, pinning a constant onto weeks it
    says nothing about and flattening the fixture sensitivity that is the
    entire reason for projecting a horizon.

    The comment inside _player_xp_raw has described this defect for several
    stages without anything acting on it.
    """

    def _player(self, ep):
        return {"id": 700, "team": 1, "element_type": 3, "status": "a",
                "minutes": 90, "starts": 1, "ep_next": ep,
                "expected_goals_per_90": 0.3, "expected_assists_per_90": 0.2,
                "chance_of_playing_next_round": None}

    def test_ep_next_is_suppressed_after_the_first_week(self):
        with harness.synthetic_world() as (bs, fx):
            lookup = fpl_tools._build_fixture_lookup(bs, fixtures_override=fx)
            p = self._player(12.0)          # far above anything the model gives
            with_ep, _ = fpl_tools._player_xp_raw(p, lookup, 1, blend_ep=True)
            without, _ = fpl_tools._player_xp_raw(p, lookup, 1, blend_ep=False)
        self.assertGreater(with_ep, without,
                           "fixture cannot discriminate: ep_next is not moving the number")

    def test_the_horizon_only_pays_the_blend_once(self):
        """Composed directly: the horizon must equal w0 * (week with ep) plus
        the remaining weights against weeks WITHOUT it."""
        with harness.synthetic_world() as (bs, fx):
            lookup = fpl_tools._build_fixture_lookup(bs, fixtures_override=fx)
            p = self._player(12.0)
            n = 4
            weights = fpl_tools.HORIZON_WEIGHTS[:n]
            expected = 0.0
            for i, w in enumerate(weights):
                raw, _ = fpl_tools._player_xp_raw(p, lookup, 1 + i,
                                                  blend_ep=(i == 0))
                expected += w * raw
            got, _ = fpl_tools._player_xp_horizon(p, lookup, 1, n=n)
        # _player_xp_horizon rounds its return to two decimals for display, so
        # the composition is compared at that resolution rather than pretending
        # to bit-equality.
        self.assertAlmostEqual(got, round(expected, 2), places=6)

    def test_the_single_gameweek_projection_still_blends(self):
        """The blend is correct for the week FPL actually forecast. Removing it
        there would be a different bug, not a fix."""
        with harness.synthetic_world() as (bs, fx):
            lookup = fpl_tools._build_fixture_lookup(bs, fixtures_override=fx)
            hi, _ = fpl_tools._player_xp_raw(self._player(12.0), lookup, 1)
            lo, _ = fpl_tools._player_xp_raw(self._player(0.5), lookup, 1)
        self.assertGreater(hi, lo, "_player_xp_raw stopped blending ep_next")


if __name__ == "__main__":
    unittest.main()
