import json
from typing import Any

from .schemas import AutonomyIn, ClaimsIn, DigestIn, JobCard, PackIn, PreferenceIn, QuestionIn

REASON_TAGS = [
    ("too_senior", "Too senior"),
    ("too_junior", "Too junior"),
    ("company_too_large", "Company too large"),
    ("wrong_domain", "Wrong domain"),
    ("location", "Location"),
    ("comp", "Compensation"),
]

# Slack rejects a message with more than this many blocks.
SLACK_BLOCK_LIMIT = 50

DANGER_COLOR = "#d64541"
GOOD_COLOR = "#2eb886"


def _txt(s: str) -> dict[str, Any]:
    return {"type": "mrkdwn", "text": s}


def _plain(s: str, limit: int = 150) -> dict[str, Any]:
    return {"type": "plain_text", "text": s[:limit], "emoji": True}


def _btn(text: str, action_id: str, value: dict[str, Any], style: str | None = None) -> dict[str, Any]:
    b = {"type": "button", "text": _plain(text, 75), "action_id": action_id, "value": json.dumps(value)}
    if style:
        b["style"] = style
    return b


def _ctx(*parts: str) -> dict[str, Any]:
    return {"type": "context", "elements": [_txt(" · ".join(p for p in parts if p))]}


def note(text: str) -> dict[str, Any]:
    return _ctx(text)


def run_footer(mode: str | None, tokens: int | None, questions: int, wall_ms: int | None) -> str:
    bits = []
    if mode:
        bits.append({"first_run": "first run", "partial_replay": "partial replay", "full_replay": "full replay"}.get(mode, mode))
    if tokens is not None:
        bits.append(f"{tokens:,} tokens")
    bits.append("0 questions" if questions == 0 else f"{questions} question{'s' if questions != 1 else ''}")
    if wall_ms is not None:
        bits.append(f"{wall_ms / 1000:.1f}s")
    return " · ".join(bits)


def job_blocks(job: JobCard, run_id: str, day: str) -> list[dict[str, Any]]:
    meta = " · ".join(x for x in (job.location, job.salary) if x)
    title = f"*<{job.url}|{job.title}>* at *{job.company}*" if job.url else f"*{job.title}* at *{job.company}*"

    blocks: list[dict[str, Any]] = [{"type": "section", "text": _txt(title)}]

    signals = [meta] if meta else []
    if job.prediction == "keep":
        conf = f" · {job.confidence:.0%} confident" if job.confidence is not None else ""
        signals.append(f":dart: I predict you'll *keep* this{conf}")
    elif job.prediction == "skip":
        conf = f" · {job.confidence:.0%} confident" if job.confidence is not None else ""
        signals.append(f":heavy_minus_sign: I predict you'll *skip* this{conf}")
    if job.coverage:
        signals.append(job.coverage)
    if job.off_slate:
        signals.append(":test_tube: control pick, held outside the ranking")
    if signals:
        blocks.append(_ctx(*signals))
    if job.why:
        blocks.append({"type": "section", "text": _txt(f">{job.why}")})

    v = {"j": job.job_id, "r": run_id, "d": day, "t": job.title, "c": job.company}
    blocks.append(
        {
            "type": "actions",
            "block_id": f"act::{job.job_id}",
            "elements": [
                _btn("Keep", "keep", v, "primary"),
                _btn("Not for me", "not_for_me", v, "danger"),
                _btn("Skip", "skip", v),
                _btn("Prepare pack", "request_pack", v),
            ],
        }
    )
    return blocks


def digest_message(d: DigestIn) -> list[dict[str, Any]]:
    count = len(d.jobs)
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": _plain(f"Day {d.day} · {count} role{'s' if count != 1 else ''}")},
        _ctx(run_footer(d.mode, d.tokens, d.questions_asked, d.wall_ms)),
    ]
    if d.decided_by_agent:
        blocks.append(_ctx(":robot_face: decided by agent — you have autonomy switched on for shortlisting"))
    for job in d.jobs:
        blocks.extend(job_blocks(job, d.run_id, d.day))
    if len(blocks) > SLACK_BLOCK_LIMIT:
        raise ValueError(
            f"{len(blocks)} blocks for {count} roles exceeds Slack's "
            f"{SLACK_BLOCK_LIMIT}; reduce slate_size or the blocks per role"
        )
    return blocks


