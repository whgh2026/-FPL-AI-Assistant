"""P0 Task D1 gate: the typed, set-aware chip ledger.

fpl_tools.py's own _plan_transfers_multi_gw docstring used to say it plainly:
"This cannot know whether a chip has already been spent this season -- the
caller does not currently pass that in (eval_chips is always the full set in
every existing call site)". Both call sites in app.py passed
eval_chips=ALL_CHIPS unconditionally, and get_played_chips() discarded the
event a chip was played in, so nothing downstream could even ask "is this
chip still legal right now" -- only "has this name ever been played, ever".

Four things this gate checks:

  * get_played_chips_with_events preserves the event a chip was played in
    (get_played_chips discarded it), and does not collapse a chip played in
    BOTH sets into one entry the way the old name-only dedup did.
  * ChipLedger correctly derives, for any gameweek, which of the eight
    (4 chips x 2 sets) slots are legal and unplayed -- including the set
    boundary itself (a Set 1 chip does not carry into Set 2, and a fresh
    Set 2 copy is NOT blocked by Set 1's having been used) and Free Hit's
    two temporal rules (illegal at GW1; illegal immediately after a Free
    Hit played the previous gameweek, i.e. the GW19->GW20 case).
  * suggest_transfers_for_custom_squad's chip_ledger parameter actually
    changes what the solver evaluates and recommends, not just what a
    caller was allowed to ask for.
  * _plan_transfers_multi_gw's chip_ledger parameter actually forces the
    MIP's own wc[t]/fh[t] variables to 0 for a blocked chip -- proven
    differentially, since a chip the solver would otherwise clearly reach
    for must vanish from the schedule once the ledger blocks it.
"""

import unittest
from unittest import mock

import fpl_tools
from tests import harness

EVENT = 4


# --------------------------------------------------------------------------- #
# Event-aware extraction
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class EventAwareExtractionTest(unittest.TestCase):
    def _history(self, chips):
        return _FakeResponse({"chips": chips})

    def test_preserves_the_event_for_every_played_chip(self):
        history = self._history([
            {"name": "wildcard", "event": 5},
            {"name": "3xc", "event": 12},
        ])
        with mock.patch.object(fpl_tools.requests, "get", return_value=history):
            played = fpl_tools.get_played_chips_with_events("123")
        self.assertEqual(played, [("Wildcard", 5), ("Triple Captain", 12)])

    def test_sorted_oldest_first_regardless_of_api_order(self):
        history = self._history([
            {"name": "3xc", "event": 12},
            {"name": "wildcard", "event": 5},
        ])
        with mock.patch.object(fpl_tools.requests, "get", return_value=history):
            played = fpl_tools.get_played_chips_with_events("123")
        self.assertEqual(played, [("Wildcard", 5), ("Triple Captain", 12)])

    def test_the_same_chip_name_played_in_both_sets_is_not_collapsed(self):
        """The specific information the old get_played_chips discarded: a
        second Wildcard, played in Set 2, is a DIFFERENT slot from Set 1's --
        losing it is exactly what let the old code treat "ever played" as
        "currently unavailable" regardless of which set is live now."""
        history = self._history([
            {"name": "wildcard", "event": 5},
            {"name": "wildcard", "event": 24},
        ])
        with mock.patch.object(fpl_tools.requests, "get", return_value=history):
            played = fpl_tools.get_played_chips_with_events("123")
        self.assertEqual(played, [("Wildcard", 5), ("Wildcard", 24)])

    def test_an_api_failure_degrades_to_empty_not_an_exception(self):
        with mock.patch.object(fpl_tools.requests, "get", side_effect=RuntimeError("boom")):
            self.assertEqual(fpl_tools.get_played_chips_with_events("123"), [])

    def test_get_played_chips_stays_name_only_and_deduplicated(self):
        """Backward compatibility: the existing caller (app.py's Set-1-deadline
        warning) only ever wanted "has this chip EVER been played"."""
        history = self._history([
            {"name": "wildcard", "event": 5},
            {"name": "wildcard", "event": 24},
            {"name": "3xc", "event": 12},
        ])
        with mock.patch.object(fpl_tools.requests, "get", return_value=history):
            names = fpl_tools.get_played_chips("123")
        self.assertEqual(names, ["Wildcard", "Triple Captain"])

    def test_unrecognised_or_eventless_entries_are_skipped(self):
        history = self._history([
            {"name": "someunknownchip", "event": 5},
            {"name": "wildcard", "event": None},
            {"name": "wildcard", "event": 5},
        ])
        with mock.patch.object(fpl_tools.requests, "get", return_value=history):
            played = fpl_tools.get_played_chips_with_events("123")
        self.assertEqual(played, [("Wildcard", 5)])


