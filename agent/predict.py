"""``predict_fit``: a deterministic scorer, not an LLM call.

This is the decision that stops the three lines fighting each other (DESIGN §7).
An LLM prediction would take a ``recall`` context that grows with the graph, so
tokens per run would *rise* exactly as memory improved. A weighted score over
graph and aggregate features holds the model fixed and lets the inputs get
richer:

    0.45 * requirement_coverage + 0.30 * vector_similarity + 0.25 * preference_fit

That is also the stronger claim: **the agent did not get a better brain, it got
a better memory.**

All three memory layers are load-bearing in the number itself, not only in the
display — coverage comes from HydraDB's claim graph, similarity from hotdata's
index over a thousand postings, preference fit from rules Cognee's extraction
proposed and the human confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from agent.config import TUNABLES
from memory.graph import GraphStore
from memory.prefs import Rule, preference_fit


@dataclass
class Prediction:
    job_id: str
    score: float
    predicted: str                      # keep | skip
    coverage: float = 0.0
    similarity: float = 0.0
    preference: float = 0.5
    supported: int = 0
    required: int = 0
    gaps: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warm_via: list[str] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "score": round(self.score, 4),
                "predicted": self.predicted, "coverage": round(self.coverage, 3),
                "similarity": round(self.similarity, 3),
                "preference": round(self.preference, 3),
                "gaps": self.gaps, "reasons": self.reasons}


def requirement_coverage(rows: Sequence[Mapping[str, Any]]) -> tuple[float, int, int, list[str]]:
    """Share of requirements with a supporting *verified* claim.

    Required requirements count double: a role can be a fine match with a
    missing nice-to-have and a bad one with a missing must-have, and a flat
    ratio cannot tell those apart.
    """
    if not rows:
        return 0.0, 0, 0, []
    weight_total = weight_met = 0.0
    supported = 0
    gaps: list[str] = []
    for row in rows:
        weight = 2.0 if row.get("kind", "required") == "required" else 1.0
        weight_total += weight
        if row.get("supporting_claims"):
            weight_met += weight
            supported += 1
        else:
            skill = row.get("skill") or row.get("requirement", "")[:40]
            if skill:
                gaps.append(skill)
    return (weight_met / weight_total if weight_total else 0.0), supported, len(rows), gaps


def rank_to_similarity(rank: int | None, pool: int) -> float:
    """Turn a search rank into a 0..1 feature.

    hotdata's search returns an order, not a comparable distance, and a raw
    cosine from one index is not comparable with a BM25 score from another.
    Rank is what both surfaces agree on.
    """
    if rank is None or pool <= 0:
        return 0.0
    return max(0.0, 1.0 - (rank / max(pool, 1)))


def predict(job: Mapping[str, Any], coverage_rows: Sequence[Mapping[str, Any]],
            rules: Sequence[Rule], similarity: float = 0.0,
            warm: Mapping[str, Any] | None = None,
            company_outcome: Mapping[str, Any] | None = None,
            salary: Mapping[str, Any] | None = None,
            tunables=TUNABLES) -> Prediction:
    """Score one job. Pure function: every input is already fetched."""
    coverage, supported, required, gaps = requirement_coverage(coverage_rows)
    pref_score, pref_reasons = preference_fit(rules, job)

    score = (tunables.w_coverage * coverage
             + tunables.w_similarity * similarity
             + tunables.w_preference * pref_score)

    reasons: list[str] = []
    if required:
        reasons.append(
            f"verified claims cover {supported} of {required} requirements"
            + (f"; no claim for {gaps[0]}" if gaps else "")
        )
    warm_via: list[str] = []
    if warm and warm.get("signal"):
        warm_via = list(warm.get("via") or [])
        shared = ", ".join(warm.get("shared_skills") or [])
        reasons.append(
            "warmest path runs through "
            + (warm_via[0] if warm_via else "an application that got a reply")
            + (f" — shared {shared}" if shared else "")
        )
        # A warm path is evidence the score's other features cannot see: the
        # boost is capped so it tilts a close call and never carries a bad one.
        score = min(1.0, score + min(0.05 * warm["signal"], 0.1))
    if company_outcome and company_outcome.get("replies"):
        reasons.append(f"{company_outcome['company']} replied to "
                       f"{company_outcome['replies']} of {company_outcome['applied']} applications")
        score = min(1.0, score + 0.05)
    if salary and salary.get("p50") and job.get("salary_max"):
        p50 = float(salary["p50"])
        if float(job["salary_max"]) < p50 * 0.85:
            reasons.append(f"pays below the p50 of ${int(p50):,} for this title "
                           f"across {salary.get('postings', 0)} postings")
            score = max(0.0, score - 0.05)
    reasons.extend(pref_reasons)

    return Prediction(
        job_id=str(job.get("id") or job.get("job_id")),
        score=round(min(1.0, max(0.0, score)), 4),
        predicted="keep" if score >= tunables.keep_threshold else "skip",
        coverage=coverage, similarity=similarity, preference=pref_score,
        supported=supported, required=required, gaps=gaps, reasons=reasons,
        warm_via=warm_via,
    )


def explain(prediction: Prediction, job: Mapping[str, Any]) -> str:
    """The "why" paragraph — the most screenshot-able thing in the build.

    Rendered from the graph result, not written by a model: a job board
    structurally cannot say any of this, because it has none of the history.
    """
    head = f"*{job.get('title', 'this role')}* at {job.get('company', 'this company')}"
    if not prediction.reasons:
        return f"{head} — shown on title and location match; no history on it yet."
    body = "; ".join(prediction.reasons[:3])
    verdict = "predicted keep" if prediction.predicted == "keep" else "predicted skip"
    return f"{head} — {body}. ({verdict}, score {prediction.score:.2f})"


def coverage_for(store: GraphStore, job_id: str) -> list[dict[str, Any]]:
    return store.run("claim_coverage", {"job_id": job_id})
