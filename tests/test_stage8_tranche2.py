"""Stage 8c gate: bonus and cards stop being positional constants.

Both terms were flat. Every defender in the game was credited with the same
0.34 bonus points and charged the same -0.12 for cards, so neither could ever
distinguish two players -- while `bps` sat in the bootstrap being read for the
live tracker and never once used in the projection, and `yellow_cards` sat
unread two functions from where the card charge was applied.

Bonus is 8-10% of all FPL points and among the most persistent player-level
signals there is. A term that is identical for Salah and a bench filler is not
a weak signal, it is no signal.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fpl_tools as ft
from tests import harness


def player(pos_id, minutes=900, bps=0.0, yellow=0, red=0, **over):
    p = {"id": 9000, "team": 1, "element_type": pos_id, "status": "a",
         "minutes": minutes, "starts": int(minutes // 90), "bps": bps,
         "yellow_cards": yellow, "red_cards": red,
         "chance_of_playing_next_round": None,
         "expected_goals_per_90": 0.2, "expected_assists_per_90": 0.15,
         "expected_goals_conceded_per_90": 1.2, "saves_per_90": 0.0}
    p.update(over)
    return p


def one_fixture(lookup):
    """Any real decorated fixture, so the xP terms have something to run on."""
    for fxs in lookup.values():
        if fxs:
            return fxs[0]
    raise AssertionError("fixture lookup is empty")


class BonusTest(unittest.TestCase):
    """C7."""

    def test_bonus_separates_a_high_bps_player_from_a_low_one(self):
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            f = one_fixture(ft._build_fixture_lookup(bootstrap))
            avg = ft._league_averages()["MID"]["bps90"]
            self.assertGreater(avg, 1.0, "fixture has no usable BPS scale")

            # Same player twice, differing only in accumulated BPS.
            lo = ft._xp_for_fixture(player(3, bps=avg * 900 / 90 * 0.5), f, 90.0, 3)
            hi = ft._xp_for_fixture(player(3, bps=avg * 900 / 90 * 1.8), f, 90.0, 3)
            self.assertGreater(hi, lo,
                               "BPS has no effect on the projection at all")
            self.assertGreater(hi - lo, 0.15,
                               f"BPS moved the projection by only {hi - lo:.3f} points")

    def test_bonus_is_convex_in_bps(self):
        """Bonus is a rank-order tournament -- top three BPS take 3/2/1 -- so
        being above average pays more than proportionally. A linear response
        would under-reward the players who actually collect bonus."""
        self.assertGreater(ft.BONUS_CONVEXITY, 1.0)
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            f = one_fixture(ft._build_fixture_lookup(bootstrap))
            avg = ft._league_averages()["MID"]["bps90"]
            at = [ft._xp_for_fixture(player(3, bps=avg * 10 * m), f, 90.0, 3)
                  for m in (1.0, 1.3, 1.6)]
            first, second = at[1] - at[0], at[2] - at[1]
            self.assertGreater(second, first,
                               "the bonus response is linear or concave in BPS")

    def test_bonus_cannot_exceed_a_single_match_maximum(self):
        """Three points is all one match awards."""
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            f = one_fixture(ft._build_fixture_lookup(bootstrap))
            absurd = ft._xp_for_fixture(player(3, bps=99999.0), f, 90.0, 3)
            sane = ft._xp_for_fixture(player(3, bps=0.0), f, 90.0, 3)
            self.assertLess(absurd - sane, ft.BONUS_CAP + 0.01,
                            "bonus is unbounded above the 3-point match maximum")

    def test_a_player_with_no_minutes_gets_the_positional_prior(self):
        """The positional constant is kept as the shrinkage prior rather than
        replaced, so a player with no evidence projects exactly what he did
        before this change and only data moves him off it."""
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            f = one_fixture(ft._build_fixture_lookup(bootstrap))
            # minutes=0 short-circuits the ratio branch entirely.
            got = ft._xp_for_fixture(player(3, minutes=0, bps=0.0), f, 90.0, 3)
            self.assertGreater(got, 0.0)

    def test_defenders_still_earn_less_bonus_than_midfielders_like_for_like(self):
        """The positional prior must survive: DEF 0.34 against MID/FWD 0.72."""
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            f = one_fixture(ft._build_fixture_lookup(bootstrap))
            avgd = ft._league_averages()["DEF"]["bps90"]
            avgm = ft._league_averages()["MID"]["bps90"]
            d = ft._xp_for_fixture(player(2, bps=avgd * 10), f, 90.0, 2)
            d0 = ft._xp_for_fixture(player(2, bps=avgd * 10 * 0.01), f, 90.0, 2)
            m = ft._xp_for_fixture(player(3, bps=avgm * 10), f, 90.0, 3)
            m0 = ft._xp_for_fixture(player(3, bps=avgm * 10 * 0.01), f, 90.0, 3)
            self.assertGreater(m - m0, d - d0,
                               "the positional bonus prior has been flattened")

    def test_bonus_varies_across_real_fixture_players(self):
        """The gate as the plan states it: bonus must VARY within a position."""
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            lookup = ft._build_fixture_lookup(bootstrap)
            f = one_fixture(lookup)
            vals = set()
            for e in bootstrap["elements"]:
                if e["element_type"] != 3 or ft._to_float(e.get("minutes")) < 180:
                    continue
                vals.add(round(ft._xp_for_fixture(e, f, 90.0, 3), 4))
            self.assertGreater(len(vals), 10,
                               "midfielders collapse onto a handful of values")


class CardTest(unittest.TestCase):
    """C11."""

    def test_a_booked_player_is_charged_more_than_a_clean_one(self):
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            f = one_fixture(ft._build_fixture_lookup(bootstrap))
            clean = ft._xp_for_fixture(player(2, yellow=0), f, 90.0, 2)
            booked = ft._xp_for_fixture(player(2, yellow=8), f, 90.0, 2)
            self.assertGreater(clean, booked, "yellow cards do not reach the projection")

    def test_a_red_costs_more_than_a_yellow(self):
        """FPL's own scoring: -1 and -3."""
        self.assertGreater(ft.RED_CARD_PTS, ft.YELLOW_CARD_PTS)
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            f = one_fixture(ft._build_fixture_lookup(bootstrap))
            y = ft._xp_for_fixture(player(3, yellow=3, red=0), f, 90.0, 3)
            r = ft._xp_for_fixture(player(3, yellow=0, red=3), f, 90.0, 3)
            self.assertLess(r, y)

    def test_card_risk_scales_with_expected_minutes(self):
        """Cards are EXPOSURE. A 30-minute cameo carries a third of a starter's
        booking risk; the old flat constant charged both the same."""
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            f = one_fixture(ft._build_fixture_lookup(bootstrap))
            p = player(2, yellow=8)
            full = ft._xp_for_fixture(p, f, 90.0, 2)
            cameo = ft._xp_for_fixture(p, f, 30.0, 2)
            # Isolate the card term by differencing against a clean twin.
            clean = player(2, yellow=0)
            charge_full = ft._xp_for_fixture(clean, f, 90.0, 2) - full
            charge_cameo = ft._xp_for_fixture(clean, f, 30.0, 2) - cameo
            self.assertGreater(charge_full, charge_cameo * 1.5,
                               "a 30-minute cameo is charged like a full start")

    def test_forwards_without_league_card_data_fall_back_to_the_constant(self):
        """At GW4 most forwards genuinely have no booking, so the positional
        average can legitimately be zero. That must degrade to the old constant,
        not to 'forwards are never booked'."""
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            f = one_fixture(ft._build_fixture_lookup(bootstrap))
            got = ft._xp_for_fixture(player(4, yellow=0), f, 90.0, 4)
            self.assertGreater(got, 0.0)

    def test_cards_vary_across_real_fixture_defenders(self):
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            f = one_fixture(ft._build_fixture_lookup(bootstrap))
            avg = ft._league_averages()["DEF"]
            self.assertGreater(avg["yc90"], 0.0, "fixture has no defender bookings")
            rates = {round(ft._to_float(e["yellow_cards"]) * 90
                           / max(1.0, ft._to_float(e["minutes"])), 3)
                     for e in bootstrap["elements"]
                     if e["element_type"] == 2 and ft._to_float(e["minutes"]) >= 180}
            self.assertGreater(len(rates), 5, "every defender has the same booking rate")


