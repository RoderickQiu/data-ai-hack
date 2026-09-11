"""The daily judgment pass: hotdata narrows, HydraDB judges, the agent predicts.

Ranking is two-stage on purpose, so the two data layers do distinct work and
neither is doing the other's job (DESIGN §4):

1. **hotdata narrows.** SQL hard filters — title class, location, released
   within the day, drop disqualifying sponsorship language, drop anything
   already signalled on — then top-k by similarity to the candidate profile.
2. **HydraDB judges.** Claim coverage, warm path, company outcome memory and
   active preference rules, over those 30 only.

Running the graph queries against a thousand rows would be slow and pointless;
running the salary percentile in Cypher would be worse. The division of labour,
for the pitch: **hotdata answers "what is out there and how much of it",
HydraDB answers "how does it relate to me and my history".**

A day's pass is not just the new rows. Today's releases *plus* every earlier
role still unjudged get ranked together, so yesterday's memory changes today's
ranking of a role that has been sitting in the pool since day 3. That is the
compounding, and it is visible without a single new row.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from agent.config import TUNABLES
from agent.predict import Prediction, explain, predict, rank_to_similarity
from insight.store import Insight
from memory.extract import ensure_requirements, seniority_of
from memory.graph import GraphStore, nid
from memory.prefs import Rule, active_rules
from memory.writers import upsert_job

# The search index covers the whole corpus, so it is queried far wider than the
# 30 we judge: the day filter and the already-judged filter live in SQL.
SEARCH_WIDTH = 400


@dataclass
class RankResult:
    day: int
    pool_size: int
    released_today: int
    ranked: list[Prediction] = field(default_factory=list)
    slate: list[Prediction] = field(default_factory=list)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    rules_applied: int = 0

    @property
    def predicted_keep(self) -> int:
        return sum(1 for p in self.slate if p.predicted == "keep")

    def digest_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "job_id": prediction.job_id,
                "title": self.jobs.get(prediction.job_id, {}).get("title", ""),
                "company": self.jobs.get(prediction.job_id, {}).get("company", ""),
                "url": self.jobs.get(prediction.job_id, {}).get("url", ""),
                "predicted": prediction.predicted,
                "score": round(prediction.score, 3),
                "why": explain(prediction, self.jobs.get(prediction.job_id, {})),
                "gaps": prediction.gaps,
            }
            for prediction in self.slate
        ]


def candidate_profile(store: GraphStore, limit: int = 12) -> str:
    """The text the semantic search runs against: the candidate's own claims.

    Verified only. An unverified claim steering the search is the same class of
    error as an unverified claim in a pack, just quieter.
    """
    claims = store.run("verified_claims")
    parts = [claim["text"] for claim in claims[:limit]]
    skills = sorted({skill for claim in claims for skill in claim.get("skills", [])})
    if skills:
        parts.append("Skills: " + ", ".join(skills[:30]))
    return "\n".join(parts)


def refresh_and_rank(insight: Insight, store: GraphStore, day: int,
                     title_like: str = "", location: str = "",
                     remote_only: bool = False,
                     tunables=TUNABLES) -> RankResult:
    """One judgment pass. The play `refresh-and-rank` replays exactly this."""
    judged = set(insight.judged_ids())
    released = [row for row in insight.released_today(day) if row["id"] not in judged]
    pool = insight.narrow(day, title_like=title_like, location=location,
                          remote_only=remote_only)
    jobs = {row["id"]: dict(row) for row in pool}
    # Today's releases join the pool even if a hard filter would have dropped
    # them — but never a role the human has already answered on. `narrow`
    # excludes those; without the same check here a "not for me" would come
    # straight back in the same day's digest.
    for row in released:
        jobs.setdefault(row["id"], dict(row))

    # Stage 1b: similarity, as a rank rather than a raw score. The search hits
    # are also what *chooses* the 30 the graph judges — the SQL order is a
    # fallback for when no index exists, not the selection criterion.
    similarity: dict[str, float] = {}
    ordered_ids: list[str] = []
    profile = candidate_profile(store)
    if profile:
        try:
            # The index has no day filter, so it is searched wide and then
            # intersected with what stage 1 allowed through. Ranking inside the
            # intersection is what "top 30 by similarity to the profile" means
            # once the hard filters have had their say.
            hits = insight.search(profile, limit=SEARCH_WIDTH)
            allowed = [hit["id"] for hit in hits if hit.get("id") in jobs]
            for index, job_id in enumerate(allowed):
                similarity[job_id] = rank_to_similarity(index, len(allowed))
                ordered_ids.append(job_id)
        except Exception:
            # A missing index must not take the ranking pass down. The two
            # remaining features still produce an honest ordering.
            similarity = {}

    # Stage 2: the graph judges the survivors, and only the survivors.
    shortlist = _shortlist(jobs, ordered_ids, tunables.narrow_top_k)
    rules: list[Rule] = active_rules(store)
    warm = {row["job_id"]: row for row in store.run("warm_path", {"limit": 50})}
    outcomes = {row["company"]: row for row in store.run("company_outcomes")}
    salary_cache: dict[str, dict[str, Any]] = {}

    # One fetch of the shortlist's descriptions, used only for the first-time
    # requirement extraction. It never leaves this function.
    descriptions = _descriptions_for(insight, store, shortlist)

    predictions: list[Prediction] = []
    for job in shortlist:
        job.setdefault("seniority", seniority_of(job.get("title", "")))
        upsert_job(store, job)               # a job enters the graph when judged
        # Requirements are extracted once per job, the first time it is judged,
        # and read for free on every run after that.
        if job["id"] in descriptions:
            ensure_requirements(store, job["id"], descriptions[job["id"]])
        coverage_rows = store.run("claim_coverage", {"job_id": job["id"]})
        title = (job.get("title") or "").lower()
        title_key = _title_class(title)
        if title_key not in salary_cache:
            salary_cache[title_key] = insight.salary_percentile(title_key,
                                                                location=location)
        predictions.append(predict(
            job, coverage_rows, rules,
            similarity=similarity.get(job["id"], 0.0),
            warm=warm.get(job["id"]),
            company_outcome=outcomes.get(job.get("company")),
            salary=salary_cache[title_key],
            tunables=tunables,
        ))

    predictions.sort(key=lambda p: -p.score)
    store.flush()

    return RankResult(
        day=day, pool_size=len(jobs), released_today=len(released),
        ranked=predictions, slate=build_slate(predictions, tunables=tunables),
        jobs=jobs, rules_applied=len(rules),
    )


def build_slate(ranked: Sequence[Prediction], tunables=TUNABLES) -> list[Prediction]:
    """Five roles: the top three, plus two drawn from outside the top ranking.

    The mix is not decoration (DESIGN §7). On an all-top-5 slate the human keeps
    nearly everything, "keep" becomes trivially predictable, and
    ``prediction_accuracy`` rises because the base rate moved rather than
    because judgment sharpened. Holding two slots for harder roles keeps skips
    occurring, which is what makes the skip-class number mean anything.

    The two are picked deterministically from the tail — same slate on a replay,
    which a random draw would quietly break.
    """
    size = tunables.slate_size
    off = tunables.slate_off_ranking
    top = list(ranked[: max(size - off, 0)])
    tail = list(ranked[max(size - off, 0):])
    if not tail:
        return list(ranked[:size])
    picks = []
    for slot in range(off):
        if not tail:
            break
        seed = int(hashlib.sha256(
            f"{slot}|{'|'.join(p.job_id for p in top)}".encode()
        ).hexdigest()[:8], 16)
        picks.append(tail.pop(seed % len(tail)))
    for pick in picks:
        pick.reasons.append("shown as a harder call, to keep the slate honest")
    return top + picks


def _descriptions_for(insight: Insight, store: GraphStore,
                      shortlist: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Descriptions for the jobs that have never been extracted. Usually empty
    after the first few days, which is the cost line falling for a real reason."""
    needed = []
    for job in shortlist:
        try:
            if not store.graph.out(nid("job", job["id"]), "REQUIRES"):
                needed.append(job["id"])
        except AttributeError:              # Bolt backend: ask the graph
            if not store.run("claim_coverage", {"job_id": job["id"]}):
                needed.append(job["id"])
    if not needed:
        return {}
    rows = insight.run("descriptions_by_id", {"job_ids": needed})
    return {row["id"]: row.get("description") or "" for row in rows}


