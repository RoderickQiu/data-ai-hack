"""``apply-pack``: one LLM call wrapped in four deterministic tool calls.

This is the muscle memory worth leading with (DESIGN §4). "We cached an HTTP
shape" invites the response that a human could have written the fetcher in
twenty minutes. "One LLM call wrapped in four deterministic tool calls, and run
two does zero reasoning on any of the plumbing" is the thing that lands.

The shape, in order:

1. pull the job (hotdata — the one query that returns a description),
2. claim coverage and verified claims (HydraDB),
3. resolve the screening questions down the ladder (memory.answers),
4. **the only reasoning step** — one model call that returns ``claim_id``s and
   connective prose,
5. the citation validator, which fails the pack rather than shipping a
   fabrication,
6. write the gaps, the Sheet row and the Slack message.

Steps 1-3, 5 and 6 are deterministic, which is exactly why they replay. A pack
whose validator fails is not returned as a partial result: it is returned as a
failure with the reason attached, so a broken replay can never quietly produce
a wrong document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from agent.llm import LLM, PACK_SYSTEM, Completion, build_pack_prompt
from agent.metrics import RunMetrics
from insight.store import Insight
from memory.answers import resolve_all
from memory.claims import render_pack_text, validate_citations
from memory.graph import GraphStore
from memory.writers import record_gaps


@dataclass
class Pack:
    job_id: str
    ok: bool
    title: str = ""
    company: str = ""
    url: str = ""
    copy: str = ""
    tagged_copy: str = ""
    provenance: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    gap_note: str = ""
    answers: list[dict[str, Any]] = field(default_factory=list)
    questions_to_ask: list[str] = field(default_factory=list)
    failure: str = ""
    tokens_in: int = 0
    tokens_out: int = 0

    def summary(self) -> dict[str, Any]:
        """Ids and one-line summaries. The full copy is fetched deliberately."""
        return {"job_id": self.job_id, "ok": self.ok, "title": self.title,
                "company": self.company, "claims_cited": len(self.provenance),
                "gaps": self.gaps, "questions_to_ask": len(self.questions_to_ask),
                "failure": self.failure}


def build_pack(insight: Insight, store: GraphStore, job_id: str,
               screening_questions: Sequence[str] = (), day: int = 0,
               llm: LLM | None = None, metrics: RunMetrics | None = None) -> Pack:
    rows = insight.run("job", {"job_id": job_id})
    if not rows:
        return Pack(job_id=job_id, ok=False, failure=f"no job {job_id!r} in the corpus")
    job = rows[0]

    coverage = store.run("claim_coverage", {"job_id": job_id})
    claims = store.run("verified_claims")
    gaps = [row["skill"] for row in coverage if row.get("is_gap") and row.get("skill")]
    if gaps:
        record_gaps(store, job_id, [{"skill": skill} for skill in gaps])

    ladder = resolve_all(store, list(screening_questions))
    if metrics:
        metrics.note_ladder(ladder)

    if not claims:
        return Pack(job_id=job_id, ok=False, title=job.get("title", ""),
                    company=job.get("company", ""), gaps=gaps,
                    answers=ladder["resolutions"],
                    questions_to_ask=ladder["questions_to_ask"],
                    failure="no verified claims — nothing may be cited yet")

    llm = llm or LLM()
    completion: Completion = llm.complete(
        PACK_SYSTEM, build_pack_prompt(job, coverage, claims))
    if metrics:
        metrics.note_llm(completion.tokens_in, completion.tokens_out)
        metrics.note_reasoned()

    try:
        result = completion.json()
    except ValueError:
        return Pack(job_id=job_id, ok=False, title=job.get("title", ""),
                    company=job.get("company", ""), gaps=gaps,
                    tokens_in=completion.tokens_in, tokens_out=completion.tokens_out,
                    failure="model did not return JSON")

    draft = str(result.get("draft") or "")
    validation = validate_citations(store, draft)
    claim_text = {claim["claim_id"]: claim["text"] for claim in claims}
    rendered = render_pack_text(draft, claim_text)

    pack = Pack(
        job_id=job_id, ok=validation.ok, title=job.get("title", ""),
        company=job.get("company", ""), url=job.get("url", ""),
        copy=rendered["copy"], tagged_copy=draft, provenance=rendered["provenance"],
        gaps=gaps, gap_note=str(result.get("gap_note") or ""),
        answers=ladder["resolutions"], questions_to_ask=ladder["questions_to_ask"],
        tokens_in=completion.tokens_in, tokens_out=completion.tokens_out,
    )
    if not validation.ok:
        pack.failure = "citation check failed: " + validation.reason()
    store.flush()
    return pack


def tracker_row(pack: Pack, day: int, run_id: str, decided_by: str = "human") -> dict[str, Any]:
    """The row appended to the Google Sheet. No PII beyond what the human sees."""
    return {
        "day": day, "run_id": run_id, "job_id": pack.job_id, "company": pack.company,
        "title": pack.title, "url": pack.url,
        "claims_cited": ",".join(item["claim_id"] for item in pack.provenance),
        "gaps": ", ".join(pack.gaps), "questions_open": len(pack.questions_to_ask),
        "status": "prepared" if pack.ok else f"blocked: {pack.failure}",
        "decided_by": decided_by,
    }
