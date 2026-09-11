#!/usr/bin/env python3
"""One P-A pass, as a command. The unit Rote captures and replays.

    python scripts/judge-day.py            # advance the clock and judge the day
    python scripts/judge-day.py --day 7    # re-judge a day without advancing

Everything else in the repo reaches P-A through a function call or an MCP tool.
A play is captured against a *process*, so the pass needs a command-line shape
too — and it has to be one that runs from any working directory, because Rote
runs it from inside the workspace rather than from the repo.

Prints the run summary as JSON on stdout, which is what the play returns.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.clock import DayClock                        # noqa: E402
from agent.pipeline import close_day, open_day, rank_day  # noqa: E402
from insight.store import Insight                       # noqa: E402
from memory.graph import GraphStore                     # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", type=int, default=0,
                        help="judge this day instead of advancing the clock")
    parser.add_argument("--title-like", default="")
    parser.add_argument("--location", default="")
    parser.add_argument("--remote-only", action="store_true")
    args = parser.parse_args()

    insight, store = Insight(), GraphStore()
    clock = DayClock.load()
    if args.day:
        clock.day = args.day

    # The same three stages the MCP tools expose, in the same order — so a play
    # captured here replays the path the orchestrator takes, not a shortcut.
    pass_ = open_day(store, clock, advance=not args.day)
    result = rank_day(pass_, insight, store, title_like=args.title_like,
                      location=args.location, remote_only=args.remote_only)
    close_day(pass_, insight, store)

    payload = result.summary()
    payload["digest"] = result.digest_text
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
