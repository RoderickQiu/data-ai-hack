"""The durable candidate graph: HydraDB, the named queries, and both backends.

**Cognee holds what was said; this holds what happened.** Cognee extracts
``Candidate``, ``Claim``, ``Skill``, ``Job``, ``Company`` and ``Requirement``
out of unstructured text. This layer holds those *plus* the edges the agent
writes from its own actions, which no text extraction can produce: ``APPLIED_TO``
stamped with a day, ``GOT_REPLY``, ``REJECTED`` with a reason, ``PREDICTED_KEEP``
against what the human actually answered, ``HOLDS`` on a confirmed preference,
``EXECUTED_BY`` on the play that did the work. Every query below traverses both
halves, which is why neither layer can answer them alone.

Two backends, one query registry
--------------------------------

DESIGN §4 said: decide the HydraDB surface before hour 0, and if the cloud key
does not answer Cypher, run the OSS engine and move on. Checked against the live
tenant on 2026-09-11 — ``hydradb-sdk`` 2.1.4 exposes ``context.ingest``,
``list``, ``relations``, ``subgraph``, ``inspect``, ``delete`` and nothing that
takes a query language. There is no Cypher on the cloud key. So:

* ``BoltBackend`` runs the Cypher below for real, against the HydraDB OSS engine
  on 7687 (``make graph-up``). Set ``GRAPH_BOLT_URL`` and it is used.
* ``LocalBackend`` (the default) evaluates the *same named queries* in process
  over a property graph that is persisted to HydraDB cloud as memory items — so
  HydraDB is still the durable, team-shared store and the graph shows up in its
  dashboard, the multi-hop just runs client-side.

Both answer ``run(name, params)`` identically, so nothing above this file knows
or cares which is live. The Cypher is not decoration: it is the definition each
Python evaluator is written against, and switching backends is one env var.

Named queries only
------------------

``hydra_query(name, params)``, never ``hydra_cypher(text)``. Same three reasons
as the SQL registry: no per-run reasoning tokens spent composing queries, a
named query is trivially a Rote play, and there is no LLM-driven injection
surface for ``snyk code test`` to find.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping

from agent.config import Settings, settings, state_path

GRAPH_FILE = "graph.json"


# --- the property graph ----------------------------------------------------

@dataclass
class Node:
    id: str
    label: str
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    src: str
    type: str
    dst: str
    props: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.src, self.type, self.dst)


class Graph:
    """A tiny property graph. MERGE semantics, because every writer re-asserts."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple[str, str, str], Edge] = {}
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}

    # writes
    def merge_node(self, node_id: str, label: str, **props: Any) -> Node:
        node = self.nodes.get(node_id)
        if node is None:
            node = Node(node_id, label, {})
            self.nodes[node_id] = node
        node.label = label or node.label
        node.props.update({k: v for k, v in props.items() if v is not None})
        return node

    def merge_edge(self, src: str, type_: str, dst: str, **props: Any) -> Edge:
        edge = self.edges.get((src, type_, dst))
        if edge is None:
            edge = Edge(src, type_, dst, {})
            self.edges[edge.key] = edge
            self._out.setdefault(src, []).append(edge)
            self._in.setdefault(dst, []).append(edge)
        edge.props.update({k: v for k, v in props.items() if v is not None})
        return edge

    def drop_edge(self, src: str, type_: str, dst: str) -> None:
        edge = self.edges.pop((src, type_, dst), None)
        if edge is None:
            return
        self._out.get(src, []).remove(edge)
        self._in.get(dst, []).remove(edge)

    # reads
    def node(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)

    def by_label(self, label: str) -> list[Node]:
        return [node for node in self.nodes.values() if node.label == label]

    def out(self, node_id: str, type_: str | None = None) -> list[Edge]:
        edges = self._out.get(node_id, [])
        return [e for e in edges if type_ is None or e.type == type_]

    def into(self, node_id: str, type_: str | None = None) -> list[Edge]:
        edges = self._in.get(node_id, [])
        return [e for e in edges if type_ is None or e.type == type_]

    def neighbours(self, node_id: str, type_: str | None = None) -> list[str]:
        return [edge.dst for edge in self.out(node_id, type_)]

    def has_edge(self, src: str, type_: str, dst: str) -> bool:
        return (src, type_, dst) in self.edges

    def props(self, node_id: str) -> dict[str, Any]:
        node = self.nodes.get(node_id)
        return node.props if node else {}

    # serialization
    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [asdict(node) for node in self.nodes.values()],
            "edges": [asdict(edge) for edge in self.edges.values()],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Graph":
        graph = cls()
        for node in data.get("nodes", []):
            graph.merge_node(node["id"], node.get("label", ""), **(node.get("props") or {}))
        for edge in data.get("edges", []):
            graph.merge_edge(edge["src"], edge["type"], edge["dst"], **(edge.get("props") or {}))
        return graph

    def stats(self) -> dict[str, Any]:
        labels: dict[str, int] = {}
        for node in self.nodes.values():
            labels[node.label] = labels.get(node.label, 0) + 1
        types: dict[str, int] = {}
        for edge in self.edges.values():
            types[edge.type] = types.get(edge.type, 0) + 1
        return {"nodes": len(self.nodes), "edges": len(self.edges),
                "labels": labels, "edge_types": types}


# --- node id conventions ---------------------------------------------------
#
# Ids are constructed, never generated, so a re-run of any writer MERGEs onto
# the node it wrote last time instead of forking a duplicate.

def nid(kind: str, *parts: Any) -> str:
    return ":".join([kind, *[str(p) for p in parts]])


