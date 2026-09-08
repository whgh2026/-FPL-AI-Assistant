"""Stage 5 gate: the scenario axis, and telling a blank apart from a bad fixture.

_generate_scenarios always produced an (S, n) array -- samples down the rows,
gameweeks across -- which is the standard Monte Carlo orientation. Its own
docstring advertised the transpose, and BOTH consumers indexed it that way, so
`S` resolved to 6 and the loop ran over the first six SCENARIOS instead of the
gameweeks. The "500 sims" in the UI were six, and _select_stress_scenarios could
only ever return 6 of its requested 50, feeding the CVaR term six gameweek slots
dressed as scenarios.

The generator was never the bug, which is why the fix is in the consumers, the
docstring and the non-numpy fallback -- not a transpose at the source.
"""

import unittest

import fpl_tools
from tests import harness

EVENT = 4


def _matrix(bs, lookup, count=40):
    ids = [e["id"] for e in bs["elements"][:count]]
    return ids, fpl_tools._generate_scenarios(ids, lookup, EVENT)[1]


class LayoutTest(unittest.TestCase):
    def test_matrix_is_samples_by_gameweeks(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            ids, matrix = _matrix(bs, lookup)
            for pid in ids[:5]:
                self.assertEqual(matrix[pid].shape,
                                 (fpl_tools.SAA_SCENARIOS, fpl_tools.PLAN_HORIZON))

    def test_distribution_consumes_every_scenario(self):
        """The headline: 500, not 6."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            ids, matrix = _matrix(bs, lookup)
            dist = fpl_tools._scenario_distribution(ids[:15], matrix)
            self.assertEqual(dist["scenarios"], fpl_tools.SAA_SCENARIOS)

    def test_quantiles_are_ordered(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            ids, matrix = _matrix(bs, lookup)
            d = fpl_tools._scenario_distribution(ids[:15], matrix)
            self.assertLessEqual(d["p5"], d["p50"])
            self.assertLessEqual(d["p50"], d["p95"])
            self.assertLess(d["p5"], d["p95"], "distribution has collapsed to a point")

    def test_spread_reflects_the_injected_variance(self):
        """A known spread on the scenario axis must come back as that spread.

        This is the assertion the old code could not pass: reading down the
        gameweek axis would have returned the variation between gameweeks
        instead.
        """
        import numpy as np
        S, n = 400, 4
        rng = np.random.default_rng(0)
        # One player, zero gameweek-to-gameweek variation, large scenario spread.
        per_scenario = rng.normal(10.0, 3.0, size=S)
        arr = np.repeat(per_scenario[:, None], n, axis=1)
        d = fpl_tools._scenario_distribution([1], {1: arr}, weights=[1, 0, 0, 0], n=n)
        self.assertEqual(d["scenarios"], S)
        self.assertAlmostEqual(d["p50"], float(np.percentile(per_scenario, 50)), places=2)
        self.assertGreater(d["p95"] - d["p5"], 5.0, "scenario spread was not seen")

    def test_stress_pool_returns_the_requested_count(self):
        """K=50 could only ever return 6 while the axes were transposed."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            ids, matrix = _matrix(bs, lookup)
            out = fpl_tools._select_stress_scenarios(matrix, K=50, selected_ids=ids[:15])
            self.assertEqual(len(next(iter(out.values()))), 50)

    def test_stress_pool_ranks_on_the_selected_squad(self):
        """The tail must be the portfolio's, not the whole candidate pool's."""
        import numpy as np
        S = 200
        rng = np.random.default_rng(1)
        held = {i: rng.normal(5.0, 2.0, size=(S, 1)) for i in range(1, 6)}
        noise = {i: rng.normal(5.0, 2.0, size=(S, 1)) for i in range(100, 140)}
        matrix = {**held, **noise}
        out = fpl_tools._select_stress_scenarios(matrix, K=20, selected_ids=list(held))

        held_tot = sum(held[i][:, 0] for i in held)
        worst = set(np.argsort(held_tot)[:20].tolist())
        got = [float(v) for v in out[1]]
        self.assertEqual(sorted(got), sorted(float(held[1][s, 0]) for s in worst))

    def test_fallback_uses_the_same_orientation(self):
        """The non-numpy path built (n, 1) -- the opposite convention -- which is
        how the two code paths disagreed for so long."""
        saved = fpl_tools.HAS_NUMPY
        fpl_tools.HAS_NUMPY = False
        try:
            with harness.synthetic_world() as (bs, _fx):
                lookup = fpl_tools._build_fixture_lookup(bs)
                ids = [e["id"] for e in bs["elements"][:5]]
                _mean, matrix = fpl_tools._generate_scenarios(ids, lookup, EVENT)
                for pid in ids:
                    rows = matrix[pid]
                    self.assertEqual(len(rows), 1, "one degenerate scenario")
                    self.assertEqual(len(rows[0]), fpl_tools.PLAN_HORIZON)
        finally:
            fpl_tools.HAS_NUMPY = saved


class FixtureCalendarTest(unittest.TestCase):
    def test_detects_the_planted_blank_and_double(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            cal = fpl_tools._fixture_calendar(lookup, 1, 12)
            self.assertTrue(cal[9]["is_bgw"], f"GW9 should be a blank: {cal[9]}")
            self.assertTrue(cal[10]["is_dgw"], f"GW10 should be a double: {cal[10]}")
            for ev in (1, 2, 3, 11, 12):
                self.assertFalse(cal[ev]["is_bgw"])
                self.assertFalse(cal[ev]["is_dgw"])

    def test_blank_is_labelled_distinctly_from_poor_form(self):
        """Both project 0.0 for the gameweek, so without the label a premium
        with no fixture is indistinguishable from a player who is simply bad --
        and gets sold."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            blanker = next(e for e in bs["elements"] if e["team"] <= 8)
            xp, note = fpl_tools._player_xp(blanker, lookup, event=9)
            self.assertEqual(xp, 0.0)
            self.assertEqual(note, "Blank")


class DoubleGameweekConsumersTest(unittest.TestCase):
    """Three call sites used next(...) and saw only the first fixture of a
    double, while _player_xp_raw correctly summed both -- so the projection and
    everything shown beside it disagreed about how many games a club had."""

    def test_gw_fixtures_returns_both_legs(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            doubling = [tid for tid in lookup
                        if len(fpl_tools._gw_fixtures(lookup[tid], 10)) > 1]
            self.assertGreaterEqual(len(doubling), 4)

    def test_fdr_strip_rates_a_double_above_the_same_single(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            doubler = next(tid for tid in lookup
                           if len(fpl_tools._gw_fixtures(lookup[tid], 10)) > 1)
            single = next(tid for tid in lookup
                          if len(fpl_tools._gw_fixtures(lookup[tid], 10)) == 1)
            p_d = {"team": doubler}
            p_s = {"team": single}
            d = fpl_tools._player_fdr_list(p_d, lookup, 10, n=1)[0]
            s = fpl_tools._player_fdr_list(p_s, lookup, 10, n=1)[0]
            self.assertGreater(d, 0.0)
            self.assertGreaterEqual(d, s * 0.9)   # the double is not undercounted

    def test_traffic_lights_mark_blanks_and_doubles(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            blanker = next(tid for tid in lookup
                           if not fpl_tools._gw_fixtures(lookup[tid], 9))
            lights = fpl_tools._fixture_traffic_lights(blanker, lookup, 9, n=2)
            self.assertIn("⚪", lights, "a blank must be visibly distinct")
            self.assertIn("🔵", lights, "a double must be visibly distinct")


class FixtureTrafficLightBandsTest(unittest.TestCase):
    """_fixture_traffic_light_bands: the structured banding data the mobile
    UI overhaul renders as real circular badges instead of emoji glyphs (see
    _pitch_fixture_dots_html). One source of truth -- _fixture_traffic_lights
    must build its emoji string from exactly these bands, never a separately
    computed banding, or the two renderers could silently disagree about
    what a given fixture rates."""

    def test_always_exactly_n_entries(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            bands = fpl_tools._fixture_traffic_light_bands(1, lookup, 1, n=4)
            self.assertEqual(len(bands), 4)

    def test_bands_are_one_of_the_known_values(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            for tid in lookup:
                for band in fpl_tools._fixture_traffic_light_bands(tid, lookup, 1, n=4):
                    self.assertIn(band, ("green", "amber", "red", "double", "blank"))

    def test_blank_and_double_detected_structurally(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            blanker = next(tid for tid in lookup
                           if not fpl_tools._gw_fixtures(lookup[tid], 9))
            bands = fpl_tools._fixture_traffic_light_bands(blanker, lookup, 9, n=2)
            self.assertIn("blank", bands)
            self.assertIn("double", bands)

    def test_emoji_string_is_built_from_exactly_these_bands(self):
        """The two renderers (text emoji, real circular badges) must never
        be able to silently disagree about a fixture's rating."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            bands = fpl_tools._fixture_traffic_light_bands(1, lookup, 1, n=4)
            emoji = fpl_tools._fixture_traffic_lights(1, lookup, 1, n=4)
            expected = "[" + " ".join(fpl_tools._TRAFFIC_LIGHT_EMOJI[b] for b in bands) + "]"
            self.assertEqual(emoji, expected)


class LiveCacheTest(unittest.TestCase):
    def test_cache_key_includes_the_gameweek(self):
        """Two gameweeks requested inside 60s used to return the first one's
        data, silently serving stale scores to the live H2H tracker."""
        import inspect
        src = inspect.getsource(fpl_tools.get_live_event)
        self.assertIn("_LIVE_CACHE_GW", src)
        self.assertIn("_LIVE_CACHE_GW == gw", src)


if __name__ == "__main__":
    unittest.main()