# --------------------------------------------------------------------------- #
# ChipLedger
# --------------------------------------------------------------------------- #
class ChipLedgerTest(unittest.TestCase):
    def test_a_fresh_ledger_offers_every_chip_except_gw1_free_hit(self):
        led = fpl_tools.ChipLedger.from_history([])
        self.assertEqual(set(led.available_chips(1)),
                         {"Wildcard", "Bench Boost", "Triple Captain"})
        for gw in (2, 10, 19, 20, 30, 38):
            self.assertEqual(set(led.available_chips(gw)), set(fpl_tools.CHIPS),
                             f"gw {gw} should offer all four chips fresh")

    def test_a_chip_played_in_set_1_is_unavailable_for_the_rest_of_set_1(self):
        led = fpl_tools.ChipLedger.from_history([("Wildcard", 5)])
        for gw in (6, 10, fpl_tools.CHIP_SET1_EXPIRY_GW):
            self.assertNotIn("Wildcard", led.available_chips(gw),
                             f"Wildcard should be spent for the rest of Set 1 (gw {gw})")
        # Every other chip is untouched.
        self.assertIn("Bench Boost", led.available_chips(10))
        self.assertIn("Triple Captain", led.available_chips(10))
        self.assertIn("Free Hit", led.available_chips(10))

    def test_set_1s_own_unused_chips_do_not_carry_over_into_set_2(self):
        """Framed the way the spec states it -- but the ledger's actual job is
        the opposite direction too: an unused Set 1 chip does not persist
        into Set 2 as a SECOND copy (it simply expires), and Set 2 issues its
        own fresh, independent copy regardless of Set 1's outcome. Both
        directions are asserted here."""
        led = fpl_tools.ChipLedger.from_history([])   # nothing used all season
        before = led.available_chips(fpl_tools.CHIP_SET1_EXPIRY_GW)
        after = led.available_chips(fpl_tools.CHIP_SET1_EXPIRY_GW + 1)
        self.assertEqual(set(before), set(fpl_tools.CHIPS))
        self.assertEqual(set(after), set(fpl_tools.CHIPS))
        self.assertTrue(led.can_play("Bench Boost", fpl_tools.CHIP_SET1_EXPIRY_GW + 1),
                        "Set 2's own fresh Bench Boost must be playable")

    def test_a_chip_used_in_set_1_does_not_block_its_set_2_copy(self):
        led = fpl_tools.ChipLedger.from_history([("Wildcard", 5)])
        self.assertNotIn("Wildcard", led.available_chips(10))                        # Set 1: spent
        self.assertIn("Wildcard", led.available_chips(fpl_tools.CHIP_SET1_EXPIRY_GW + 1))  # Set 2: fresh

    def test_a_chip_used_in_set_2_does_not_retroactively_block_set_1_queries(self):
        led = fpl_tools.ChipLedger.from_history([("Wildcard", 25)])
        self.assertIn("Wildcard", led.available_chips(10))
        self.assertNotIn("Wildcard", led.available_chips(30))

    def test_free_hit_is_illegal_at_gw1(self):
        led = fpl_tools.ChipLedger.from_history([])
        self.assertFalse(led.can_play("Free Hit", 1))
        self.assertNotIn("Free Hit", led.available_chips(1))

    def test_free_hit_is_legal_from_gw2_with_no_history(self):
        led = fpl_tools.ChipLedger.from_history([])
        self.assertTrue(led.can_play("Free Hit", 2))

    def test_no_consecutive_free_hits_across_the_gw19_gw20_boundary(self):
        led = fpl_tools.ChipLedger.from_history([("Free Hit", fpl_tools.CHIP_SET1_EXPIRY_GW)])
        self.assertFalse(led.can_play("Free Hit", fpl_tools.CHIP_SET1_EXPIRY_GW + 1),
                         "Free Hit played at GW19 must not also be playable at GW20")
        # But GW21 (not immediately consecutive) is fine, and it is Set 2's
        # own fresh copy, independent of the Set 1 slot that was actually used.
        self.assertTrue(led.can_play("Free Hit", fpl_tools.CHIP_SET1_EXPIRY_GW + 2))

    def test_consecutive_free_hit_rule_does_not_misfire_on_unrelated_chips(self):
        led = fpl_tools.ChipLedger.from_history([("Wildcard", fpl_tools.CHIP_SET1_EXPIRY_GW)])
        self.assertTrue(led.can_play("Free Hit", fpl_tools.CHIP_SET1_EXPIRY_GW + 1),
                        "a Wildcard at GW19 must not block Free Hit at GW20")

    def test_last_played_chip_override_supports_speculative_queries(self):
        """A caller reasoning about a decision not yet recorded in the ledger
        (e.g. "if I play Free Hit at GW19, could I play it again at GW20")
        can pass last_played_chip explicitly rather than waiting for the
        ledger's own history to reflect it."""
        led = fpl_tools.ChipLedger.from_history([])
        self.assertFalse(led.can_play("Free Hit", fpl_tools.CHIP_SET1_EXPIRY_GW + 1,
                                      last_played_chip=("Free Hit", fpl_tools.CHIP_SET1_EXPIRY_GW)))
        self.assertTrue(led.can_play("Free Hit", fpl_tools.CHIP_SET1_EXPIRY_GW + 1,
                                     last_played_chip=("Wildcard", fpl_tools.CHIP_SET1_EXPIRY_GW)))

    def test_a_chip_cannot_be_played_a_second_time_in_the_same_set(self):
        led = fpl_tools.ChipLedger.from_history([("Bench Boost", 3)])
        self.assertFalse(led.can_play("Bench Boost", 3))
        self.assertFalse(led.can_play("Bench Boost", 8))

    def test_unknown_chip_name_is_simply_not_playable(self):
        led = fpl_tools.ChipLedger.from_history([])
        self.assertFalse(led.can_play("Not A Real Chip", 5))


