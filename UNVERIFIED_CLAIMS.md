# Unverified claims — Tier 6 & 7

Every audit-derived claim in Tiers 6 and 7 was checked against the real source
before any patch was written. This file records the ones that **did not
reproduce**. They were skipped, not "fixed": patching code that does not have
the claimed defect is how regressions get introduced under the appearance of
diligence.

Claims that *did* reproduce were patched and are covered by
`tests/test_phase_audit_items.py`.

---

## Tier 6.1 — Visual asset decoupling (badge / headshot mismatch)

**Claim.** `_headshot_img` / `_badge_img` use a raw `team` int fallback, so the
badge shows the wrong club when `team_id` is missing — reported as a Morgan
Rogers Villa/Chelsea mismatch. Proposed fix: a `_resolve_team_id(ref)` helper
accepting ints, short codes (`"AVL"`) and team codes.

**Verdict: NOT REPRODUCED.**

**What the source actually does.** `app.py:482-486`:

```python
team_id = p.get("team_id")
if team_id is None and isinstance(p.get("team"), int):
    team_id = p.get("team")
```

The `isinstance(..., int)` guard is the thing that makes this safe, and the
two conventions in the codebase never collide:

- **Bootstrap-shaped dicts** (`app.py:646`, `:720`) set `"team": e.get("team")`,
  and a bootstrap element's `team` **is** the team id. Using it as one is
  correct.
- **Engine-shaped dicts** (`fpl_tools.py:2615`, `:3223`, `:4833`, `:4990`,
  `:5038`, `:5186`) set `"team": teams_by_id.get(...)` — a **name string**. The
  `isinstance` check rejects it, so no lookup happens.

A grep for any assignment of `team_code` into a `"team"` key returns nothing,
so there is no path where an integer `team` resolves to a *different* club.

**The worst case is a MISSING badge, never a wrong one.** When `team_id` is
absent and `team` is a name string, `badge` stays `""` and no overlay renders.
`_badge_img` itself already degrades honestly for an unmapped id, rendering
short-name initials rather than a wrong crest (`app.py:533-540`).

The two call sites that pass `r["team"]` straight in as an id
(`app.py:667`, `:2919`) are fed by the bootstrap-shaped rows above, so they
receive a genuine team id.

**What would change the verdict.** A concrete reproduction: a player dict that
reaches `_headshot_img` with no `team_id` and an integer `team` that is a team
*code* rather than an id. I could not construct one from any live call site. If
the original Rogers report came from a real screenshot, the likely cause is
upstream of these helpers — a squad row built without `team_id` at all — and
that is a different fix from the one proposed.

---

## Tier 7.9 — `grid_mass` in `_DC_RAW`

**Claim.** `grid_mass` should be included in the raw Dixon-Coles fit stored in
`_DC_RAW`.

**Verdict: NOT REPRODUCED.**

`grep -c grid_mass fpl_tools.py` returns **0**. No such quantity exists
anywhere in the codebase — not in `_fit_dixon_coles`, not in `_DC_RAW`, not in
`_fixture_lambdas`. There is nothing to include and no consumer that wants it.

`_DC_RAW` currently stores `{"ratings", "gamma", "mu", "rho"}`, which is the
complete parameter set `_fixture_lambdas` needs to reconstruct both scoring
rates plus the tau correction.

**What would change the verdict.** A definition of what `grid_mass` is meant to
be — most plausibly the normalising mass of the bivariate scoreline grid, which
would be a genuine addition if some consumer needed exact scoreline
probabilities rather than the two lambdas. Nothing currently does.

---

## Not assessed

These Tier 7 items were neither verified nor patched in this pass, and are
listed so the gap is explicit rather than implied:

| Item | Why not assessed |
|---|---|
| 7.4 no-hit override as a hard constraint | Needs a behavioural repro of `allow_hits=False` being violated; not attempted. |
| 7.5 FT state machine (`F_{t+1} = min(5, R_t)`) | Touches Invariant I-6; needs its own verification pass. |
| 7.6 Tightrope double-count | `TIGHTROPE_DISCOUNT == 0.85` confirmed present and applied once in `_player_xp_horizon`; the *double*-count claim was not independently reproduced. |
| 7.7 GK lock units | An open design decision, not a defect with a single correct answer. |
| 7.8 Wildcard chip score normalisation | Same: a calibration choice. The Bench Boost contamination half of it WAS fixed, in Tier 1.1. |
| 7.11–7.17 | Request time budget, banner/stale inverts, replay provenance keys, dead-code sweep, set-piece heuristics, calibration parity, numerics. Several are research-scale rather than patches. |
