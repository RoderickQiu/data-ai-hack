"""The ``runs`` row: cost, quality and human effort logged side by side.

A falling token count alone is a cache, and a judge can say so. Every run writes
one row carrying all three lines (DESIGN §7):

* **cost** — ``tokens_in + tokens_out``, ``wall_ms``, and
  ``steps_replayed / (steps_reasoned + steps_replayed)`` as the supporting ratio;
* **quality** — ``precision_at_5`` and ``prediction_accuracy``, the latter
  reported on the skip class;
* **human effort** — ``questions_asked`` and ``human_touches``.

``mode`` is ``first_run | partial_replay | full_replay``, so the chart's points
can be coloured by it and the step down at the first replay is *visible* rather
than narrated.

**Honesty rule: only numbers the system actually logged go on the chart.** No
interpolated points, no back-filled runs, no "representative" values. There is
deliberately no way to write a row that was not produced by a run — every field
below is incremented by a real call site.

If RocketRide's task response turns out not to carry token usage, count at the
MCP boundary instead (:meth:`RunMetrics.note_tool_call`) and say on the slide
that the cost line is a proxy. Lines 2 and 3 are unaffected either way, which is
the other reason for having three.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence

from agent.schema import RUNS_COLUMNS, now_iso

MODES = ("first_run", "partial_replay", "full_replay")

# Which run's row the boundary calls belong to, if any. A context variable
# rather than an argument threaded through `refresh_and_rank`: the counting
# happens at the transport (insight/hotdata.py), six call layers below the
# thing that owns the row, and every layer between them would otherwise have to
# carry a metrics object it does not use. Unset outside a run — a script, a
# test or the dashboard build queries the same tables and must not be billed to
# whichever run happens to be open.
_ACCOUNTING: ContextVar["RunMetrics | None"] = ContextVar("run_metrics", default=None)


@contextmanager
def accounting_for(metrics: "RunMetrics") -> Iterator["RunMetrics"]:
    """Bill every boundary call made inside this block to ``metrics``."""
    token = _ACCOUNTING.set(metrics)
    try:
        yield metrics
    finally:
        _ACCOUNTING.reset(token)


def note_boundary_call(returned_bytes: int = 0) -> None:
    """One call out of the process. Called by the transport, no-op outside a run.

    This is the proxy the module header promises: the orchestrator does not
    report token usage, so cost is counted where the agent actually reaches out
    of itself. It is a proxy and the dashboard says so — ``cost_basis`` is
    ``tool_calls``, never ``tokens``, when this is the line being drawn.
    """
    metrics = _ACCOUNTING.get()
    if metrics is not None:
        metrics.note_tool_call(returned_bytes)


@dataclass
class RunMetrics:
    """Accumulator for one run. Start it, tell it what happened, hand it over."""

    day: int
    run_id: str = field(default_factory=lambda: f"run-{uuid.uuid4().hex[:10]}")
    started_at: str = field(default_factory=now_iso)
    mode: str = "first_run"
    # Which pipeline is writing this row. `day` is not a key — P-A judges the
    # day and each P-B prepares a pack — so without this nothing downstream can
    # tell a judgment pass from a pack, or roll a day up into one chart point.
    pipeline: str = "P-A"

    tokens_in: int = 0
    tokens_out: int = 0
    steps_reasoned: int = 0
    steps_replayed: int = 0
    plays_used: list[str] = field(default_factory=list)

    questions_asked: int = 0
    human_touches: int = 0

    values_from_memory: int = 0
    values_replayed: int = 0
    values_reasoned: int = 0

    jobs_released_today: int = 0
    pool_size: int = 0
    shown: int = 0
    predicted_keep: int = 0
    actual_keep: int = 0
    precision_at_5: float | None = None
    prediction_accuracy: float | None = None

    # Proxy counters, used when the orchestrator does not report token usage.
    tool_calls: int = 0
    tool_bytes: int = 0

    _start: float = field(default_factory=time.monotonic, repr=False)

    # -- during the run --------------------------------------------------

    def note_llm(self, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self.tokens_in += int(tokens_in or 0)
        self.tokens_out += int(tokens_out or 0)

    def note_reasoned(self, steps: int = 1) -> None:
        self.steps_reasoned += steps

    def note_replayed(self, play_id: str, steps: int = 1) -> None:
        self.steps_replayed += steps
        if play_id not in self.plays_used:
            self.plays_used.append(play_id)

    def note_tool_call(self, returned_bytes: int = 0) -> None:
        """The MCP-boundary proxy for cost. Counted always, used when needed."""
        self.tool_calls += 1
        self.tool_bytes += int(returned_bytes or 0)

    def note_question(self, count: int = 1) -> None:
        self.questions_asked += count
        self.human_touches += count

    def note_touch(self, count: int = 1) -> None:
        """An approval, an edit, or an answer given. The line that needs no
        explaining to a non-technical judge."""
        self.human_touches += count

    def note_ladder(self, breakdown: Mapping[str, Any]) -> None:
        """Fold in one ``answers.resolve_all`` result."""
        self.values_from_memory += int(breakdown.get("values_from_memory", 0))
        self.values_reasoned += int(breakdown.get("values_reasoned", 0))
        self.questions_asked += int(breakdown.get("questions_asked", 0))
        self.human_touches += int(breakdown.get("questions_asked", 0))

    def note_slate(self, pool_size: int, released_today: int,
                   predictions: Sequence[Mapping[str, Any]]) -> None:
        self.pool_size = pool_size
        self.jobs_released_today = released_today
        self.shown = len(predictions)
        self.predicted_keep = sum(1 for p in predictions if p.get("predicted") == "keep")

    def settle_mode(self) -> str:
        """Derive the mode from what actually happened, rather than trusting a
        caller's label: a run that reasoned about anything is not a full replay."""
        if self.steps_replayed and not self.steps_reasoned:
            self.mode = "full_replay"
        elif self.steps_replayed and self.steps_reasoned:
            self.mode = "partial_replay"
        else:
            self.mode = "first_run"
        return self.mode

    # -- after the human answers -----------------------------------------

    def score_responses(self, responses: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Fold the human's answers in. ``responses`` is what the digest got back.

        Each item is ``{"job_id", "predicted", "actual"}`` where ``actual`` is
        ``keep`` or ``skip`` — a ``skip`` and a ``not_for_me`` both count as not
        keeping, because precision asks whether the slate was worth showing.
        """
        answered = [r for r in responses if r.get("actual")]
        if not answered:
            return {}
        self.actual_keep = sum(1 for r in answered if r["actual"] == "keep")
        self.precision_at_5 = round(self.actual_keep / len(answered), 4)
        self.prediction_accuracy = prediction_accuracy(answered)
        self.human_touches += len(answered)
        return {"precision_at_5": self.precision_at_5,
                "prediction_accuracy": self.prediction_accuracy}

    # -- the row ---------------------------------------------------------

    def wall_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)

    def row(self) -> dict[str, Any]:
        self.settle_mode()
        data = {
            "run_id": self.run_id, "started_at": self.started_at, "day": self.day,
            "mode": self.mode, "pipeline": self.pipeline, "wall_ms": self.wall_ms(),
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "steps_reasoned": self.steps_reasoned, "steps_replayed": self.steps_replayed,
            "plays_used": ",".join(self.plays_used),
            "tool_calls": self.tool_calls, "tool_bytes": self.tool_bytes,
            "questions_asked": self.questions_asked, "human_touches": self.human_touches,
            "values_from_memory": self.values_from_memory,
            "values_replayed": self.values_replayed,
            "values_reasoned": self.values_reasoned,
            "jobs_released_today": self.jobs_released_today, "pool_size": self.pool_size,
            "shown": self.shown, "predicted_keep": self.predicted_keep,
            "actual_keep": self.actual_keep, "precision_at_5": self.precision_at_5,
            "prediction_accuracy": self.prediction_accuracy,
        }
        return {column: data.get(column) for column in RUNS_COLUMNS}

    def replay_ratio(self) -> float:
        total = self.steps_reasoned + self.steps_replayed
        return round(self.steps_replayed / total, 3) if total else 0.0


def prediction_accuracy(responses: Sequence[Mapping[str, Any]]) -> float | None:
    """How well the agent called it, averaged over the classes it had to call.

    Balanced accuracy: recall within each class the human actually used, then
    the mean of those. Two properties matter and no simpler measure has both.

    **The base rate cannot inflate it.** Plain accuracy rewards guessing the
    majority: the human skips 77% of the time in this build, so an agent
    predicting skip on everything scores 78% while knowing nothing. Averaging
    per-class recall gives that agent 50% — it recalls every skip and no keep.

    **A one-sided day is not scored as total failure.** The measure this
    replaces counted any row where either side said skip, so a day the human
    kept everything scored 0% on the strength of ten wrong skip predictions,
    while the single keep it got right was excluded as an easy win. One correct
    call in eleven is 9%, not nothing, and that gap is the difference between
    "it was wrong here" and "it is broken".

    ``None`` when nothing resolved: no data and got-them-all-wrong are
    different facts, and only one of them belongs on a chart.
    """
    by_actual: dict[str, list[bool]] = {}
    for row in responses:
        predicted, actual = row.get("predicted"), row.get("actual")
        if not predicted or not actual:
            continue
        by_actual.setdefault(actual, []).append(predicted == actual)
    if not by_actual:
        return None
    recalls = [sum(hits) / len(hits) for hits in by_actual.values()]
    return round(sum(recalls) / len(recalls), 4)