def canonical_skill(name: str) -> str:
    """Normalize so 'LLM evals' and 'AI evals' land on one node (DESIGN §4)."""
    text = " ".join((name or "").lower().replace("-", " ").replace("/", " ").split())
    aliases = {
        "ai evals": "llm evals", "llm evaluation": "llm evals", "evals": "llm evals",
        "k8s": "kubernetes", "postgres": "postgresql", "gcp": "google cloud",
        "js": "javascript", "ts": "typescript", "ml": "machine learning",
        "nlp": "natural language processing", "iac": "infrastructure as code",
    }
    return aliases.get(text, text)


# --- the named queries -----------------------------------------------------

@dataclass(frozen=True)
class GraphQuery:
    name: str
    cypher: str
    params: tuple[str, ...]
    summary: str
    evaluate: Callable[[Graph, dict[str, Any]], list[dict[str, Any]]]


_REGISTRY: dict[str, GraphQuery] = {}


def _register(name: str, cypher: str, params: tuple[str, ...], summary: str):
    def decorator(fn: Callable[[Graph, dict[str, Any]], list[dict[str, Any]]]):
        _REGISTRY[name] = GraphQuery(name, " ".join(cypher.split()), params, summary, fn)
        return fn
    return decorator


def _cand(params: Mapping[str, Any]) -> str:
    return nid("candidate", params["candidate_id"])


