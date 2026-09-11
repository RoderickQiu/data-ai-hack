"""The resolution ladder, which is where the whole of P1 lives.

Preparing a pack needs facts the resume does not carry: work authorization,
notice period, salary band, relocation. On run 1 the agent has none of them and
asks. Each answer is written once and **never asked again** — which is why
``questions_asked`` per run falls to zero, and why it is the most legible line
on the chart: nobody needs the metric explained.

Per question, in order (DESIGN §6.2):

1. a ``StandardAnswer`` exists — filled, zero tokens, zero human touches;
2. a prior *approved* answer to a canonically similar question — reused as a
   draft, flagged as reused;
3. derivable from verified claims — generated, marked generated, one approval;
4. an unknown fact about the human — asked. Always. This is the one thing the
   agent never guesses and never earns autonomy over.

Rung 3 is deliberately a deterministic derivation and not an LLM call: it reads
the claim graph and quotes it. A generated answer that invents a number is the
same failure as a fabricated resume bullet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agent.schema import now_iso
from memory.graph import GraphStore, canonical_skill, nid
from memory.writers import canonical_question_id, record_answer, record_question

# The standard keys, and the phrasings a board actually uses for each. Matching
# is by keyword rather than by embedding: it has to be explainable on stage, and
# a false positive here answers a question wrongly on the candidate's behalf.
STANDARD_KEYS: dict[str, tuple[str, ...]] = {
    "work_authorization": ("work authorization", "authorized to work", "right to work",
                           "visa", "sponsorship", "sponsor", "eligible to work"),
    "notice_period": ("notice period", "when can you start", "start date", "availability"),
    "salary_expectation": ("salary expectation", "compensation expectation", "desired salary",
                           "expected salary", "salary range", "comp expectation"),
    "relocation": ("relocate", "relocation", "willing to move"),
    "location_now": ("where are you based", "current location", "where do you live"),
    "remote_preference": ("remote", "hybrid", "onsite", "in office", "work from"),
    "pronouns": ("pronouns",),
    "linkedin": ("linkedin",),
    "github": ("github", "portfolio"),
}

_YEARS_RE = re.compile(r"(?:how many years|years of experience|years'? experience)", re.I)
_HAVE_EXPERIENCE_RE = re.compile(r"(?:do you have|experience with|familiar with|worked with)", re.I)


@dataclass
class Resolution:
    question: str
    source: str            # standard_answer | reused_answer | generated | ask
    text: str = ""
    needs_approval: bool = False
    key: str | None = None
    from_claims: tuple[str, ...] = ()

    @property
    def asked(self) -> bool:
        return self.source == "ask"

    def summary(self) -> dict[str, Any]:
        return {"question": self.question, "source": self.source, "text": self.text,
                "needs_approval": self.needs_approval, "key": self.key,
                "from_claims": list(self.from_claims)}


def standard_key_for(question: str) -> str | None:
    text = (question or "").lower()
    for key, phrases in STANDARD_KEYS.items():
        if any(phrase in text for phrase in phrases):
            return key
    return None


def set_standard_answer(store: GraphStore, key: str, text: str) -> dict[str, Any]:
    """Write one standard answer. Only a human's own words ever get here."""
    candidate = store.candidate_node()
    node = nid("standard_answer", key)
    store.merge_node(candidate, "Candidate", candidate_id=store.candidate_id)
    store.merge_node(node, "StandardAnswer", key=key, text=text, confirmed_at=now_iso())
    store.merge_edge(candidate, "HAS_ANSWER", node)
    return {"key": key, "text": text}


def known_standard_answers(store: GraphStore) -> dict[str, str]:
    return {row["key"]: row["text"] for row in store.run("standard_answers")
            if row.get("key")}


def resolve(store: GraphStore, question: str, scope: str = "role") -> Resolution:
    """Walk the ladder for one question. The only entry point."""
    # 1. a standard answer we already hold
    key = standard_key_for(question)
    if key:
        answers = known_standard_answers(store)
        if key in answers:
            return Resolution(question, "standard_answer", answers[key], key=key)

    # 2. a prior approved answer to a canonically similar question
    similar = similar_question_ids(store, question)
    if similar:
        prior = store.run("similar_answers", {"question_ids": similar})
        if prior:
            return Resolution(question, "reused_answer", prior[0]["text"],
                              needs_approval=False, key=key)

    # 3. derivable from verified claims
    derived = derive_from_claims(store, question)
    if derived:
        return Resolution(question, "generated", derived["text"], needs_approval=True,
                          key=key, from_claims=tuple(derived["claim_ids"]))

    # 4. an unknown fact about the human. Asked, always.
    return Resolution(question, "ask", key=key)


