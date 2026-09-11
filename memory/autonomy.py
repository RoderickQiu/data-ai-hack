"""Earning autonomy: the closing beat, and the one guardrail with a hard ceiling.

The three lines in DESIGN §7 describe an agent that needs the human less.
This is what that turns into as a product.

**Readiness comes from the agreement record, not from model confidence.**
Self-reported confidence is poorly calibrated and a judge knows it. "You agreed
with 13 of my last 15 picks" is a fact the human can check, and every number it
is made of is already logged.

Two domains, because two is what fits and both already have their metrics:

===== ================================================ ==========================
D1    shortlist and prepare packs for its own picks     skip-class accuracy ≥ 85%
D2    activate an inferred preference without asking    4 of the last 5 confirmed
===== ================================================ ==========================

``Supervised → Ready → Autonomous``, with downgrade. At ``Ready`` the agent
posts **one** prompt and waits. After "Not yet" it does not re-prompt that
domain until 10 more decisions are logged. Two overrides in the last 10
autonomous decisions returns the domain to ``Supervised``, with the reason said
out loud.

**Never autonomous, whatever the record says** (:data:`NEVER_AUTONOMOUS`): the
agent can earn autonomy over *judgment*, never over *facts* — facts about a
person cannot be learned by watching them click — and never over submitting
anything to anything. That ceiling is a better answer to "isn't this dangerous"
than any amount of guardrail prose, so it is a constant, not a policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agent.config import TUNABLES
from agent.schema import now_iso
from memory.graph import GraphStore, nid

D1_SHORTLIST = "shortlisting"
D2_PREFERENCES = "preference_rules"
DOMAINS = (D1_SHORTLIST, D2_PREFERENCES)

STATES = ("Supervised", "Ready", "Autonomous")

NEVER_AUTONOMOUS = (
    "add or change a Claim",
    "add or change a StandardAnswer",
    "mark an unverified claim verified",
    "submit anything, to anything",
)


@dataclass
class Readiness:
    domain: str
    state: str
    value: float
    window: int
    threshold: float
    decisions_since_prompt: int = 0
    recent_overrides: int = 0
    ready: bool = False
    reason: str = ""

    def prompt(self) -> str | None:
        """The one message. Cites the real count from this session or nothing."""
        if not self.ready or self.state != "Ready":
            return None
        if self.domain == D1_SHORTLIST:
            agreed = round(self.value * self.window)
            return (f"You've agreed with {agreed} of my last {self.window} calls "
                    f"({self.value:.0%}). I'd like to prepare packs for my own top picks "
                    f"without asking first. You'll get the digest either way, and you can "
                    f"turn it off anytime.  *Turn it on* / *Not yet*")
        return (f"{round(self.value * self.window)} of my last {self.window} preference "
                f"guesses were right. I'd like to activate the next obvious one without "
                f"asking.  *Turn it on* / *Not yet*")


def state_of(store: GraphStore, domain: str) -> dict[str, Any]:
    rows = {row["domain"]: row for row in store.run("autonomy_state")}
    row = rows.get(domain) or {}
    return {
        "domain": domain,
        "state": row.get("state") or "Supervised",
        "readiness_value": row.get("readiness_value") or 0.0,
        "decisions_since_prompt": row.get("decisions_since_prompt") or 0,
        "recent_overrides": row.get("recent_overrides") or 0,
    }


def _write_state(store: GraphStore, domain: str, **props: Any) -> None:
    candidate = store.candidate_node()
    node = nid("autonomy", domain)
    store.merge_node(candidate, "Candidate", candidate_id=store.candidate_id)
    store.merge_node(node, "AutonomyState", domain=domain, updated_at=now_iso(), **props)
    store.merge_edge(candidate, "HAS_AUTONOMY", node)


def skip_class_accuracy(record: Sequence[Mapping[str, Any]]) -> tuple[float, int]:
    """How often the agent called a *skip* correctly (DESIGN §7).

    Measured across everything shown, the number is dishonest: as ranking
    improves the slate becomes uniformly good, the human keeps nearly
    everything, and "keep" turns trivially predictable — accuracy rises because
    the base rate moved, not because judgment sharpened. So the headline number
    is the skip class only, and the slate is mixed to keep skips occurring.
    """
    relevant = [row for row in record
                if row.get("predicted") == "skip" or row.get("actual") == "skip"]
    if not relevant:
        return 0.0, 0
    correct = sum(1 for row in relevant if row.get("predicted") == row.get("actual"))
    return correct / len(relevant), len(relevant)


def evaluate(store: GraphStore, domain: str) -> Readiness:
    """Compute readiness from the logged record. Never grants anything."""
    current = state_of(store, domain)
    if domain == D1_SHORTLIST:
        record = store.run("agreement_record", {"limit": TUNABLES.d1_window})
        value, window = skip_class_accuracy(record)
        threshold = TUNABLES.d1_accuracy_threshold
        enough = window >= max(3, TUNABLES.d1_window // 3)
        reason = (f"{round(value * window)} of {window} skip calls correct"
                  if window else "no resolved predictions yet")
    else:
        proposals = _recent_proposals(store, TUNABLES.d2_window)
        confirmed = sum(1 for p in proposals if p.get("status") == "active")
        window = len(proposals)
        value = confirmed / window if window else 0.0
        threshold = TUNABLES.d2_confirm_threshold / max(TUNABLES.d2_window, 1)
        enough = window >= TUNABLES.d2_window
        reason = f"{confirmed} of the last {window} preference guesses confirmed"

    state = current["state"]
    ready = bool(enough and value >= threshold and state == "Supervised")
    if ready and current["decisions_since_prompt"] < 0:
        ready = False
    if ready:
        state = "Ready"
        _write_state(store, domain, state="Ready", readiness_value=round(value, 4))
    else:
        _write_state(store, domain, readiness_value=round(value, 4))

    return Readiness(domain=domain, state=state, value=value, window=window or 1,
                     threshold=threshold,
                     decisions_since_prompt=current["decisions_since_prompt"],
                     recent_overrides=current["recent_overrides"],
                     ready=ready, reason=reason)


def _recent_proposals(store: GraphStore, limit: int) -> list[dict[str, Any]]:
    try:
        nodes = [node.props for node in store.graph.by_label("Preference")]
    except AttributeError:
        return []
    decided = [p for p in nodes if p.get("status") in ("active", "rejected")]
    decided.sort(key=lambda p: p.get("confirmed_at") or p.get("rejected_at") or "",
                 reverse=True)
    return decided[:limit]


def grant(store: GraphStore, domain: str) -> dict[str, Any]:
    """The human toggled it on. This is the only way a domain becomes Autonomous."""
    if domain not in DOMAINS:
        raise ValueError(f"unknown autonomy domain {domain!r}; have {DOMAINS}")
    _write_state(store, domain, state="Autonomous", granted_at=now_iso(),
                 decisions_since_prompt=0, recent_overrides=0)
    return {"domain": domain, "state": "Autonomous"}


def decline(store: GraphStore, domain: str) -> dict[str, Any]:
    """"Not yet". Anti-nag: no re-prompt until 10 more decisions are logged."""
    _write_state(store, domain, state="Supervised", declined_at=now_iso(),
                 decisions_since_prompt=-TUNABLES.autonomy_renag_after)
    return {"domain": domain, "state": "Supervised",
            "renag_after": TUNABLES.autonomy_renag_after}


def note_decision(store: GraphStore, domain: str, overridden: bool = False) -> dict[str, Any]:
    """Log one decision in a domain, and downgrade if the overrides pile up.

    An override is an undo, or a post-hoc "not for me" on something the agent
    picked itself. Two in the last ten autonomous decisions and the domain goes
    back to Supervised, with the reason stated rather than the state quietly
    changing.
    """
    current = state_of(store, domain)
    decisions = current["decisions_since_prompt"] + 1
    overrides = current["recent_overrides"] + (1 if overridden else 0)
    state = current["state"]
    downgraded = False
    if state == "Autonomous" and overrides >= TUNABLES.autonomy_downgrade_overrides:
        state, downgraded = "Supervised", True
        overrides = 0
    if decisions % TUNABLES.autonomy_downgrade_window == 0:
        overrides = 0  # the window slides; overrides are counted per ten decisions
    _write_state(store, domain, state=state, decisions_since_prompt=decisions,
                 recent_overrides=overrides)
    return {"domain": domain, "state": state, "downgraded": downgraded,
            "decisions_since_prompt": decisions, "recent_overrides": overrides,
            "message": (f"Two of my last {TUNABLES.autonomy_downgrade_window} picks were "
                        f"overridden, so I've handed {domain} back to you.")
            if downgraded else ""}


def may_act_alone(store: GraphStore, domain: str) -> bool:
    return state_of(store, domain)["state"] == "Autonomous"


def guardrails() -> dict[str, Any]:
    """What the agent will not do at any autonomy level. For the digest footer."""
    return {"never_autonomous": list(NEVER_AUTONOMOUS),
            "hard_ceiling": "no submission to any job board, ever"}
