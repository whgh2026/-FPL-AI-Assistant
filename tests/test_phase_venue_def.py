"""Tier 1.4 gate: the venue shift is applied to the conceded rate exactly once.

_xp_for_fixture built the clean-sheet lambda as:

    lam = _to_float(f.get("lam_against")) or (xgc90 * att_adj * venue_def)
    lam = max(0.0, lam * venue_def)

Two defects in two lines. The fallback branch already multiplied by
`venue_def`, so the shared line below applied it a SECOND time -- venue_def^2,
which is ~1.21 away and ~0.90 at home. And the Dixon-Coles branch should not
be scaled at all: its lambda comes from _fixture_lambdas, where the home
rate is built with the fitted `+ gamma` home-advantage term, so the venue
shift is already inside it.

Clean sheets run through exp(-lam), so a 21% inflation of the conceded rate
is a ~19% relative cut in P(CS) for every away defender and keeper priced off
the fallback -- silently, with no error and a plausible-looking number.

Nothing in the suite pinned either path before this file.
"""

import math
import unittest

import fpl_tools
from tests import harness


def _player(**over):
    p = {"id": 900, "team": 1, "element_type": 2, "status": "a",
         "minutes": 900, "starts": 10,
         "expected_goals_conceded_per_90": 1.30,
         "expected_goals_per_90": 0.10, "expected_assists_per_90": 0.10}
    p.update(over)
    return p


def _fixture(is_home, lam_against=None):
    f = {"event": 5, "is_home": is_home, "opponent": 2,
         "opp_strength_def": 3.0, "opp_strength_att": 3.0,
         "lam_for": 1.3, "win_prob": None, "official_fdr": 3}
    if lam_against is not None:
        f["lam_against"] = lam_against
    return f


def _venue_def(is_home):
    adv = fpl_tools._load_weights()["home_advantage"]
    return (1.0 - 0.05 * adv) if is_home else (1.0 + 0.05 * adv)


class VenueDefAppliedOnceTest(unittest.TestCase):

    def test_the_dixon_coles_lambda_is_not_rescaled_by_venue(self):
        """The DC branch. Its lambda already carries the fitted home-advantage
        term, so an IDENTICAL lambda must produce an identical clean-sheet
        contribution home and away.

        Isolated with a zero-attack player on effectively infinite minutes.
        The minutes matter: _reg applies empirical-Bayes shrinkage toward the
        POSITIONAL average, so a plain 0.0 xG90 on 900 minutes still comes out
        well above zero and venue_att keeps something to scale. Swamping the
        PRIOR_MINUTES weight is what actually drives the attacking terms to
        zero, leaving the clean sheet as the only venue-sensitive term.

        Pre-fix the two differed by exp(-lam*0.95) against exp(-lam*1.05) --
        roughly 11% of the clean-sheet term."""
        lam = 1.10
        flat = _player(expected_goals_per_90=0.0, expected_assists_per_90=0.0,
                       minutes=10 ** 7, bps=0, yellow_cards=0, red_cards=0)
        with harness.synthetic_world():
            harness.load_synthetic()
            home = fpl_tools._xp_for_fixture(flat, _fixture(True, lam), 90.0, 2)
            away = fpl_tools._xp_for_fixture(flat, _fixture(False, lam), 90.0, 2)
        # delta, not places=9: PRIOR_MINUTES still contributes ~270/1e7 of the
        # positional xG average, so a trace of venue_att survives on the
        # attacking side. It lands at ~3.6e-06, against ~3.07e-02 before the
        # fix -- four orders of magnitude apart, so 1e-4 discriminates
        # decisively without pretending the isolation is perfect.
        self.assertAlmostEqual(
            home, away, delta=1e-4,
            msg="venue is still being applied on top of the Dixon-Coles lambda")

    def test_the_fallback_branch_applies_venue_exactly_once(self):
        """The squared-venue branch, pinned by construction: hand one fixture
        the rate the other one INFERS. If the fallback applies venue once, the
        two paths agree exactly. Pre-fix the fallback squared it and came out
        strictly lower."""
        with harness.synthetic_world():
            harness.load_synthetic()
            p = _player()
            avg = fpl_tools._league_averages()["DEF"]
            # Reproduce _reg's empirical-Bayes shrinkage on xgc90.
            mins = float(p["minutes"])
            xgc90 = ((p["expected_goals_conceded_per_90"] * mins
                      + avg["xgc"] * fpl_tools.PRIOR_MINUTES)
                     / (mins + fpl_tools.PRIOR_MINUTES))
            # att_adj == 1.0 for a neutral opponent (opp_strength_att == 3.0).
            inferred_once = xgc90 * 1.0 * _venue_def(False)

            fallback = fpl_tools._xp_for_fixture(p, _fixture(False), 90.0, 2)
            explicit = fpl_tools._xp_for_fixture(
                p, _fixture(False, inferred_once), 90.0, 2)

        self.assertAlmostEqual(
            fallback, explicit, places=9,
            msg="the inferred branch is not applying venue exactly once")

    def test_a_zero_lambda_is_respected_not_treated_as_missing(self):
        """`or` treated a legitimately computed 0.0 -- a side expected to
        concede nothing -- as ABSENT and silently re-inferred the rate from
        xgc90. Behavioural, not source-level: a zero lambda must yield
        P(CS) = exp(0) = 1.0, the maximum clean-sheet term available."""
        with harness.synthetic_world():
            harness.load_synthetic()
            zero = fpl_tools._xp_for_fixture(_player(), _fixture(False, 0.0), 90.0, 2)
            inferred = fpl_tools._xp_for_fixture(_player(), _fixture(False), 90.0, 2)
            cs_conf = fpl_tools._load_weights()["clean_sheet_confidence"]
        # exp(-0) = 1.0 -> the full CS_PTS. Inference gives exp(-~1.36) ~ 0.26.
        self.assertGreater(zero - inferred, 0.5 * fpl_tools.CS_PTS[2] * cs_conf,
                           "a zero lambda is still being treated as missing")

    def test_the_truthiness_fallthrough_is_gone_from_the_code_itself(self):
        """Source-level backstop, with COMMENTS STRIPPED -- the explanatory
        comment above the fix quotes the old expression verbatim, and a naive
        substring check would be satisfied by that prose rather than by the
        code. Same trap test_stage8_hygiene.py calls out for the CI workflow's
        `run:` lines."""
        import inspect
        src = inspect.getsource(fpl_tools._xp_for_fixture)
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))
        self.assertNotIn('_to_float(f.get("lam_against")) or (', code,
                         "the `or` fallthrough on lam_against is still live code")
        self.assertIn("dc_lam is not None", code)

if __name__ == "__main__":
    unittest.main()
