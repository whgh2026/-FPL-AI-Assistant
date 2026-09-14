"""Tier 1.3 gate: the roadmap's armband obeys the same rule as the real XI.

`_select_captaincy` applies CAPTAINCY_SAFE_POSITIONS with a
CAPTAINCY_OVERRIDE_MARGIN escape hatch, because a defender's or keeper's
biggest weeks are clean-sheet dependent -- one binary event a single
added-time concession erases -- while MID/FWD have several independent routes
to points. `build_transfer_gantt_data` reimplemented captaincy as "top two by
xP" with no positional guard at all, so a manager whose two best assets were
both defenders saw a D-D armband pair on the roadmap: both halves exposed to
the same goal conceded, and the roadmap disagreeing with the lineup the app
itself would pick for the same gameweek.
"""

import unittest

import fpl_tools

MARGIN = fpl_tools.CAPTAINCY_OVERRIDE_MARGIN


def _squad(rows):
    return [{"name": n, "position": p} for n, p, _xp in rows]


def _gantt(rows, gw=5):
    squad = _squad(rows)
    schedule = [{"gw": gw, "chip": None, "buys": [], "sells": []}]
    return fpl_tools.build_transfer_gantt_data(
        squad, schedule,
        xp_lookup={n: xp for n, _p, xp in rows},
        position_lookup={n: p for n, p, _xp in rows},
    )["captains"][gw]


# A full fifteen so the top-eleven slice is actually exercised.
def _fifteen(def1_xp, def2_xp, best_attacker_xp):
    return [
        ("D1", "DEF", def1_xp), ("D2", "DEF", def2_xp),
        ("M1", "MID", best_attacker_xp), ("M2", "MID", best_attacker_xp - 0.4),
        ("F1", "FWD", best_attacker_xp - 0.8), ("F2", "FWD", 3.2),
        ("D3", "DEF", 3.0), ("D4", "DEF", 2.8), ("D5", "DEF", 2.6),
        ("M3", "MID", 2.4), ("M4", "MID", 2.2), ("M5", "MID", 2.0),
        ("F3", "FWD", 1.8), ("G1", "GK", 1.6), ("G2", "GK", 1.4),
    ]


class GanttCaptaincyTest(unittest.TestCase):

    def test_a_double_defence_stack_does_not_produce_a_def_def_armband(self):
        """The reported failure mode. Both defenders out-project every
        attacker, but neither clears the margin, so the armband must default
        to the best attacker and the vice must not be the naive runner-up."""
        caps = _gantt(_fifteen(6.0, 5.8, 5.0))
        self.assertEqual(caps["captain"], "M1")
        self.assertNotEqual(caps["vice_captain"], caps["captain"])

    def test_the_margin_override_still_fires_inside_the_gantt(self):
        """Past CAPTAINCY_OVERRIDE_MARGIN the raw projection wins, and the
        vice anchors to the best attacker so the two picks are not both
        clean-sheet dependent."""
        caps = _gantt(_fifteen(5.0 + MARGIN + 1.0, 5.5, 5.0))
        self.assertEqual(caps["captain"], "D1")
        self.assertEqual(caps["vice_captain"], "M1")

    def test_the_override_does_not_fire_below_the_margin(self):
        caps = _gantt(_fifteen(5.0 + MARGIN - 0.3, 4.0, 5.0))
        self.assertEqual(caps["captain"], "M1")

    def test_the_armband_comes_from_the_top_eleven_only(self):
        """_select_captaincy maxes over whatever list it is handed, and
        `pool_this_week` is the FIFTEEN -- so without the slice the roadmap
        could crown a player who would never be started."""
        rows = _fifteen(3.0, 2.9, 6.0)
        caps = _gantt(rows)
        top11 = {n for n, _p, _x in sorted(rows, key=lambda r: -r[2])[:11]}
        self.assertIn(caps["captain"], top11)
        self.assertIn(caps["vice_captain"], top11)

    def test_the_roadmap_agrees_with_the_live_xi_selector(self):
        """The two must not disagree about the same gameweek. Runs the same
        players through select_starting_xi and compares the armband."""
        rows = _fifteen(6.0, 5.8, 5.0)
        caps = _gantt(rows)
        squad = [{"player_id": n, "name": n, "position": p, "xp": xp}
                 for n, p, xp in rows]
        xi = fpl_tools.select_starting_xi(squad)
        self.assertEqual(caps["captain"], xi["captain"]["name"],
                         "roadmap and lineup disagree on the captain")

    def test_a_squad_with_no_attacker_at_all_still_returns_an_armband(self):
        """Degenerate input must not crash or return None: _select_captaincy
        falls back to the outright best when there is no MID/FWD to prefer."""
        rows = [(f"D{i}", "DEF", 5.0 - i * 0.1) for i in range(11)]
        caps = _gantt(rows)
        self.assertIsNotNone(caps["captain"])


if __name__ == "__main__":
    unittest.main()