def _shortlist(jobs: Mapping[str, dict[str, Any]], ordered_ids: Sequence[str],
               limit: int) -> list[dict[str, Any]]:
    """Search hits first, then fill from the SQL order — but round-robin by
    company, so one employer with 126 open roles cannot own the whole slate."""
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for job_id in ordered_ids:
        if job_id in jobs and job_id not in seen:
            seen.add(job_id)
            picked.append(jobs[job_id])
    by_company: dict[str, list[dict[str, Any]]] = {}
    for job_id, job in jobs.items():
        if job_id not in seen:
            by_company.setdefault(job.get("company") or "?", []).append(job)
    while len(picked) < limit and any(by_company.values()):
        for company in sorted(by_company):
            bucket = by_company[company]
            if bucket and len(picked) < limit:
                picked.append(bucket.pop(0))
    return picked[:limit]


def _title_class(title: str) -> str:
    """Coarse title bucket for the salary percentile. Overfitting a percentile
    to an exact title gives an N of three and a meaningless number."""
    for word in ("engineering manager", "product manager", "data scientist",
                 "machine learning", "software engineer", "designer", "analyst",
                 "researcher", "developer", "engineer", "manager"):
        if word in title:
            return word
    return title.split(",")[0].strip()[:40]


def record_slate(store: GraphStore, result: RankResult, run_id: str) -> None:
    """Write the predictions before the human sees the digest.

    Order matters: a prediction logged after the response is not a prediction.
    """
    from memory.writers import record_prediction

    for prediction in result.slate:
        record_prediction(store, prediction.job_id, prediction.predicted,
                          day=result.day, score=prediction.score, run_id=run_id)
    store.flush()
