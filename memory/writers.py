"""Every edge the agent writes from its own actions.

This is the half of the graph that no text extraction can produce, and the
reason HydraDB is not a mirror of Cognee: ``APPLIED_TO`` with a day on it,
``GOT_REPLY``, ``REJECTED`` with a reason, ``PREDICTED_KEEP`` against what the
human actually said, ``EXECUTED_BY`` on the play that did the work.

Jobs enter the graph **only once someone acts on one** (DESIGN §4). The bulk
corpus lives in hotdata; copying a thousand postings in here would make the
graph queries slow and the division of labour meaningless.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Sequence

from agent.schema import now_iso
from memory.graph import GraphStore, canonical_skill, nid


def _hash(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


# --- jobs, companies, requirements ----------------------------------------

def upsert_job(store: GraphStore, job: Mapping[str, Any]) -> str:
    """Bring one job (a hotdata row or a Job.row()) into the graph."""
    job_id = job["id"]
    job_node = nid("job", job_id)
    company_name = job.get("company") or ""
    company_node = nid("company", job.get("company_slug") or company_name.lower())
    store.merge_node(company_node, "Company", name=company_name,
                     slug=job.get("company_slug"), size=job.get("company_size"),
                     industry=job.get("industry"))
    store.merge_node(
        job_node, "Job", id=job_id, title=job.get("title"), company=company_name,
        source=job.get("source"), ats_type=job.get("ats_type"), url=job.get("url"),
        location=job.get("location"), remote=job.get("remote"),
        salary_min=job.get("salary_min"), salary_max=job.get("salary_max"),
        release_day=job.get("release_day"), seniority=job.get("seniority"),
        company_size=job.get("company_size"), industry=job.get("industry"),
    )
    store.merge_edge(company_node, "POSTED", job_node)
    return job_node


def upsert_requirements(store: GraphStore, job_id: str,
                        requirements: Iterable[Mapping[str, Any]]) -> list[str]:
    """Requirements as Cognee extracted them from the JD.

    The JD is untrusted text: it arrives here already reduced to structured
    fields by an extraction call with no tool access (DESIGN §4, §8).
    """
    job_node = nid("job", job_id)
    nodes = []
    for requirement in requirements:
        text = (requirement.get("text") or "").strip()
        if not text:
            continue
        skill_name = requirement.get("skill") or text
        skill = canonical_skill(skill_name)
        skill_node = nid("skill", skill)
        req_node = nid("requirement", job_id, _hash(text))
        store.merge_node(skill_node, "Skill", name=skill_name, canonical_name=skill)
        store.merge_node(req_node, "Requirement", text=text,
                         kind=requirement.get("kind", "required"), skill_ref=skill)
        store.merge_edge(job_node, "REQUIRES", req_node)
        store.merge_edge(req_node, "ABOUT", skill_node)
        nodes.append(req_node)
    return nodes


def record_gaps(store: GraphStore, job_id: str, gaps: Iterable[Mapping[str, Any]]) -> list[str]:
    """A requirement with no supporting verified claim. Surfaced, never papered over."""
    job_node = nid("job", job_id)
    nodes = []
    for gap in gaps:
        skill = canonical_skill(gap.get("skill", ""))
        if not skill:
            continue
        skill_node = nid("skill", skill)
        gap_node = nid("gap", job_id, _hash(skill))
        store.merge_node(skill_node, "Skill", name=gap.get("skill"), canonical_name=skill)
        store.merge_node(gap_node, "Gap", skill_ref=skill, job_ref=job_id,
                         kind=gap.get("kind", "required"), noted_at=now_iso())
        store.merge_edge(job_node, "HAS_GAP", gap_node)
        store.merge_edge(gap_node, "ABOUT", skill_node)
        nodes.append(gap_node)
    return nodes


# --- signals, predictions, outcomes ---------------------------------------

def record_signal(store: GraphStore, job_id: str, kind: str, day: int,
                  reason_tags: Sequence[str] = (), reason_text: str = "",
                  run_id: str = "") -> str:
    """One human response. Only ``not_for_me`` teaches (DESIGN §6.3).

    ``skip`` deliberately carries no preference weight: without that split,
    every busy afternoon becomes fake evidence that the candidate dislikes
    something.
    """
    if kind not in ("keep", "skip", "not_for_me"):
        raise ValueError(f"unknown signal kind {kind!r} (keep | skip | not_for_me)")
    candidate = store.candidate_node()
    signal_node = nid("signal", _hash(job_id, kind, day, run_id, now_iso()))
    store.merge_node(candidate, "Candidate", candidate_id=store.candidate_id)
    store.merge_node(signal_node, "Signal", signal_id=signal_node.split(":", 1)[1],
                     kind=kind, reason_tags=list(reason_tags), reason_text=reason_text,
                     day=day, run_id=run_id, at=now_iso())
    store.merge_edge(candidate, "GAVE", signal_node)
    store.merge_edge(signal_node, "ON", nid("job", job_id))
    if kind == "not_for_me":
        store.merge_edge(candidate, "REJECTED", nid("job", job_id),
                         reason=reason_text, reason_tags=list(reason_tags), day=day)
    return signal_node


def record_prediction(store: GraphStore, job_id: str, predicted: str, day: int,
                      score: float, run_id: str = "",
                      reasons: Sequence[str] = ()) -> None:
    """What the agent guessed, before the human saw the digest.

    Written as an edge with the prediction on it so the agreement record that
    autonomy is earned from is a graph fact, not a log line (DESIGN §8).

    ``reasons`` is the "why" line as it was shown — ``explain`` composes it from
    the live ``Prediction`` at digest time, and without storing it here nothing
    can reconstruct the sentence the human actually read. A reconstruction from
    the graph is a different sentence, and only one of the two is evidence of
    what was on the screen.
    """
    candidate = store.candidate_node()
    store.merge_edge(candidate, "PREDICTED_KEEP", nid("job", job_id),
                     predicted=predicted, score=round(score, 4), day=day,
                     run_id=run_id, reasons=[str(r) for r in reasons if r] or None)


def record_answer_to_prediction(store: GraphStore, job_id: str, actual: str,
                                predicted: str | None = None) -> None:
    """Close the loop on one prediction.

    ``predicted`` is passed in rather than assumed to be on the edge already:
    the feedback pipeline knows what was shown, and a resolution written against
    an edge that never carried a prediction would silently count as a
    disagreement in the record autonomy is earned from.
    """
    store.merge_edge(store.candidate_node(), "PREDICTED_KEEP", nid("job", job_id),
                     predicted=predicted, actual=actual, resolved_at=now_iso())


def record_application(store: GraphStore, job_id: str, day: int, play_id: str | None = None,
                       claim_ids: Sequence[str] = (), decided_by: str = "human",
                       run_id: str = "") -> str:
    """The human said they applied. Prepare, log, hand over — never submit."""
    candidate = store.candidate_node()
    app_node = nid("application", _hash(job_id, day, run_id))
    store.merge_node(app_node, "Application", application_id=app_node.split(":", 1)[1],
                     job_ref=job_id, day=day, decided_by=decided_by, run_id=run_id,
                     at=now_iso())
    store.merge_edge(app_node, "BY", candidate)
    store.merge_edge(app_node, "FOR", nid("job", job_id))
    store.merge_edge(candidate, "APPLIED_TO", nid("job", job_id), day=day,
                     decided_by=decided_by, run_id=run_id)
    for claim_id in claim_ids:
        store.merge_edge(app_node, "USED_CLAIM", nid("claim", claim_id))
    if play_id:
        store.merge_edge(app_node, "EXECUTED_BY", nid("play", play_id))
    return app_node


def record_outcome(store: GraphStore, job_id: str, kind: str, day: int,
                   detail: str = "") -> str:
    """The candidate reports a reply or a rejection. Same signal a real inbox
    would give, without the recruiter simulator (DESIGN §12)."""
    outcome_node = nid("outcome", _hash(job_id, kind, day))
    store.merge_node(outcome_node, "Outcome", kind=kind, day=day, detail=detail,
                     at=now_iso())
    edge_type = "GOT_REPLY" if kind in ("replied", "interview", "offer") else "GOT_OUTCOME"
    store.merge_edge(nid("job", job_id), edge_type, outcome_node)
    return outcome_node


# --- questions and answers -------------------------------------------------

def canonical_question_id(text: str) -> str:
    """Canonicalize so a near-match reuses the answer instead of asking again."""
    words = [w for w in "".join(
        ch if ch.isalnum() or ch.isspace() else " " for ch in (text or "").lower()
    ).split() if w not in _STOPWORDS]
    return _hash(" ".join(sorted(set(words))))


_STOPWORDS = {
    "a", "an", "the", "is", "are", "do", "does", "you", "your", "please", "we",
    "our", "to", "of", "for", "in", "on", "and", "or", "if", "this", "that",
    "will", "would", "can", "could", "have", "has", "what", "when", "any",
}


def record_question(store: GraphStore, text: str, scope: str = "role") -> str:
    question_id = canonical_question_id(text)
    node = nid("question", question_id)
    store.merge_node(node, "Question", question_id=question_id, canonical_text=text,
                     scope=scope)
    return node


def record_answer(store: GraphStore, question_text: str, answer_text: str,
                  approved: bool, day: int, application_node: str | None = None,
                  scope: str = "role") -> str:
    question_node = record_question(store, question_text, scope)
    answer_node = nid("answer", _hash(question_node, answer_text))
    store.merge_node(answer_node, "Answer", text=answer_text, approved=approved, day=day,
                     at=now_iso())
    store.merge_edge(question_node, "WITH", answer_node)
    if application_node:
        store.merge_edge(application_node, "ANSWERED", question_node)
    return answer_node


# --- plays -----------------------------------------------------------------

def upsert_play(store: GraphStore, play_id: str, task_type: str, fingerprint: str,
                rote_ref: str = "", status: str = "active", fields: Sequence[str] = (),
                success_count: int | None = None) -> str:
    """The index find_play reads. Rote holds the play; this holds the routing."""
    node = nid("play", play_id)
    props: dict[str, Any] = {
        "play_id": play_id, "task_type": task_type, "fingerprint": fingerprint,
        "rote_ref": rote_ref, "status": status, "fields": list(fields),
        "last_used": now_iso(),
    }
    if success_count is not None:
        props["success_count"] = success_count
    store.merge_node(node, "Play", **props)
    return node


def note_play_run(store: GraphStore, play_id: str, success: bool) -> None:
    node = nid("play", play_id)
    try:
        current = store.graph.props(node)
    except AttributeError:          # Bolt backend: read it back through a query
        current = {}
    count = int(current.get("success_count") or 0) + (1 if success else 0)
    store.merge_node(node, "Play", success_count=count, last_used=now_iso(),
                     status="active" if success else "stale")
