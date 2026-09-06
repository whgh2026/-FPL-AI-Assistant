"""Replay the model against archived gameweeks and score it against reality.

This is the question the whole rebuild has been unable to answer: did any of it
help? Every stage so far has been justified by a defect argument -- the defence
sign was inverted, hits were charged 5x, bonus was a constant. Those arguments
are sound, but "this code was wrong" is not the same claim as "the forecast is
better", and only a replay can settle the second one.

    python scripts/backtest.py                     # every archived gameweek
    python scripts/backtest.py --from 5 --to 12    # a range
    python scripts/backtest.py --baseline ep_next  # compare against FPL's own
    python scripts/backtest.py --json out.json     # machine-readable

What it does NOT do, and cannot: re-run a HISTORICAL model. The archive stores
the bootstrap as it was, so the fixtures, prices, injuries and accumulated stats
are the ones the model could see at the time -- but the code doing the
projecting is today's. So this measures "how would the current model have done
on that gameweek", not "how did the model we shipped that week actually do". It
answers whether a change helps; it cannot reconstruct a past verdict. Comparing
two commits means running it on both.

The comparison that matters is against `ep_next`, FPL's own projection, which is
free, requires no modelling, and ships in the same payload. A model that cannot
beat it is not earning its complexity.
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import fpl_tools

MIN_ROWS_PER_GW = 20      # below this a gameweek's statistics say nothing
ACTIVE_MINUTES = 1        # only players who actually featured (C18)


def _rank_correlation(pairs):
    """Spearman rho. The ranking is what a transfer decision turns on -- being
    uniformly 0.5 points high costs nothing if the ORDER is right."""
    n = len(pairs)
    if n < 3:
        return None

    def ranked(values):
        order = sorted(range(n), key=lambda i: values[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx = ranked([p[0] for p in pairs])
    ry = ranked([p[1] for p in pairs])
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx and dy else None


def _score(pairs):
    """RMSE, bias and rank correlation for one set of (predicted, actual)."""
    n = len(pairs)
    if not n:
        return None
    rmse = math.sqrt(sum((p - a) ** 2 for p, a in pairs) / n)
    bias = sum(p - a for p, a in pairs) / n
    mae = sum(abs(p - a) for p, a in pairs) / n
    return {"n": n, "rmse": rmse, "bias": bias, "mae": mae,
            "rank_corr": _rank_correlation(pairs)}


def _actuals(gameweek):
    """{player_id: points} for players who actually featured.

    Filtered on minutes for the same reason auto_tune is (C18): roughly 60% of
    a bootstrap is players who did not play, all scoring zero against a near-zero
    projection, and including them measures how well the model predicts absence.
    """
    live = fpl_tools.get_live_event(gameweek)
    out = {}
    for pid, row in (live or {}).items():
        if row.get("minutes", 0) >= ACTIVE_MINUTES:
            out[int(pid)] = float(row.get("total_points", 0))
    return out


def replay_gameweek(gameweek, snapshot=None, actuals=None):
    """Project every player from the archived bootstrap, score against reality."""
    bootstrap = snapshot if snapshot is not None else db.load_bootstrap_snapshot(gameweek)
    if not bootstrap:
        return None
    if actuals is None:
        actuals = _actuals(gameweek)
    if not actuals:
        return None

    try:
        lookup = fpl_tools._build_fixture_lookup(bootstrap)
    except fpl_tools.FixtureDataUnavailable:
        return None

    ours, theirs = [], []
    for e in bootstrap.get("elements", []):
        pid = int(e["id"])
        if pid not in actuals:
            continue
        actual = actuals[pid]
        try:
            xp, _note = fpl_tools._player_xp(e, lookup, event=gameweek)
        except Exception:
            continue
        ours.append((float(xp), actual))
        ep = fpl_tools._to_float(e.get("ep_next"))
        if ep > 0:
            theirs.append((ep, actual))

    if len(ours) < MIN_ROWS_PER_GW:
        return None
    return {"gameweek": gameweek, "model": _score(ours), "ep_next": _score(theirs)}


def _fmt(s):
    if not s:
        return "        --"
    rc = f"{s['rank_corr']:+.3f}" if s["rank_corr"] is not None else "  --  "
    return f"{s['n']:>5}  {s['rmse']:6.3f}  {s['bias']:+6.3f}  {rc}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="gw_from", type=int, default=1)
    ap.add_argument("--to", dest="gw_to", type=int, default=38)
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args(argv)

    results = []
    for gw in range(args.gw_from, args.gw_to + 1):
        r = replay_gameweek(gw)
        if r:
            results.append(r)

    if not results:
        print("No archived gameweeks could be replayed.")
        print("The nightly archive (scripts/archive_bootstrap.py) needs to have "
              "run for at least one completed gameweek, and the results database "
              "must be reachable.")
        return 1

    print(f"Backtest -- model {fpl_tools.MODEL_VERSION}")
    print()
    print("            model                        ep_next (FPL's own)")
    print("  GW      n    RMSE    bias    rank      n    RMSE    bias    rank")
    print("  " + "-" * 68)
    for r in results:
        print(f"  {r['gameweek']:>2}  {_fmt(r['model'])}   {_fmt(r['ep_next'])}")

    agg = defaultdict(list)
    for r in results:
        for key in ("model", "ep_next"):
            if r[key]:
                agg[key].append(r[key])

    print()
    for key in ("model", "ep_next"):
        rows = agg[key]
        if not rows:
            continue
        n = sum(x["n"] for x in rows)
        rmse = math.sqrt(sum(x["rmse"] ** 2 * x["n"] for x in rows) / n)
        corrs = [x["rank_corr"] for x in rows if x["rank_corr"] is not None]
        rc = sum(corrs) / len(corrs) if corrs else float("nan")
        print(f"  {key:<8} n={n:<6} RMSE={rmse:.4f}  mean rank corr={rc:+.3f}")

    if agg["model"] and agg["ep_next"]:
        mn = sum(x["n"] for x in agg["model"])
        en = sum(x["n"] for x in agg["ep_next"])
        m = math.sqrt(sum(x["rmse"] ** 2 * x["n"] for x in agg["model"]) / mn)
        e = math.sqrt(sum(x["rmse"] ** 2 * x["n"] for x in agg["ep_next"]) / en)
        verdict = "BETTER than" if m < e else ("WORSE than" if m > e else "level with")
        print()
        print(f"  Verdict: the model is {verdict} FPL's own ep_next on RMSE "
              f"({m:.4f} vs {e:.4f}).")
        if m >= e:
            print("  That is the number to fix. A model that cannot beat the free "
                  "baseline shipped in the same payload is not earning its complexity.")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"model_version": fpl_tools.MODEL_VERSION,
                       "gameweeks": results}, fh, indent=2)
        print(f"\n  Written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
