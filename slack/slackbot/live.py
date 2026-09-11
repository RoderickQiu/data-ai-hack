"""Running the Slack surface against the real agent instead of fixtures.

    python -m slack.slackbot.live --day          # judge one day and post the digest
    python -m slack.slackbot.live --claims       # post whatever is awaiting verification

The bot process itself picks this up through :class:`BackendSink`: with
``SLACK_BACKEND=1`` set, every click stops at the JSONL file *and* goes on into
``agent.feedback``. Without it the surface behaves exactly as it did against
fixtures, which is what keeps it demonstrable when the stores are down.

The loop closes here. A ``not for me`` is written, the induction runs on the way
out, and if three answers now share a reason the Confirm prompt is posted from
inside the same click — because a preference that appears a minute later reads
as a coincidence rather than as the agent having just learned something.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from typing import Any, Mapping

from . import bridge
from .config import Config, load_config
from .handlers import SLATES
from .signals import Signal, SignalSink


class BackendSink(SignalSink):
    """The sink, plus the write into the agent's memory.

    Disk first, always: ``agent.feedback`` can raise, the stores can be down,
    and a click that is only half-recorded is worse than one that is recorded
    twice. The JSONL line is written before anything else is attempted, so the
    human-effort line of the chart survives any failure below it.
    """

    def __init__(self, cfg: "Config", *, store, insight, remember=None):
        super().__init__(cfg.signal_log, cfg.signal_webhook, cfg.signal_webhook_token)
        self.cfg = cfg
        self.store = store
        self.insight = insight
        self.remember = remember

    def emit(self, signal: Signal) -> dict[str, Any]:
        record = super().emit(signal)
        try:
            result = bridge.apply_signal(
                record, store=self.store, insight=self.insight,
                remember=self.remember,
                predictions=SLATES.get(record.get("run_id") or "", {}),
            )
        except Exception as exc:
            print(f"[backend] {record.get('kind')} not written, it is still in "
                  f"{self.log_path}: {type(exc).__name__}: {exc}")
            return record

        self._follow_up(record, result)
        return record

    def _follow_up(self, record: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        """Whatever that click set in motion, do it now rather than next tick."""
        try:
            for hypothesis in result.get("preference_hypotheses") or []:
                call(self.cfg, "/preference", bridge.preference_payload(
                    hypothesis, run_id=record.get("run_id") or "").model_dump())

            if result.get("action") == "prepare_pack":
                post_pack(self.cfg, self.insight, self.store,
                          result["job_id"], result["day"], run_id=result["run_id"])
        except Exception as exc:
            print(f"[backend] follow-up after {record.get('kind')} failed: "
                  f"{type(exc).__name__}: {exc}")


def call(cfg: Config, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Post to the bot's own API — the same path a RocketRide pipeline takes."""
    request = urllib.request.Request(
        f"http://{cfg.api_host}:{cfg.api_port}{path}",
        data=json.dumps(payload, default=str).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg.api_token}"},
        method="POST",
    )
    # `path` is a literal at every call site and `api_host` is checked as a bare
    # host name at config load, so the origin here cannot be moved.
    # deepcode ignore Ssrf: api_host validated by agent.net.hostname at config load
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def post_day(cfg: Config, insight, store, clock=None, advance: bool = True) -> dict[str, Any]:
    """P-A, then the digest, then the autonomy prompt if one is due."""
    from agent.pipeline import judge_the_day
    from memory.autonomy import D1_SHORTLIST, evaluate

    # This is the shared surface: the tables it writes are the ones everyone
    # else's chart is drawn from, and the day counter is a local file. Sync
    # before judging or a second machine stamps today's session on day 1
    # (agent/clock.py:catch_up). Skipped when re-judging a pinned day.
    if clock is not None and advance:
        clock.catch_up(insight)

    result = judge_the_day(insight, store, clock, advance=advance)
    posted = call(cfg, "/digest", bridge.digest_payload(result).model_dump())

    prompt = bridge.autonomy_payload(evaluate(store, D1_SHORTLIST), run_id=result.run_id)
    if prompt is not None:
        call(cfg, "/autonomy", prompt.model_dump())

    return {"run_id": result.run_id, "day": result.day,
            "shown": len(result.rank.slate), "mode": result.metrics.mode,
            "ts": posted.get("ts"), "autonomy_prompt": prompt is not None}


