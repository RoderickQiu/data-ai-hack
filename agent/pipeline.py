"""The three pipelines, as functions. RocketRide sequences them; this is what it calls.

Pipelines split at every human gate and never block waiting for a person
(DESIGN §4). Each decision starts a *new* pipeline, so runs stay short,
restartable, and each is a clean unit for Rote to capture:

========= ========================================= ===========================
P-A       timer tick, or "next day" in chat         digest posted, runs row written
P-B       human taps a role, or D1 autonomy picks   pack in Slack and the Sheet
P-C       human answers, keeps, skips, reports      memory written, preference checked
========= ========================================= ===========================

Every pass asks Rote for a play before doing the work and writes a
``SkillRunEntry`` afterwards, hit or miss. That write is what routes the *next*
``find_play``, and it is the difference between a system that persists and one
that compounds.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from agent.clock import DayClock
from agent.digest import render_blocks, render_text
from agent.metrics import RunMetrics, accounting_for
from agent.pack import Pack, build_pack
from agent.rank import RankResult, record_slate, refresh_and_rank
from insight.store import Insight
from memory.autonomy import D1_SHORTLIST, evaluate, may_act_alone
from memory.graph import GraphStore
from memory.remember import Remember
from mcp_server.rote import PlayIndex, Rote, fingerprint_task

REFRESH_INPUTS = ("day", "candidate_id", "title_class", "location")
PACK_INPUTS = ("job_id", "candidate_id", "screening_questions")


@dataclass
class DayResult:
    run_id: str
    day: int
    rank: RankResult
    metrics: RunMetrics
    digest_text: str = ""
    digest_blocks: list[dict[str, Any]] = field(default_factory=list)
    autonomy_prompt: str | None = None
    play: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Ids and one-liners. The digest text is fetched, never pushed into a
        tool result, because tool returns are what dominate tokens per run."""
        return {
            "run_id": self.run_id, "day": self.day,
            "released_today": self.rank.released_today, "pool": self.rank.pool_size,
            "shown": len(self.rank.slate),
            "predicted_keep": self.rank.predicted_keep,
            "mode": self.metrics.mode, "play": self.play,
            "autonomy_prompt": bool(self.autonomy_prompt),
            "jobs": [
                {"job_id": p.job_id, "predicted": p.predicted,
                 "score": round(p.score, 3),
                 "title": self.rank.jobs.get(p.job_id, {}).get("title", "")}
                for p in self.rank.slate
            ],
        }


@dataclass
class DayPass:
    """A P-A pass in flight, between the tool calls that carry it.

    P-A is one logical pass, but the orchestrator's turn is not one call:
    ``judge_day`` as a single tool ranks the pool and extracts requirements
    inside one request, which is long enough that the agent's turn times out
    and the run is lost after doing all of the work. So the pass is opened,
    ranked and closed by three tools (DESIGN §4 keeps the *pipeline* split at
    human gates; this is a split at the timeout, which is a different seam).

    The state between them lives here rather than in the caller: the run_id, the
    metrics counters and the play match all have to survive from `open` to
    `close` or the row that lands is not the row that describes the run.
    """
    run_id: str
    day: int
    metrics: RunMetrics
    match: Any
    fingerprint: Any
    started: float
    result: DayResult | None = None


# Passes are held by run_id between calls. Bounded, because an orchestrator that
# opens a day and never closes it must not grow the server's memory — and
# because a pass older than the current day is not resumable anyway.
_OPEN_PASSES: "OrderedDict[str, DayPass]" = OrderedDict()
MAX_OPEN_PASSES = 8


def _remember_pass(pass_: DayPass) -> DayPass:
    _OPEN_PASSES[pass_.run_id] = pass_
    while len(_OPEN_PASSES) > MAX_OPEN_PASSES:
        _OPEN_PASSES.popitem(last=False)
    return pass_


def open_pass(run_id: str) -> DayPass:
    """The pass `day_rank` and `day_close` are talking about."""
    pass_ = _OPEN_PASSES.get(run_id)
    if pass_ is None:
        raise KeyError(f"no open pass {run_id!r}; call day_open first "
                       f"(open: {sorted(_OPEN_PASSES)})")
    return pass_


def open_day(store: GraphStore, clock: DayClock | None = None, advance: bool = True,
             plays: PlayIndex | None = None) -> DayPass:
    """Stage 1 of P-A: advance the clock and ask Rote for a play.

    Deliberately cheap — a clock write and one play lookup. Nothing here touches
    hotdata, so this call cannot be the one that times out.
    """
    clock = clock or DayClock.load()
    day = clock.advance() if advance else clock.day
    metrics = RunMetrics(day=day, pipeline="P-A")

    plays = plays or PlayIndex(store, Rote())
    fingerprint = fingerprint_task("refresh-and-rank", REFRESH_INPUTS)
    match = plays.find("refresh-and-rank", fingerprint)
    if match.mode == "exact":
        metrics.note_replayed(match.play_id or "refresh-and-rank")
    elif match.mode == "partial":
        metrics.note_replayed(match.play_id or "refresh-and-rank")
        metrics.note_reasoned()
    else:
        metrics.note_reasoned()

    return _remember_pass(DayPass(run_id=metrics.run_id, day=day, metrics=metrics,
                                  match=match, fingerprint=fingerprint,
                                  started=time.monotonic()))


