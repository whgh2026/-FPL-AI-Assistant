"""Stage 4 gate: Dixon-Coles identification, the team-strength scale, and Blocker A.

Three coupled defects, fixed in one commit because fixing any one alone makes
things worse:

  1. The defence mapping's sign was inverted, so attackers were boosted against
     elite defences and suppressed against poor ones.
  2. `def` was never centred, so it absorbed the league scoring level
     (mean ~ -log(1.4) ~ -0.33). Correcting the sign WITHOUT centring shifts the
     league-average multiplier from 0.82 to 1.28 -- a ~28% inflation against
     neutral, and ~56% relative to where it started. That is Blocker A.
  3. `strength_overall_*` is on a ~1000-1400 scale but was blended into a [1,5]
     one, clamping every club to 5.0 and making fixture difficulty inert until
     roughly GW10.

Not covered here: rho. The gate specified for it (-0.25 < rho < 0.05) describes
real football, where the low-score correlation is genuinely negative. It cannot
be validated against synthetic data -- an independent-Poisson generator has a
true rho of 0, and a rejection sampler that applies the tau correction reweights
the marginals, shifting the very mu and gamma it is supposed to hold fixed. The
rho gradient normalisation here is justified analytically (only four scorelines
have a non-zero d(tau)/d(rho), so normalising by all fixtures diluted the step)
but its output is unverified offline and must be checked against real data.
"""

import math
import os
import random
import unittest

import fpl_tools
from tests import harness

EVENT = 4


def _poisson(lam, rng):
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def _synthetic_league(seed=3, mu=math.log(1.42), gamma=0.25, n=20, rounds=1):
    """Double round-robin from known parameters, independent Poisson."""
    rng = random.Random(seed)
    att = [rng.gauss(0, 0.3) for _ in range(n)]
    dfn = [rng.gauss(0, 0.3) for _ in range(n)]
    fixtures = []
    for _ in range(rounds):
        for h in range(n):
            for a in range(n):
                if h == a:
                    continue
                lh = math.exp(mu + att[h] - dfn[a] + gamma)
                la = math.exp(mu + att[a] - dfn[h])
                fixtures.append({
                    "team_h": h + 1, "team_a": a + 1,
                    "team_h_score": _poisson(lh, rng), "team_a_score": _poisson(la, rng),
                    "kickoff_time": "2026-08-01T15:00:00Z", "finished": True,
                })
    return att, dfn, fixtures


class IdentificationTest(unittest.TestCase):
    """mu owns the scoring level; att and def are both centred."""

    @classmethod
    def setUpClass(cls):
        cls.att, cls.dfn, fixtures = _synthetic_league()
        cls.ratings, cls.gamma, cls.rho, cls.mu = fpl_tools._fit_dixon_coles(
            fixtures, decay=0.0, tau=-0.10)

    def test_attack_is_centred(self):
        mean = sum(v["att"] for v in self.ratings.values()) / len(self.ratings)
        self.assertLess(abs(mean), 0.01, f"mean(att) = {mean:+.5f}")

    def test_defence_is_centred(self):
        """The Blocker A precondition. Was ~-0.33 before the intercept existed."""
        mean = sum(v["def"] for v in self.ratings.values()) / len(self.ratings)
        self.assertLess(abs(mean), 0.01, f"mean(def) = {mean:+.5f}")

    def test_intercept_recovers_the_scoring_level(self):
        self.assertGreater(math.exp(self.mu), 1.2)
        self.assertLess(math.exp(self.mu), 1.8)

    def test_home_advantage_is_no_longer_confounded(self):
        """gamma absorbed part of the level before mu existed: 0.354 vs a true 0.25."""
        self.assertGreater(self.gamma, 0.15)
        self.assertLess(self.gamma, 0.40)

    def test_rho_stays_inside_its_clamp(self):
        """Weak assertion by design -- see the module docstring on why rho
        cannot be validated offline."""
        self.assertGreaterEqual(self.rho, -0.3)
        self.assertLessEqual(self.rho, 0.3)

    def test_ratings_track_the_true_strengths(self):
        n = len(self.ratings)
        fit_att = [self.ratings[i + 1]["att"] for i in range(n)]
        fit_def = [self.ratings[i + 1]["def"] for i in range(n)]
        self.assertGreater(_corr(self.att, fit_att), 0.8)
        self.assertGreater(_corr(self.dfn, fit_def), 0.8)