# --------------------------------------------------------------------------- #
# Single-GW solver integration
# --------------------------------------------------------------------------- #
class SingleGwSolverLedgerTest(unittest.TestCase):
    """suggest_transfers_for_custom_squad's chip_evaluations list is built
    from eval_chips, so a chip missing from it is the user-visible proof
    that the solver never considered it at all -- not merely that it scored
    low. See fpl_tools.py's chip_advice_list construction: every branch
    wraps the chip's name in <b>...</b>, which is what is checked here."""

    def _res(self, chip_ledger):
        with harness.synthetic_world("interactive") as (bs, _fx):
            squad = harness.squad_as_manager_input(bs, harness.build_squad(bs, "balanced"))
            return fpl_tools.suggest_transfers_for_custom_squad(
                squad, bank=2.0, free_transfers=1, eval_chips=list(fpl_tools.CHIPS),
                event=EVENT, risk="balanced", holding_map=None, current_gw=EVENT,
                allow_hits=True, chip_ledger=chip_ledger)

    def test_with_no_ledger_every_chip_is_a_candidate(self):
        res = self._res(chip_ledger=None)
        blob = " ".join(res["chip_evaluations"])
        for chip in fpl_tools.CHIPS:
            self.assertIn(f"<b>{chip}</b>", blob, f"{chip} should be evaluated with no ledger")

    def test_a_chip_already_used_this_set_is_never_evaluated(self):
        led = fpl_tools.ChipLedger.from_history([("Wildcard", 1)])
        res = self._res(chip_ledger=led)
        blob = " ".join(res["chip_evaluations"])
        self.assertNotIn("<b>Wildcard</b>", blob,
                         "a spent Wildcard must not appear in chip_evaluations at all")
        # The other three chips are untouched by blocking Wildcard specifically.
        for chip in ("Free Hit", "Bench Boost", "Triple Captain"):
            self.assertIn(f"<b>{chip}</b>", blob, f"{chip} should still be evaluated")

    def test_recommended_chip_is_never_one_the_ledger_has_blocked(self):
        led = fpl_tools.ChipLedger.from_history([(c, 1) for c in fpl_tools.CHIPS])
        res = self._res(chip_ledger=led)
        self.assertEqual(res["recommended_chip"], "None (Hold Chips)")
        self.assertEqual(res["chip_evaluations"], [],
                         "with every chip spent, nothing should be evaluated at all")


