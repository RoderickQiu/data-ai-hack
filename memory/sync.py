"""The Cognee → HydraDB bridge. Roughly forty lines of MERGE, deliberately.

``GET /api/v1/datasets/{id}/graph?full=true`` hands back the whole graph as
``{"nodes": [...], "edges": [...]}`` with our typed labels on it, so the sync is
a MERGE loop and nothing else: no export call, no intermediate file, no format
of our own to keep in step with two services.

(The earlier plan was a spike to point Cognee's ``neo4j`` graph provider at
HydraDB's Bolt port and skip the bridge entirely. That is off the table on a
managed tenant — storage is Cognee's and no graph provider is configurable —
and this endpoint is a better version of the fallback anyway.)

**What stops the second graph being a mirror.** The sync copies across what was
*said*: Candidate, Claim, Skill, Job, Company, Requirement. What the agent
*did* — ``APPLIED_TO`` with a day on it, ``GOT_REPLY``, ``REJECTED`` with a
reason, ``PREDICTED_KEEP`` against the human's actual answer — is written
directly by :mod:`memory.writers` and exists nowhere in Cognee. Every query in
:mod:`memory.graph` traverses both halves, which is why neither layer can answer
them alone.

Nodes that are Cognee's own plumbing (document chunks, text summaries, node
sets, the schema's own root object) are dropped on the way through: they are how
the extraction works, not what it found, and copying them would fill the graph
with nodes no query names.

What the export actually looks like, checked against the tenant on 2026-09-11
after a cognify with our ``graphModel``:

* ``type`` carries our entity name — ``Candidate``, ``Skill`` — exactly as the
  schema declares it. The typed model works;
* **``label`` carries the entity's name** ("Alex Rivera", "Terraform"), not a
  label in the Cypher sense. Anonymous container nodes use ``Type_<uuid>``
  instead, which is how you tell the two apart;
* **edge labels are the schema's own property names** — ``skills``, ``claims``,
  ``candidates`` — not relationship verbs. :data:`EDGE_TYPES` maps them, keyed
  by the *source* type where one name means two things (a Candidate's ``skills``
  is ``HAS_SKILL``; a Claim's is ``DEMONSTRATES``);
* a root object of the schema's own title appears as a node and is dropped.
"""

from __future__ import annotations

from typing import Any, Mapping

from memory.claims import claim_id_for
from memory.graph import GraphStore, canonical_skill, nid
from memory.remember import Remember

# Cognee's type name -> our label. Anything not in here is skipped.
LABELS = {
    "candidate": "Candidate", "claim": "Claim", "skill": "Skill", "job": "Job",
    "company": "Company", "requirement": "Requirement",
    "standardanswer": "StandardAnswer", "standard_answer": "StandardAnswer",
    "question": "Question",
}

SKIPPED_TYPES = {"documentchunk", "textsummary", "textdocument", "entitytype",
                 "nodeset", "document", "chunk", "entity",
                 # the schema's own root object, and the tenant's skill-run
                 # bookkeeping, which memory.remember already owns
                 "candidategraph", "skillrun", "candidateskill"}

# (source type, edge label) -> our edge type, then a fallback by label alone.
# An unrecognized relationship is kept under its own uppercased name rather than
# dropped: an unexpected edge is information, an unexpected *node* is noise.
EDGE_BY_SOURCE = {
    ("candidate", "skills"): "HAS_SKILL",
    ("candidate", "claims"): "HAS_CLAIM",
    ("candidate", "standard_answers"): "HAS_ANSWER",
    ("claim", "skills"): "DEMONSTRATES",
    ("job", "requirements"): "REQUIRES",
    ("company", "jobs"): "POSTED",
    ("requirement", "skill"): "ABOUT",
}

EDGE_TYPES = {
    "has_claim": "HAS_CLAIM", "demonstrates": "DEMONSTRATES", "has_skill": "HAS_SKILL",
    "requires": "REQUIRES", "about": "ABOUT", "posted": "POSTED",
    "supported_by": "SUPPORTED_BY", "works_at": "WORKED_AT", "mentions": "MENTIONS",
    "claims": "HAS_CLAIM", "skills": "HAS_SKILL", "requirements": "REQUIRES",
    "jobs": "POSTED", "standard_answers": "HAS_ANSWER",
}


