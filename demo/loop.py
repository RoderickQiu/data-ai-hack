"""The timed loop that seeds the curve, and the local harness behind it.

From hour 3 the loop runs on a timer through the rest of the build, one day per
tick, with a teammate giving real feedback as digests arrive. By demo time the
chart has 20+ genuine points and the live run is simply the last point on an
existing curve. Nothing is fabricated; the feedback was just given earlier in
the day than the pitch (DESIGN §7).

Two ways to tick, and the difference matters for the pitch:

* ``--webhook URL`` curls the RocketRide ``webhook`` source, so **every point on
  the chart is a real RocketRide run**. This is the honest one and the default
  when a URL is configured.
* ``--local`` runs the same pipeline in process. It is the debugging harness
  from DESIGN §11 — useful when the tunnel is down, never the demo path, since
  judges need RocketRide load-bearing.

``--auto-feedback`` answers the digests from a small persona so the loop keeps
producing rows unattended. It is off by default and the chart should be seeded
with real human responses; use it only to keep the loop warm between people.
"""

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.parse
import urllib.request
from typing import Any, Mapping, Sequence

from agent.clock import DayClock
from agent.config import env
from agent.feedback import record_response
from agent.pipeline import judge_the_day
from agent.schema import USAGE_RUN_SUFFIX, is_usage_row, now_iso
from insight.store import Insight
from memory.graph import GraphStore
from rocketride.client import RocketRide, RocketRideError