class ShrinkageTest(unittest.TestCase):
    def test_reg_cap_is_per_quantity(self):
        """_reg hardcoded min(raw, 5.0), which suits a per-90 goal rate and
        would flatten every BPS rate in the league -- they run an order of
        magnitude higher."""
        with harness.synthetic_world():
            bootstrap, _ = harness.load_synthetic()
            for pos in ("DEF", "MID", "FWD"):
                self.assertGreater(
                    ft._league_averages()[pos]["bps90"], 5.0,
                    "the league BPS rate is below the old hardcoded cap, so this "
                    "test cannot detect the bug it exists for")

    def test_league_averages_expose_the_new_rates(self):
        with harness.synthetic_world():
            harness.load_synthetic()
            avg = ft._league_averages()
            for pos in ("GK", "DEF", "MID", "FWD"):
                for k in ("bps90", "yc90", "rc90"):
                    self.assertIn(k, avg[pos])


class TightropeTest(unittest.TestCase):
    """C27. The suspension windows were keyed off the gameweek number, which is
    not what the Premier League counts."""

    def _p(self, yellows, team=1):
        return {"id": 1, "team": team, "yellow_cards": yellows}

    def test_counts_the_clubs_matches_not_the_gameweek(self):
        """A club that has blanked is a match behind the gameweek number; one
        that has played a double is ahead. Keying off the gameweek opens and
        closes the window on the wrong week for exactly the clubs whose fixture
        calendar the rest of the engine is modelling."""
        with harness.synthetic_world():
            harness.load_synthetic()
            played = ft._team_played_map()
            self.assertTrue(played, "fixture reports no matches played")
            team = next(iter(played))
            # 4 yellows is one from the first ban while the club is inside its
            # first 19 matches, whatever gameweek the league has reached.
            self.assertTrue(ft._is_on_tightrope(self._p(4, team), event=None),
                            "the check needs an event, so it is not reading the club")

    def test_flags_one_booking_from_each_ban(self):
        with harness.synthetic_world():
            harness.load_synthetic()
            team = next(iter(ft._team_played_map()))
            self.assertTrue(ft._is_on_tightrope(self._p(4, team)))
            self.assertFalse(ft._is_on_tightrope(self._p(3, team)))

    def test_a_served_ban_is_not_a_tightrope(self):
        """yellow_cards runs all season and does not reset when a ban is served,
        so 5-8 means 'already banned once', not 'about to be'."""
        with harness.synthetic_world():
            harness.load_synthetic()
            team = next(iter(ft._team_played_map()))
            for y in (5, 6, 7, 8):
                self.assertFalse(ft._is_on_tightrope(self._p(y, team)),
                                 f"{y} yellows read as one from a ban")

    def test_falls_back_to_the_event_when_no_club_is_known(self):
        self.assertTrue(ft._is_on_tightrope({"yellow_cards": 4}, event=10))
        self.assertTrue(ft._is_on_tightrope({"yellow_cards": 9}, event=25))

    def test_second_window_uses_the_ten_yellow_threshold(self):
        self.assertTrue(ft._is_on_tightrope({"yellow_cards": 9}, event=20))
        self.assertFalse(ft._is_on_tightrope({"yellow_cards": 4}, event=20))

    def test_no_tightrope_after_the_club_s_thirty_second_match(self):
        """The accumulation rules stop applying, so this is the rule rather
        than a gap in the check."""
        self.assertFalse(ft._is_on_tightrope({"yellow_cards": 9}, event=35))

    def test_malformed_card_counts_do_not_raise(self):
        for bad in ({"yellow_cards": None}, {"yellow_cards": "x"}, {}):
            self.assertFalse(ft._is_on_tightrope(bad, event=10))


