"""Verified claims, and the validator that makes "it never lies" structural.

The rule (DESIGN §6.1): **tailoring is selection and ordering of verified
claims; the agent never writes a new one.** The LLM step in ``apply-pack``
returns ``claim_id``s plus connective prose, and :func:`validate_citations`
rejects any factual sentence that is not tagged with a citation to a verified
claim belonging to this candidate.

That is the cheapest possible anti-hallucination story: it is a deterministic
check on the output, not an instruction in the prompt, and it holds the token
line down as a side effect — the model emits ids and a few sentences rather
than a whole document, so ``apply-pack``'s output cost does not grow as the
claim graph does.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from agent.schema import now_iso
from memory.graph import GraphStore, canonical_skill, nid

CITATION_RE = re.compile(r"\[claim:([A-Za-z0-9_\-]+)\]")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# A sentence asserting something about the candidate needs a citation.
# Connective prose ("I'd be glad to bring that to the team") does not.
_FACT_MARKERS = re.compile(
    r"\b(led|built|shipped|launched|designed|scaled|reduced|increased|grew|owned|"
    r"managed|migrated|delivered|architected|founded|ran|drove|cut|saved|improved|"
    r"years|team of|worked on|responsible for)\b",
    re.I,
)
_NUMBER_RE = re.compile(r"\d")


@dataclass
class Claim:
    claim_id: str
    text: str
    kind: str = "bullet"           # bullet | summary | fact
    status: str = "unverified"     # verified | unverified | retired
    source_doc: str = ""
    excerpt_ref: str = ""
    skills: list[str] = field(default_factory=list)


def claim_id_for(text: str, source_doc: str = "") -> str:
    """Stable id, so re-ingesting the same resume does not fork the claim.

    Keyed on the **text alone**, deliberately. The resume loader stores a bullet
    with ``source_doc="resume"`` and Cognee's extraction returns the same bullet
    with no source attached; folding the document into the hash would give the
    graph two copies of every claim, one verified and one not. Two identical
    sentences are the same claim whichever document they arrived in.

    ``source_doc`` is kept in the signature because every call site reads better
    with it and it is stored on the node either way.
    """
    return "cl-" + hashlib.sha256(text.strip().encode()).hexdigest()[:12]


def claims_from_resume(text: str, source_doc: str = "resume") -> list[Claim]:
    """Bullets become claims. The candidate wrote them, so they enter verified.

    Everything the extraction pipeline infers from looser material — preference
    transcripts, old cover letters — goes through :func:`claims_from_extraction`
    instead and enters ``unverified`` (DESIGN §6.1).
    """
    claims = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            continue          # a heading is not a claim about anything
        line = line.lstrip("•-*–—").strip()
        if len(line) < 20 or line.endswith(":"):
            continue
        claims.append(Claim(
            claim_id=claim_id_for(line, source_doc), text=line,
            kind="bullet", status="verified", source_doc=source_doc,
        ))
    return claims


def claims_from_extraction(items: Iterable[Mapping[str, Any]],
                           source_doc: str) -> list[Claim]:
    """Inferred claims. Unverified until the human confirms them in Slack."""
    claims = []
    for item in items:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        claims.append(Claim(
            claim_id=claim_id_for(text, source_doc), text=text,
            kind=item.get("kind", "fact"), status="unverified", source_doc=source_doc,
            excerpt_ref=item.get("excerpt_ref", ""),
            skills=[s for s in item.get("skills", []) if s],
        ))
    return claims


def store_claims(store: GraphStore, claims: Sequence[Claim]) -> dict[str, int]:
    candidate = store.candidate_node()
    store.merge_node(candidate, "Candidate", candidate_id=store.candidate_id)
    counts = {"verified": 0, "unverified": 0}
    for claim in claims:
        claim_node = nid("claim", claim.claim_id)
        store.merge_node(claim_node, "Claim", claim_id=claim.claim_id, text=claim.text,
                         kind=claim.kind, status=claim.status, source_doc=claim.source_doc)
        store.merge_edge(candidate, "HAS_CLAIM", claim_node)
        if claim.source_doc:
            evidence_node = nid("evidence", claim.claim_id)
            store.merge_node(evidence_node, "Evidence", source_doc=claim.source_doc,
                             excerpt_ref=claim.excerpt_ref or claim.text[:120])
            store.merge_edge(claim_node, "SUPPORTED_BY", evidence_node)
        for skill_name in claim.skills:
            skill = canonical_skill(skill_name)
            skill_node = nid("skill", skill)
            store.merge_node(skill_node, "Skill", name=skill_name, canonical_name=skill)
            store.merge_edge(claim_node, "DEMONSTRATES", skill_node)
            store.merge_edge(candidate, "HAS_SKILL", skill_node)
        counts[claim.status] = counts.get(claim.status, 0) + 1
    return counts


def pending_verification(store: GraphStore) -> list[dict[str, Any]]:
    """What goes in the one-time Slack digest with Confirm / Fix / Discard."""
    candidate = store.candidate_node()
    rows = []
    for edge in store.graph.out(candidate, "HAS_CLAIM"):
        props = store.graph.props(edge.dst)
        if props.get("status") != "unverified":
            continue
        rows.append({"claim_id": props.get("claim_id"), "text": props.get("text"),
                     "source_doc": props.get("source_doc")})
    return rows


def verify_claim(store: GraphStore, claim_id: str, status: str = "verified",
                 text: str | None = None) -> dict[str, Any]:
    """Confirm, fix or discard one claim. Only a human ever calls this.

    Marking a claim verified is on the never-autonomous list whatever the
    agreement record says: the agent can earn autonomy over judgment, never over
    facts about a person (DESIGN §8).
    """
    if status not in ("verified", "unverified", "retired"):
        raise ValueError(f"unknown claim status {status!r}")
    node = nid("claim", claim_id)
    props: dict[str, Any] = {"status": status, "verified_at": now_iso()}
    if text:
        props["text"] = text
    store.merge_node(node, "Claim", claim_id=claim_id, **props)
    return {"claim_id": claim_id, "status": status}


# --- the validator ---------------------------------------------------------

@dataclass
class ValidationResult:
    ok: bool
    cited: list[str] = field(default_factory=list)
    unknown_claims: list[str] = field(default_factory=list)
    unverified_claims: list[str] = field(default_factory=list)
    foreign_claims: list[str] = field(default_factory=list)
    uncited_facts: list[str] = field(default_factory=list)

    def reason(self) -> str:
        problems = []
        if self.unknown_claims:
            problems.append(f"{len(self.unknown_claims)} citation(s) to a claim that does not exist")
        if self.unverified_claims:
            problems.append(f"{len(self.unverified_claims)} citation(s) to an unverified claim")
        if self.foreign_claims:
            problems.append(f"{len(self.foreign_claims)} citation(s) to another candidate's claim")
        if self.uncited_facts:
            problems.append(f"{len(self.uncited_facts)} factual sentence(s) with no citation")
        return "; ".join(problems) or "ok"


def validate_citations(store: GraphStore, text: str) -> ValidationResult:
    """Fail the pack unless every factual sentence cites a verified claim of ours.

    Three checks, all deterministic:

    1. every ``[claim:ID]`` resolves to a Claim node,
    2. that claim is ``verified`` and is linked to *this* candidate,
    3. every sentence that asserts something (a number, or a verb like "led",
       "shipped", "reduced") carries at least one citation.

    Connective prose is left alone on purpose. Requiring a citation on "I'd
    welcome the chance to talk" produces packs nobody would send.
    """
    result = ValidationResult(ok=True)
    candidate = store.candidate_node()
    try:
        mine = {store.graph.props(e.dst).get("claim_id") for e in
                store.graph.out(candidate, "HAS_CLAIM")}
        lookup = {props.get("claim_id"): props for props in
                  (store.graph.props(node.id) for node in store.graph.by_label("Claim"))}
    except AttributeError:  # Bolt backend
        rows = store.run("verified_claims")
        mine = {row["claim_id"] for row in rows}
        lookup = {row["claim_id"]: {"status": "verified"} for row in rows}

    for claim_id in CITATION_RE.findall(text):
        result.cited.append(claim_id)
        props = lookup.get(claim_id)
        if props is None:
            result.unknown_claims.append(claim_id)
        elif props.get("status") != "verified":
            result.unverified_claims.append(claim_id)
        elif claim_id not in mine:
            result.foreign_claims.append(claim_id)

    for sentence in _SENTENCE_RE.split(text):
        sentence = sentence.strip()
        if not sentence or CITATION_RE.search(sentence):
            continue
        stripped = CITATION_RE.sub("", sentence)
        if _FACT_MARKERS.search(stripped) or _NUMBER_RE.search(stripped):
            result.uncited_facts.append(sentence[:160])

    result.ok = not (result.unknown_claims or result.unverified_claims
                     or result.foreign_claims or result.uncited_facts)
    return result


def render_pack_text(text: str, claims: Mapping[str, str]) -> dict[str, Any]:
    """Split the tagged draft into the copy a human sends and its provenance.

    The tagged form is what the validator sees and what we store. The human-
    facing copy has the markers stripped; the provenance list underneath names
    every claim the copy leaned on, in order of first use, so "where did that
    sentence come from" is answerable without re-reading the graph.
    """
    order: list[str] = []
    for claim_id in CITATION_RE.findall(text):
        if claim_id not in order:
            order.append(claim_id)
    clean = _WS_AFTER_TAG.sub(" ", CITATION_RE.sub("", text)).strip()
    return {
        "copy": "\n".join(line.strip() for line in clean.splitlines()),
        "provenance": [{"claim_id": cid, "text": claims.get(cid, "(claim not found)")}
                       for cid in order],
    }


_WS_AFTER_TAG = re.compile(r"[ \t]{2,}")