def post_pack(cfg: Config, insight, store, job_id: str, day: int,
              questions=(), run_id: str = "") -> dict[str, Any]:
    """P-B. Called when the human taps *Prepare pack*."""
    from agent.pipeline import prepare_pack

    pack, metrics = prepare_pack(insight, store, job_id, day, screening_questions=questions)
    payload = bridge.pack_payload(pack, run_id=run_id or metrics.run_id,
                                  play=metrics.mode.replace("_", " "),
                                  candidate_name=candidate_name(store))
    return call(cfg, "/pack", payload.model_dump())


def update_run_metrics(insight, run_id: str) -> dict[str, Any]:
    """Fold the human's answers back into the run row they belong to.

    A run row is written when the digest is posted — before anyone has answered
    it — so precision, accuracy and human_touches are all still empty at that
    moment. demo/loop.py re-logs the row once its persona has answered; the
    Slack path never did. So a real human answering a real digest reached the
    graph and hotdata's applications table, and never reached the one table the
    chart reads: 10 clicks on day 11 against a row saying human_touches 0.

    Recomputed from applications rather than accumulated per click, because
    clicks arrive one at a time and the answer to "how good was this slate" is
    only meaningful over the whole slate.
    """
    from agent.metrics import skip_class_accuracy

    rows = [r for r in insight.run("runs_series", {"limit": 500})
            if r.get("run_id") == run_id]
    answered = [r for r in insight.run("predictions_window", {"limit": 500})
                if r.get("run_id") == run_id and r.get("predicted") and r.get("actual")]
    if not rows or not answered:
        return {}

    row = dict(rows[0])
    keeps = sum(1 for r in answered if r["actual"] == "keep")
    row["actual_keep"] = keeps
    row["precision_at_5"] = round(keeps / len(answered), 4)
    row["prediction_accuracy"] = skip_class_accuracy(answered)
    row["human_touches"] = len(answered)
    insight.log_run(row)
    return {"run_id": run_id, "answered": len(answered),
            "precision_at_5": row["precision_at_5"],
            "prediction_accuracy": row["prediction_accuracy"]}


def candidate_name(store) -> str:
    """Whatever the graph calls the candidate, for the resume heading."""
    for node in store.graph.nodes.values():
        if node.label == "Candidate" and node.props.get("name"):
            return str(node.props["name"])
    return "Candidate"


def post_pending_claims(cfg: Config, store, run_id: str = "verify") -> dict[str, Any]:
    from memory.claims import pending_verification

    rows = pending_verification(store)
    if not rows:
        return {"claims": 0}
    payload = bridge.claims_payload(rows, run_id=run_id)
    return {"claims": len(rows), **call(cfg, "/claims", payload.model_dump())}


def main() -> None:
    parser = argparse.ArgumentParser(description="Drive the Slack surface from the real agent.")
    parser.add_argument("--day", action="store_true", help="judge one day and post the digest")
    parser.add_argument("--claims", action="store_true", help="post claims awaiting verification")
    parser.add_argument("--pack", metavar="JOB_ID", help="build and post one apply-pack")
    parser.add_argument("--no-advance", action="store_true", help="re-judge the current day")
    args = parser.parse_args()

    if not (args.day or args.claims or args.pack):
        parser.error("nothing to do; pass --day, --claims or --pack JOB_ID")

    from agent.clock import DayClock
    from insight.store import Insight
    from memory.graph import GraphStore

    cfg = load_config()
    insight, store, clock = Insight(), GraphStore(), DayClock.load()

    try:
        if args.day:
            print(json.dumps(post_day(cfg, insight, store, clock,
                                      advance=not args.no_advance), indent=2))
        if args.pack:
            print(json.dumps(post_pack(cfg, insight, store, args.pack, clock.day), indent=2))
        if args.claims:
            print(json.dumps(post_pending_claims(cfg, store), indent=2))
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach the bot on {cfg.api_host}:{cfg.api_port} — "
                         f"is `python -m slack.slackbot.app` running? ({exc})")


if __name__ == "__main__":
    main()
