"""P0 remediation, Task 2: get_free_transfers() must honour the real FPL rule
that playing Wildcard or Free Hit does not touch the free-transfer bank.

Two independent defects, both in the same function:

  * A chip gameweek forced `ft = 1`, discarding whatever the manager had
    actually banked. The real rule is the opposite: a chip week accumulates
    exactly like any other week with zero transfers made against the bank.

  * `except Exception: return 1` made a genuine failure (network error,
    malformed API payload) indistinguishable from "this manager truly has 1
    free transfer" -- to callers and to the MILP hit/roll-value logic that
    budgets against the number. It must now raise, with the reason recorded
    for last_free_transfers_error().

These tests mock fpl_tools._cached_json directly (per-URL), since
test_stage7_render.py already asserts get_free_transfers routes every manager
endpoint through it rather than a raw requests.get.
"""

import unittest
from unittest import mock

import fpl_tools

MANAGER_ID = "12345"
BASE = fpl_tools.BASE_URL


def _entry(started_event=1):
    return {"started_event": started_event}


def _history(chip_events=()):
    """chip_events: iterable of (chip_name, event) tuples."""
    return {"chips": [{"name": name, "event": ev} for name, ev in chip_events]}


def _transfers(made_by_event):
    """made_by_event: {event: count}. Emits `count` synthetic transfer rows
    for each event -- get_free_transfers only counts len(), not content."""
    rows = []
    for ev, count in made_by_event.items():
        rows.extend({"event": ev} for _ in range(count))
    return rows


def _mock_api(entry, history, transfers):
    def _dispatch(url, ttl=300):
        if url == f"{BASE}/entry/{MANAGER_ID}/":
            return entry
        if url == f"{BASE}/entry/{MANAGER_ID}/history/":
            return history
        if url == f"{BASE}/entry/{MANAGER_ID}/transfers/":
            return transfers
        raise AssertionError(f"unexpected URL in get_free_transfers: {url}")
    return _dispatch


