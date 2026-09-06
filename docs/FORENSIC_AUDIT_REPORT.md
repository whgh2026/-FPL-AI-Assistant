# FPL Quant Engine — Forensic Audit & Rebuild Report

**Branch:** `claude/fpl-quant-ui-model-review-js5x46`
**Scope:** full audit of the projection model, solver, calibration loop, data
integrations and UI, followed by a nine-stage rebuild.
**Result:** 20 commits, 45 files, ~22,300 insertions. Test suite grew from
essentially none to **304 tests plus 60 subtests**, green under both the
interactive and deterministic solver profiles.

---

## 1. Why this document exists

Three complaints started this:

1. the recommended transfers were not good;
2. the UI was disjointed and messy in places;
3. the copy was written in data-science jargon rather than plain English.

All three turned out to have concrete, findable causes. This report records
what was wrong, what was changed, what was **measured**, and — importantly —
what remains unverified. The last category is the one worth reading twice.

A note on the standard applied throughout: *"this code was wrong"* and *"the
forecast is now better"* are different claims. Almost everything below
establishes the first. Only the backtester (§7) can establish the second, and
it needs live data that did not exist while this work was done.

---

## 2. Headline findings

The single largest defect was a **sign inversion in the Dixon–Coles defence
rating**, compounded by an **identification failure** in the fitter.

`_fit_dixon_coles` centred the attack parameters but not the defence ones, so
the defence term silently absorbed the league mean scoring rate. Verified
numerically against a synthetic double round-robin with known parameters
(μ = ln 1.42, γ = 0.25):

```
mean(att) = -0.0000    mean(dfn) = -0.3291    gamma = 0.354    rho = 0.069
exp(-mean dfn) = 1.390     (true league goals/team/match = 1.42)
```

`dfn` was absorbing μ almost exactly. The downstream effect on
`def_adj = 3.0 / opp_def`, the multiplier applied to every attacker's expected
goals:

| Opponent defence | Before | Sign fix only | Sign + centring + μ |
|---|---|---|---|
| Elite | **1.058** ← *boosted* attackers | 0.948 | **0.785** ✓ |
| League average | 0.820 | **1.281** ← +28% vs neutral | **1.000** ✓ |
| Worst | 0.626 ← *suppressed* attackers | 2.484 | **1.608** ✓ |

Read the first column again: **the model was rewarding attackers for facing the
best defences in the league and penalising them for facing the worst.** That
alone accounts for a great deal of complaint (1).

Two further findings from the same run: γ came back at 0.354 against a true
0.25 (a 40% over-estimate of home advantage, because γ and μ are confounded
without an explicit intercept), and ρ initialised at +0.2 barely moved — the
gradient was under-normalised, and real football ρ is *negative*.

A second, subtler point: this defect was **masked** for the early season. The
`dc_blend = min(1, n/100)` clamp pins every club's rating at 5.0 until roughly
GW10, so fixture difficulty was inert rather than inverted. There was no point
in the season at which fixture handling was correct — only a handover from
"switched off" to "backwards".

---

## 3. Defects found and fixed

### 3.1 Solver and objective

| # | Defect | Consequence |
|---|---|---|
| T0.4 | **Hit cost charged at 5× on some paths.** | The engine refused hits that were clearly correct. |
| T0.5 | **No formation constraints on the MIP.** | The solver could construct 1-10-0 or 1-2-7 "starting XIs" and then buy players to serve that phantom. |
| C1 | **Captain valued on 4-GW horizon xP, not single-GW.** | The armband is re-chosen weekly; this over-rewarded the best horizon asset by ~3.1 gameweeks of points and distorted which premium got bought. |
| C2 | **Two post-solve vetoes double-gated the new in-objective hurdle.** | A good multi-week move was binned because its *immediate* gain was under 1.5. |
| C3 | **`_get_moves` recomputed net gain from a different formula than the objective.** | The displayed number could not reconcile with the decision. |
| C4 | `roll_hurdle` matched an un-aliased risk string, so "rank protecting (shield)" silently fell back to 1.5. | Strategy quietly ignored. |
| C5 | **Move pairing was fabricated** — sold and bought sorted by xP within position and `zip`ped. | Every per-row gain, rationale and badge was built on an invented 1:1 mapping. |
| — | `bank[0]` double-counted sold players' equity in the multi-GW planner. | The plan believed it had money it did not have. |

