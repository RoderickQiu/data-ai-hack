"""Preference learning: signals in, structured rules out, human in the middle.

The loop that is P2 end to end (DESIGN §6.3):

1. count signals sharing a reason tag or an attribute across the last 20;
2. at ``evidence_count >= 3``, with no previously *rejected* rule matching,
   create a ``Preference {status: hypothesis}``;
3. Slack asks "I think you prefer companies under 2,000 people — three
   not-for-mes in a row (X, Y, Z). Confirm?";
4. confirm sets ``active``; "not quite" sets ``rejected`` **permanently**, so
   the same wrong guess is never proposed twice;
5. the current shortlist re-ranks immediately, and that visible reorder is the
   proof moment.

Two rules the code enforces rather than documents:

* **Preferences never change behaviour before the human confirms them.** Only
  ``active`` rules reach :func:`preference_fit`.
* **Rules are structured, never prose.** ``{field, op, value, effect, weight}``
  applies deterministically in Python or SQL, which is what keeps ``predict_fit``
  free of LLM calls and lets the digest explain a ranking in one line.

Only ``not_for_me`` teaches. ``skip`` means "not now" and carries no weight —
without that split, every busy afternoon becomes fake evidence that the
candidate dislikes something.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from agent.config import TUNABLES
from agent.schema import now_iso
from memory.graph import GraphStore, nid

OPS = ("lt", "lte", "gt", "gte", "eq", "ne", "in", "not_in", "contains", "not_contains")
EFFECTS = ("penalty", "boost")

# Fields a rule may name. A rule over an unknown field is rejected at
# construction: a typo would otherwise become a silently inert preference.
FIELDS = (
    "company_size", "seniority", "industry", "location", "remote",
    "salary_max", "salary_min", "company", "title", "source",
)


@dataclass(frozen=True)
class Rule:
    field: str
    op: str
    value: Any
    effect: str = "penalty"
    weight: float = 0.5

    def __post_init__(self) -> None:
        if self.field not in FIELDS:
            raise ValueError(f"unknown preference field {self.field!r}; have {FIELDS}")
        if self.op not in OPS:
            raise ValueError(f"unknown preference op {self.op!r}; have {OPS}")
        if self.effect not in EFFECTS:
            raise ValueError(f"unknown preference effect {self.effect!r}")
        if not 0 < self.weight <= 1:
            raise ValueError("preference weight must be in (0, 1]")

    @classmethod
    def parse(cls, data: Mapping[str, Any] | str) -> "Rule":
        if isinstance(data, str):
            data = json.loads(data)
        return cls(field=data["field"], op=data["op"], value=data["value"],
                   effect=data.get("effect", "penalty"),
                   weight=float(data.get("weight", 0.5)))

    def key(self) -> str:
        return hashlib.sha256(
            json.dumps([self.field, self.op, self.value, self.effect], sort_keys=True,
                       default=str).encode()
        ).hexdigest()[:12]

    def matches(self, job: Mapping[str, Any]) -> bool:
        actual = job.get(self.field)
        if actual is None:
            return False
        try:
            return _APPLY[self.op](actual, self.value)
        except (TypeError, ValueError):
            return False

    def sentence(self) -> str:
        phrasing = {
            "lt": "below", "lte": "at most", "gt": "above", "gte": "at least",
            "eq": "exactly", "ne": "other than", "in": "one of", "not_in": "not one of",
            "contains": "mentioning", "not_contains": "not mentioning",
        }[self.op]
        direction = "prefer" if self.effect == "boost" else "avoid"
        # A list value reaches a human in the Confirm prompt, so it is spelled
        # out rather than printed as a Python repr: "manager or staff", not
        # "['manager', 'staff']". Applies to the industry and location rules
        # too, which have always carried lists.
        if isinstance(self.value, (list, tuple)):
            items = [str(v) for v in self.value]
            value = " or ".join(items) if len(items) < 3 else \
                ", ".join(items[:-1]) + f" or {items[-1]}"
        else:
            value = self.value
        return f"{direction} roles with {self.field.replace('_', ' ')} {phrasing} {value}"


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


_APPLY = {
    "lt": lambda a, b: float(a) < float(b),
    "lte": lambda a, b: float(a) <= float(b),
    "gt": lambda a, b: float(a) > float(b),
    "gte": lambda a, b: float(a) >= float(b),
    "eq": lambda a, b: _lower(a) == _lower(b),
    "ne": lambda a, b: _lower(a) != _lower(b),
    "in": lambda a, b: _lower(a) in [_lower(v) for v in b],
    "not_in": lambda a, b: _lower(a) not in [_lower(v) for v in b],
    "contains": lambda a, b: str(b).lower() in str(a).lower(),
    "not_contains": lambda a, b: str(b).lower() not in str(a).lower(),
}


@dataclass
class Preference:
    preference_id: str
    rule: Rule
    status: str = "hypothesis"      # hypothesis | active | rejected
    confidence: float = 0.5
    evidence_count: int = 0
    evidence_jobs: list[str] = field(default_factory=list)
    explanation: str = ""

    def prompt(self) -> str:
        """The Slack question. Names the evidence, because that is what makes it
        checkable rather than a guess the human has to take on trust."""
        examples = ", ".join(self.evidence_jobs[:3])
        named = f" ({examples})" if examples else ""
        return (f"I think you {self.rule.sentence()} — {self.evidence_count} "
                f"not-for-mes in a row{named}. Confirm?")


# --- induction -------------------------------------------------------------

# The level memory.extract.seniority_of falls back to when no word in the
# title matches. It means "unclassified", so it never induces a rule.
_UNCLASSIFIED_LEVEL = "mid"

_SENIOR_WORDS = ("staff", "principal", "director", "head of", "vp", "distinguished")
_JUNIOR_WORDS = ("junior", "associate", "intern", "graduate", "entry level")

# Each reason chip maps to the one attribute it is actually about. Free text is
# turned into these tags upstream by Cognee; anything unmapped falls through to
# the attribute-based inducer below.
_TAG_FIELDS = {
    "too_senior": "title", "too_junior": "title",
    "company_too_large": "company_size", "company_too_small": "company_size",
    "wrong_domain": "industry", "location": "location", "comp": "salary_max",
    "stack": "title",
}


def induce(signals: Sequence[Mapping[str, Any]], rejected_keys: Iterable[str] = (),
           threshold: int = TUNABLES.pref_evidence_threshold) -> list[Preference]:
    """Propose rules from the teaching signals. Never writes; never decides."""
    teaching = [s for s in signals if s.get("kind") == "not_for_me"]
    rejected = set(rejected_keys)
    proposals: list[Preference] = []

    by_tag: dict[str, list[Mapping[str, Any]]] = {}
    for signal in teaching:
        for tag in signal.get("reason_tags") or []:
            by_tag.setdefault(tag, []).append(signal)

    for tag, group in by_tag.items():
        if len(group) < threshold:
            continue
        rule = _rule_for_tag(tag, group)
        if rule is None or rule.key() in rejected:
            continue
        proposals.append(_proposal(rule, group,
                                   f"{len(group)} roles you marked not-for-me tagged '{tag}'"))

    # Attribute agreement, for the case where the chips disagree but the roles
    # do not: three rejections that all happen to be 5,000-person companies.
    for attribute in ("company_size", "industry", "location", "source"):
        group = [s for s in teaching if s.get(attribute) is not None]
        if len(group) < threshold:
            continue
        rule = _rule_for_attribute(attribute, group)
        if rule is None or rule.key() in rejected:
            continue
        if any(p.rule.key() == rule.key() for p in proposals):
            continue
        proposals.append(_proposal(rule, group,
                                   f"{len(group)} of your last not-for-mes share {attribute}"))

    proposals.sort(key=lambda p: -p.evidence_count)
    return proposals


def _proposal(rule: Rule, group: Sequence[Mapping[str, Any]], explanation: str) -> Preference:
    # A signal whose job no longer resolves is left out of the examples rather
    # than rendered as "?". Naming the evidence is what makes the hypothesis
    # checkable instead of something the human has to take on trust, and a "?"
    # in that list spends the trust the naming was there to earn. The signal
    # still counts toward evidence_count: the reason tag on it is real whether
    # or not the posting is still in the graph.
    jobs = [name for s in group
            if (name := f"{s.get('company') or ''} {s.get('title') or ''}".strip())]
    return Preference(
        preference_id="pref-" + rule.key(), rule=rule, status="hypothesis",
        confidence=min(0.5 + 0.1 * len(group), 0.95), evidence_count=len(group),
        evidence_jobs=jobs[:5], explanation=explanation,
    )


def _rule_for_tag(tag: str, group: Sequence[Mapping[str, Any]]) -> Rule | None:
    if tag == "too_senior":
        word = _common_word(group, _SENIOR_WORDS)
        return (Rule("title", "contains", word, "penalty", 0.6) if word
                else _rule_for_level(group))
    if tag == "too_junior":
        word = _common_word(group, _JUNIOR_WORDS)
        return (Rule("title", "contains", word, "penalty", 0.6) if word
                else _rule_for_level(group))
    if tag == "company_too_large":
        sizes = _numbers(group, "company_size")
        cutoff = _round_size(min(sizes)) if sizes else 2000
        return Rule("company_size", "gte", cutoff, "penalty", 0.5)
    if tag == "company_too_small":
        sizes = _numbers(group, "company_size")
        cutoff = _round_size(max(sizes)) if sizes else 50
        return Rule("company_size", "lte", cutoff, "penalty", 0.5)
    if tag == "wrong_domain":
        domains = _modes(group, "industry")
        return Rule("industry", "in", domains, "penalty", 0.6) if domains else None
    if tag == "location":
        places = _modes(group, "location")
        return Rule("location", "in", places, "penalty", 0.5) if places else None
    if tag == "comp":
        salaries = _numbers(group, "salary_max")
        if not salaries:
            return None
        return Rule("salary_max", "lt", _round_size(max(salaries)), "penalty", 0.5)
    return None


def _rule_for_level(group: Sequence[Mapping[str, Any]]) -> Rule | None:
    """Fall back to the ``seniority`` column when the titles share no word.

    "Too senior" spans staff, manager, director and VP — words that by
    definition do not co-occur, so :func:`_common_word` finds nothing in the
    one case the chip is most often used for. Three rejections reading
    *Member of Technical Staff*, *Manager, Billing* and *Sr. Manager,
    Platform* have no common title word and induced no rule at all, which is
    the persona's stated preference — "senior individual-contributor, not a
    management track" — going unlearned.

    ``seniority`` is a corpus column and was already in ``FIELDS``; nothing
    else needed changing to apply or explain a rule keyed on it.

    Every signal in the group has to carry a level, so a rule is never induced
    from the two rows that happened to have the column filled in.
    """
    levelled = [str(s["seniority"]) for s in group if s.get("seniority")]
    if len(levelled) < len(group):
        return None
    # "mid" is what memory.extract.seniority_of returns when it recognises no
    # word in the title — the unclassified bucket, 45% of this corpus, not a
    # level. A rejection that landed there says the classifier missed, not that
    # the human dislikes mid-level work, and a rule including it penalised 63%
    # of the corpus off three clicks.
    classified = [level for level in levelled if level != _UNCLASSIFIED_LEVEL]
    # The classified rejections must themselves clear the evidence threshold,
    # the same bar _common_word holds titles to. Otherwise one genuinely senior
    # rejection among two unclassifiable ones induces a rule that claims three
    # not-for-mes and rests on one — seen live with "Senior Staff Applied AI
    # Engineer", "Software Engineer - New Grad" and "Research Scientist".
    if len(classified) < TUNABLES.pref_evidence_threshold:
        return None
    levels = set(classified)
    # Three rejections at three different levels is a span, not a pattern; past
    # that the "rule" would penalise most of the corpus on no real evidence.
    if len(levels) > 3:
        return None
    return Rule("seniority", "in", sorted(levels), "penalty", 0.6)


def _rule_for_attribute(attribute: str, group: Sequence[Mapping[str, Any]]) -> Rule | None:
    if attribute == "company_size":
        sizes = _numbers(group, "company_size")
        if len(sizes) < TUNABLES.pref_evidence_threshold:
            return None
        if min(sizes) >= 1000:
            return Rule("company_size", "gte", _round_size(min(sizes)), "penalty", 0.4)
        if max(sizes) <= 100:
            return Rule("company_size", "lte", _round_size(max(sizes)), "penalty", 0.4)
        return None
    values = _modes(group, attribute)
    # One value has to account for the whole group; two different industries is
    # not a preference, it is a coincidence.
    if len(values) == 1 and sum(1 for s in group if s.get(attribute) == values[0]) >= len(group):
        return Rule(attribute, "eq", values[0], "penalty", 0.4)
    return None


def _numbers(group: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    out = []
    for item in group:
        value = item.get(key)
        try:
            if value is not None:
                out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def _modes(group: Sequence[Mapping[str, Any]], key: str) -> list[str]:
    values = [str(item.get(key)) for item in group if item.get(key)]
    if not values:
        return []
    top = statistics.mode(values)
    return [top] if values.count(top) >= TUNABLES.pref_evidence_threshold else []


def _common_word(group: Sequence[Mapping[str, Any]], words: Sequence[str]) -> str | None:
    titles = [str(item.get("title") or "").lower() for item in group]
    for word in words:
        if sum(1 for title in titles if word in title) >= TUNABLES.pref_evidence_threshold:
            return word
    return None


def _round_size(value: float) -> int:
    """Round to a number a human would say out loud. '2,000 people' reads as a
    preference; '1,873 people' reads as overfitting, and it is."""
    for step in (10, 50, 100, 500, 1000, 5000, 10000):
        if value <= step * 10:
            return int(round(value / step) * step) or step
    return int(round(value / 10000) * 10000)


# --- storage and lifecycle -------------------------------------------------

def propose(store: GraphStore, preference: Preference) -> str:
    """Write a hypothesis. It changes nothing until the human confirms it."""
    candidate = store.candidate_node()
    node = nid("preference", preference.preference_id)
    store.merge_node(node, "Preference", preference_id=preference.preference_id,
                     rule=asdict(preference.rule), rule_key=preference.rule.key(),
                     status=preference.status, confidence=preference.confidence,
                     evidence_count=preference.evidence_count,
                     evidence_jobs=preference.evidence_jobs,
                     explanation=preference.explanation, proposed_at=now_iso())
    store.merge_edge(candidate, "PROPOSED", node)
    return node


def confirm(store: GraphStore, preference_id: str) -> dict[str, Any]:
    """Human said yes. Now — and only now — it changes the ranking."""
    node = nid("preference", preference_id)
    store.merge_node(node, "Preference", preference_id=preference_id, status="active",
                     confirmed_at=now_iso())
    store.merge_edge(store.candidate_node(), "HOLDS", node)
    return {"preference_id": preference_id, "status": "active"}


def reject(store: GraphStore, preference_id: str) -> dict[str, Any]:
    """Human said "not quite". Permanently — the same guess is never re-proposed."""
    node = nid("preference", preference_id)
    store.merge_node(node, "Preference", preference_id=preference_id, status="rejected",
                     rejected_at=now_iso())
    store.merge_edge(store.candidate_node(), "REJECTED_PREF", node)
    return {"preference_id": preference_id, "status": "rejected"}


def rejected_rule_keys(store: GraphStore) -> list[str]:
    try:
        return [node.props.get("rule_key") for node in store.graph.by_label("Preference")
                if node.props.get("status") == "rejected" and node.props.get("rule_key")]
    except AttributeError:
        return []


def active_rules(store: GraphStore) -> list[Rule]:
    """The confirmed rules, with redundant ones folded together.

    preference_fit subtracts each rule's full weight, and that is deliberate:
    distinct preferences should compound, so a role that is too senior *and* at
    too large a company is penalised twice over.

    What must not compound is the same preference confirmed more than once. The
    seniority inducer keys a rule on the set of levels rejected, so successive
    rejections proposed {manager, staff}, then {principal, staff}, then
    {manager, principal, staff} — three different rule keys, each of which the
    human reasonably confirmed because each looked new. A staff role then
    matched all three and lost 1.8 against a 0.5 starting score.

    Rules sharing a field, operator and effect collapse into one carrying the
    union of their values: strictly the broadest thing the human agreed to, and
    charged once.
    """
    rules: list[Rule] = []
    for row in store.run("active_preferences"):
        try:
            rules.append(Rule.parse(row["rule"]))
        except (KeyError, ValueError, TypeError):
            continue

    merged: dict[tuple[str, str, str], Rule] = {}
    for rule in rules:
        key = (rule.field, rule.op, rule.effect)
        seen = merged.get(key)
        if seen is None:
            merged[key] = rule
        elif rule.op in ("in", "not_in"):
            values = sorted({*_as_list(seen.value), *_as_list(rule.value)})
            merged[key] = Rule(rule.field, rule.op, values, rule.effect,
                               max(seen.weight, rule.weight))
        # Any other operator: the first confirmation stands. Two thresholds on
        # one field are a contradiction to resolve with the human, not by
        # quietly averaging them.
    return list(merged.values())


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def check_for_hypothesis(store: GraphStore) -> list[Preference]:
    """Run after every feedback write. Returns what to ask, having stored it."""
    signals = store.run("recent_signals", {"limit": TUNABLES.pref_signal_window})
    existing = set()
    try:
        existing = {node.props.get("rule_key") for node in store.graph.by_label("Preference")}
    except AttributeError:
        pass
    proposals = [p for p in induce(signals, rejected_rule_keys(store))
                 if p.rule.key() not in existing]
    for proposal in proposals:
        propose(store, proposal)
    return proposals


# --- application -----------------------------------------------------------

def preference_fit(rules: Sequence[Rule], job: Mapping[str, Any]) -> tuple[float, list[str]]:
    """Score a job against the active rules. Deterministic, explainable, no LLM.

    Starts neutral at 0.5 and moves by each matching rule's full weight, so a job
    the rules say nothing about is neither rewarded nor punished. The returned
    reasons are what the digest prints under "why".

    Full weight, not half: a rule only reaches here after the human explicitly
    confirmed it, and at 0.25 of the total score a half-weight penalty moves a
    role by under a tenth of a point — which is not the visible reorder the
    confirmation is supposed to cause. A strong confirmed rule should be able to
    push a well-matched role off the top, and a weak one should not.
    """
    score, reasons = 0.5, []
    for rule in rules:
        if not rule.matches(job):
            continue
        score += rule.weight if rule.effect == "boost" else -rule.weight
        reasons.append(f"you {rule.sentence()}")
    return max(0.0, min(1.0, score)), reasons