@_register(
    "skill_overlap",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[:HAS_SKILL]->(s:Skill)
             <-[:ABOUT]-(r:Requirement)<-[:REQUIRES]-(j:Job)<-[:POSTED]-(co:Company)
       WHERE NOT (c)-[:APPLIED_TO]->(j) AND j.id IN $job_ids
       RETURN j.id AS job_id, co.name AS company, count(DISTINCT s) AS overlap
       ORDER BY overlap DESC LIMIT $limit""",
    ("candidate_id", "job_ids", "limit"),
    "How many of my skills each role actually asks for.",
)
def _skill_overlap(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    mine = {e.dst for e in graph.out(candidate, "HAS_SKILL")}
    wanted = set(params.get("job_ids") or [])
    rows = []
    for job in graph.by_label("Job"):
        if wanted and job.props.get("id") not in wanted:
            continue
        if graph.has_edge(candidate, "APPLIED_TO", job.id):
            continue
        skills = _job_skills(graph, job.id)
        overlap = len(skills & mine)
        rows.append({
            "job_id": job.props.get("id", job.id),
            "company": _company_of(graph, job.id),
            "overlap": overlap,
            "required": len(skills),
        })
    rows.sort(key=lambda row: -row["overlap"])
    return rows[: params.get("limit", 10)]


@_register(
    "warm_path",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[:APPLIED_TO]->(j1:Job)-[:GOT_REPLY]->(),
             (j1)-[:REQUIRES]->(:Requirement)-[:ABOUT]->(s:Skill)
             <-[:ABOUT]-(:Requirement)<-[:REQUIRES]-(j2:Job)
       WHERE NOT (c)-[:APPLIED_TO]->(j2)
       RETURN j2.id AS job_id, count(DISTINCT s) AS signal, collect(DISTINCT j1.id) AS via
       ORDER BY signal DESC LIMIT $limit""",
    ("candidate_id", "limit"),
    "Roles sharing skills with the applications that actually got a reply.",
)
def _warm_path(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    replied = [e.dst for e in graph.out(candidate, "APPLIED_TO")
               if graph.out(e.dst, "GOT_REPLY")]
    warm_skills: dict[str, set[str]] = {}
    for job in replied:
        for skill in _job_skills(graph, job):
            warm_skills.setdefault(skill, set()).add(job)
    rows = []
    for job in graph.by_label("Job"):
        if graph.has_edge(candidate, "APPLIED_TO", job.id):
            continue
        shared = _job_skills(graph, job.id) & set(warm_skills)
        if not shared:
            continue
        via = sorted({v for skill in shared for v in warm_skills[skill]})
        rows.append({"job_id": job.props.get("id", job.id), "signal": len(shared),
                     "via": [graph.props(v).get("id", v) for v in via[:3]],
                     "shared_skills": sorted(graph.props(s).get("name", s) for s in shared)[:5]})
    rows.sort(key=lambda row: -row["signal"])
    return rows[: params.get("limit", 10)]


@_register(
    "company_outcomes",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[:APPLIED_TO]->(j:Job)<-[:POSTED]-(co:Company)
       OPTIONAL MATCH (j)-[:GOT_REPLY]->(o:Outcome)
       RETURN co.name AS company, count(DISTINCT j) AS applied, count(DISTINCT o) AS replies
       ORDER BY replies DESC""",
    ("candidate_id",),
    "Who actually replies to this candidate.",
)
def _company_outcomes(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    tally: dict[str, dict[str, Any]] = {}
    for edge in graph.out(candidate, "APPLIED_TO"):
        company = _company_of(graph, edge.dst) or "unknown"
        entry = tally.setdefault(company, {"company": company, "applied": 0, "replies": 0})
        entry["applied"] += 1
        entry["replies"] += len(graph.out(edge.dst, "GOT_REPLY"))
    rows = sorted(tally.values(), key=lambda row: (-row["replies"], -row["applied"]))
    return rows


@_register(
    "skill_adjacency",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[:HAS_SKILL]->(s1:Skill)
             <-[:ABOUT]-(:Requirement)<-[:REQUIRES]-(j:Job)-[:REQUIRES]->(:Requirement)-[:ABOUT]->(s2:Skill)
       WHERE NOT (c)-[:HAS_SKILL]->(s2)
       RETURN s2.name AS skill, count(DISTINCT j) AS demand ORDER BY demand DESC LIMIT $limit""",
    ("candidate_id", "limit"),
    "What roles wanting my skills also want, that I do not have.",
)
def _skill_adjacency(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    mine = {e.dst for e in graph.out(candidate, "HAS_SKILL")}
    demand: dict[str, int] = {}
    for job in graph.by_label("Job"):
        skills = _job_skills(graph, job.id)
        if not skills & mine:
            continue
        for skill in skills - mine:
            demand[skill] = demand.get(skill, 0) + 1
    rows = [{"skill": graph.props(s).get("name", s), "demand": n} for s, n in demand.items()]
    rows.sort(key=lambda row: -row["demand"])
    return rows[: params.get("limit", 10)]


@_register(
    "rejection_reasons",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[r:REJECTED]->(j:Job)
       RETURN j.id AS job_id, j.title AS title, r.reason AS reason, r.reason_tags AS tags, r.day AS day
       ORDER BY r.day DESC LIMIT $limit""",
    ("candidate_id", "limit"),
    "What I turned down and why — the input to the 'this repeats a reason' line.",
)
def _rejection_reasons(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    rows = []
    for edge in graph.out(candidate, "REJECTED"):
        job = graph.props(edge.dst)
        rows.append({"job_id": job.get("id", edge.dst), "title": job.get("title", ""),
                     "company": _company_of(graph, edge.dst),
                     "reason": edge.props.get("reason", ""),
                     "tags": edge.props.get("reason_tags", []),
                     "day": edge.props.get("day")})
    rows.sort(key=lambda row: -(row["day"] or 0))
    return rows[: params.get("limit", 20)]


@_register(
    "followups_due",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[a:APPLIED_TO]->(j:Job)<-[:POSTED]-(co:Company)
       WHERE NOT (j)-[:GOT_REPLY]->() AND a.day < $cutoff_day
       RETURN co.name AS company, j.title AS title, a.day AS applied_day""",
    ("candidate_id", "cutoff_day"),
    "Applied, no reply, older than the cutoff.",
)
def _followups_due(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    cutoff = params["cutoff_day"]
    rows = []
    for edge in graph.out(candidate, "APPLIED_TO"):
        if graph.out(edge.dst, "GOT_REPLY"):
            continue
        day = edge.props.get("day")
        if day is None or day >= cutoff:
            continue
        job = graph.props(edge.dst)
        rows.append({"company": _company_of(graph, edge.dst), "title": job.get("title", ""),
                     "job_id": job.get("id", edge.dst), "applied_day": day})
    rows.sort(key=lambda row: row["applied_day"])
    return rows


@_register(
    "claim_coverage",
    """MATCH (j:Job {id:$job_id})-[:REQUIRES]->(r:Requirement)-[:ABOUT]->(s:Skill)
       OPTIONAL MATCH (c:Candidate {candidate_id:$candidate_id})-[:HAS_CLAIM]->
                      (cl:Claim {status:'verified'})-[:DEMONSTRATES]->(s)
       RETURN r.text AS requirement, r.kind AS kind, s.name AS skill,
              collect(cl.claim_id) AS supporting_claims""",
    ("candidate_id", "job_id"),
    "Which requirements have verified evidence and which are gaps. Makes P5 structural.",
)
def _claim_coverage(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    job = nid("job", params["job_id"])
    verified: dict[str, list[str]] = {}
    for edge in graph.out(candidate, "HAS_CLAIM"):
        claim = graph.props(edge.dst)
        if claim.get("status") != "verified":
            continue
        for skill in graph.neighbours(edge.dst, "DEMONSTRATES"):
            verified.setdefault(skill, []).append(claim.get("claim_id", edge.dst))
    rows = []
    for edge in graph.out(job, "REQUIRES"):
        requirement = graph.props(edge.dst)
        skills = graph.neighbours(edge.dst, "ABOUT")
        supporting = sorted({c for s in skills for c in verified.get(s, [])})
        rows.append({
            "requirement": requirement.get("text", ""),
            "kind": requirement.get("kind", "required"),
            "skill": ", ".join(graph.props(s).get("name", s) for s in skills),
            "supporting_claims": supporting,
            "is_gap": not supporting,
        })
    return rows


@_register(
    "similar_answers",
    """MATCH (:Candidate {candidate_id:$candidate_id})<-[:BY]-(a:Application)
             -[:ANSWERED]->(q:Question)-[:WITH]->(ans:Answer {approved:true})
       WHERE q.question_id IN $question_ids
       RETURN ans.text AS text, q.scope AS scope, a.day AS day
       ORDER BY a.day DESC LIMIT 3""",
    ("candidate_id", "question_ids"),
    "A prior approved answer to a canonically similar question. Every hit is a question not asked again.",
)
def _similar_answers(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    wanted = set(params.get("question_ids") or [])
    rows = []
    for question in graph.by_label("Question"):
        if question.props.get("question_id") not in wanted:
            continue
        for edge in graph.out(question.id, "WITH"):
            answer = graph.props(edge.dst)
            if not answer.get("approved"):
                continue
            rows.append({"text": answer.get("text", ""), "scope": question.props.get("scope", ""),
                         "question_id": question.props.get("question_id"),
                         "day": answer.get("day")})
    rows.sort(key=lambda row: -(row["day"] or 0))
    return rows[:3]


@_register(
    "active_preferences",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[:HOLDS]->(p:Preference {status:'active'})
       RETURN p.rule AS rule, p.confidence AS confidence, p.evidence_count AS evidence_count""",
    ("candidate_id",),
    "Structured rules predict_fit applies deterministically.",
)
def _active_preferences(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    rows = []
    for edge in graph.out(candidate, "HOLDS"):
        pref = graph.props(edge.dst)
        if pref.get("status") != "active":
            continue
        rows.append({"preference_id": pref.get("preference_id", edge.dst),
                     "rule": pref.get("rule"), "confidence": pref.get("confidence"),
                     "evidence_count": pref.get("evidence_count"),
                     "explanation": pref.get("explanation", "")})
    return rows


@_register(
    "standard_answers",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[:HAS_ANSWER]->(sa:StandardAnswer)
       RETURN sa.key AS key, sa.text AS text, sa.confirmed_at AS confirmed_at""",
    ("candidate_id",),
    "Asked once, never again. Rung 1 of the resolution ladder.",
)
def _standard_answers(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    return [
        {"key": graph.props(e.dst).get("key"), "text": graph.props(e.dst).get("text"),
         "confirmed_at": graph.props(e.dst).get("confirmed_at")}
        for e in graph.out(candidate, "HAS_ANSWER")
    ]


@_register(
    "verified_claims",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[:HAS_CLAIM]->(cl:Claim {status:'verified'})
       OPTIONAL MATCH (cl)-[:DEMONSTRATES]->(s:Skill)
       RETURN cl.claim_id AS claim_id, cl.text AS text, cl.kind AS kind,
              collect(s.name) AS skills""",
    ("candidate_id",),
    "The only claims an apply-pack may cite.",
)
def _verified_claims(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    rows = []
    for edge in graph.out(candidate, "HAS_CLAIM"):
        claim = graph.props(edge.dst)
        if claim.get("status") != "verified":
            continue
        rows.append({
            "claim_id": claim.get("claim_id", edge.dst), "text": claim.get("text", ""),
            "kind": claim.get("kind", "bullet"),
            "skills": [graph.props(s).get("name", s) for s in graph.neighbours(edge.dst, "DEMONSTRATES")],
            "source_doc": claim.get("source_doc", ""),
        })
    return rows


@_register(
    "recent_signals",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[:GAVE]->(sig:Signal)-[:ON]->(j:Job)
       RETURN sig.kind AS kind, sig.reason_tags AS reason_tags, sig.day AS day,
              j.id AS job_id ORDER BY sig.day DESC LIMIT $limit""",
    ("candidate_id", "limit"),
    "The episodic record preference induction counts over.",
)
def _recent_signals(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    rows = []
    for edge in graph.out(candidate, "GAVE"):
        signal = graph.props(edge.dst)
        job_ids = graph.neighbours(edge.dst, "ON")
        job = graph.props(job_ids[0]) if job_ids else {}
        rows.append({
            "signal_id": signal.get("signal_id", edge.dst),
            "kind": signal.get("kind"), "reason_tags": signal.get("reason_tags", []),
            "reason_text": signal.get("reason_text", ""), "day": signal.get("day"),
            "job_id": job.get("id"), "company": _company_of(graph, job_ids[0]) if job_ids else "",
            "title": job.get("title", ""),
            "company_size": job.get("company_size"), "seniority": job.get("seniority"),
            "industry": job.get("industry"), "location": job.get("location"),
            "remote": job.get("remote"), "salary_max": job.get("salary_max"),
        })
    rows.sort(key=lambda row: -(row["day"] or 0))
    return rows[: params.get("limit", 20)]


@_register(
    "current_slate",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[p:PREDICTED_KEEP]->(j:Job)
       WHERE p.actual IS NULL AND p.predicted IS NOT NULL
         AND ($day = 0 OR p.day = $day)
       RETURN j.id AS job_id, j.title AS title, j.company AS company, j.url AS url,
              j.location AS location, j.remote AS remote, j.salary_min AS salary_min,
              j.salary_max AS salary_max, j.company_size AS company_size,
              j.seniority AS seniority, p.predicted AS predicted, p.score AS score,
              p.reasons AS reasons, p.day AS day
       ORDER BY p.day DESC, p.score DESC""",
    ("candidate_id", "day"),
    "The slate the human has not answered yet — what the digest is showing right "
    "now. The mirror of agreement_record, which deliberately returns only "
    "resolved predictions.",
)
def _current_slate(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    rows = []
    for edge in graph.out(candidate, "PREDICTED_KEEP"):
        # Unresolved only. An edge that has an `actual` belongs to the agreement
        # record, not to today's slate.
        if edge.props.get("actual") or not edge.props.get("predicted"):
            continue
        job = graph.props(edge.dst)
        rows.append({
            "job_id": job.get("id", edge.dst), "title": job.get("title", ""),
            "company": job.get("company", ""), "url": job.get("url", ""),
            "location": job.get("location", ""), "remote": job.get("remote"),
            "salary_min": job.get("salary_min"), "salary_max": job.get("salary_max"),
            "company_size": job.get("company_size"), "seniority": job.get("seniority"),
            "predicted": edge.props.get("predicted"),
            "score": edge.props.get("score"),
            # Written by record_prediction once the reasons are persisted; absent
            # on every edge older than that, which the reader handles.
            "reasons": edge.props.get("reasons") or [],
            "day": edge.props.get("day"),
        })
    # `day = 0` means "whatever the latest slate is", so a caller that does not
    # track the clock still gets one day's worth rather than every open
    # prediction ever made.
    wanted = params.get("day") or 0
    if not wanted:
        days = [row["day"] for row in rows if row["day"] is not None]
        wanted = max(days) if days else 0
    rows = [row for row in rows if not wanted or row["day"] == wanted]
    rows.sort(key=lambda row: -(row["score"] or 0))
    return rows


@_register(
    "agreement_record",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[p:PREDICTED_KEEP]->(j:Job)
       WHERE p.actual IS NOT NULL AND p.predicted IS NOT NULL
       RETURN j.id AS job_id, p.predicted AS predicted, p.actual AS actual,
              p.day AS day, p.score AS score ORDER BY p.day DESC LIMIT $limit""",
    ("candidate_id", "limit"),
    "Predicted versus what the human did. Autonomy readiness is computed from this, not from model confidence.",
)
def _agreement_record(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    rows = []
    for edge in graph.out(candidate, "PREDICTED_KEEP"):
        # Both halves or it is not a record: an unresolved prediction and a
        # resolution with nothing to compare against are both "no data".
        if not (edge.props.get("actual") and edge.props.get("predicted")):
            continue
        job = graph.props(edge.dst)
        rows.append({"job_id": job.get("id", edge.dst), "title": job.get("title", ""),
                     "predicted": edge.props.get("predicted"),
                     "actual": edge.props.get("actual"),
                     "decided_by": edge.props.get("decided_by", "agent"),
                     "day": edge.props.get("day"), "score": edge.props.get("score")})
    rows.sort(key=lambda row: -(row["day"] or 0))
    return rows[: params.get("limit", 15)]


@_register(
    "play_index",
    """MATCH (p:Play) WHERE p.task_type = $task_type
       RETURN p.play_id AS play_id, p.rote_ref AS rote_ref, p.fingerprint AS fingerprint,
              p.success_count AS success_count, p.status AS status, p.last_used AS last_used
       ORDER BY p.success_count DESC""",
    ("task_type",),
    "What find_play looks at before any task.",
)
def _play_index(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for play in graph.by_label("Play"):
        if play.props.get("task_type") != params["task_type"]:
            continue
        rows.append({k: play.props.get(k) for k in
                     ("play_id", "rote_ref", "fingerprint", "task_type", "success_count",
                      "status", "last_used", "fields")})
    rows.sort(key=lambda row: -(row.get("success_count") or 0))
    return rows


@_register(
    "autonomy_state",
    """MATCH (c:Candidate {candidate_id:$candidate_id})-[:HAS_AUTONOMY]->(a:AutonomyState)
       RETURN a.domain AS domain, a.state AS state, a.readiness_value AS readiness_value,
              a.decisions_since_prompt AS decisions_since_prompt,
              a.recent_overrides AS recent_overrides""",
    ("candidate_id",),
    "Per-domain autonomy, earned from the agreement record.",
)
def _autonomy_state(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    candidate = _cand(params)
    return [
        {k: graph.props(e.dst).get(k) for k in
         ("domain", "state", "readiness_value", "decisions_since_prompt", "recent_overrides")}
        for e in graph.out(candidate, "HAS_AUTONOMY")
    ]


@_register(
    "job_gaps",
    """MATCH (j:Job {id:$job_id})-[:HAS_GAP]->(g:Gap)-[:ABOUT]->(s:Skill)
       RETURN s.name AS skill, g.kind AS kind""",
    ("job_id",),
    "Requirements with no supporting verified claim. Surfaced, never papered over.",
)
def _job_gaps(graph: Graph, params: dict[str, Any]) -> list[dict[str, Any]]:
    job = nid("job", params["job_id"])
    rows = []
    for edge in graph.out(job, "HAS_GAP"):
        gap = graph.props(edge.dst)
        skills = graph.neighbours(edge.dst, "ABOUT")
        rows.append({"skill": ", ".join(graph.props(s).get("name", s) for s in skills),
                     "kind": gap.get("kind", "required")})
    return rows


def _job_skills(graph: Graph, job_node: str) -> set[str]:
    skills: set[str] = set()
    for edge in graph.out(job_node, "REQUIRES"):
        skills.update(graph.neighbours(edge.dst, "ABOUT"))
    return skills


def _company_of(graph: Graph, job_node: str) -> str:
    for edge in graph.into(job_node, "POSTED"):
        return graph.props(edge.src).get("name", "")
    return ""


def query_catalogue() -> list[dict[str, str]]:
    return [{"name": q.name, "params": ", ".join(q.params) or "-", "summary": q.summary}
            for q in _REGISTRY.values()]


def cypher_for(name: str) -> str:
    """The Cypher a named query is defined as. Used by the Bolt backend and
    by anyone who wants to see that the queries are real."""
    return _REGISTRY[name].cypher


# --- backends --------------------------------------------------------------

class LocalBackend:
    """Evaluate the named queries in process, over a graph HydraDB persists.

    The cloud key has no query language (see the module docstring), so the
    multi-hop runs here and HydraDB holds the nodes and edges. Reads are served
    from memory within a run — write-then-read has to be correct, and HydraDB's
    ingest is explicitly asynchronous.
    """

    def __init__(self, graph: Graph | None = None, mirror: "HydraMirror | None" = None):
        self.graph = graph if graph is not None else Graph()
        self.mirror = mirror
        self._dirty: list[tuple[str, Any]] = []
        self._lock = threading.Lock()
        self._pusher: threading.Thread | None = None
        self._last_push: dict[str, Any] = {"pushed": 0, "state": "not yet pushed"}

    def run(self, name: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        query = _REGISTRY[name]
        return query.evaluate(self.graph, dict(params))

    def merge_node(self, node_id: str, label: str, **props: Any) -> None:
        with self._lock:
            self.graph.merge_node(node_id, label, **props)
            self._dirty.append(("node", node_id))

    def merge_edge(self, src: str, type_: str, dst: str, **props: Any) -> None:
        with self._lock:
            self.graph.merge_edge(src, type_, dst, **props)
            self._dirty.append(("edge", (src, type_, dst)))

    def flush(self, wait: bool = False) -> dict[str, Any]:
        """Persist locally, then mirror to HydraDB off the request path.

        The local write is the truth a run reads back, so it is synchronous. The
        HydraDB push is the durable copy and the team's view — and it was 5.6s
        of an 8s ranking pass, on the one call whose length decides whether the
        orchestrator's turn survives. It runs on a worker thread instead, and
        the *previous* flush's outcome is reported on the next one, so a mirror
        that has been failing all afternoon is still visible rather than
        swallowed. ``wait=True`` for callers that need the push to have landed.
        """
        with self._lock:
            dirty, self._dirty = self._dirty, []
        path = state_path(GRAPH_FILE)
        merged = self._merge_onto_disk(path)
        result: dict[str, Any] = {"local": str(path), "changed": len(dirty),
                                  "merged_from_disk": merged}
        if self.mirror and dirty:
            self._push_async(dirty)
            if wait:
                self.wait_for_mirror()
            result["hydradb"] = dict(self._last_push)
        return result

    def _merge_onto_disk(self, path: Any) -> int:
        """Write the graph by merging onto whatever is on disk, never over it.

        This used to be ``write_text(self.graph.to_dict())`` — the whole
        in-memory graph, stamped over the file. A process holds the snapshot it
        loaded at start-up, so with two writers the later flush silently erased
        everything the other had written. That is not theoretical: the MCP
        server held a graph from 13:05 while the human answered in Slack, and
        every Signal, REJECTED and resolved PREDICTED_KEEP went to hotdata and
        vanished from the graph, which is where rule induction and the autonomy
        record read from.

        Disk first, ours on top: MERGE semantics mean a writer that only touched
        a Claim cannot delete another's Signal, and re-asserting a node it
        already has is a no-op. Written to a temp file and renamed so a reader
        never sees a half-written graph.

        Returns the node count read back off disk, so a caller can see that a
        merge actually happened.
        """
        on_disk = 0
        combined = Graph()
        try:
            if path.exists():
                combined = Graph.from_dict(json.loads(path.read_text()))
                on_disk = len(combined.nodes)
        except (OSError, json.JSONDecodeError):
            combined = Graph()  # unreadable: ours is better than nothing
        for node in self.graph.nodes.values():
            combined.merge_node(node.id, node.label, **node.props)
        for edge in self.graph.edges.values():
            combined.merge_edge(edge.src, edge.type, edge.dst, **edge.props)
        self.graph = combined
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(combined.to_dict(), indent=1, default=str))
        tmp.replace(path)
        return on_disk

    def _push_async(self, dirty: list[tuple[str, Any]]) -> None:
        def work() -> None:
            try:
                outcome = self.mirror.push(self.graph, dirty)
            except Exception as exc:  # noqa: BLE001 - any client error, same answer
                outcome = {"pushed": 0, "error": f"{type(exc).__name__}: {exc}"}
            self._last_push = outcome

        # One worker at a time: two concurrent pushes of the same graph race on
        # the same ids, and the budget is per-request anyway.
        self.wait_for_mirror()
        self._pusher = threading.Thread(target=work, daemon=True,
                                        name="hydradb-mirror")
        self._pusher.start()

    def wait_for_mirror(self, timeout: float = 60.0) -> dict[str, Any]:
        """Block until the mirror thread is done. For scripts and tests."""
        pusher = getattr(self, "_pusher", None)
        if pusher is not None and pusher.is_alive():
            pusher.join(timeout)
        return dict(self._last_push)


class BoltBackend:
    """Run the Cypher for real against the HydraDB OSS engine (or any Bolt graph).

    Selected by setting ``GRAPH_BOLT_URL``. The queries are the same strings the
    local backend is written against, so nothing above this file changes.
    """

    def __init__(self, url: str, user: str, password: str):
        from neo4j import GraphDatabase  # imported lazily: optional dependency

        self.driver = GraphDatabase.driver(url, auth=(user, password))

    def run(self, name: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        query = _REGISTRY[name]
        with self.driver.session() as session:
            return [record.data() for record in session.run(query.cypher, dict(params))]

    def merge_node(self, node_id: str, label: str, **props: Any) -> None:
        safe_label = "".join(ch for ch in label if ch.isalnum() or ch == "_")
        with self.driver.session() as session:
            session.run(
                f"MERGE (n:{safe_label} {{node_id:$id}}) SET n += $props",
                {"id": node_id, "props": _flatten(props)},
            )

    def merge_edge(self, src: str, type_: str, dst: str, **props: Any) -> None:
        safe_type = "".join(ch for ch in type_ if ch.isalnum() or ch == "_")
        with self.driver.session() as session:
            session.run(
                f"MATCH (a {{node_id:$src}}), (b {{node_id:$dst}}) "
                f"MERGE (a)-[r:{safe_type}]->(b) SET r += $props",
                {"src": src, "dst": dst, "props": _flatten(props)},
            )

    def flush(self) -> dict[str, Any]:
        return {"bolt": "committed per statement"}


def _flatten(props: Mapping[str, Any]) -> dict[str, Any]:
    """Bolt takes scalars and flat lists; everything else goes in as JSON."""
    out: dict[str, Any] = {}
    for key, value in props.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(v, (str, int, float, bool)) for v in value
        ):
            out[key] = list(value)
        else:
            out[key] = json.dumps(value, default=str)
    return out


# HydraDB rejects a memory id over 100 bytes ("source_id length 105 exceeds
# maximum of 100 bytes"), and an edge id is `src|TYPE|dst` — three ATS job ids'
# worth of string. The natural id is kept in `metadata` either way, and `pull`
# reads the id from there, so shortening the key costs nothing on the way back.
HYDRA_ID_MAX = 100


# HydraDB caps metadata at 16 KiB per item. A job's props carry a description
# that can be longer than that on its own, and one oversized item rejects the
# whole batch — so the props are trimmed here rather than discovered at ingest.
HYDRA_METADATA_MAX = 15_000

# `context.list` rejects a page over 100 rather than clamping it.
HYDRA_PAGE_MAX = 100

# Ingest is billed per request against a budget of 5000, and one over-budget
# request rejects every item in it. 50 is the starting guess, halved on
# rejection; the pause is for the per-second half of the same budget.
HYDRA_BATCH_ITEMS = 50
HYDRA_BATCH_PAUSE = 0.2

# A memory item's metadata is write-once on this tenant, and that is not
# something the API says anywhere. Measured on 2026-09-11, three results:
#
# * a *new* id ingests with its metadata and pulls back complete, within ~40s;
# * `upsert="true"` on an *existing* id returns success and leaves the stored
#   metadata exactly as it was — the new props are accepted and discarded;
# * `context.delete` then re-ingesting the same id leaves the item gone. The id
#   is tombstoned, so delete-and-rewrite is not a repair, it is a deletion.
#
# So the items written before the props fix cannot be corrected in place. The
# id carries a schema version instead: bump it and every item re-lands as a new
# item with the current shape, while the stale ones keep merging in harmlessly
# (they carry no props, and `merge_node` only overwrites with values it has).
#
# Bump this whenever the *shape* of what goes in `metadata` changes — or when
# `_item_id` changes how it derives one, which is the same problem wearing a
# different hat: an id nobody can reproduce is an item nobody can upsert. It is
# cheap: one full `--mirror` run.
#
# v3: `_item_id`'s digest moved from SHA-1 to SHA-256, so every over-cap id
# changed. Without the bump those items would have orphaned quietly while the
# short ones kept upserting, which is the confusing half-migration.
HYDRA_ITEM_SCHEMA = "v3"


def _props_for_metadata(props: Mapping[str, Any]) -> str:
    """The props as they have to travel to survive the round trip.

    JSON-encoded, because metadata values are scalars, and truncated to fit the
    item cap: a key dropped for length is better than a batch that never lands.
    Longest values go first, since one description is usually the whole problem.
    """
    trimmed = dict(props)
    while trimmed and len(json.dumps(trimmed, default=str)) > HYDRA_METADATA_MAX:
        longest = max(trimmed, key=lambda k: len(str(trimmed[k])))
        trimmed.pop(longest)
    # Encoded, not handed over as a dict: HydraDB rejects a metadata value more
    # than one level deep ("unsupported metadata value at metadata.props.reasons:
    # maximum nesting depth is 1"), and `reasons` on a PREDICTED_KEEP edge is a
    # list — so the natural shape 400s the *whole batch*, which is the same
    # all-or-nothing failure the id cap was about. `pull` decodes it back.
    return json.dumps(trimmed, default=str)


def _item_id(natural: str) -> str:
    """The id an item is stored under: schema version, then the natural id.

    Truncated with a digest when the result would pass the 100-byte cap — still
    deterministic, so a re-push lands on the same item rather than duplicating.
    """
    tagged = f"{HYDRA_ITEM_SCHEMA}:{natural}"
    if len(tagged.encode()) <= HYDRA_ID_MAX:
        return tagged
    digest = hashlib.sha256(tagged.encode()).hexdigest()[:16]
    head = tagged.encode()[: HYDRA_ID_MAX - len(digest) - 1].decode(errors="ignore")
    return f"{head}-{digest}"


class HydraMirror:
    """Write the graph through to HydraDB cloud as memory items.

    One memory item per node and per edge, keyed by our own id so a re-push
    upserts rather than duplicating. ``text`` is the sentence form ("Candidate
    cand-001 APPLIED_TO job greenhouse:stripe:123") so HydraDB's own indexing
    has something to work with, and ``metadata`` carries the structured props.

    Ingest is asynchronous and returns 202: this is the durable copy and the
    dashboard view, never the read path inside a run.
    """

    def __init__(self, config: Settings | None = None, collection: str | None = None):
        from hydra_db import HydraDB  # optional at import time

        self.config = config or settings()
        if not self.config.hydradb_api_key:
            raise RuntimeError("HYDRADB_APIKEY is not set (see .env.example)")
        self.client = HydraDB(token=self.config.hydradb_api_key)
        self.database = self.config.hydradb_database
        self.collection = collection or self.config.hydradb_collection

    def push(self, graph: Graph, dirty: Iterable[tuple[str, Any]]) -> dict[str, Any]:
        items = []
        for kind, key in dirty:
            if kind == "node":
                node = graph.node(key)
                if node is None:
                    continue
                items.append({
                    "id": _item_id(node.id),
                    "text": f"{node.label} {node.id} :: " + json.dumps(node.props, default=str)[:1500],
                    "metadata": {"kind": "node", "label": node.label, "node_id": node.id,
                                 "props": _props_for_metadata(node.props)},
                })
            else:
                edge = graph.edges.get(key)
                if edge is None:
                    continue
                items.append({
                    "id": _item_id(f"{edge.src}|{edge.type}|{edge.dst}"),
                    "text": f"{edge.src} {edge.type} {edge.dst} :: "
                            + json.dumps(edge.props, default=str)[:1000],
                    "metadata": {"kind": "edge", "type": edge.type,
                                 "src": edge.src, "dst": edge.dst,
                                 "props": _props_for_metadata(edge.props)},
                })
        if not items:
            return {"pushed": 0}
        # Deduplicate: a run touches the same node many times.
        unique = {item["id"]: item for item in items}
        payload = list(unique.values())
        pushed = self._ingest(payload)
        return {"pushed": pushed, "batches": -(-len(payload) // HYDRA_BATCH_ITEMS),
                "database": self.database, "collection": self.collection}

    def _ingest(self, payload: list[dict[str, Any]], size: int = HYDRA_BATCH_ITEMS) -> int:
        """Ingest in batches, halving on a too-large rejection.

        HydraDB bills a request against a per-request budget it does not publish
        per item ("combined cost 51018 exceeds the per-request budget of 5000"),
        so the batch size cannot be computed up front — a batch of long job
        descriptions costs many times a batch of requirement nodes. Halving on
        rejection finds the size this particular payload allows, and the whole
        push is otherwise a single item over the line away from landing nothing.
        """
        pushed = 0
        for start in range(0, len(payload), size):
            batch = payload[start:start + size]
            try:
                self.client.context.ingest(
                    database=self.database, collection=self.collection, type="memory",
                    memories=json.dumps(batch, default=str), upsert="true",
                )
                pushed += len(batch)
            except Exception as exc:
                if "too large" not in str(exc) or len(batch) == 1:
                    raise
                pushed += self._ingest(batch, max(1, size // 2))
            time.sleep(HYDRA_BATCH_PAUSE)
        return pushed

    def pull(self, page_size: int = HYDRA_PAGE_MAX, max_pages: int = 200) -> Graph:
        """Rebuild the graph from HydraDB. The disaster-recovery path.

        ``page_size`` is clamped: HydraDB rejects anything over 100 outright,
        and the default used to be 200 — so the recovery path failed on its
        first call, which is the worst possible time to find out.
        """
        graph = Graph()
        page_size = max(1, min(page_size, HYDRA_PAGE_MAX))
        page = 1
        while page <= max_pages:
            response = self.client.context.list(
                database=self.database, collection=self.collection, type="memory",
                page=page, page_size=page_size,
            )
            items = response.data.user_memories or []
            for item in items:
                meta = item.metadata or {}
                # `text` is what HydraDB indexes, not what it hands back:
                # `context.list` returns `description` empty on every item, so
                # parsing the props out of it restored the topology with every
                # node blank. `metadata` does round-trip, so that is where the
                # props are read from — and written to, in `push`.
                props = meta.get("props")
                if isinstance(props, str):
                    try:
                        props = json.loads(props)
                    except json.JSONDecodeError:
                        props = {}
                if not isinstance(props, dict):
                    props = {}
                if meta.get("kind") == "node":
                    graph.merge_node(meta["node_id"], meta.get("label", ""), **props)
                elif meta.get("kind") == "edge":
                    graph.merge_edge(meta["src"], meta["type"], meta["dst"], **props)
            if not (response.data.pagination and response.data.pagination.has_next):
                break
            page += 1
        return graph


# --- the front door --------------------------------------------------------

class GraphStore:
    """What the rest of the repo imports. Backend choice is an env var."""

    def __init__(self, config: Settings | None = None, backend: Any | None = None,
                 mirror: bool = True):
        self.config = config or settings()
        self.candidate_id = self.config.candidate_id
        if backend is not None:
            self.backend = backend
        elif self.config.graph_bolt_url:
            self.backend = BoltBackend(self.config.graph_bolt_url,
                                       self.config.graph_bolt_user,
                                       self.config.graph_bolt_password)
        else:
            graph = self._load_local()
            hydra: HydraMirror | None = None
            if mirror and self.config.hydradb_api_key:
                try:
                    hydra = HydraMirror(self.config)
                except Exception:  # a mirror failure must never block a run
                    hydra = None
            self.backend = LocalBackend(graph, hydra)

    @staticmethod
    def _load_local() -> Graph:
        path = state_path(GRAPH_FILE)
        if path.exists():
            return Graph.from_dict(json.loads(path.read_text()))
        return Graph()

    # reads
    def run(self, name: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        if name not in _REGISTRY:
            raise KeyError(f"unknown graph query {name!r}; have {sorted(_REGISTRY)}")
        merged = {"candidate_id": self.candidate_id, **(params or {})}
        query = _REGISTRY[name]
        merged = {k: v for k, v in merged.items() if k in query.params}
        for param in query.params:
            if param not in merged:
                merged[param] = _DEFAULT_PARAMS.get(param)
                if merged[param] is None:
                    raise KeyError(f"graph query {name!r} needs {param}")
        return self.backend.run(name, merged)

    # writes
    def merge_node(self, node_id: str, label: str, **props: Any) -> str:
        self.backend.merge_node(node_id, label, **props)
        return node_id

    def merge_edge(self, src: str, type_: str, dst: str, **props: Any) -> None:
        self.backend.merge_edge(src, type_, dst, **props)

    def flush(self, wait: bool = False) -> dict[str, Any]:
        """``wait=True`` blocks on the HydraDB mirror; a run never needs to."""
        backend = self.backend
        if wait and isinstance(backend, LocalBackend):
            return backend.flush(wait=True)
        return backend.flush()

    @property
    def graph(self) -> Graph:
        """The in-memory graph. Present on the local backend only."""
        backend = self.backend
        if isinstance(backend, LocalBackend):
            return backend.graph
        raise AttributeError("the Bolt backend has no in-process graph")

    def candidate_node(self) -> str:
        return nid("candidate", self.candidate_id)


_DEFAULT_PARAMS = {"limit": 10, "job_ids": [], "question_ids": [], "cutoff_day": 0,
                   "task_type": "apply-pack", "day": 0}
