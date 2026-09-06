"""
Nightly archive: store the raw FPL bootstrap so gameweeks can be replayed later.

This exists so that "did that change actually help?" becomes an answerable
question. The Stage 8 backtester replays the model against the data it could see
at the time; without an archive there is nothing to replay, and no amount of
later work recovers a gameweek that was never captured. That is why this runs
from Stage 0 rather than alongside the backtester it feeds.

One row per (gameweek, date), gzipped: ~250KB per snapshot against ~3MB raw, so
a full season of nightly captures is well under 100MB.

Idempotent -- re-running on the same day overwrites that day's row.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fpl_tools
import db


def main() -> None:
    if not db.run_migrations():
        print("archive: database unreachable, nothing captured", file=sys.stderr)
        sys.exit(1)

    bootstrap = fpl_tools._get_bootstrap()
    gw = fpl_tools._next_gameweek(bootstrap)

    if not db.save_bootstrap_snapshot(bootstrap, gw):
        print(f"archive: failed to persist GW{gw} snapshot", file=sys.stderr)
        sys.exit(1)

    print(f"archive: stored GW{gw} bootstrap "
          f"({len(bootstrap.get('elements', []))} elements, "
          f"{len(bootstrap.get('teams', []))} teams)")


if __name__ == "__main__":
    main()