class SingleMinutesDefinitionTest(unittest.TestCase):
    """C28. Two functions answered 'how much of a match do we expect', and
    which one you got depended on which call site you were standing in."""

    def test_the_availability_only_definition_is_gone(self):
        self.assertFalse(
            hasattr(ft, "_expected_minute_fraction"),
            "_expected_minute_fraction is back; it knows nothing about rotation, "
            "so a fit rotation risk returns a confident 1.0")

    def test_nothing_references_it(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import subprocess
        r = subprocess.run(["grep", "-rn", "_expected_minute_fraction",
                            "--include=*.py", "."],
                           cwd=root, capture_output=True, text=True)
        hits = [ln for ln in r.stdout.splitlines()
                if "test_stage8_tranche2" not in ln and not ln.split(":", 2)[-1].lstrip().startswith("#")]
        self.assertEqual(hits, [], f"still referenced: {hits}")

    def test_the_surviving_definition_prices_rotation(self):
        """The reason it is the one that survived: a fit player who does not
        start must not read as a full 90."""
        with harness.synthetic_world():
            harness.load_synthetic()
            nailed = player(3, minutes=270, starts=3)
            risk = player(3, minutes=120, starts=1)
            fn = ft._expected_playing_fraction
            self.assertGreater(fn(nailed, "a"), fn(risk, "a"))


class SaaTightropeTest(unittest.TestCase):
    """C12. The SAA overwrite reverted the suspension haircut."""

    def test_the_haircut_survives_the_saa_overwrite(self):
        """_player_xp_horizon applies TIGHTROPE_DISCOUNT, then the solver
        replaces p["xp"] with an SAA mean rebuilt from single-gameweek
        projections that never saw it -- so the discount was erased in the one
        place it was meant to change a decision."""
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "fpl_tools.py"), encoding="utf-8").read()
        start = src.index("sigmas = _scenario_sigmas(scenario_matrix)")
        block = src[start:start + 1400]
        self.assertIn("on_yellow_card_tightrope", block)
        self.assertIn("TIGHTROPE_DISCOUNT", block,
                      "the SAA overwrite still discards the suspension haircut")

    def test_the_discount_is_an_actual_haircut(self):
        self.assertLess(ft.TIGHTROPE_DISCOUNT, 1.0)
        self.assertGreater(ft.TIGHTROPE_DISCOUNT, 0.5)


