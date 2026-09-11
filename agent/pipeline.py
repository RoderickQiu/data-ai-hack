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
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from agent.clock import DayClock
from agent.digest import render_blocks, render_text
from agent.metrics import RunMetrics
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


def judge_the_day(insight: Insight, store: GraphStore, clock: DayClock | None = None,
                  advance: bool = True, remember: Remember | None = None,
                  plays: PlayIndex | None = None, title_like: str = "",
                  location: str = "", remote_only: bool = False) -> DayResult:
    """P-A. One day, one judgment pass, one row on the chart.

    The pass covers today's releases **plus** every earlier role still unjudged,
    because yesterday's memory changing today's ranking of a day-3 role is the
    compounding — and it is visible without a single new row.
    """
    clock = clock or DayClock.load()
    day = clock.advance() if advance else clock.day
    metrics = RunMetrics(day=day)
    started = time.monotonic()

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

    result = refresh_and_rank(insight, store, day, title_like=title_like,
                              location=location, remote_only=remote_only)
    record_slate(store, result, metrics.run_id)

    rows = result.digest_rows()
    metrics.note_slate(result.pool_size, result.released_today, rows)
    metrics.values_from_memory += result.rules_applied

    readiness = evaluate(store, D1_SHORTLIST)
    prompt = readiness.prompt()

    day_result = DayResult(
        run_id=metrics.run_id, day=day, rank=result, metrics=metrics,
        digest_text=render_text(day, rows), digest_blocks=render_blocks(day, rows),
        autonomy_prompt=prompt, play=match.summary(),
    )

    if match.mode == "none":
        # First run of this shape: the path we just took is what gets captured.
        plays.register("refresh-and-rank-v1", "refresh-and-rank", fingerprint)
    elif match.play_id:
        plays.note_run(match.play_id, success=True)

    _write_skill_run(remember, match.play_id or "refresh-and-rank",
                     f"judge day {day}", day_result.summary(),
                     started, metrics)
    insight.log_run(metrics.row())
    store.flush()
    return day_result


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

    metrics = RunMetrics(day=day)
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