def _node_type(node: Mapping[str, Any]) -> str:
    for key in ("type", "label", "node_type", "__type__"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    properties = node.get("properties") or {}
    return str(properties.get("type", "")).lower()


def _node_name(node: Mapping[str, Any]) -> str:
    """The entity's name. ``label`` holds it, unless the node is anonymous.

    An anonymous node's label is ``<Type>_<uuid>``, which is a name for nothing
    and must not become a Skill called "Skill_3fffb174".
    """
    properties = node.get("properties") or {}
    for source in (properties, node):
        for key in ("name", "text", "title", "canonical_name", "description"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    label = str(node.get("label") or "")
    node_id = str(node.get("id") or "")
    if label and not (node_id and label.endswith(node_id)):
        return label
    return node_id


def _our_id(label: str, name: str, cognee_id: str) -> str:
    """Give the node the id our own writers would have given it.

    This is what makes the two halves meet: a ``Skill`` Cognee extracted from
    the resume and a ``Skill`` the requirement loader created land on the same
    node, so ``claim_coverage`` can join them. Same for a ``Claim`` — Cognee
    extracts the resume bullet the loader already stored, and keying on the
    tenant's uuid instead of the text would give the graph two copies of every
    bullet, one verified and one not.
    """
    if label == "Skill":
        return nid("skill", canonical_skill(name))
    if label == "Company":
        return nid("company", name.lower())
    if label == "Claim":
        return nid("claim", claim_id_for(name))
    return nid(label.lower(), cognee_id)


def _node_id_for(store: GraphStore, label: str, name: str, cognee_id: str) -> str:
    # One candidate in v1, so Cognee's extracted person is *the* candidate
    # rather than a second one. Keyed on the store's id, which is what every
    # query takes as a parameter.
    if label == "Candidate":
        return store.candidate_node()
    return _our_id(label, name, cognee_id)


def _is_new(store: GraphStore, node_id: str) -> bool:
    try:
        return store.graph.node(node_id) is None
    except AttributeError:      # Bolt backend: assume it exists and touch nothing
        return False


def sync_graph(store: GraphStore, payload: Mapping[str, Any]) -> dict[str, Any]:
    """MERGE one ``{nodes, edges}`` export into the durable graph."""
    mapping: dict[str, str] = {}
    source_types: dict[str, str] = {}
    kept, skipped = 0, 0

    for node in payload.get("nodes") or []:
        cognee_type = _node_type(node)
        if cognee_type in SKIPPED_TYPES:
            skipped += 1
            continue
        label = LABELS.get(cognee_type)
        if label is None:
            skipped += 1
            continue
        cognee_id = str(node.get("id") or node.get("node_id") or "")
        name = _node_name(node)
        node_id = _node_id_for(store, label, name, cognee_id)
        properties = dict(node.get("properties") or {})
        properties.pop("type", None)
        if label == "Skill":
            properties.setdefault("name", name)
            properties["canonical_name"] = canonical_skill(name)
        elif label == "Claim":
            properties.setdefault("text", name)
            properties.setdefault("claim_id", claim_id_for(name))
            # Only *new* claims enter unverified. An extraction of a resume
            # bullet the candidate already stood behind must not quietly
            # downgrade it — "verified" is a human's word, and the pack
            # validator is the one thing standing between the graph and a
            # fabricated application.
            if _is_new(store, node_id):
                properties["status"] = "unverified"
        elif label == "Company":
            properties.setdefault("name", name)
        elif label == "Candidate":
            properties.setdefault("name", name)
            properties["candidate_id"] = store.candidate_id
        elif label in ("Job", "Requirement", "StandardAnswer", "Question"):
            properties.setdefault("name", name)
        properties["cognee_id"] = cognee_id
        store.merge_node(node_id, label, **properties)
        mapping[cognee_id] = node_id
        source_types[cognee_id] = cognee_type
        kept += 1

    edges = 0
    for edge in payload.get("edges") or []:
        source = str(edge.get("source") or edge.get("source_node_id") or edge.get("from") or "")
        target = str(edge.get("target") or edge.get("target_node_id") or edge.get("to") or "")
        if source not in mapping or target not in mapping:
            continue  # an edge onto skipped plumbing
        raw_type = str(edge.get("relationship_name") or edge.get("label")
                       or edge.get("type") or "related_to").lower()
        source_type = source_types.get(source, "")
        edge_type = (EDGE_BY_SOURCE.get((source_type, raw_type))
                     or EDGE_TYPES.get(raw_type)
                     or raw_type.upper().replace(" ", "_"))
        store.merge_edge(mapping[source], edge_type, mapping[target], origin="cognee")
        edges += 1

    store.flush()
    return {"nodes_merged": kept, "nodes_skipped": skipped, "edges_merged": edges}


def sync_from_cognee(store: GraphStore, remember: Remember | None = None) -> dict[str, Any]:
    """Pull the live export and merge it. Run after every ingest, not per run."""
    owned = remember is None
    remember = remember or Remember()
    try:
        payload = remember.graph()
    finally:
        if owned:
            remember.close()
    return sync_graph(store, payload)


def link_candidate_skills(store: GraphStore) -> int:
    """Give the candidate the skills their verified claims demonstrate.

    Cognee extracts ``(Claim)-[:DEMONSTRATES]->(Skill)`` from the resume but has
    no reason to assert ``(Candidate)-[:HAS_SKILL]->(Skill)``; that is an
    inference over *verified* claims, so it belongs on this side of the bridge
    where "verified" means something.
    """
    candidate = store.candidate_node()
    linked = 0
    for edge in store.graph.out(candidate, "HAS_CLAIM"):
        if store.graph.props(edge.dst).get("status") != "verified":
            continue
        for skill in store.graph.neighbours(edge.dst, "DEMONSTRATES"):
            if not store.graph.has_edge(candidate, "HAS_SKILL", skill):
                store.merge_edge(candidate, "HAS_SKILL", skill, via="verified_claim")
                linked += 1
    store.flush()
    return linked