class ChipWeekAccountingTest(unittest.TestCase):
    """The 2026/27 (and, in substance, every prior season's) rule: a
    Wildcard/Free Hit gameweek preserves and rolls forward the bank."""

    def test_a_wildcard_week_accumulates_instead_of_resetting_to_one(self):
        # Season start GW1, Wildcard at GW2 (with a full-squad rebuild's
        # worth of transfers recorded against it), asking for GW4.
        entry = _entry(started_event=1)
        history = _history([("wildcard", 2)])
        transfers = _transfers({2: 12})
        with mock.patch.object(fpl_tools, "_cached_json",
                               side_effect=_mock_api(entry, history, transfers)):
            ft = fpl_tools.get_free_transfers(MANAGER_ID, target_gw=4)
        # 1 (GW1) -> 2 (into GW2) -> 3 (into GW3, chip week treated as a
        # zero-transfer week) -> 4 (into GW4). The old code forced ft=1 at
        # the chip week and would have returned 2 here, not 4.
        self.assertEqual(ft, 4)

    def test_the_chip_weeks_own_bulk_transfers_do_not_deflate_the_bank(self):
        """The naive fix -- deleting the reset branch without also zeroing
        `made` for that week -- would subtract the chip's own (very real,
        API-recorded) transfer count from the bank instead of ignoring it,
        landing on a different wrong answer. Pin the right one."""
        entry = _entry(started_event=1)
        history = _history([("wildcard", 2)])
        transfers = _transfers({2: 15})
        with mock.patch.object(fpl_tools, "_cached_json",
                               side_effect=_mock_api(entry, history, transfers)):
            ft = fpl_tools.get_free_transfers(MANAGER_ID, target_gw=3)
        # 1 -> 2 (into GW2) -> 3 (into GW3, the chip's 15 recorded transfers
        # excluded). A naive "just remove the reset" fix would compute
        # max(2 - 15, 0) = 0 at the chip week instead.
        self.assertEqual(ft, 3)

    def test_free_hit_is_treated_the_same_as_wildcard(self):
        entry = _entry(started_event=1)
        history = _history([("freehit", 2)])
        transfers = _transfers({2: 11})
        with mock.patch.object(fpl_tools, "_cached_json",
                               side_effect=_mock_api(entry, history, transfers)):
            ft = fpl_tools.get_free_transfers(MANAGER_ID, target_gw=3)
        self.assertEqual(ft, 3)

    def test_playing_the_chip_in_the_target_gameweek_itself_is_also_preserved(self):
        """target_gw IS the chip week (e.g. querying "how many FT do I have
        this week" the same week Wildcard was played). The bank must still
        roll forward untouched, not reset to 1."""
        entry = _entry(started_event=1)
        history = _history([("wildcard", 2)])
        transfers = _transfers({2: 9})
        with mock.patch.object(fpl_tools, "_cached_json",
                               side_effect=_mock_api(entry, history, transfers)):
            ft = fpl_tools.get_free_transfers(MANAGER_ID, target_gw=2)
        # 1 (GW1) -> 2 (into GW2). GW2's own 9 chip transfers must not be
        # subtracted, and must not force a hard reset to 1 either.
        self.assertEqual(ft, 2)

    def test_a_quiet_season_still_accumulates_and_caps_at_five(self):
        """Unaffected-by-this-fix regression guard: no chip at all."""
        entry = _entry(started_event=1)
        history = _history([])
        transfers = _transfers({})
        with mock.patch.object(fpl_tools, "_cached_json",
                               side_effect=_mock_api(entry, history, transfers)):
            ft = fpl_tools.get_free_transfers(MANAGER_ID, target_gw=8)
        self.assertEqual(ft, 5, "must cap at 5, not accumulate unbounded")

    def test_a_transfer_in_a_normal_week_still_reduces_the_bank(self):
        """Unaffected-by-this-fix regression guard: an ordinary (non-chip)
        transfer must still draw against the bank exactly as before."""
        entry = _entry(started_event=1)
        history = _history([])
        transfers = _transfers({2: 1})
        with mock.patch.object(fpl_tools, "_cached_json",
                               side_effect=_mock_api(entry, history, transfers)):
            ft = fpl_tools.get_free_transfers(MANAGER_ID, target_gw=3)
        # 1 -> 2 (into GW2) -> made=1 -> 1 -> 2 (into GW3, +1 with no further
        # transfers recorded).
        self.assertEqual(ft, 2)


class UnknownStateHandlingTest(unittest.TestCase):
    """A failure must raise, not masquerade as a confident '1'."""

    def setUp(self):
        fpl_tools._LAST_FT_ERROR = None

    def test_an_api_failure_raises_rather_than_returning_a_guess(self):
        with mock.patch.object(fpl_tools, "_cached_json",
                               side_effect=RuntimeError("simulated FPL API outage")):
            with self.assertRaises(RuntimeError):
                fpl_tools.get_free_transfers(MANAGER_ID, target_gw=4)

    def test_the_failure_is_recorded_for_last_free_transfers_error(self):
        with mock.patch.object(fpl_tools, "_cached_json",
                               side_effect=RuntimeError("simulated FPL API outage")):
            with self.assertRaises(RuntimeError):
                fpl_tools.get_free_transfers(MANAGER_ID, target_gw=4)
        recorded = fpl_tools.last_free_transfers_error()
        self.assertIsNotNone(recorded)
        mgr, detail = recorded
        self.assertEqual(mgr, MANAGER_ID)
        self.assertIn("simulated FPL API outage", detail)

    def test_a_malformed_response_also_raises_not_a_guess(self):
        """Anything that breaks the arithmetic (a non-dict entry, a missing
        'chips' key that isn't even a dict) must surface the same way as a
        network failure -- not silently degrade to '1'."""
        def _dispatch(url, ttl=300):
            if url.endswith("/entry/12345/"):
                return {"started_event": 1}
            if url.endswith("/history/"):
                return None   # malformed: .get("chips", []) will raise
            return []
        with mock.patch.object(fpl_tools, "_cached_json", side_effect=_dispatch):
            with self.assertRaises(Exception):
                fpl_tools.get_free_transfers(MANAGER_ID, target_gw=4)


if __name__ == "__main__":
    unittest.main()