Also removed: an unbounded liquidity bonus that paid **+2.8 xP for a £7m
downgrade** (now capped at 0.05 xP per £1m, £1m total, terminal only, and
labelled decision utility rather than football xP), and a floor penalty that
applied at zero hits.

### 3.2 The projection

| # | Defect | Consequence |
|---|---|---|
| C6 | **Every defender received an identical +1.36 DefCon.** `influence` is season-cumulative, so `min(influence/60, 2.0)` saturated at 2.0 for every regular. **No per-player defensive data was read anywhere.** | A flat ~1.25-point positional bias toward defenders in every comparison the solver made. |
| C7 | **Bonus was three hardcoded constants.** Salah, Haaland and a £4.5m bench filler received identical bonus xP. `bps` was read elsewhere for the live tracker and **never used in the projection**. | Bonus is 8–10% of all FPL points and among the most persistent player-level signals. A term identical for every player at a position is not a weak signal — it is none. |
| C8 | **The cameo clean-sheet paradox.** `frac` sat inside the exponent, so a 30-minute appearance implied P(CS) = 65% and a 90-minute one 27%. | Rotation-risk defenders systematically over-valued. |
| C9 | **`EP_BLEND = 0.5`** — half of every projection was FPL's own `ep_next`, which already embeds fixture difficulty. | Double-counted fixtures, and capped the benefit of the entire Dixon–Coles fix at ~50%. |
| C10 | **No recency on minutes.** Season-long `starts/games`. | A player benched ten matches then nailed for five read as 0.33. Minutes *multiply* every other component, so this is "sell the nailed player, buy the one who has been dropped". |
| C11 | **Cards were flat** (−0.12 / −0.05) with `yellow_cards` sitting unread two functions away. | No per-player booking risk, and no scaling by exposure. |
| C12 | **SAA erased the tightrope discount**, overwriting `p["xp"]` with a mean rebuilt from projections that never saw it. | The suspension haircut was reverted in the one place it was meant to change a decision. |
| C13 | **DGW/BGW consumers saw only the first fixture** (three `next(...)` call sites). | Doubles under-counted. |
| C27 | **The suspension tightrope counted gameweeks, not the club's matches played.** | The 5- and 10-yellow windows opened and closed on the wrong week for exactly the clubs whose fixtures the engine models. |
| C28 | **Two competing definitions of expected minutes**, and which you got depended on the call site. | One knew nothing about rotation, so a fit rotation risk returned a confident 1.0. |

### 3.3 The calibration loop — inert, and silently so

Every defect here shares a property: **nothing errors.** The job runs, prints a
cheerful line, and the weights do not move — or move for the wrong reason.

| # | Defect | Consequence |
|---|---|---|
| C14 | **`DAMPING = 0.05` against a ±10% probe** capped movement at 0.5% per run — 53 weekly runs to move a weight 30%, against a 38-gameweek season. The convergence test passed `damping=1.0`, **20× the shipped value**. | `weights.json` could not meaningfully change within a season, and the production setting was never exercised. |
| C15 | **`dc_sensitivity` was written as a literal `0.0`.** The surrogate multiplies it by `(decay − default)`. | `dixon_coles_decay` — one of the four parameters the tuner is supposed to fit — had an identically zero gradient and could never move in either direction. |
| C16 | **The surrogate did not match production.** The real cameo penalty is *clamped*; the surrogate charged it unclamped, inventing a gradient where production has none. Penalties are applied *before* the `ep_next` blend, so a unit change moves the projection by `(1 − ep_w)`, not 1.0. | Coordinate descent optimised a model the app does not run, over-attributing by up to 2×. |
| C17 | **No floor on the Poisson deviance.** One zero-or-negative prediction against a 12-point haul contributed **+331** to a metric whose typical row is order 1. | A single bad row outweighed several hundred good ones, and the descent chased it. |
| C18 | ~60% of training rows were non-players. | The fit was dominated by `(base ≈ 0.2, actual = 0)`. |
| C19 | `conn.commit()` sat outside the per-GW loop. | A failure on GW *n+1* discarded GW *n*. |
| C20 | **`_WEIGHTS_CACHE` had no TTL and no mtime check.** | A long-running process pinned the weights it read at boot, so the weekly re-tune never reached the live app without a redeploy. |
| C21 | **`fpl_predictions` and `manager_transfer_ledger` had no `CREATE TABLE` anywhere**, and `ensure_calibration_columns` was called only from the Wednesday job, never from the Friday writer. | A fresh database crashed on its first snapshot. |
| C22 | `DELETE FROM fpl_predictions WHERE gameweek=%s` combined with a `workflow_dispatch` that ran all three steps. | A manual run destroyed the current gameweek's paired data. |

