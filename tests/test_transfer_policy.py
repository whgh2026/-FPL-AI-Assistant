import unittest
from unittest import mock
import fpl_tools

GW = 10
POS_ID = {1:1,2:1,3:2,4:2,5:2,6:2,7:2,8:3,9:3,10:3,11:3,12:3,13:4,14:4,15:4,16:4,17:4}
POS_NAME = {1:"GK",2:"DEF",3:"MID",4:"FWD"}


def _element(pid, status="a", price=5.0):
    return {"id": pid, "element_type": POS_ID[pid], "team": ((pid - 1) % 6) + 1,
            "now_cost": int(price * 10), "first_name": "P%d" % pid, "second_name": "P%d" % pid,
            "status": status, "chance_of_playing_next_round": None, "minutes": 1000}


def _squad():
    out = []
    for pid in range(1, 16):
        out.append({"player_id": pid, "position": POS_NAME[POS_ID[pid]],
                    "team_id": ((pid - 1) % 6) + 1, "team": "T%d" % (((pid - 1) % 6) + 1),
                    "price": 5.0, "selling_price": 5.0, "xp": 3.0, "status": "Available"})
    return out


def _run(squad, horizon, gwxp, notes, free_transfers, allow_hits=False, elements=None):
    if elements is None:
        elements = [_element(pid) for pid in range(1, 16)] + [_element(16)]
    bootstrap = {"elements": elements,
                 "teams": [{"id": t, "name": "T%d" % t, "short_name": "T%d" % t} for t in range(1, 7)],
                 "events": [{"id": GW, "is_next": True, "finished": False}]}
    def h(e, fl, ev, risk="balanced", n=4):
        return horizon.get(e["id"], 12.0), notes.get(e["id"], "Available")
    def g(e, fl, event=None, risk="balanced"):
        return gwxp.get(e["id"], 3.0), notes.get(e["id"], "Available")
    with mock.patch.object(fpl_tools, "_get_bootstrap", return_value=bootstrap), \
         mock.patch.object(fpl_tools, "_build_fixture_lookup", return_value={}), \
         mock.patch.object(fpl_tools, "_player_xp_horizon", side_effect=h), \
         mock.patch.object(fpl_tools, "_player_xp", side_effect=g), \
         mock.patch.object(fpl_tools, "_player_fdr_list", return_value=[2, 2, 2, 2]), \
         mock.patch.object(fpl_tools, "_is_on_tightrope", return_value=False):
        return fpl_tools.suggest_transfers_for_custom_squad(
            squad, bank=0.0, free_transfers=free_transfers, eval_chips=[],
            event=GW, risk="balanced", holding_map=None, current_gw=GW,
            allow_hits=allow_hits)


class TransferPolicyTest(unittest.TestCase):

    def test_marginal_gain_rolls_transfer(self):
        squad = _squad()
        res = _run(squad, {16: 16.0}, {16: 3.4}, {}, free_transfers=1)
        self.assertEqual(len(res["transfers"]), 0)
        self.assertTrue(res["roll_transfer"])

    def test_rejects_paid_hit_for_multi_gw_gain(self):
        squad = _squad()
        elements = [_element(pid) for pid in range(1, 16)] + [_element(16), _element(17)]
        res = _run(squad, {16: 16.0, 17: 16.0}, {16: 4.0, 17: 4.0}, {}, free_transfers=1,
                   allow_hits=False, elements=elements)
        self.assertEqual(res["hits"], 0)
        self.assertLessEqual(len(res["transfers"]), 1)

    def test_injury_triggers_transfer(self):
        squad = _squad()
        squad[14]["status"] = "Injured"
        squad[14]["xp"] = 0.0
        # Under the bench-decay model the injured 0.0-xP player is bench fodder
        # (valued ~0), so the replacement must clearly upgrade the XI (20.0 vs 12.0)
        # to clear the transfer-friction hurdle.
        res = _run(squad, {15: 0.0, 16: 20.0}, {15: 0.0, 16: 5.0}, {15: "Injured"}, free_transfers=1)
        outs = [m["out"]["id"] for m in res["transfers"]]
        self.assertIn(15, outs)

    def test_at_max_ft_cap_burns_transfer(self):
        # At the 5-FT cap banking is nearly worthless (the 5th banked transfer
        # is worth 0.05), so a clear upgrade should be taken rather than wasted.
        #
        # The incoming projection was raised from 14.0 to 20.0 in Stage 3. Under
        # the old economics a +2.0 horizon gain cleared the friction stack; the
        # separable hurdle that replaced it charges HURDLE_BASE plus 1.2*sigma
        # per leg, and sigma on this pool is ~1.1, so a swap now has to clear
        # roughly 3.4 points. 14.0 left the decision genuinely marginal, which
        # is not what this test is for -- it tests the FT-cap incentive, so the
        # gain is now unambiguous.
        squad = _squad()
        res = _run(squad, {16: 20.0}, {16: 5.0}, {}, free_transfers=5)
        self.assertEqual(len(res["transfers"]), 1)


if __name__ == "__main__":
    unittest.main()