def webhook_url(raw: str) -> str:
    """Check a ``--webhook`` target before anything is sent to it.

    The value arrives from the command line or ``ROCKETRIDE_WEBHOOK_URL``, and
    the request carries ``ROCKETRIDE_APIKEY`` in a header — so the scheme has to
    be pinned. urllib will happily open ``file://`` or ``ftp://`` from the same
    call, which would turn a typo'd env var into a local file read with a
    credential attached. http is allowed because the tunnel is sometimes plain
    http in the room; anything else is a mistake, not a configuration.
    """
    parsed = urllib.parse.urlsplit((raw or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{raw!r} is not an http(s) webhook URL")
    return parsed.geturl()


def tick_webhook(url: str, payload: Mapping[str, Any], bearer: str = "",
                 timeout: float = 300.0) -> dict[str, Any]:
    """One real RocketRide run."""
    request = urllib.request.Request(
        webhook_url(url), data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {bearer}"} if bearer else {})},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"status": response.status, "body": body[:400]}


def token_of(url: str) -> str:
    """The task token out of the webhook URL the loop is already ticking.

    ``up`` prints ``…/webhook?token=tk_…`` and that token is the handle for
    ``GET /task`` — so the loop can read a run's usage back without being told
    anything it does not already have.
    """
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return (query.get("token") or [""])[0]


def attribute_usage(insight: Insight, client: RocketRide, token: str,
                    limit: int = 500) -> dict[str, Any]:
    """Read one run's token usage off the engine and put it on the chart.

    The tokens the pitch is actually about are spent by the orchestrator on the
    far side of the MCP boundary, so ``RunMetrics`` never sees them and the run
    row it writes carries zero. This closes that gap after the fact: poll
    ``GET /task``, ask :meth:`RocketRide.usage` whether the engine reports
    counters, and if it does, attach them to the run that just happened.

    **Written as its own row rather than as an edit to the run's**, permanently
    and not just until ``log_run``'s append-with-``key`` semantics are pinned
    down. A distinct ``run_id`` is correct whether that is an upsert or a plain
    append, so verifying it would buy nothing; summing is what a day roll-up
    does anyway, so a second row is the natural shape rather than a workaround;
    and an upsert would mutate a row after the run that produced it had
    finished, which is the thing the plan rejects a write-back for in the first
    place. The suffix is ``schema.USAGE_RUN_SUFFIX`` because the reader has to
    agree with this exactly — two copies of that string drifting apart shows up
    as a day counted twice, which is the hardest kind of wrong number to spot on
    a chart.

    Returns the usage dict. ``reported: False`` is the answer that matters —
    it means the cost line has to be the MCP-boundary proxy instead, and the
    dashboard's ``cost_basis`` will say so rather than captioning a proxy as a
    token count.
    """
    usage = RocketRide.usage(client.status(token))
    if not usage["reported"]:
        return usage
    rows = insight.run("runs_series", {"limit": limit})
    # A usage row is not a run, so it is not a candidate to attach usage to —
    # otherwise a second poll would write `<run>-usage-usage` and count the same
    # tokens twice on the same day.
    runs = [row for row in rows if row.get("run_id") and not is_usage_row(row)]
    if not runs:
        return {**usage, "attached_to": None}
    latest = max(runs, key=lambda row: str(row.get("started_at") or ""))
    run_id = f"{latest['run_id']}{USAGE_RUN_SUFFIX}"
    insight.log_run({
        "run_id": run_id, "started_at": now_iso(), "day": latest.get("day"),
        "mode": latest.get("mode"), "pipeline": latest.get("pipeline") or "P-A",
        "wall_ms": 0, "tokens_in": usage["tokens_in"], "tokens_out": usage["tokens_out"],
        "steps_reasoned": 0, "steps_replayed": 0, "plays_used": "",
        "tool_calls": 0, "tool_bytes": 0, "questions_asked": 0, "human_touches": 0,
        "values_from_memory": 0, "values_replayed": 0, "values_reasoned": 0,
        "jobs_released_today": 0, "pool_size": 0, "shown": 0,
        "predicted_keep": 0, "actual_keep": 0,
        "precision_at_5": None, "prediction_accuracy": None,
    })
    return {**usage, "attached_to": latest["run_id"], "run_id": run_id}


def tick_local(insight: Insight, store: GraphStore, clock: DayClock,
               auto_feedback: bool = False, seed: int = 0) -> dict[str, Any]:
    """One pass through the same code the pipeline calls. Debugging only."""
    result = judge_the_day(insight, store, clock, advance=True)
    payload = result.summary()
    if auto_feedback:
        signals = _persona_response(result.rank.digest_rows(), seed)
        predictions = {p.job_id: p.predicted for p in result.rank.slate}
        feedback = record_response(store, insight, day=result.day,
                                   run_id=result.run_id, signals=signals,
                                   predictions=predictions)
        result.metrics.score_responses(feedback["resolved_predictions"])
        insight.log_run(result.metrics.row())
        payload["feedback"] = {
            "signals": len(signals),
            "hypotheses": [h["rule"] for h in feedback["preference_hypotheses"]],
            "precision_at_5": result.metrics.precision_at_5,
            "prediction_accuracy": result.metrics.prediction_accuracy,
        }
    return payload


def _persona_response(rows: Sequence[Mapping[str, Any]], seed: int) -> list[dict[str, Any]]:
    """A stand-in reviewer with one consistent dislike, so the loop keeps moving.

    Deliberately simple and deliberately *not* the source of the numbers on the
    slide: it dislikes staff-level titles, which is a preference the induction
    should find on its own. If it does not, that is a real finding about the
    induction rather than a tuning knob.
    """
    rng = random.Random(seed)
    signals = []
    for row in rows:
        title = (row.get("title") or "").lower()
        if any(word in title for word in ("staff", "principal", "director")):
            signals.append({"job_id": row["job_id"], "kind": "not_for_me",
                            "reason_text": "too senior for what I want",
                            "reason_tags": ["too_senior"]})
        elif row.get("predicted") == "keep" and rng.random() < 0.75:
            signals.append({"job_id": row["job_id"], "kind": "keep"})
        else:
            signals.append({"job_id": row["job_id"], "kind": "skip"})
    return signals


def main() -> None:
    parser = argparse.ArgumentParser(description="Tick the day clock on a timer.")
    parser.add_argument("--interval", type=float, default=600.0,
                        help="seconds between ticks (default 600 = 10 min)")
    parser.add_argument("--ticks", type=int, default=0, help="0 means run forever")
    parser.add_argument("--webhook", default=env("ROCKETRIDE_WEBHOOK_URL"),
                        help="RocketRide webhook URL; the honest path")
    parser.add_argument("--local", action="store_true",
                        help="run in process instead (debugging harness only)")
    parser.add_argument("--auto-feedback", action="store_true")
    parser.add_argument("--no-usage", action="store_true",
                        help="skip the GET /task usage read after each webhook tick")
    args = parser.parse_args()

    if not args.local and not args.webhook:
        parser.error("no --webhook configured; pass one, or --local to use the harness")
    if not args.local:
        # Fail here rather than on tick 1: the loop swallows per-tick errors, so
        # a bad URL would otherwise print the same failure every interval.
        try:
            args.webhook = webhook_url(args.webhook)
        except ValueError as exc:
            parser.error(str(exc))

    insight = store = clock = None
    if args.local:
        insight, store, clock = Insight(), GraphStore(), DayClock.load()

    # The usage read is best effort and never blocks a tick: it needs a token in
    # the webhook URL and credentials the tunnel may not have here, and a loop
    # that dies because it could not read a counter is worse than a chart with
    # the cost panel suppressed.
    usage_client: RocketRide | None = None
    task_token = "" if args.local else token_of(args.webhook)
    if not args.local and not args.no_usage:
        if not task_token:
            print("note: no token in the webhook URL — usage cannot be read; "
                  "the dashboard will report cost_basis 'unavailable'")
        else:
            try:
                usage_client, insight = RocketRide(), insight or Insight()
            except Exception as exc:
                print(f"note: usage read disabled ({type(exc).__name__}: {exc})")

    tick = 0
    while args.ticks == 0 or tick < args.ticks:
        tick += 1
        started = time.monotonic()
        try:
            if args.local:
                result = tick_local(insight, store, clock,
                                    auto_feedback=args.auto_feedback, seed=tick)
            else:
                result = tick_webhook(args.webhook, {"action": "next_day"},
                                      bearer=env("ROCKETRIDE_APIKEY"))
            print(f"[tick {tick}] {json.dumps(result, default=str)[:400]}")
            if usage_client is not None:
                # Printed on every tick whether or not it reported, because
                # "the engine does not publish token counters" is the finding
                # that decides what the chart's first axis is allowed to say.
                try:
                    usage = attribute_usage(insight, usage_client, task_token)
                    print(f"[tick {tick}] usage reported={usage['reported']} "
                          f"in={usage.get('tokens_in')} out={usage.get('tokens_out')} "
                          f"counters={list(usage.get('counters') or {})[:6]}")
                except Exception as exc:      # including RocketRideError
                    print(f"[tick {tick}] usage unavailable "
                          f"{type(exc).__name__}: {str(exc)[:160]}")
        except Exception as exc:            # a failed tick must not end the loop
            print(f"[tick {tick}] FAILED {type(exc).__name__}: {exc}")
        if args.ticks and tick >= args.ticks:
            break
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
