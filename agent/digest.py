"""Rendering the digest: the five roles, the why, and the two prompts.

Two surfaces, one renderer. Slack blocks with buttons if the hour-3 go/no-go
said interactivity round-trips; a numbered chat message if it did not. The demo
script does not change when the surface does — the human either taps *Keep* or
types ``keep 1,3; not-for-me 2 too senior``, and :func:`parse_reply` turns the
second into the same structure the first produces.

The "why" line is the point. It is rendered from the graph result, not written
by a model, and it is the thing a job board structurally cannot say:

    Shown because you have verified claims covering 4 of 5 requirements, the
    warmest path runs through the role at Stripe that replied to you, and you
    rejected the staff-level variant of this title on day 6 — this one is senior.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from agent.schema import REASON_TAGS

_KEEP_RE = re.compile(r"\bkeep\b\s*([\d,\s]+)", re.I)
_SKIP_RE = re.compile(r"\bskip\b\s*([\d,\s]+)", re.I)
_NFM_RE = re.compile(r"\bnot[- ]for[- ]me\b\s*(\d+)\s*([^;\n]*)", re.I)


def render_text(day: int, rows: Sequence[Mapping[str, Any]],
                header: str | None = None) -> str:
    """The chat-source form. Numbered, so a reply can name a row."""
    lines = [header or f"*Day {day}* — {len(rows)} roles"]
    for index, row in enumerate(rows, start=1):
        mark = "✅" if row.get("predicted") == "keep" else "➖"
        lines.append(f"{index}. {mark} {row.get('why') or row.get('title', '')}")
        if row.get("gaps"):
            lines.append(f"    gap: no verified claim for {', '.join(row['gaps'][:2])}")
        if row.get("url"):
            lines.append(f"    {row['url']}")
    lines.append("")
    lines.append("Reply e.g. `keep 1,3; not-for-me 2 too senior; skip 4`")
    return "\n".join(lines)


def render_blocks(day: int, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Slack Block Kit. One action row per role; the action id carries the job id."""
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Day {day} — {len(rows)} roles"}}
    ]
    for row in rows:
        text = row.get("why") or f"*{row.get('title','')}* at {row.get('company','')}"
        if row.get("gaps"):
            text += f"\n_gap: no verified claim for {', '.join(row['gaps'][:2])}_"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Keep"},
             "action_id": f"keep::{row['job_id']}", "style": "primary"},
            {"type": "button", "text": {"type": "plain_text", "text": "Skip"},
             "action_id": f"skip::{row['job_id']}"},
            {"type": "button", "text": {"type": "plain_text", "text": "Not for me"},
             "action_id": f"not_for_me::{row['job_id']}", "style": "danger"},
            {"type": "button", "text": {"type": "plain_text", "text": "Apply pack"},
             "action_id": f"pack::{row['job_id']}"},
        ]})
    return blocks


def parse_reply(text: str, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Turn ``keep 1,3; not-for-me 2 too senior`` into signals.

    The fallback path has to produce exactly what a button click produces, or
    the two surfaces diverge and only one of them gets tested.
    """
    signals: list[dict[str, Any]] = []

    def job_at(position: str) -> Mapping[str, Any] | None:
        try:
            index = int(position) - 1
        except ValueError:
            return None
        return rows[index] if 0 <= index < len(rows) else None

    for match in _NFM_RE.finditer(text):
        row = job_at(match.group(1))
        if row:
            reason = match.group(2).strip()
            signals.append({"job_id": row["job_id"], "kind": "not_for_me",
                            "reason_text": reason, "reason_tags": tags_from_text(reason)})
    for pattern, kind in ((_KEEP_RE, "keep"), (_SKIP_RE, "skip")):
        for match in pattern.finditer(text):
            for position in match.group(1).split(","):
                row = job_at(position.strip())
                if row and not any(s["job_id"] == row["job_id"] for s in signals):
                    signals.append({"job_id": row["job_id"], "kind": kind,
                                    "reason_text": "", "reason_tags": []})
    return signals


def tags_from_text(text: str) -> list[str]:
    """Map free text onto the reason chips.

    Keyword matching, not a model call: this runs on every feedback write, and a
    tag is what preference induction counts. Anything unmatched stays as free
    text for Cognee to turn into structured tags later.
    """
    lowered = (text or "").lower()
    hits = []
    phrases = {
        "too_senior": ("too senior", "staff", "principal", "director", "head of"),
        "too_junior": ("too junior", "junior", "entry", "graduate"),
        "company_too_large": ("too big", "too large", "enterprise", "megacorp"),
        "company_too_small": ("too small", "too early", "seed stage"),
        "wrong_domain": ("wrong domain", "not interested in", "wrong industry",
                         "crypto", "adtech", "gambling", "defense"),
        "location": ("location", "commute", "onsite", "relocat", "wrong city"),
        "comp": ("comp", "salary", "pay", "underpaid", "low band"),
        "stack": ("stack", "language", "framework", "legacy"),
    }
    for tag, needles in phrases.items():
        if any(needle in lowered for needle in needles):
            hits.append(tag)
    if not hits and lowered.strip():
        hits.append("other")
    return [tag for tag in hits if tag in REASON_TAGS]


def preference_prompt_blocks(preference: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Confirm / Not quite. "Not quite" is permanent (DESIGN §6.3)."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": preference["prompt"]}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Confirm"},
             "action_id": f"pref_confirm::{preference['preference_id']}", "style": "primary"},
            {"type": "button", "text": {"type": "plain_text", "text": "Not quite"},
             "action_id": f"pref_reject::{preference['preference_id']}"},
        ]},
    ]


def autonomy_prompt_blocks(domain: str, prompt: str) -> list[dict[str, Any]]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": prompt}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Turn it on"},
             "action_id": f"autonomy_grant::{domain}", "style": "primary"},
            {"type": "button", "text": {"type": "plain_text", "text": "Not yet"},
             "action_id": f"autonomy_decline::{domain}"},
        ]},
    ]


def render_pack(pack: Mapping[str, Any]) -> str:
    """The apply-pack message: copy, citations, gaps, and what was not asked."""
    lines = [f"*Apply pack — {pack.get('title','')} at {pack.get('company','')}*", ""]
    lines.append(pack.get("copy", ""))
    if pack.get("provenance"):
        lines.append("")
        lines.append("_Every line above comes from a verified claim:_")
        for item in pack["provenance"]:
            lines.append(f"  • `{item['claim_id']}` {item['text'][:120]}")
    if pack.get("gaps"):
        lines.append("")
        lines.append("*Gaps, stated rather than papered over:*")
        for gap in pack["gaps"]:
            lines.append(f"  • no verified claim for {gap}")
    answered = pack.get("answers") or []
    reused = [a for a in answered if a["source"] in ("standard_answer", "reused_answer")]
    if reused:
        lines.append("")
        lines.append(f"_{len(reused)} screening answer(s) filled from memory; "
                     f"{len(pack.get('questions_to_ask') or [])} left to ask._")
    if pack.get("questions_to_ask"):
        lines.append("")
        lines.append("*I need these from you once, and then never again:*")
        for question in pack["questions_to_ask"]:
            lines.append(f"  • {question}")
    return "\n".join(lines)