def rank_day(pass_: DayPass, insight: Insight, store: GraphStore,
             title_like: str = "", location: str = "",
             remote_only: bool = False) -> DayResult:
    """Stage 2 of P-A: the judgment itself. The long call, and the only one.

    Covers today's releases **plus** every earlier role still unjudged, because
    yesterday's memory changing today's ranking of a day-3 role is the
    compounding — and it is visible without a single new row.
    """
    metrics, day = pass_.metrics, pass_.day
    with accounting_for(metrics):
        result = refresh_and_rank(insight, store, day, title_like=title_like,
                                  location=location, remote_only=remote_only)
        record_slate(store, result, metrics.run_id)

    rows = result.digest_rows()
    metrics.note_slate(result.pool_size, result.released_today, rows)
    metrics.values_from_memory += result.rules_applied

    readiness = evaluate(store, D1_SHORTLIST)
    pass_.result = DayResult(
        run_id=metrics.run_id, day=day, rank=result, metrics=metrics,
        digest_text=render_text(day, rows), digest_blocks=render_blocks(day, rows),
        autonomy_prompt=readiness.prompt(), play=pass_.match.summary(),
    )
    return pass_.result


def close_day(pass_: DayPass, insight: Insight, store: GraphStore,
              remember: Remember | None = None,
              plays: PlayIndex | None = None) -> DayResult:
    """Stage 3 of P-A: the row on the chart, and the play that did the work.

    Separate from `rank_day` so that a pass which ranked but timed out on the
    way back still has somewhere to land: re-closing an already-closed pass is
    the caller's to avoid, but re-opening a lost one costs the whole judgment.
    """
    if pass_.result is None:
        raise ValueError(f"pass {pass_.run_id} has not ranked yet — call day_rank first")
    plays = plays or PlayIndex(store, Rote())
    match, metrics = pass_.match, pass_.metrics

    if match.mode == "none":
        # First run of this shape: the path we just took is what gets captured.
        plays.register("refresh-and-rank-v1", "refresh-and-rank", pass_.fingerprint)
    elif match.play_id:
        plays.note_run(match.play_id, success=True)

    _write_skill_run(remember, match.play_id or "refresh-and-rank",
                     f"judge day {pass_.day}", pass_.result.summary(),
                     pass_.started, metrics)
    insight.log_run(metrics.row())
    store.flush()
    _OPEN_PASSES.pop(pass_.run_id, None)
    return pass_.result


def judge_the_day(insight: Insight, store: GraphStore, clock: DayClock | None = None,
                  advance: bool = True, remember: Remember | None = None,
                  plays: PlayIndex | None = None, title_like: str = "",
                  location: str = "", remote_only: bool = False) -> DayResult:
    """P-A in one call: open, rank, close.

    The in-process path — `demo.loop --local`, the tests, and any caller that is
    not an LLM turn with a timeout on it. The staged tools above are the same
    three steps with the orchestrator's turn boundary between them.
    """
    pass_ = open_day(store, clock, advance=advance, plays=plays)
    rank_day(pass_, insight, store, title_like=title_like, location=location,
             remote_only=remote_only)
    return close_day(pass_, insight, store, remember=remember, plays=plays)


def prepare_pack(insight: Insight, store: GraphStore, job_id: str, day: int,
                 screening_questions: Sequence[str] = (),
                 remember: Remember | None = None, plays: PlayIndex | None = None,
                 decided_by: str = "human") -> tuple[Pack, RunMetrics]:
    """P-B. One LLM call wrapped in four deterministic tool calls.

    ``decided_by='agent'`` is only legal once D1 is Autonomous; the check is
    here rather than at the call site so no caller can route around it.
    """
    if decided_by == "agent" and not may_act_alone(store, D1_SHORTLIST):
        raise PermissionError("D1 shortlisting is not autonomous yet — the human "
                              "has to ask for this pack")

    metrics = RunMetrics(day=day, pipeline="P-B")
    started = time.monotonic()
    plays = plays or PlayIndex(store, Rote())

    fingerprint = fingerprint_task("apply-pack", list(PACK_INPUTS) + sorted(
        f"q:{q[:24]}" for q in screening_questions))
    match = plays.find("apply-pack", fingerprint)
    if match.mode == "exact":
        metrics.note_replayed(match.play_id or "apply-pack", steps=4)
    elif match.mode == "partial":
        # The known fields replay from memory; only the novel ones cost tokens.
        metrics.note_replayed(match.play_id or "apply-pack", steps=3)
        metrics.note_reasoned()
    else:
        metrics.note_reasoned(steps=4)

    with accounting_for(metrics):
        pack = build_pack(insight, store, job_id, screening_questions, day=day,
                          metrics=metrics)
    metrics.values_replayed += len(match.known_fields)

    if match.mode == "none":
        plays.register("apply-pack-v1", "apply-pack", fingerprint)
    elif match.play_id:
        plays.note_run(match.play_id, success=pack.ok)

    _write_skill_run(remember, match.play_id or "apply-pack",
                     f"apply pack for {job_id}", pack.summary(), started, metrics)
    insight.log_run(metrics.row())
    return pack, metrics


def _write_skill_run(remember: Remember | None, skill_id: str, task: str,
                     summary: Mapping[str, Any], started: float,
                     metrics: RunMetrics) -> None:
    """Rote's output becoming Cognee's memory. Best effort, never fatal."""
    if remember is None:
        return
    try:
        remember.skill_run(
            skill_id=skill_id, task_text=task,
            result_summary=str(summary), success_score=1.0 if summary.get("ok", True) else 0.0,
            latency_ms=int((time.monotonic() - started) * 1000),
            tool_trace=[{"step": "replayed", "count": metrics.steps_replayed},
                        {"step": "reasoned", "count": metrics.steps_reasoned}],
            run_id=metrics.run_id,
        )
    except Exception:
        return
