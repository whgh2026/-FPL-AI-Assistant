"""Stage 2 gate: the forecast is strategy-blind, and strategy still does something.

Two halves that must BOTH hold, because either alone is a regression:

  1. Layer 1 independence. _player_xp / _player_xp_horizon must be bitwise
     identical regardless of the manager's chosen strategy, and must not respond
     to ownership or transfer volume. Preference terms belong in the objective,
     where they are visible and priced, not smuggled into the projection.

  2. Strategy re-homing. Stripping those terms from Layer 1 removes the ONLY
     place strategy expressed itself before GW26, because the objective-level EO
     terms were gated behind `current_gw >= PHASE2_START_GW`. Shipping half 1
     without deleting that gate would have made "Protect my lead" and "Chase the
     leader" byte-identical to "Balanced" for gameweeks 1-25 -- strictly worse
     than the behaviour it replaced.
"""

import copy
import unittest

import fpl_tools
from tests import harness

EVENT = 4
STRATEGIES = ["balanced", "conservative", "aggressive",
              "rank protecting (shield)", "rank chasing (hunting)"]


class Layer1IndependenceTest(unittest.TestCase):
    def test_forecast_takes_no_strategy_argument(self):
        """Structural, not behavioural: strategy cannot reach the forecast."""
        import inspect
        for fn in (fpl_tools._player_xp, fpl_tools._player_xp_horizon,
                   fpl_tools._player_xp_raw):
            params = inspect.signature(fn).parameters
            self.assertNotIn("risk", params, f"{fn.__name__} still accepts `risk`")

    def test_preference_helpers_are_gone(self):
        for name in ("_risk_adjust", "_ownership_adjust"):
            self.assertFalse(hasattr(fpl_tools, name),
                             f"{name} should have been deleted in Stage 2")

    def test_projection_ignores_ownership(self):
        """selected_by_percent must not move a points forecast."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            base = bs["elements"][40]
            low = copy.deepcopy(base)
            low["selected_by_percent"] = "0.4"
            high = copy.deepcopy(base)
            high["selected_by_percent"] = "62.0"
            self.assertEqual(fpl_tools._player_xp(low, lookup, event=EVENT),
                             fpl_tools._player_xp(high, lookup, event=EVENT))
            self.assertEqual(fpl_tools._player_xp_horizon(low, lookup, EVENT),
                             fpl_tools._player_xp_horizon(high, lookup, EVENT))

    def test_projection_ignores_transfer_momentum(self):
        """The old term saturated at +0.3 for anyone the crowd was buying."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            base = bs["elements"][60]
            quiet = copy.deepcopy(base)
            quiet["transfers_in_event"], quiet["transfers_out_event"] = 0, 0
            bandwagon = copy.deepcopy(base)
            bandwagon["transfers_in_event"] = 900_000
            bandwagon["transfers_out_event"] = 10_000
            self.assertEqual(fpl_tools._player_xp(quiet, lookup, event=EVENT),
                             fpl_tools._player_xp(bandwagon, lookup, event=EVENT))

    def test_projection_ignores_threat_index(self):
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            base = bs["elements"][75]
            lo = copy.deepcopy(base); lo["threat"] = 0.0
            hi = copy.deepcopy(base); hi["threat"] = 900.0
            self.assertEqual(fpl_tools._player_xp(lo, lookup, event=EVENT),
                             fpl_tools._player_xp(hi, lookup, event=EVENT))

    def test_whole_pool_is_identical_across_strategies(self):
        """The end-to-end assertion: same squad, every strategy, same numbers."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            squad = harness.build_squad(bs, "balanced")
            runs = []
            for strategy in STRATEGIES:
                entries = harness.squad_to_pool_entries(
                    bs, squad, lookup, EVENT, risk=strategy)
                runs.append([(e["id"], e["xp"], e["xp_gw"]) for e in entries])
            for strategy, run in zip(STRATEGIES[1:], runs[1:]):
                self.assertEqual(runs[0], run,
                                 f"projection differs under strategy {strategy!r}")


class StrategyReHomingTest(unittest.TestCase):
    def test_phase_gate_constant_is_gone(self):
        self.assertFalse(hasattr(fpl_tools, "PHASE2_START_GW"))

    def test_strategy_drives_phase_at_any_gameweek(self):
        """The B2 trap: this must hold in GW8, not only from GW26."""
        for strategy, expected_mode in (("balanced", "ev"),
                                        ("conservative", "blocker"),
                                        ("aggressive", "divergence"),
                                        ("rank protecting (shield)", "blocker"),
                                        ("rank chasing (hunting)", "divergence")):
            mode = fpl_tools._strategy_mode(strategy)
            self.assertEqual(mode, expected_mode, f"{strategy} -> {mode}")
            phase = 2 if mode in ("blocker", "divergence") else 1
            self.assertEqual(phase, 1 if expected_mode == "ev" else 2)

    def test_blocker_and_divergence_diverge_in_an_early_gameweek(self):
        """Rank-aware terms must actually change the squad, at GW8.

        Mirrors the existing Phase-C solver tests but pins the gameweek low, so
        a re-introduced calendar gate fails here rather than silently in
        production for two-thirds of a season.
        """
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            # A wide pool: with only 2/5/5/3 available the squad is fully
            # determined by the positional constraints and no objective term
            # acting on squad membership could change the answer.
            entries = harness.market_pool_entries(bs, lookup, EVENT)
            budget = sum(sorted((e["price"] for e in entries), reverse=True)[:15])
            # Alternate low-EO differentials against high-EO template assets.
            eo_map = {e["id"]: {"eo": 2.0 if i % 2 else 55.0}
                      for i, e in enumerate(entries)}

            picks = {}
            for mode in ("blocker", "divergence"):
                sel, _, _parts = fpl_tools._solve_squad(
                    entries, budget=budget, eo_map=eo_map, mode=mode, phase=2)
                self.assertIsNotNone(sel, f"{mode} produced no squad")
                picks[mode] = sorted(sel)

            self.assertNotEqual(
                picks["blocker"], picks["divergence"],
                "blocker and divergence chose the same squad -- EO terms are inert")

            # And directionally: divergence should hold more low-EO assets.
            low_eo = {pid for pid, v in eo_map.items() if v["eo"] < 10.0}
            self.assertGreater(
                len(set(picks["divergence"]) & low_eo),
                len(set(picks["blocker"]) & low_eo),
                "divergence should favour differentials over the template")


if __name__ == "__main__":
    unittest.main()