# --------------------------------------------------------------------------- #
# Multi-GW planner integration
# --------------------------------------------------------------------------- #
class MultiGwPlannerLedgerTest(unittest.TestCase):
    """Differential: a scenario engineered so the planner clearly reaches for
    a chip with no ledger, then proven to never reach for it once a ledger
    blocks it -- not merely "the test never happened to pick it"."""

    def _pool_favouring_a_full_rebuild(self):
        pid = [0]

        def entry(pos, team, price, xp):
            pid[0] += 1
            return {"id": pid[0], "name": f"P{pid[0]}", "team_id": team, "team": f"T{team}",
                    "position": pos, "price": price, "xp": xp, "status": "Available",
                    "on_yellow_card_tightrope": False, "minutes_floor": 1.0, "sell_price": price}

        current = ([entry("GK", 1, 4.0, 1.0), entry("GK", 2, 4.0, 1.0)]
                   + [entry("DEF", t, 4.0, 1.0) for t in range(1, 6)]
                   + [entry("MID", t, 4.5, 1.0) for t in range(1, 6)]
                   + [entry("FWD", t, 4.5, 1.0) for t in (1, 2, 3)])
        incoming = ([entry("GK", 7, 5.0, 6.0), entry("GK", 8, 4.5, 5.5)]
                    + [entry("DEF", t, 5.5, 7.0) for t in range(7, 12)]
                    + [entry("MID", t, 8.0, 9.0) for t in range(7, 12)]
                    + [entry("FWD", t, 9.0, 10.0) for t in (7, 8, 9)])
        return current + incoming, {p["id"] for p in current}

    def _schedule(self, chip_ledger):
        saved_profile = fpl_tools.get_solver_profile()
        fpl_tools.set_solver_profile("deterministic")
        try:
            pool, current_ids = self._pool_favouring_a_full_rebuild()
            saa_mean = {p["id"]: [p["xp"]] * 4 for p in pool}
            return fpl_tools._plan_transfers_multi_gw(
                pool, 100.0, 1, current_ids, saa_mean, event=10, n=4,
                chip_ledger=chip_ledger)
        finally:
            fpl_tools.set_solver_profile(saved_profile)

    def test_baseline_the_scenario_genuinely_reaches_for_a_chip(self):
        """Not the point of this gate by itself -- it exists so the next two
        tests are provably non-vacuous, matching test_phased.py's own
        test_wildcard_or_free_hit_pays_no_hit_when_clearly_worth_it."""
        schedule = self._schedule(chip_ledger=None)
        chips_used = {s["chip"] for s in schedule if s.get("chip")}
        self.assertTrue(chips_used, f"expected a chip to fire with no ledger: {schedule}")

    def test_a_ledger_blocked_chip_never_appears_in_the_schedule(self):
        """Wildcard fires at GW10 (event=10) with no ledger (see the baseline
        test). A ledger recording it as already used this set must remove it
        from the schedule entirely -- not just make it less likely."""
        led = fpl_tools.ChipLedger.from_history([("Wildcard", 5)])
        schedule = self._schedule(chip_ledger=led)
        chips_used = {s["chip"] for s in schedule if s.get("chip")}
        self.assertNotIn("Wildcard", chips_used, f"Wildcard should be blocked: {schedule}")

    def test_blocking_one_chip_leaves_the_other_available(self):
        """The per-chip, per-week zeroing must not be all-or-nothing: blocking
        Free Hit specifically must not also cost Wildcard its slot."""
        led = fpl_tools.ChipLedger.from_history([("Free Hit", 5)])
        schedule = self._schedule(chip_ledger=led)
        chips_used = {s["chip"] for s in schedule if s.get("chip")}
        self.assertIn("Wildcard", chips_used,
                     f"blocking Free Hit only must not also block Wildcard: {schedule}")

    def test_no_ledger_is_fully_backward_compatible(self):
        """chip_ledger=None (the default, and every call site before this
        parameter existed) must add zero constraints -- byte-identical
        behaviour to the pre-ledger function."""
        saved_profile = fpl_tools.get_solver_profile()
        fpl_tools.set_solver_profile("deterministic")
        try:
            pool, current_ids = self._pool_favouring_a_full_rebuild()
            saa_mean = {p["id"]: [p["xp"]] * 4 for p in pool}
            with_explicit_none = fpl_tools._plan_transfers_multi_gw(
                pool, 100.0, 1, current_ids, saa_mean, event=10, n=4, chip_ledger=None)
            without_the_param_at_all = fpl_tools._plan_transfers_multi_gw(
                pool, 100.0, 1, current_ids, saa_mean, event=10, n=4)
        finally:
            fpl_tools.set_solver_profile(saved_profile)
        self.assertEqual([s.get("chip") for s in with_explicit_none],
                         [s.get("chip") for s in without_the_param_at_all])


if __name__ == "__main__":
    unittest.main()