The tuner now also fits on a deterministic train split and **refuses to write
when the held-out metric worsens**. It previously wrote unconditionally.

### 3.4 Data, integrations and resilience

| # | Defect | Consequence |
|---|---|---|
| C24 | `_CLUB_ALIASES` was a stale 2024/25 club list (Ipswich, Leicester, Southampton). | Any unmapped club **silently lost its odds** and fell back to static FDR with no signal. See the correction in §7.7 — the hazard is fixed, but not the way an earlier draft of this report described. |
| C25 | The `totals` market was never requested despite the spec claiming it. | The best available clean-sheet signal was left on the table. |
| C26 | `get_live_event` ignored its `gw` argument. | Two different gameweeks within 60s returned the first one's data. |
| C29 | **The AI critiqued a different plan than the screen showed.** With a Wildcard confirmed, the UI rendered `wildcard_transfers` while the prompt was fed the standard 1-transfer plan. | Confident review of something the user could not see. |
| C30 | **OCR name matching took the first bootstrap element whose full name merely *contained* the string.** | "Son" resolved to whichever of Jackson, Wilson, Robertson or Son sat earliest in the file — silently analysing a squad the user does not own, with nothing downstream able to detect it. |
| C31 | `gemini_summary.py`: 50 lines, never imported, and containing **no LLM call at all** despite the name. | Dead code; the actual Gemini call lives in `squad_override.py`. |
| C33 | `_build_fixture_lookup` was not memoised, across 50+ call sites. | Every call re-walked and re-sorted every fixture, refetched the odds and re-read the ratings. |
| C34 | `score_my_squad` walked back up to **38 sequential uncached requests**. | And that slow path was the *common* one — it only triggers for a manager who has not set a team yet. |
| C35 | The dropdown builder ran a full projection for **every player in the game** to build a label string, on every rerun. | Typing one character in the Manager ID field re-projected ~700 players. |
| C36 | **`_build_fixture_lookup` returned `{}` on any fixture-fetch failure.** | Every player became "Blank" at 0.0 xP, so a squad read as fifteen worthless assets and the engine recommended selling all of them. **A total outage rendered as confident advice.** |
| C37 | Two independent session-state caches of the same data, neither with a TTL. | Stale for the whole browser session; price changes and injury flags never refreshed. |
| C38 | Ten CSS classes styled light-only and never overridden. | Rendered light-on-dark. |
| C39 | **Zero version pins**, no `Procfile`, and `python-dotenv` installed but never imported. | `.env` files were silently not loaded — worse than absent, because it looks like they would be. |
| — | No `connect_timeout` on any database connection. | An unreachable database did not fail, it **hung**, and took the page render with it. |

---

## 4. What changed in the UI

The information architecture is now four tabs named for the reader — **My Plan
/ Fixtures / Players / Model Health** — replacing names that described the code
rather than the content.

- **Squad Scorecard** replaces a three-metric xP row that only said what the
  squad would *score*. It now also says how it is *built*: starting XI points,
  money on the bench, who's nailed on, and set-piece takers. A 60-point XI
  resting on four rotation risks and £22m of bench is a different proposition
  from the same 60 points on eleven nailed starters.
