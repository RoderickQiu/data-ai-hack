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
from insight.store import Insight
from memory.graph import GraphStore


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
        except Exception as exc:            # a failed tick must not end the loop
            print(f"[tick {tick}] FAILED {type(exc).__name__}: {exc}")
        if args.ticks and tick >= args.ticks:
            break
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
