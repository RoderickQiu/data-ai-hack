"""Requirements out of a job description, deterministically, and once per job.

``predict_fit`` is a deterministic scorer and must stay one (DESIGN §7), which
means the requirement coverage it reads cannot come from an LLM call made on
every run. So requirements are extracted **once per job, the first time the job
reaches stage 2**, cached on the graph, and read for free ever after. That is
also why the cost line falls: day 1 pays for thirty extractions, day 12 pays for
almost none, and nothing re-extracts.

The extractor is a lexicon match, not a model. Three reasons:

* it is explainable on stage — "the JD says Kubernetes, you have no verified
  claim mentioning Kubernetes" is checkable by reading two strings;
* it cannot hallucinate a requirement, which matters because a phantom
  requirement produces a phantom *gap*, and gaps are what P5 rests on;
* **a job description is a text field a stranger controls.** Matching a fixed
  vocabulary against it cannot be talked into anything. The richer typed
  extraction still exists — :data:`memory.graph_model.JD_EXTRACTION_PROMPT`, no
  tool access, JSON out — and :func:`merge_extracted` folds its results in over
  the top, marked with their origin.

Adding to the lexicon is cheap and safe. Leaving something out costs a gap that
never gets noticed, which is the failure worth avoiding.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from memory.graph import GraphStore, canonical_skill, nid
from memory.writers import upsert_requirements

# One entry per skill: the canonical name, then the spellings a JD might use.
LEXICON: dict[str, tuple[str, ...]] = {
    "python": ("python",),
    "typescript": ("typescript", "ts"),
    "javascript": ("javascript", "node.js", "nodejs"),
    "go": ("golang",),
    "rust": ("rust",),
    "java": ("java",),
    "scala": ("scala",),
    "c++": ("c++",),
    "sql": ("sql",),
    "kubernetes": ("kubernetes", "k8s"),
    "docker": ("docker", "containers"),
    "terraform": ("terraform",),
    "infrastructure as code": ("infrastructure as code", "iac"),
    "aws": ("aws", "amazon web services"),
    "google cloud": ("gcp", "google cloud"),
    "azure": ("azure",),
    "ci cd": ("ci/cd", "continuous integration", "continuous delivery"),
    "observability": ("observability", "prometheus", "grafana", "datadog"),
    "postgresql": ("postgres", "postgresql"),
    "mysql": ("mysql",),
    "clickhouse": ("clickhouse",),
    "snowflake": ("snowflake",),
    "bigquery": ("bigquery",),
    "spark": ("spark", "pyspark"),
    "kafka": ("kafka",),
    "airflow": ("airflow",),
    "dbt": ("dbt",),
    "data modeling": ("data modeling", "data modelling", "data contracts"),
    "machine learning": ("machine learning", "ml engineering"),
    "deep learning": ("deep learning", "pytorch", "tensorflow", "jax"),
    "llm evals": ("evals", "evaluation harness", "llm evaluation", "ai evals"),
    "llms": ("llm", "llms", "large language model", "foundation model"),
    "rag": ("rag", "retrieval augmented", "vector search", "embeddings"),
    "nlp": ("nlp", "natural language processing"),
    "computer vision": ("computer vision", "cv models"),
    "recommender systems": ("recommender", "ranking systems", "personalization"),
    "distributed systems": ("distributed systems", "high availability"),
    "microservices": ("microservices", "service oriented"),
    "api design": ("api design", "rest api", "graphql", "grpc"),
    "fastapi": ("fastapi",),
    "django": ("django",),
    "react": ("react", "react.js"),
    "frontend": ("frontend", "front-end"),
    "mobile": ("ios", "android", "swift", "kotlin"),
    "security": ("security", "appsec", "threat model"),
    "privacy": ("gdpr", "privacy", "data protection"),
    "performance": ("latency", "throughput", "performance tuning"),
    "on call": ("on-call", "on call", "incident response", "sre"),
    "mentoring": ("mentor", "mentoring", "coaching engineers"),
    "people management": ("manage a team", "people management", "direct reports",
                          "hiring and performance"),
    "product sense": ("product sense", "product thinking", "roadmap"),
    "stakeholder management": ("stakeholder", "cross-functional partners"),
    "experimentation": ("a/b test", "experimentation", "ab testing"),
    "analytics": ("analytics", "dashboards", "reporting"),
    "statistics": ("statistics", "statistical", "causal inference"),
}

# A requirement is "preferred" when the sentence hedges. Everything else is
# required, which errs toward reporting a gap rather than hiding one.
_PREFERRED = re.compile(
    r"\b(nice to have|nice-to-have|bonus|a plus|preferred|ideally|would be great|"
    r"desirable|not required)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?;:•\n])\s+")
_YEARS = re.compile(r"\b(\d{1,2})\+?\s*years?\b", re.I)


def requirements_from_jd(text: str, max_requirements: int = 20) -> list[dict[str, Any]]:
    """Lexicon match over a job description. Returns our Requirement shape."""
    if not text:
        return []
    lowered = text.lower()
    found: dict[str, dict[str, Any]] = {}
    sentences = [s.strip() for s in _SENTENCE.split(text) if s.strip()]

    for canonical, spellings in LEXICON.items():
        hit_sentence = None
        for spelling in spellings:
            index = lowered.find(spelling)
            if index == -1:
                continue
            # Whole-word only, so "go" does not match "going" and "ts" does not
            # match "requirements".
            before = lowered[index - 1] if index else " "
            after_index = index + len(spelling)
            after = lowered[after_index] if after_index < len(lowered) else " "
            if before.isalnum() or (after.isalnum() and not spelling.endswith("+")):
                continue
            hit_sentence = next((s for s in sentences if spelling in s.lower()), spelling)
            break
        if hit_sentence is None:
            continue
        years = _YEARS.search(hit_sentence)
        found[canonical] = {
            "text": (hit_sentence[:180]).strip(),
            "kind": "preferred" if _PREFERRED.search(hit_sentence) else "required",
            "skill": canonical,
            "years": int(years.group(1)) if years else None,
            "origin": "lexicon",
        }
    # Required first, so a truncated list never drops a must-have for a bonus.
    ordered = sorted(found.values(), key=lambda r: (r["kind"] != "required", r["skill"]))
    return ordered[:max_requirements]


def ensure_requirements(store: GraphStore, job_id: str, description: str) -> int:
    """Extract and store, unless this job already has requirements. Idempotent."""
    job_node = nid("job", job_id)
    try:
        if store.graph.out(job_node, "REQUIRES"):
            return 0
    except AttributeError:                       # Bolt backend
        if store.run("claim_coverage", {"job_id": job_id}):
            return 0
    requirements = requirements_from_jd(description)
    if not requirements:
        return 0
    upsert_requirements(store, job_id, requirements)
    return len(requirements)


def merge_extracted(store: GraphStore, job_id: str,
                    extracted: Mapping[str, Any]) -> dict[str, Any]:
    """Fold in the richer typed extraction when it has run for this job.

    ``extracted`` is what the guarded JD prompt returned: JSON only, from a call
    with no tool access. Its requirements land beside the lexicon's, and its
    company attributes go on the Job node where the preference rules read them.
    """
    requirements = [dict(r, origin="llm") for r in extracted.get("requirements") or []]
    if requirements:
        upsert_requirements(store, job_id, requirements)
    attributes = {key: extracted.get(key) for key in
                  ("seniority", "company_size", "industry") if extracted.get(key)}
    if attributes:
        store.merge_node(nid("job", job_id), "Job", id=job_id, **attributes)
    return {"requirements": len(requirements), "attributes": sorted(attributes)}


_SENIORITY_WORDS = (("internship", "intern"), ("intern", "intern"),
                    ("junior", "junior"), ("associate", "junior"),
                    ("principal", "principal"), ("staff", "staff"),
                    ("director", "director"), ("head of", "director"),
                    ("vp", "executive"), ("vice president", "executive"),
                    ("senior", "senior"), ("lead", "lead"), ("leader", "lead"),
                    ("manager", "manager"))

# Whole words only. Substring matching read "intern" out of "Internal" and
# "International", and because intern is checked first — before staff and
# principal — it won every time: "Staff Software Engineer - Database Engine
# Internals" classified as an internship. 12 titles in the corpus, and the
# seniority preference rules read this column.
_SENIORITY_RE = {word: re.compile(rf"\b{re.escape(word)}\b") for word, _ in _SENIORITY_WORDS}


def seniority_of(title: str) -> str:
    """Coarse seniority from the title, for the preference rules to bite on.

    Returns ``"mid"`` when nothing matches. That is the *unclassified* bucket,
    not a level — 45% of this corpus — so a caller inferring a preference from
    it is inferring one from "the title said nothing".
    """
    lowered = (title or "").lower()
    for word, level in _SENIORITY_WORDS:
        if _SENIORITY_RE[word].search(lowered):
            return level
    return "mid"


def skills_in(text: str) -> set[str]:
    """Canonical skills a piece of text mentions. Used to link claims to skills."""
    return {canonical_skill(r["skill"]) for r in requirements_from_jd(text, 50)}


def link_claim_skills(store: GraphStore, claim_ids: Iterable[str] | None = None) -> int:
    """Give claims their skills from their own wording.

    Cognee does this properly from the resume; this covers the case where the
    graph has claims before a cognify has run, so a cold start still produces
    real coverage rather than a graph full of unlinked bullets.
    """
    candidate = store.candidate_node()
    linked = 0
    wanted = set(claim_ids) if claim_ids else None
    for edge in store.graph.out(candidate, "HAS_CLAIM"):
        props = store.graph.props(edge.dst)
        if wanted and props.get("claim_id") not in wanted:
            continue
        for skill in skills_in(props.get("text", "")):
            skill_node = nid("skill", skill)
            store.merge_node(skill_node, "Skill", name=skill, canonical_name=skill)
            if not store.graph.has_edge(edge.dst, "DEMONSTRATES", skill_node):
                store.merge_edge(edge.dst, "DEMONSTRATES", skill_node, origin="lexicon")
                linked += 1
            if props.get("status") == "verified":
                store.merge_edge(candidate, "HAS_SKILL", skill_node, via="verified_claim")
    store.flush()
    return linked
