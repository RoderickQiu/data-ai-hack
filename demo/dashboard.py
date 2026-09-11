"""The dashboard's feed: three tables and a graph, rolled up into one document.

``dashboard/index.html`` reads exactly one thing — ``dashboard/data.json`` — and
this builds it. The page never touches a table, never names a query and never
knows what a pipeline is; when a backend shape changes, this file changes and
the page does not (docs/dashboard-backend-plan.md).

Three things here are load-bearing and are not obvious from the output:

* **A day is a roll-up, not a row.** ``runs`` holds one row per *pipeline
  invocation* — P-A judges the day, each P-B prepares a pack, and a day is all
  of them. The chart shows one point per day, so the rows are summed. Without
  this the token and question lines read zero forever, because the judgment pass
  is deterministic and spends neither (agent/rank.py:86).
* **Quality is derived, never read.** ``prediction_accuracy`` is NULL on the
  real path — ``judge_the_day`` writes its row before the human has answered
  (agent/pipeline.py:122). The answers land in ``applications``, so accuracy is
  computed from there through the existing ``skip_class_accuracy``, which keeps
  the None-versus-0.0 distinction in one place.
* **Nothing here writes.** A dashboard that mutates state when someone refreshes
  a browser tab is a dashboard that changes the thing it is measuring. That is
  why readiness is assembled from ``state_of`` and ``agreement_record`` rather
  than from ``autonomy.evaluate``, which persists as it computes.

**Only numbers the system logged go in the document.** A metric with no data is
``null`` and the page draws a gap; it is never zero, never interpolated and
never back-filled. ``cost_basis`` says which cost signal is real, so a proxy can
never be captioned as a token count.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agent.clock import DayClock
from agent.config import ROOT, TUNABLES
from agent.metrics import prediction_accuracy
from agent.schema import is_usage_row, now_iso
from insight.store import Insight
from memory.autonomy import D1_SHORTLIST, DOMAINS, state_of
from memory.autonomy import skip_class_accuracy as readiness_accuracy
from memory.graph import GraphStore

DEFAULT_OUT = ROOT / "dashboard" / "data.json"

# Every task type that can become a play. "Tasks it can now do from memory" is a
# count over these, because play_index is scoped to one type at a time.
TASK_TYPES = ("refresh-and-rank", "apply-pack", "ingest-ats")


# --- the run series --------------------------------------------------------

def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    return int(_num(value))


def _known_sum(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> int | None:
    """Sum a cost field across a day, or ``None`` when nothing reported one.

    The distinction the whole chart rests on, one level finer than the run row:
    a total of zero across every row means the counter was never populated, not
    that the day was free.
    """
    total = sum(_int(row.get(field)) for row in rows for field in fields)
    return total or None


def pipeline_of(row: Mapping[str, Any], first_of_day: bool) -> str:
    """Which pipeline wrote this row.

    Reads the ``pipeline`` column when it exists. Until it does (change 4 in the
    plan), falls back twice: a row that showed a slate is the judgment pass, and
    failing that the day's earliest row is — P-A runs first and the packs follow
    from what it showed.
    """
    explicit = str(row.get("pipeline") or "").strip()
    if explicit:
        return explicit
    if _int(row.get("shown")) > 0:
        return "P-A"
    return "P-A" if first_of_day else "P-B"


def quality_by_day(insight: Insight, limit: int = 2000) -> dict[int, dict[str, Any]]:
    """Accuracy, keeps and human responses per day, out of ``applications``.

    ``predictions_window`` already returns every resolved prediction with its
    day on it. Grouping them is the whole derivation — no new table, no
    write-back, and it fills runs that were logged long before this existed.
    """
    try:
        rows = insight.run("predictions_window", {"limit": limit})
    except Exception:
        return {}                      # no applications table yet: a cold start
    by_day: dict[int, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("predicted") and row.get("actual"):
            by_day.setdefault(_int(row.get("day")), []).append(row)
    return {
        day: {
            "answered": len(group),
            "kept": sum(1 for row in group if row.get("actual") == "keep"),
            # None when the slate produced no skips. "No data" and "got them all
            # wrong" are different facts and only one belongs on a chart.
            "accuracy": prediction_accuracy(group),
        }
        for day, group in by_day.items()
    }


def roll_up(rows: Sequence[Mapping[str, Any]],
            quality: Mapping[int, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One point per day, summed across that day's pipeline invocations."""
    days: dict[int, list[Mapping[str, Any]]] = {}
    for row in sorted(rows, key=lambda r: str(r.get("started_at") or "")):
        days.setdefault(_int(row.get("day")), []).append(row)

    points = []
    for day in sorted(days):
        group = days[day]
        # A usage row is a cost attachment, not an invocation: its tokens belong
        # in the day's total, but it must never be mistaken for the judgment pass
        # and must not inflate the invocation count. It copies the run's
        # `pipeline` and so answers "P-A" to `pipeline_of`, and it happens to sort
        # last today only because it is written after the run — neither is
        # something to rely on.
        invocations = [row for row in group if not is_usage_row(row)] or group
        judged = next(
            (row for index, row in enumerate(invocations)
             if pipeline_of(row, first_of_day=index == 0) == "P-A"),
            invocations[0],
        )
        replayed = sum(_int(r.get("steps_replayed")) for r in group)
        reasoned = sum(_int(r.get("steps_reasoned")) for r in group)
        answers = quality.get(day, {})

        # The runs row counts touches it saw; applications counts every response
        # the human actually gave. Neither is a superset of the other, and the
        # larger is the one that is not under-reporting.
        logged_touches = sum(_int(r.get("human_touches")) for r in group)

        points.append({
            "day": day,
            "run_id": judged.get("run_id"),
            "runs": len(invocations),
            "mode": settle_mode(replayed, reasoned),
            # Zero is not a measurement. The judgment pass calls no model, so a
            # day whose rows all read zero is a day nobody counted — usually
            # because the usage poll found nothing, or failed for that tick. It
            # has to draw as a gap, not as a free day, or one failed poll becomes
            # the most impressive point on the cost chart.
            "tokens": _known_sum(group, ("tokens_in", "tokens_out")),
            "tool_calls": _known_sum(group, ("tool_calls",)),
            "wall": round(sum(_num(r.get("wall_ms")) for r in group) / 1000, 1),
            "questions": sum(_int(r.get("questions_asked")) for r in group),
            "touches": max(logged_touches, _int(answers.get("answered"))),
            "shown": _int(judged.get("shown")) or _int(answers.get("answered")) or None,
            "kept": answers.get("kept", _int(judged.get("actual_keep")) or None),
            "acc": answers.get("accuracy"),
            "replay_ratio": round(replayed / (replayed + reasoned), 3)
                            if replayed + reasoned else 0.0,
        })
    return annotate(points)