def decided_context(kind: str, tags: list[str] | None = None) -> dict[str, Any]:
    label = {
        "keep": ":white_check_mark: Kept",
        "skip": ":arrow_right: Skipped for now",
        "not_for_me": ":no_entry_sign: Not for me",
        "request_pack": ":package: Preparing pack…",
    }.get(kind, kind)
    if tags:
        pretty = ", ".join(dict(REASON_TAGS).get(t, t) for t in tags)
        label = f"{label} — {pretty}"
    return {"type": "context", "elements": [_txt(label)]}


def not_for_me_modal(v: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "not_for_me_submit",
        "private_metadata": json.dumps(v),
        "title": _plain("Not for me", 24),
        "submit": _plain("Save", 24),
        "close": _plain("Cancel", 24),
        "blocks": [
            {"type": "section", "text": _txt(f"*{v.get('t', '')}* at *{v.get('c', '')}*")},
            {
                "type": "input",
                "block_id": "reasons",
                "label": _plain("Why not? This is the only signal that teaches me."),
                "element": {
                    "type": "checkboxes",
                    "action_id": "tags",
                    "options": [{"text": _plain(label, 75), "value": key} for key, label in REASON_TAGS],
                },
            },
            {
                "type": "input",
                "block_id": "note",
                "optional": True,
                "label": _plain("Anything else"),
                "element": {"type": "plain_text_input", "action_id": "text", "multiline": True},
            },
        ],
    }


def pack_message(p: PackIn) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pending = len(p.questions)
    footer = [p.play] if p.play else []
    if p.tokens is not None:
        footer.append(f"{p.tokens:,} tokens")
    footer.append(
        f":white_check_mark: 0 questions — all {p.answers_from_memory} answers came from memory"
        if pending == 0
        else f"{pending} question{'s' if pending != 1 else ''} still need you"
    )

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": _plain(f"Apply pack · {p.title}")},
        _ctx(f"*{p.company}*", " · ".join(footer)),
        {"type": "section", "text": _txt(p.summary)},
    ]

    if p.claims:
        lines = "\n".join(f"• {c.text}  `[claim:{c.claim_id}]`" for c in p.claims)
        blocks.append({"type": "section", "text": _txt(f"*Claims used* — every one verified\n{lines}")})

    elements = []
    v = {"j": p.job_id, "r": p.run_id, "t": p.title, "c": p.company}
    if pending:
        elements.append(_btn(f"Answer {pending} question{'s' if pending != 1 else ''}", "answer_questions", v, "primary"))
    if p.doc_url:
        elements.append({"type": "button", "text": _plain("Open draft"), "url": p.doc_url, "action_id": "open_doc"})
    if p.sheet_url:
        elements.append({"type": "button", "text": _plain("Open tracker"), "url": p.sheet_url, "action_id": "open_sheet"})
    elements.append(_btn("I applied", "report_applied", v))
    blocks.append({"type": "actions", "block_id": f"pack::{p.job_id}", "elements": elements})

    attachments: list[dict[str, Any]] = []
    if p.gaps:
        gap_lines = "\n".join(f"• *{g.skill}* — {g.note}" if g.note else f"• *{g.skill}*" for g in p.gaps)
        attachments.append(
            {
                "color": DANGER_COLOR,
                "blocks": [
                    {
                        "type": "section",
                        "text": _txt(
                            f":warning: *Gaps — I did not claim these*\n{gap_lines}\n\n"
                            "_No verified claim supports them, so the pack says so instead of implying otherwise._"
                        ),
                    }
                ],
            }
        )
    return blocks, attachments


def questions_modal(p: PackIn, token: str) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": _txt(f"*{p.title}* at *{p.company}*")},
    ]
    for q in p.questions:
        element: dict[str, Any] = {"type": "plain_text_input", "action_id": "answer", "multiline": True}
        if q.draft:
            element["initial_value"] = q.draft
        blocks.append(
            {
                "type": "input",
                "block_id": f"q::{q.question_id}",
                "label": _plain(q.text, 2000),
                "element": element,
                "hint": _plain("Reused from a previous application — edit if it needs changing.")
                if q.source == "reused"
                else None,
            }
        )
    blocks = [{k: v for k, v in b.items() if v is not None} for b in blocks]
    return {
        "type": "modal",
        "callback_id": "questions_submit",
        "private_metadata": token,
        "title": _plain("Answer once, never again", 24),
        "submit": _plain("Save", 24),
        "close": _plain("Cancel", 24),
        "blocks": blocks,
    }


