"""P-C: absorb a human response, everywhere it has to land.

One human action — a Keep, a Not-for-me with a reason, an answer to a screening
question, a reported reply — has to reach all three memory layers, and it has to
reach them in an order that keeps them consistent if the run dies halfway:

1. **HydraDB** first: the ``Signal`` and its edges are the durable record, and
   the preference check reads them back immediately.
2. **hotdata** second: the ``applications`` row. Losing it costs a chart point,
   not a fact.
3. **Cognee** last, best-effort: feedback prose for the next ``recall``. It is
   the only one of the three whose failure must not fail the write, because it
   is a hosted tenant and a run that cannot record a Keep is worse than a run
   with slightly staler preference context.

Every pipeline here is short and restartable by design. A pipeline that sits
open waiting for a Slack click is one that times out, cannot be restarted
cleanly, and produces one enormous Rote capture spanning a human pause — so
each decision starts a *new* pipeline instead (DESIGN §4).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from agent.schema import ApplicationEvent, now_iso
from agent.digest import tags_from_text
from insight.store import Insight
from memory.autonomy import D1_SHORTLIST, D2_PREFERENCES, note_decision
from memory.graph import GraphStore
from memory.prefs import check_for_hypothesis
from memory.remember import Remember
from memory.writers import (
    record_answer_to_prediction,
    record_application,
    record_outcome,
    record_signal,
)

_EVENT_FOR_KIND = {"keep": "shortlisted", "skip": "skipped", "not_for_me": "not_for_me"}


def record_response(store: GraphStore, insight: Insight, day: int, run_id: str,
                    signals: Sequence[Mapping[str, Any]],
                    remember: Remember | None = None,
                    predictions: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Write one digest's worth of responses and check for a new preference.

    ``predictions`` maps job_id to what the agent guessed, so the agreement
    record is closed in the same write that records the human's answer. A
    prediction that is never resolved is not evidence of anything.
    """
    predictions = predictions or {}
    events: list[ApplicationEvent] = []
    resolved: list[dict[str, Any]] = []

    for signal in signals:
        job_id = signal["job_id"]
        kind = signal["kind"]
        tags = list(signal.get("reason_tags") or [])
        reason = signal.get("reason_text", "")
        if reason and not tags:
            tags = tags_from_text(reason)

        record_signal(store, job_id, kind, day=day, reason_tags=tags,
                      reason_text=reason, run_id=run_id)

        actual = "keep" if kind == "keep" else "skip"
        if job_id in predictions:
            record_answer_to_prediction(store, job_id, actual,
                                        predicted=predictions[job_id])
            resolved.append({"job_id": job_id, "predicted": predictions[job_id],
                             "actual": actual})
            note_decision(store, D1_SHORTLIST,
                          overridden=predictions[job_id] != actual
                          and signal.get("decided_by") == "agent")

        events.append(ApplicationEvent(
            job_id=job_id, event=_EVENT_FOR_KIND.get(kind, "seen"), at=now_iso(),
            day=day, run_id=run_id, reason_tags=tags, reason=reason,
            decided_by=signal.get("decided_by", "human"),
            predicted=predictions.get(job_id), actual=actual,
        ))

    store.flush()
    insight.log_events(events)

    proposals = check_for_hypothesis(store)
    store.flush()

    if remember is not None:
        _remember_feedback(remember, signals, day)

    return {
        "signals": len(signals),
        "resolved_predictions": resolved,
        "preference_hypotheses": [
            {"preference_id": p.preference_id, "prompt": p.prompt(),
             "rule": p.rule.sentence(), "evidence_count": p.evidence_count}
            for p in proposals
        ],
    }


def _remember_feedback(remember: Remember, signals: Sequence[Mapping[str, Any]],
                       day: int) -> None:
    """Best effort, and deliberately last. Cognee turns the prose into tags."""
    for signal in signals:
        if not signal.get("reason_text"):
            continue
        try:
            remember.feedback(
                f"On day {day} the candidate marked job {signal['job_id']} "
                f"'{signal['kind']}': {signal['reason_text']}",
                question=f"Keep the role {signal['job_id']}?",
                answer=signal["kind"],
            )
        except Exception:
            continue


def record_applied(store: GraphStore, insight: Insight, job_id: str, day: int,
                   run_id: str, claim_ids: Sequence[str] = (),
                   play_id: str | None = None, decided_by: str = "human") -> dict[str, Any]:
    """The human applied. We prepared it; they sent it. Never the other way round."""
    node = record_application(store, job_id, day=day, play_id=play_id,
                              claim_ids=claim_ids, decided_by=decided_by, run_id=run_id)
    store.flush()
    insight.log_event(ApplicationEvent(job_id=job_id, event="applied", at=now_iso(),
                                       day=day, run_id=run_id, decided_by=decided_by))
    return {"application": node, "job_id": job_id}


def record_reply(store: GraphStore, insight: Insight, job_id: str, day: int,
                 run_id: str, kind: str = "replied", detail: str = "") -> dict[str, Any]:
    """The candidate reports an outcome. Same signal a real inbox would give,
    without the recruiter simulator (DESIGN §12)."""
    node = record_outcome(store, job_id, kind=kind, day=day, detail=detail)
    store.flush()
    event = "replied" if kind in ("replied", "interview", "offer") else "rejected"
    insight.log_event(ApplicationEvent(job_id=job_id, event=event, at=now_iso(),
                                       day=day, run_id=run_id, reason=detail))
    return {"outcome": node, "job_id": job_id, "kind": kind}


def confirm_preference(store: GraphStore, preference_id: str, confirmed: bool) -> dict[str, Any]:
    """Confirm or permanently reject a hypothesis, and log it against D2.

    On confirm the caller re-ranks the current shortlist immediately — that
    visible reorder *is* the proof moment, and it has to happen in the same
    interaction to read as cause and effect.
    """
    from memory.prefs import confirm, reject

    result = confirm(store, preference_id) if confirmed else reject(store, preference_id)
    note_decision(store, D2_PREFERENCES, overridden=not confirmed)
    store.flush()
    return result


def answer_question(store: GraphStore, question: str, answer: str, day: int,
                    remember: Remember | None = None) -> dict[str, Any]:
    """One answer, stored once, never asked again (P1).

    Writing a StandardAnswer is on the never-autonomous list: an unknown fact
    about a person is always asked, whatever the agreement record says.
    """
    from memory.answers import record_human_answer

    result = record_human_answer(store, question, answer, day=day)
    store.flush()
    if remember is not None:
        try:
            remember.qa(question, answer)
        except Exception:
            pass
    return result