- **Model Health** is new, and it closes an overclaim. The hero said you could
  check the model's homework; there was nowhere to check it.
- **Reasoning now renders above the decision.** Accept/Hold used to sit above
  the three explanation panels, so the reader was asked to commit their
  gameweek and could only then scroll down to why.
- **Five stylesheets collapsed into one tokenised `static/app.css`**, with no
  hardcoded hex outside `:root` — now an enforced test rather than a line in a
  plan, which is why it had drifted.
- **Copy pass** throughout, in British English. `Ω(f) bundling` → *"The next few
  weeks"*. `Stranded bench capital` → *"Money sat on your bench"*. `Deadlock:
  bank £0.3m cannot fund the cheapest formation-pivot swap` → *"You're stuck:
  £0.3m isn't enough to switch shape, even with your cheapest swap."*

One deliberate omission: **there is no separate Squad tab.** The squad views are
produced by the planner wizard, so a tab of their own would sit empty until a
plan had been run and then duplicate what the plan already shows — more
disjointed, not less.

---

## 5. Honesty repairs

Several fixes exist purely because the app was claiming things it did not do.

- The hero claimed *"the deeper we get into the season, the smarter and more
  ruthless the algorithm becomes."* With the active-player filter, the 5,000-row
  recalibration threshold is **~18 gameweeks**, not the ~7 the unfiltered
  arithmetic implies. The status line is now generated from a **live row count**
  rather than a hardcoded gameweek, so it cannot be wrong.
- The UI described the solver's output as *"the mathematically optimal squad"*
  while the interactive profile returns a time-limited incumbent with no proven
  bound. It now says *"best plan found in the time available"* when the gap is
  unproven.
- `📈 24h Ownership Momentum` read `transfers_in_event`, which is
  gameweek-cumulative. The label was simply wrong; it is now *"This week's ins
  and outs"*, which matches the data.

---

## 6. Verification

| Gate | Status |
|---|---|
| Squad legality (2/5/5/3, 3–5/2–5/1–3 XI, ≤3 per club) on all four solve paths | ✅ 20/20 squads |
| Layer 1 invariant to strategy, bitwise | ✅ |
| Blocker mode changes the squad at GW8 | ✅ |
| Hit charged exactly 4.0; displayed net gain == Σ decomposition | ✅ |
| `abs(mean att) < 0.01`, `abs(mean dfn) < 0.01`, `0.15 < γ < 0.40`, `1.2 < exp(μ) < 1.8` | ✅ |
| Elite defence yields a *lower* `def_adj` than the worst defence | ✅ |
| `matrix[pid].shape == (S, n)`; `p5 ≤ p50 ≤ p95` over 500 scenarios | ✅ |
| P(CS) invariant to expected minutes; DefCon varies across defenders | ✅ |
| Bonus and cards vary within a position | ✅ |
| Cold render < 3s | ✅ **0.17s** |
| Every card degrades visibly when the FPL API is stubbed to fail | ✅ |
| No hardcoded hex outside `:root`; no light-only class survives | ✅ |
| Suite green under the deterministic solver profile | ✅ 304 passed |

### Bugs found *by* the tests

Worth recording, because they are the argument for having written them:

- **An infeasible solve returned 15 players with a 10-man XI.** The harness
  accepted any result where `len(selected) == 15`; infeasible solves leave stale
  variable values behind. Only `"Not Solved"` now carries a usable incumbent.
- **A failed `element-summary` fetch was retried on every render.** The skip
  check keyed on cache membership, so the players already known to fail were
  exactly the ones costing a doomed request each time.
- **Team ratings were rounded to 2dp**, which quantised `def_adj` — and
  forwards, who carry no clean-sheet or conceded term, had *no* remaining path
  for opponent strength to reach them at finer resolution. Every forward
  measured a decay sensitivity of exactly zero. Found while measuring the C15
  derivative, not by reading the code.
- **A copy change broke two tests**, because they looked diagnostic checks up by
  their *user-facing label*. Each check now carries a stable `key`. Copy should
  never be an identifier.

### Fixture defects found

The synthetic fixture itself was wrong in ways that would have made tests
measure artefacts:

- `bps` was written as `220 * quality`, **independent of minutes** — a
  100-minute fringe player carried the same season BPS as a 900-minute regular,
  about 180 BPS per 90, roughly five times anything real.
- A test comment described team 2 as the strong defence when the generator
  makes it the *weak* one. The assertion was right so it passed — but that
  misreading is plausibly what produced the sign bug in the first place.

---

## 7. What is **not** verified

This section is the one to read before trusting any of the above.

1. **Nobody has established that the model is good.** Every fix above is
   justified by a defect argument. `scripts/backtest.py` now exists to answer
   the real question — RMSE, bias and rank correlation against FPL's own
   `ep_next` — but it needs archived gameweeks, and the archive starts
   accumulating only once this deploys. **A model that cannot beat `ep_next` is
   not earning its complexity**, and that verdict is currently unknown.

2. **ρ is not validated.** The `−0.25 < ρ < 0.05` gate describes real football
   and cannot be checked offline: an independent-Poisson generator has a true ρ
   of 0, and a rejection sampler applying the τ correction reweights the
   marginals, shifting the very μ and γ it is meant to hold fixed (γ drifted to
   0.32/0.18 against 0.248 in the clean run — the sampler, not the fitter). The
   gradient normalisation is justified analytically and ρ now initialises
   negative, but the *estimate* is unverified.

3. **The `strength_overall_*` scale premise is unconfirmed.** The build
   environment's proxy denies `fantasy.premierleague.com` outright. Mitigated:
   the fix min-max normalises the *observed* values, so it produces the same
   [1,5] output whether the field is 1000–1400 or 1–5. Still worth one live
   check (see the deployment checklist).

4. **The 0.6–1.1 transfers/week sanity band could not be gated.** Best available
   single-swap gain across the 20 synthetic squads takes exactly two values —
   2.11 and 14–17 — because the generator lays players on a smooth quality
   ladder. A sweep of the hurdle constants returned only 0.45, 0.50 or 1.20 per
   week at every setting. Constants were left at reviewed values rather than
   tuned to an artefact. **Measured for judgement:** σ median 0.49, max 2.05 on
   the horizon scale ⇒ implied bar ≈ 1.0 per leg, 2.0 per swap.

5. **`BONUS_CONVEXITY = 1.6` and `FT_OPTION_MARGINAL` are uncalibrated
   constants** standing in for fits that need real history. Tagged for the
   backtester.

6. **The test suite runs with the minutes-recency prefetch disabled**, because
   there is no FPL API in the build environment. The suite therefore exercises
   the *season* minutes path by default; the recency path is covered by
   populating the cache directly.

7. **Correction — C24 was described wrongly in the first draft of this report.**
   Attempting the pre-merge live checks surfaced that `_CLUB_ALIASES` still
   contains Ipswich, Leicester and Southampton, relegated after 2024/25. The
   earlier text said the list had been "refreshed to the current 20 clubs". It
   had not been.

   What Stage 4 actually did was better than a refresh, and it does fix the
   danger: `_canonical_club` now resolves against the **live bootstrap first**
   — exact match on club name or short name, then containment, longest name
   first — so every club currently in the Premier League resolves from FPL's
   own data and cannot go stale. The alias map is only consulted afterwards.

   The stale entries were still a live hazard through the fallback's
   containment loop, which would return `LEI` for an odds feed mentioning
   Leicester: a code no current club holds, matching nothing downstream and
   losing that fixture's odds silently. Every alias lookup is now **gated on
   the live club list**, so a stale entry is inert by construction rather than
   by anyone remembering to prune it, and the map is documented as a
   name-variant supplement rather than a roster.

   Deliberately **not** done: rewriting the list to the 2026/27 clubs. That
   would mean asserting a league composition this build cannot verify, and
   writing a guessed roster into the code as fact is worse than the stale list
   it replaced. The bootstrap already knows, and now the code asks it.

---

## 8. Model versioning

`MODEL_VERSION` gates which rows the calibrator will fit together. It bumped
**four** times, not the three originally planned:

| Version | Stage | What changed in the forecast |
|---|---|---|
| `v2-dc-centred` | 4 | Dixon–Coles centring, μ, strength scale, de-vig, conceded points |
| `v3-layer1-clean` | 2 | Momentum, ownership and risk tilts removed from xP |
| `v4-xp-overhaul` | 5b | `EP_BLEND` taper, clean sheets, DefCon, Beta prior on start rate |
| `v5-continuous-ratings` | 8b | Team ratings no longer rounded to 2dp |
| `v6-bonus-and-cards` | 8c | Bonus and cards become per-player |
| **`v7-minutes-recency`** | 8c | Exponentially weighted minutes; doubt redistribution |

v5 was tiny in effect (max 0.005 points, 0.097% relative) and bumped anyway.
The rule is applied **without a size exemption on purpose**: the point of the
stamp is that nobody has to adjudicate whether a change was "big enough".

**Consequence:** the usable calibration archive starts from `v7`. At ~280
filtered rows per gameweek against `MIN_ROWS = 5000`, calibration first fires
roughly 18 gameweeks after deployment. The UI copy derives this from a live row
count, so it stays honest as the figure moves.

---

## 9. Commit index

| Commit | Stage |
|---|---|
| `5241e57` | Housekeeping — bytecode ignore |
| `81bfb03` | Stage 0 — offline golden-file harness, deterministic solver profiles |
| `19e9c8e` | Stage 1 — legal starting formations |
| `c5e75f3` | Stage 0 (cont.) — schema migrations, archiving, ingest repairs, CI |
| `3394d9d` | **Stage 4 — Dixon–Coles identification, strength scale, Blocker A (atomic)** |
| `2a5d9ad` | Stage 2 — strategy leaves the forecast, re-homes in the objective |
| `2005414` | Stage 3 — solver economics, honest hit pricing, single search brake |
| `8a1ccb0` | Stage 5 — scenario axis, blank vs bad fixture |
| `412889c` | Stage 5b — the player forecast (Tranche 1) |
| `7eb686c` | Stage 6 — chip isolation, reservation curves, solver-validity bug |
| `757f789` | Stage 7a — resilience |
| `e88de77` | Stage 7b — one tokenised stylesheet |
| `d793272` | Stage 7c — copy pass, objective waterfall, honest hero |
| `7cbbdc4` | Stage 7d — IA, Squad Scorecard, Model Health |
| `579a8dd` | Stage 7e — OCR matching, dead code |
| `5354208` | Stage 8a/8b — pins, Procfile, calibration loop repairs |
| `e5e9b56` | Stage 7 completion — four items the build had missed |
| `387dd15` | Stage 8c — bonus, cards, tightrope, single minutes definition |
| `04c0b66` | C10b — minutes recency |
| `4f26616` | Stage 8d — connection pooling, structured DB errors, backtester |

---

## 10. Reviewer reconciliation

This work was reviewed against two independent expert reviews. Points where the
audit was **wrong and conceded**:

- **SAA matrix layout.** Transposing to `(n, S)` was recommended and was worse —
  `(S, n)` is the standard Monte Carlo layout and enables contiguous matvec.
- **Backtest-first sequencing.** Framing a replay harness as a gate before Tier 0
  created a dependency deadlock.
- **Stage ordering.** Stage 1 must precede Stage 4: feeding sharper fixture
  ratings into a solver that still lacks formation bounds is *worse* than
  feeding it inverted ones, because better signal means the MIP more
  aggressively constructs whatever phantom XI maximises xP.
- **Card copy.** `Floor / Expected / Ceiling` beats `Bad week / Typical / Dream
  week` — floor and ceiling are native FPL vernacular, so they are
  simultaneously plainer *and* more precise.

One deviation from the reviewed spec, approved: **the bench cap applies to
chip and construction solves, not to standard gameweeks.** On a standard
gameweek the fifteen are fixed, so the only way to satisfy a bench-cost cap is
to change *who starts* — forcing price into a decision that should be made on
expected points. Measured: a £16m cap cut the XI by **12.7 points**, *raised*
bench cost, and made the program infeasible where a squad could not comply.