def settle_mode(replayed: int, reasoned: int) -> str:
    """The day's mode, by the same rule one run uses (agent/metrics.py:117)."""
    if replayed and not reasoned:
        return "full_replay"
    if replayed and reasoned:
        return "partial_replay"
    return "first_run"


def annotate(points: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive the "it met something new" callout instead of storing it.

    A ``partial_replay`` after a ``full_replay`` *is* that event: the agent had
    been working entirely from memory and had to reason about something again.
    Computed here so nobody can write a better story than the run had.
    """
    out = list(points)
    for index in range(1, len(out)):
        if (out[index]["mode"] == "partial_replay"
                and out[index - 1]["mode"] == "full_replay"):
            out[index]["note"] = ("It met a task shape it had never seen, so it had "
                                  "to work that one out. The rest came from memory.")
    return out


def cost_basis(points: Sequence[Mapping[str, Any]]) -> str:
    """Which cost signal is real, so the page can title the panel truthfully.

    ``unavailable`` until the RocketRide usage wiring lands (change 6). The page
    hides the panel rather than drawing a flat line at zero, because a flat line
    at zero looks like a measurement and is not one.
    """
    # Two days, or it is not a line. One day with a number and eleven without
    # draws a spike out of an empty panel and captions it a percentage change
    # against nothing — which is how a single afternoon's packs came to read as
    # "176% more expensive than day 1".
    for basis in ("tokens", "tool_calls"):
        if sum(1 for p in points if _int(p.get(basis))) >= 2:
            return basis
    return "unavailable"


# --- the headline numbers --------------------------------------------------

def headline(points: Sequence[Mapping[str, Any]], basis: str) -> dict[str, Any]:
    """First day against last, for the four cards.

    Always the same shape, nulls where there is no data — the page renders this
    straight and has no fallback arithmetic of its own, which is the point of
    having a feed at all.

    Every number carries the day it was measured on. Accuracy is the one card
    whose value is not the latest day's: a day nobody answered resolves no
    predictions, so the figure shown is the last day that *has* one — which is
    honest only if the card can say which day that was. ``touches`` carries
    ``measured``: zero on a day with no human response at all means "nobody
    opened the digest", not "it needed no help", and those must not read the
    same on a slide.
    """
    empty = {"value": None, "first": None, "day": None, "first_day": None}
    if not points:
        return {"day": None, "cost": {**empty, "drop_pct": None},
                "questions": dict(empty), "accuracy": dict(empty),
                "touches": {**empty, "measured": False}}

    first, last = points[0], points[-1]
    known = [p for p in points if p.get("acc") is not None]
    live_cost = basis != "unavailable"
    cost_key = "tokens" if basis == "tokens" else "tool_calls"

    def pct_drop(a: Any, b: Any) -> int | None:
        """None unless both ends are real numbers.

        _num coerces None to 0.0, so a missing latest value used to read as a
        100% saving: 4,961 tokens down to "no data" was rendered as cost falling
        to zero. "No data" and "free" are different facts, the same way §7 has
        "no data" and "got them all wrong" as different facts.
        """
        if a is None or b is None:
            return None
        a, b = _num(a), _num(b)
        return round((a - b) / a * 100) if a else None

    # The first day that has a human in it at all, not simply the first row.
    # A day the human never answered has no "at first" to compare against, and
    # a second machine joining on day 1 (agent/clock.py:catch_up) can put a
    # session run minutes ago at the front of the series.
    engaged = [p for p in points if p["touches"] or p.get("acc") is not None]
    baseline = engaged[0] if engaged else first

    return {
        "day": last["day"],
        "cost": {"value": last.get(cost_key) if live_cost else None,
                 "first": first.get(cost_key) if live_cost else None,
                 "day": last["day"], "first_day": first["day"],
                 "drop_pct": pct_drop(first.get(cost_key), last.get(cost_key))
                             if live_cost else None},
        "questions": {"value": last["questions"], "first": baseline["questions"],
                      "day": last["day"], "first_day": baseline["day"]},
        "accuracy": {"value": known[-1]["acc"] if known else None,
                     "first": known[0]["acc"] if known else None,
                     "day": known[-1]["day"] if known else None,
                     "first_day": known[0]["day"] if known else None},
        "touches": {"value": last["touches"], "first": baseline["touches"],
                    "day": last["day"], "first_day": baseline["day"],
                    "measured": bool(last["touches"] or last.get("acc") is not None)},
    }


# --- today's slate ---------------------------------------------------------

def shortlist(insight: Insight, store: GraphStore, day: int) -> list[dict[str, Any]]:
    """What the digest is showing right now, and why.

    Read out of the graph rather than by re-running the ranking: a judgment pass
    writes a ``runs`` row, so a dashboard that ranked on every page load would
    put a phantom point on its own chart.
    """
    slate = store.run("current_slate", {"day": day})
    if not slate:
        return []
    ids = [row["job_id"] for row in slate]
    try:
        detail = {row["id"]: row for row in
                  insight.run("jobs_by_id", {"job_ids": ids})}
    except Exception:
        detail = {}

    rows = []
    for entry in slate:
        job = {**entry, **detail.get(entry["job_id"], {})}
        gaps = [row["skill"] for row in store.run("job_gaps", {"job_id": entry["job_id"]})
                if row.get("skill")]
        coverage = store.run("claim_coverage", {"job_id": entry["job_id"]})
        supported = sum(1 for row in coverage if row.get("supporting_claims"))
        stored = [str(reason) for reason in (entry.get("reasons") or []) if reason]
        rows.append({
            "job_id": entry["job_id"],
            "title": job.get("title") or "",
            "company": job.get("company") or "",
            "url": job.get("url") or "",
            "meta": _meta_line(job, supported, len(coverage)),
            "predicted": entry.get("predicted"),
            "score": round(_num(entry.get("score")), 3),
            "why": "; ".join(stored[:3]) if stored
                   else _reconstruct_why(job, supported, len(coverage)),
            # The reasons the human actually saw are written by record_prediction
            # once change 5 lands. Until then this is a reconstruction from the
            # graph, and says so rather than passing itself off as the original.
            "why_source": "stored" if stored else "reconstructed",
            "gaps": gaps,
        })
    return rows


def _meta_line(job: Mapping[str, Any], supported: int, required: int) -> str:
    parts = []
    if job.get("location"):
        parts.append(str(job["location"]))
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if lo and hi:
        parts.append(f"${_int(lo) // 1000}k–${_int(hi) // 1000}k")
    if required:
        parts.append(f"you can back up {supported} of the {required} things they ask for")
    return " · ".join(parts)


def _reconstruct_why(job: Mapping[str, Any], supported: int, required: int) -> str:
    bits = []
    if required:
        bits.append(f"{supported} of {required} requirements have a verified claim behind them")
    size = job.get("company_size")
    if size:
        bits.append(f"about {_int(size):,} people")
    if job.get("remote"):
        bits.append("remote")
    return "; ".join(bits) or "shown on title and location match; no history on it yet"


# --- what it knows ---------------------------------------------------------

def knowledge(store: GraphStore) -> dict[str, Any]:
    """The counters, the rules, and how close it is to acting alone."""
    claims = store.run("verified_claims")
    answers = store.run("standard_answers")
    preferences = store.run("active_preferences")
    plays = {row.get("play_id") for task in TASK_TYPES
             for row in store.run("play_index", {"task_type": task})
             if row.get("play_id")}

    # Assembled from a pure read and the existing accuracy function rather than
    # from autonomy.evaluate, which persists a state node as it computes — a
    # page refresh must not move the thing it is displaying.
    record = store.run("agreement_record", {"limit": TUNABLES.d1_window})
    value, window = readiness_accuracy(record)

    return {
        "claims": len(claims),
        "answers": len(answers),
        "rules": len(preferences),
        "plays": len(plays),
        "rule_texts": [row.get("explanation") or row.get("rule") or ""
                       for row in preferences],
        "readiness": {
            "value": round(value, 4) if window else None,
            "window": window,
            "agreed": round(value * window) if window else 0,
            "threshold": TUNABLES.d1_accuracy_threshold,
            "state": state_of(store, D1_SHORTLIST)["state"],
        },
        "autonomy": {domain: state_of(store, domain)["state"] for domain in DOMAINS},
    }


# --- the document ----------------------------------------------------------

def build(insight: Insight | None = None, store: GraphStore | None = None,
          clock: DayClock | None = None, limit: int = 500) -> dict[str, Any]:
    insight = insight or Insight()
    store = store or GraphStore()
    clock = clock or DayClock.load()

    try:
        rows = insight.run("runs_series", {"limit": limit})
    except Exception:
        rows = []
    points = roll_up(rows, quality_by_day(insight))
    basis = cost_basis(points)

    return {
        "generated_at": now_iso(),
        "live": True,
        "day": points[-1]["day"] if points else clock.day,
        "cost_basis": basis,
        "runs": points,
        "stats": headline(points, basis),
        # `day=0` is "whatever the latest open slate is", not "today". The clock
        # advances at the start of a run, so by the time anyone looks at the page
        # it has usually moved past the day the open slate belongs to.
        "shortlist": shortlist(insight, store, day=0),
        "knowledge": knowledge(store),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build dashboard/data.json.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--print", action="store_true", help="stdout instead of a file")
    args = parser.parse_args()

    document = build(limit=args.limit)
    text = json.dumps(document, indent=2, default=str)
    if args.print:
        print(text)
        return
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    runs = len(document["runs"])
    print(f"wrote {args.out} — {runs} day(s), cost basis: {document['cost_basis']}")


if __name__ == "__main__":
    main()