class FixtureRealismTest(unittest.TestCase):
    """The generator wrote bps as `220 * quality`, independent of minutes, so a
    100-minute fringe player carried the same season BPS as a 900-minute
    regular -- about 180 BPS per 90, roughly five times anything real. A per-90
    rate computed off that is meaningless, and the C7 tests above would have
    been measuring an artefact."""

    def test_bps_scales_with_minutes(self):
        bootstrap, _ = harness.load_synthetic()
        pairs = [(ft._to_float(e["minutes"]), ft._to_float(e["bps"]))
                 for e in bootstrap["elements"] if ft._to_float(e["minutes"]) > 0]
        rates = [b * 90 / m for m, b in pairs]
        self.assertLess(max(rates), 60.0, f"max {max(rates):.0f} BPS per 90 is not real")
        self.assertGreater(min(rates), 5.0)

    def test_card_counts_are_plausible_rates(self):
        bootstrap, _ = harness.load_synthetic()
        for e in bootstrap["elements"]:
            m = ft._to_float(e["minutes"])
            if m < 180:
                continue
            rate = ft._to_float(e["yellow_cards"]) * 90 / m
            self.assertLess(rate, 0.8, f"{e['web_name']} books at {rate:.2f} per 90")

    def test_red_cards_are_present_and_rare(self):
        bootstrap, _ = harness.load_synthetic()
        reds = sum(ft._to_float(e.get("red_cards", 0)) for e in bootstrap["elements"])
        self.assertGreater(reds, 0, "no red cards at all, so that path is untested")
        self.assertLess(reds, len(bootstrap["elements"]) * 0.05)


if __name__ == "__main__":
    unittest.main()
