"""The seam between the agent and Slack.

Two directions, deliberately asymmetric.

**Out is pure mapping.** The agent's own result objects become the payloads the
block builders already take. No side effects, so a rendering change can never
reach a store, and every mapping is testable without Slack or a network.

**In is pure dispatch.** One Signal becomes one call into ``agent.feedback``,
which owns the order the three memory layers are written in — HydraDB first
because it is the durable record, hotdata second because losing a row costs a
chart point rather than a fact, Cognee last and best-effort. Nothing here
writes to a store directly. If it did, those ordering guarantees would have a
second, quieter implementation that nobody maintains.

One thing deliberately not mapped: ``Prediction.score`` is a weighted fit score
(coverage, similarity, preference fit), not a calibrated probability. It never
becomes a "% sure" on a card. A ranking weight dressed up as confidence is the
kind of number a judge asks about once and never trusts again.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from agent.feedback import (
    answer_question,
    confirm_preference,
    record_applied,
    record_reply,
    record_response,
)
from memory.autonomy import decline, grant
from memory.claims import verify_claim

from .schemas import (
    AutonomyIn,
    ClaimsIn,
    ClaimUsed,
    DigestIn,
    GapOut,
    JobCard,
    PackIn,
    PreferenceIn,
    QuestionOut,
    UnverifiedClaim,
)

ANSWERED_FROM_MEMORY = ("standard_answer", "reused_answer")


def question_id_for(text: str) -> str:
    return "q-" + hashlib.sha256(text.strip().lower().encode()).hexdigest()[:10]


# --- out: agent results -> Slack payloads ----------------------------------

def _location(job: Mapping[str, Any]) -> str | None:
    """Board postings list every office in one field — "San Francisco, CA, New
    York City, NY, Seattle, WA; San Francisco, CA". Show the first and count the
    rest, or the card is three lines of cities before it says anything."""
    raw = (job.get("location") or "").strip()
    places = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    seen: list[str] = []
    for place in places:
        if place not in seen:
            seen.append(place)
    where = seen[0] if seen else ""
    if len(seen) > 2:
        where += f" +{len(seen) - 1} more"
    elif len(seen) == 2:
        where = " / ".join(seen)
    parts = [where, "Remote" if job.get("remote") else ""]
    return " · ".join(p for p in parts if p) or None


def _salary(job: Mapping[str, Any]) -> str | None:
    low, high = job.get("salary_min"), job.get("salary_max")
    if not low and not high:
        return None
    if low and high:
        return f"${int(low):,}–${int(high):,}"
    return f"${int(low or high):,}"


def _coverage(gaps: Sequence[str]) -> str | None:
    if not gaps:
        return None
    named = ", ".join(gaps[:2])
    more = f" and {len(gaps) - 2} more" if len(gaps) > 2 else ""
    return f"no verified claim for {named}{more}"


def digest_payload(result: Any, *, channel: str | None = None,
                   decided_by_agent: bool = False) -> DigestIn:
    """A ``DayResult`` from ``agent.pipeline.judge_the_day`` becomes one digest."""
    metrics = result.metrics
    cards = []
    for row in result.rank.digest_rows():
        job = result.rank.jobs.get(row["job_id"], {})
        cards.append(JobCard(
            job_id=row["job_id"],
            title=row.get("title") or job.get("title", ""),
            company=row.get("company") or job.get("company", ""),
            location=_location(job),
            salary=_salary(job),
            url=row.get("url") or None,
            why=row.get("why") or None,
            prediction=row.get("predicted") if row.get("predicted") in ("keep", "skip") else None,
            coverage=_coverage(row.get("gaps") or []),
        ))
    return DigestIn(
        run_id=result.run_id,
        day=str(result.day),
        jobs=cards,
        mode=metrics.settle_mode(),
        tokens=metrics.tokens_in + metrics.tokens_out,
        wall_ms=metrics.wall_ms(),
        questions_asked=metrics.questions_asked,
        decided_by_agent=decided_by_agent,
        channel=channel,
    )


def pack_payload(pack: Any, *, run_id: str, play: str | None = None,
                 channel: str | None = None) -> PackIn:
    """A ``Pack`` from ``agent.pack.build_pack`` becomes one apply-pack message."""
    answers = pack.answers or []
    return PackIn(
        run_id=run_id,
        job_id=pack.job_id,
        title=pack.title,
        company=pack.company,
        summary=pack.copy or pack.failure or "",
        claims=[ClaimUsed(claim_id=p["claim_id"], text=p.get("text", ""))
                for p in pack.provenance],
        gaps=[GapOut(skill=skill, note=pack.gap_note or None) for skill in pack.gaps],
        questions=[QuestionOut(question_id=question_id_for(q), text=q)
                   for q in pack.questions_to_ask],
        answers_from_memory=sum(1 for a in answers
                                if a.get("source") in ANSWERED_FROM_MEMORY),
        play=play,
        tokens=(pack.tokens_in + pack.tokens_out) or None,
        channel=channel,
    )


def preference_payload(hypothesis: Mapping[str, Any], *, run_id: str,
                       channel: str | None = None) -> PreferenceIn:
    """One entry of ``record_response()['preference_hypotheses']``."""
    evidence = list(hypothesis.get("evidence_jobs") or [])
    if not evidence:
        count = hypothesis.get("evidence_count", 0)
        evidence = [f"{count} 'not for me' answers in a row"]
    return PreferenceIn(
        run_id=run_id,
        preference_id=hypothesis["preference_id"],
        rule_text=hypothesis.get("rule") or hypothesis.get("prompt", ""),
        evidence=evidence,
        channel=channel,
    )


def autonomy_payload(readiness: Any, *, run_id: str,
                     channel: str | None = None) -> AutonomyIn | None:
    """``None`` when the domain is not Ready — there is no prompt to post."""
    prompt = readiness.prompt()
    if not prompt:
        return None
    return AutonomyIn(
        run_id=run_id,
        domain=readiness.domain,
        headline=prompt,
        agreed=round(readiness.value * readiness.window),
        total=readiness.window,
        accuracy=readiness.value,
        channel=channel,
    )


def claims_payload(rows: Sequence[Mapping[str, Any]], *, run_id: str,
                   channel: str | None = None) -> ClaimsIn:
    return ClaimsIn(
        run_id=run_id,
        claims=[UnverifiedClaim(claim_id=r["claim_id"], text=r.get("text", ""),
                                source_doc=r.get("source_doc"))
                for r in rows],
        channel=channel,
    )


# --- in: one Signal -> one call into the agent -----------------------------

DECISIONS = ("keep", "skip", "not_for_me")


def apply_signal(signal: Mapping[str, Any], *, store, insight, remember=None,
                 predictions: Mapping[str, str] | None = None,
                 default_day: int = 0) -> dict[str, Any]:
    """Route one human response to whichever agent function owns it.

    Returns what that function returned, plus ``kind``. A decision also carries
    ``preference_hypotheses``, which is what lets the caller post the Confirm
    prompt in the same breath as the click that produced it — the visible
    reorder only reads as cause and effect if it happens immediately.
    """
    kind = signal.get("kind", "")
    run_id = signal.get("run_id") or ""
    day = int(signal.get("day") or default_day or 0)
    payload = signal.get("payload") or {}

    if kind in DECISIONS:
        result = record_response(
            store, insight, day=day, run_id=run_id,
            signals=[{
                "job_id": signal.get("job_id"),
                "kind": kind,
                "reason_tags": list(signal.get("reason_tags") or []),
                "reason_text": signal.get("reason_text") or "",
                "decided_by": signal.get("decided_by", "human"),
            }],
            remember=remember,
            predictions=dict(predictions or {}),
        )
        return {"kind": kind, **result}

    if kind == "answer":
        question = payload.get("question") or payload.get("question_id") or ""
        if not question:
            return {"kind": kind, "skipped": "no question text on the signal"}
        return {"kind": kind, **answer_question(
            store, question, payload.get("answer") or "", day=day, remember=remember)}

    if kind in ("confirm_preference", "reject_preference"):
        preference_id = payload.get("p") or payload.get("preference_id") or ""
        return {"kind": kind, **confirm_preference(
            store, preference_id, confirmed=kind == "confirm_preference")}

    if kind in ("grant_autonomy", "defer_autonomy"):
        domain = payload.get("d") or payload.get("domain") or ""
        fn = grant if kind == "grant_autonomy" else decline
        return {"kind": kind, **fn(store, domain)}

    if kind in ("verify_claim", "discard_claim"):
        claim_id = payload.get("cl") or payload.get("claim_id") or ""
        status = "verified" if kind == "verify_claim" else "retired"
        return {"kind": kind, **verify_claim(store, claim_id, status=status)}

    if kind == "report_reply":
        job_id = signal.get("job_id") or ""
        event = payload.get("event", "applied")
        if event == "applied":
            return {"kind": kind, **record_applied(
                store, insight, job_id, day=day, run_id=run_id)}
        return {"kind": kind, **record_reply(
            store, insight, job_id, day=day, run_id=run_id, kind=event,
            detail=payload.get("detail", ""))}

    if kind == "request_pack":
        # Not a record. The caller runs P-B and posts the result.
        return {"kind": kind, "action": "prepare_pack", "job_id": signal.get("job_id"),
                "day": day, "run_id": run_id}

    return {"kind": kind, "skipped": f"no handler for {kind!r}"}