def _corr(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return cov / (va * vb)


class DefenceSignTest(unittest.TestCase):
    """The headline fix: a tough defence must suppress our attackers."""

    def test_elite_defence_suppresses_and_poor_defence_boosts(self):
        with harness.synthetic_world() as (bs, _fx):
            ratings = fpl_tools._team_attack_def_ratings()
            # Fixture team 1 is the strongest side, team 20 the weakest.
            best, worst = ratings[1]["def"], ratings[20]["def"]
            self.assertGreater(
                best, worst,
                "a better defence must map to a HIGHER opp_strength_def")

            adj_vs_best = 3.0 / max(ratings[1]["def_away"], 1.5)
            adj_vs_worst = 3.0 / max(ratings[20]["def_away"], 1.5)
            self.assertLess(adj_vs_best, 1.0, "facing an elite defence must not boost xG")
            self.assertGreater(adj_vs_worst, 1.0, "facing a poor defence should boost xG")
            self.assertLess(adj_vs_best, adj_vs_worst)

    def test_multiplier_is_bounded(self):
        """3.0/x is convex; the floor stops the weakest club tripling xG."""
        with harness.synthetic_world() as (bs, _fx):
            ratings = fpl_tools._team_attack_def_ratings()
            worst = min(v["def_away"] for v in ratings.values())
            self.assertLessEqual(3.0 / max(worst, 1.5), 2.0)


class StrengthScaleTest(unittest.TestCase):
    def test_ratings_are_not_all_clamped(self):
        """Every club used to pin at 5.0 because a ~1290 field met a [1,5] scale."""
        with harness.synthetic_world() as (bs, _fx):
            ratings = fpl_tools._team_attack_def_ratings()
            atts = [v["att"] for v in ratings.values()]
            defs = [v["def"] for v in ratings.values()]
            self.assertGreater(max(atts) - min(atts), 1.0, "attack ratings are degenerate")
            self.assertGreater(max(defs) - min(defs), 1.0, "defence ratings are degenerate")
            self.assertLess(max(atts), 5.01)
            self.assertGreater(min(atts), 0.99)

    def test_fixtures_actually_differentiate(self):
        """Distinct opponents must produce distinct multipliers."""
        with harness.synthetic_world() as (bs, _fx):
            lookup = fpl_tools._build_fixture_lookup(bs)
            seen = {round(f["opp_strength_def"], 2)
                    for fixtures in lookup.values() for f in fixtures}
            self.assertGreater(len(seen), 5, "fixture difficulty is effectively constant")


class ConcessionPenaltyTest(unittest.TestCase):
    """FPL deducts 1 point per TWO goals: E[floor(G/2)], not E[G]/2."""

    def test_matches_a_direct_expectation(self):
        for lam in (0.6, 1.0, 1.3, 1.8, 2.5):
            brute = sum((g // 2) * math.exp(-lam) * lam ** g / math.factorial(g)
                        for g in range(0, 30))
            self.assertAlmostEqual(
                fpl_tools._expected_concession_penalty(lam), brute, places=5)

    def test_old_linear_form_over_penalised(self):
        """Worst at low lambda, so elite defences were hurt most."""
        for lam in (0.6, 1.0, 1.3):
            self.assertLess(fpl_tools._expected_concession_penalty(lam), 0.5 * lam)

    def test_zero_lambda_is_zero(self):
        self.assertEqual(fpl_tools._expected_concession_penalty(0.0), 0.0)


class ClubResolutionTest(unittest.TestCase):
    """The alias table was a 2024/25 club list; the live bootstrap is now primary."""

    def test_resolves_clubs_from_the_live_bootstrap(self):
        with harness.synthetic_world() as (bs, _fx):
            for team in bs["teams"]:
                self.assertEqual(
                    fpl_tools._canonical_club(team["name"], bs), team["short_name"])

    def test_tolerates_odds_feed_suffixes(self):
        with harness.synthetic_world() as (bs, _fx):
            self.assertEqual(fpl_tools._canonical_club("Arsenal FC", bs), "ARS")
            self.assertEqual(fpl_tools._canonical_club("Everton F.C.", bs), "EVE")

    def test_draw_is_not_a_club(self):
        with harness.synthetic_world() as (bs, _fx):
            self.assertIsNone(fpl_tools._canonical_club("Draw", bs))


class ModelVersionTest(unittest.TestCase):
    def test_version_is_stamped_and_past_the_stage_4_boundary(self):
        """Asserts the ORDERING, not a literal.

        Pinning the exact string here meant every later bump broke a Stage 4
        test that has nothing to do with the change -- which trains people to
        edit the assertion rather than think about it. What Stage 4 actually
        needs is that rows fitted before its Dixon-Coles centring can never be
        mixed with rows after it, and that is a "the version moved past v1"
        claim.
        """
        import re
        m = re.match(r"^v(\d+)-", fpl_tools.MODEL_VERSION)
        self.assertIsNotNone(m, f"unversioned stamp {fpl_tools.MODEL_VERSION!r}")
        self.assertGreaterEqual(int(m.group(1)), 2,
                                "pre-Stage-4 rows would be fitted alongside post-fix ones")

    def test_weights_are_reset_to_neutral(self):
        """A global_xP_modifier fitted against the old biased scale would undo
        this commit; it must be back at 1.0 with every key present."""
        import json
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "weights.json")
        with open(path) as fh:
            w = json.load(fh)
        self.assertEqual(w["global_xP_modifier"], 1.0)
        self.assertEqual(len(w), 23, "weights.json must carry every key, not a subset")
        self.assertLess(w["dixon_coles_tau"], 0.0, "football rho is negative")


if __name__ == "__main__":
    unittest.main()


class ClubAliasStalenessTest(unittest.TestCase):
    """C24, revisited. The alias map still names Ipswich, Leicester and
    Southampton -- relegated after 2024/25. Stage 4 fixed the DANGER by
    resolving against the live bootstrap first, but the fallback list was never
    pruned, and its containment loop would still return a code for a club that
    no longer exists."""

    def test_a_stale_alias_cannot_resolve(self):
        with harness.synthetic_world():
            bs, _ = harness.load_synthetic()
            shorts = {t["short_name"] for t in bs["teams"]}
            stale = [a for a, c in fpl_tools._CLUB_ALIASES.items() if c not in shorts]
            self.assertTrue(stale, "fixture happens to contain every alias; "
                                   "this test cannot detect the bug it exists for")
            for alias in stale[:5]:
                self.assertIsNone(
                    fpl_tools._canonical_club(alias, bs),
                    f"{alias!r} resolved to a club not in the live list")

    def test_current_clubs_still_resolve(self):
        with harness.synthetic_world():
            bs, _ = harness.load_synthetic()
            for t in bs["teams"]:
                self.assertEqual(
                    fpl_tools._canonical_club(t["name"], bs), t["short_name"])

    def test_the_map_is_documented_as_variants_not_a_roster(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "fpl_tools.py"), encoding="utf-8").read()
        head = src[max(0, src.index("_CLUB_ALIASES = {") - 700):src.index("_CLUB_ALIASES = {")]
        self.assertIn("not a club roster", head)