def similar_question_ids(store: GraphStore, question: str) -> list[str]:
    """Canonical id plus any stored question sharing most of its content words.

    Cheap on purpose. The expensive version is an embedding lookup, and a wrong
    match here silently reuses an answer to a *different* question, which is
    worse than asking one extra time.
    """
    ids = [canonical_question_id(question)]
    words = _content_words(question)
    if not words:
        return ids
    try:
        stored = store.graph.by_label("Question")
    except AttributeError:
        return ids
    for node in stored:
        other = _content_words(node.props.get("canonical_text", ""))
        if not other:
            continue
        overlap = len(words & other) / max(len(words | other), 1)
        if overlap >= 0.7:
            ids.append(node.props.get("question_id"))
    return [qid for qid in dict.fromkeys(ids) if qid]


def _content_words(text: str) -> set[str]:
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in (text or "").lower())
    return {word for word in cleaned.split() if len(word) > 2}


def derive_from_claims(store: GraphStore, question: str) -> dict[str, Any] | None:
    """Rung 3: answer a skill question by quoting the claims that demonstrate it.

    Returns ``None`` unless a verified claim actually supports it — an empty
    hand here means rung 4, which is the correct outcome.
    """
    if not (_YEARS_RE.search(question) or _HAVE_EXPERIENCE_RE.search(question)):
        return None
    claims = store.run("verified_claims")
    if not claims:
        return None
    wanted = _content_words(question)
    scored = []
    for claim in claims:
        skills = {canonical_skill(s) for s in claim.get("skills", [])}
        hit = wanted & (_content_words(claim.get("text", "")) | {w for s in skills for w in s.split()})
        if hit:
            scored.append((len(hit), claim))
    if not scored:
        return None
    scored.sort(key=lambda pair: -pair[0])
    picked = [claim for _, claim in scored[:2]]
    text = " ".join(claim["text"] for claim in picked)
    return {"text": text, "claim_ids": [claim["claim_id"] for claim in picked]}


def resolve_all(store: GraphStore, questions: Sequence[str],
                scope: str = "role") -> dict[str, Any]:
    """Resolve a screening set and report the ladder breakdown for the run row.

    ``values_from_memory / values_replayed / values_reasoned`` on the ``runs``
    row come straight out of this: rungs 1 and 2 are memory, rung 3 is
    derivation, rung 4 is a question to the human.
    """
    resolutions = [resolve(store, question, scope) for question in questions]
    return {
        "resolutions": [r.summary() for r in resolutions],
        "questions_to_ask": [r.question for r in resolutions if r.asked],
        "values_from_memory": sum(1 for r in resolutions
                                  if r.source in ("standard_answer", "reused_answer")),
        "values_reasoned": sum(1 for r in resolutions if r.source == "generated"),
        "questions_asked": sum(1 for r in resolutions if r.asked),
    }


def record_human_answer(store: GraphStore, question: str, answer: str, day: int,
                        approved: bool = True, application_node: str | None = None,
                        scope: str = "role") -> dict[str, Any]:
    """Write an answer the human gave, once, so it is never asked again.

    A question that maps to a standard key becomes a ``StandardAnswer`` as well
    as an ``Answer``: the first is what rung 1 reads, the second is what rung 2
    reuses when the wording differs but the question does not.
    """
    record_question(store, question, scope)
    record_answer(store, question, answer, approved=approved, day=day,
                  application_node=application_node, scope=scope)
    key = standard_key_for(question)
    if key and approved:
        set_standard_answer(store, key, answer)
    return {"question": question, "stored_as": key or "answer", "approved": approved}


def ladder_report(store: GraphStore) -> Mapping[str, Any]:
    """What the digest says when it asks nothing: how much it already knew."""
    answers = known_standard_answers(store)
    try:
        questions = len(store.graph.by_label("Question"))
    except AttributeError:
        questions = 0
    return {"standard_answers": len(answers), "keys": sorted(answers),
            "questions_seen": questions}