def preference_message(p: PreferenceIn) -> list[dict[str, Any]]:
    evidence = "\n".join(f"• {e}" for e in p.evidence)
    conf = f" · {p.confidence:.0%} confident" if p.confidence is not None else ""
    v = {"p": p.preference_id, "r": p.run_id, "rule": p.rule_text}
    return [
        {"type": "header", "text": _plain("I think I spotted a preference")},
        {"type": "section", "text": _txt(f"*{p.rule_text}*")},
        {"type": "section", "text": _txt(f"Because of these:\n{evidence}")},
        _ctx(f"I will not change any ranking until you confirm{conf}"),
        {
            "type": "actions",
            "block_id": f"pref::{p.preference_id}",
            "elements": [
                _btn("Confirm", "pref_confirm", v, "primary"),
                _btn("Not quite", "pref_reject", v, "danger"),
            ],
        },
    ]


def autonomy_message(a: AutonomyIn) -> list[dict[str, Any]]:
    record = ""
    if a.agreed is not None and a.total is not None:
        pct = f" ({a.accuracy:.0%})" if a.accuracy is not None else ""
        record = f"You've agreed with {a.agreed} of my last {a.total} calls{pct}."
    v = {"d": a.domain, "r": a.run_id}
    return [
        {"type": "header", "text": _plain("May I run this one alone?")},
        {"type": "section", "text": _txt(f"{record} {a.headline}".strip())},
        _ctx("You'll still get the digest either way, and you can switch it off at any time"),
        {
            "type": "actions",
            "block_id": f"auto::{a.domain}",
            "elements": [
                _btn("Turn it on", "autonomy_on", v, "primary"),
                _btn("Not yet", "autonomy_later", v),
            ],
        },
    ]


def claims_message(c: ClaimsIn) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": _plain(f"Confirm {len(c.claims)} claim{'s' if len(c.claims) != 1 else ''}")},
        _ctx("I inferred these rather than reading them off your resume. Only confirmed claims can appear in a pack."),
    ]
    for claim in c.claims:
        v = {"cl": claim.claim_id, "r": c.run_id}
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": _txt(claim.text)})
        if claim.source_doc:
            blocks.append(_ctx(f"from {claim.source_doc}"))
        blocks.append(
            {
                "type": "actions",
                "block_id": f"claim::{claim.claim_id}",
                "elements": [
                    _btn("Confirm", "claim_confirm", v, "primary"),
                    _btn("Discard", "claim_discard", v, "danger"),
                ],
            }
        )
    return blocks


def question_message(q: QuestionIn) -> list[dict[str, Any]]:
    v = {"q": q.question_id, "r": q.run_id, "j": q.job_id, "text": q.text, "draft": q.draft}
    blocks: list[dict[str, Any]] = [{"type": "section", "text": _txt(f"*{q.text}*")}]
    if q.context:
        blocks.append(_ctx(q.context))
    blocks.append(_ctx("I'll remember this one and never ask again"))
    blocks.append(
        {
            "type": "actions",
            "block_id": f"ask::{q.question_id}",
            "elements": [_btn("Answer", "answer_one", v, "primary")],
        }
    )
    return blocks


def single_question_modal(v: dict[str, Any]) -> dict[str, Any]:
    element: dict[str, Any] = {"type": "plain_text_input", "action_id": "answer", "multiline": True}
    if v.get("draft"):
        element["initial_value"] = v["draft"]
    return {
        "type": "modal",
        "callback_id": "single_question_submit",
        "private_metadata": json.dumps(v),
        "title": _plain("Answer once", 24),
        "submit": _plain("Save", 24),
        "close": _plain("Cancel", 24),
        "blocks": [
            {
                "type": "input",
                "block_id": "single",
                "label": _plain(v.get("text", "Your answer"), 2000),
                "element": element,
            }
        ],
    }
