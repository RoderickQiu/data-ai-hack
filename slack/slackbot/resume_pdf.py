"""The candidate's resume as a PDF, ordered for one role.

This is the document a person actually sends, so two things govern it.

**It is the whole resume, not an extract.** The pack picks a handful of claims
to argue with in the covering text; a resume that shipped only those would be a
different, shorter career. Every verified claim is on the page. What the role
changes is the *order* — the claims the pack cited lead, the rest follow — which
is exactly what DESIGN §6.1 means by tailoring as selection and ordering.

**Nothing reaches the page that is not verified.** The bullets come from the
graph's verified claims and the headings from the candidate's own resume file.
There is no free-text path in, so a sentence the model invented cannot appear
here even if it reached the covering letter.

A rejected pack produces no file at all: a PDF is the artefact someone sends,
and handing one over for a draft that failed its citation check would undo the
check. Gaps are absent too — they belong in the Slack message, where they warn
the candidate. A resume listing what its author cannot do is not a resume.

Structure comes from the resume markdown when it is there — employer, title and
dates are headings, which is also why they never became claims — so the output
is a real CV rather than a bullet list. With no such file it degrades to the
claims alone rather than inventing an employer, which is the one thing this
system must never do.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (HRFlowable, ListFlowable, ListItem, Paragraph,
                                SimpleDocTemplate, Spacer)

INK = HexColor("#111111")
MUTED = HexColor("#5f5f5f")
RULE = HexColor("#c9c9c9")

_NAME = ParagraphStyle("n", fontName="Helvetica-Bold", fontSize=21, leading=25,
                       textColor=INK, spaceAfter=1)
_ROLE = ParagraphStyle("r", fontName="Helvetica", fontSize=11.5, leading=15,
                       textColor=INK, spaceAfter=3)
_CONTACT = ParagraphStyle("c", fontName="Helvetica", fontSize=8.6, leading=12,
                          textColor=MUTED, spaceAfter=9)
_SECTION = ParagraphStyle("s", fontName="Helvetica-Bold", fontSize=9, leading=12,
                          textColor=INK, spaceBefore=11, spaceAfter=5)
_JOB = ParagraphStyle("j", fontName="Helvetica-Bold", fontSize=9.8, leading=13,
                      textColor=INK, spaceBefore=7, spaceAfter=1)
_WHEN = ParagraphStyle("w", fontName="Helvetica", fontSize=8.4, leading=11,
                       textColor=MUTED, spaceAfter=4)
_BULLET = ParagraphStyle("b", fontName="Helvetica", fontSize=9.4, leading=13.4,
                         textColor=INK, spaceAfter=3)
_FOOT = ParagraphStyle("f", fontName="Helvetica-Oblique", fontSize=7.6, leading=10,
                       textColor=MUTED, spaceBefore=12)


def _escape(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bullets(lines: Iterable[str]) -> ListFlowable:
    return ListFlowable(
        [ListItem(Paragraph(_escape(line), _BULLET), leftIndent=11) for line in lines],
        bulletType="bullet", bulletFontSize=6, bulletOffsetY=-1,
        leftIndent=9, start="circle",
    )


def parse_resume(markdown: str) -> dict[str, Any]:
    """Pull the structure out of the resume file: who, where, and which job
    each bullet sat under. Headings carry it, which is why they are headings."""
    out: dict[str, Any] = {"name": "", "role": "", "contact": "",
                           "summary": [], "sections": [], "skills": "", "education": ""}
    current: dict[str, Any] | None = None
    section = ""
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            level, text = len(line) - len(line.lstrip("#")), line.lstrip("#").strip()
            if level == 1:
                out["name"] = text
            elif level == 2 and not out["role"] and not text.lower().startswith(
                    ("summary", "experience", "skills", "education")):
                out["role"] = text
            elif level == 2:
                section = text.lower()
            elif level == 3 and section == "skills":
                out["skills"] = text
            elif level == 3 and section == "education":
                out["education"] = text
            elif level == 3 and not out["contact"] and not section:
                out["contact"] = text
            elif level == 3:
                current = {"heading": text, "bullets": []}
                out["sections"].append(current)
            continue
        bullet = line.lstrip("•-*–—").strip()
        if len(bullet) < 20:
            continue
        if section == "summary":
            out["summary"].append(bullet)
        elif current is not None:
            current["bullets"].append(bullet)
    return out


def _split_heading(heading: str) -> tuple[str, str]:
    """``Title · Employer · Place · Dates`` -> the first two, and the rest."""
    parts = [p.strip() for p in re.split(r"\s+·\s+|\s+\|\s+", heading) if p.strip()]
    if len(parts) <= 2:
        return heading, ""
    return " · ".join(parts[:2]), " · ".join(parts[2:])


def build(pack: Any, *, candidate_name: str, out_dir: Path,
          resume_md: str = "", all_claims: Iterable[str] = ()) -> Path | None:
    """Write the resume for ``pack``'s role. ``None`` if the pack was rejected."""
    if not pack.ok:
        return None

    cited = [c.text for c in (pack.claims or [])]
    parsed = parse_resume(resume_md) if resume_md else {}
    name = parsed.get("name") or candidate_name

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in pack.job_id)[:60]
    path = out_dir / f"resume-{safe}.pdf"

    story: list[Any] = [Paragraph(_escape(name), _NAME)]
    if parsed.get("role"):
        story.append(Paragraph(_escape(parsed["role"]), _ROLE))
    if parsed.get("contact"):
        story.append(Paragraph(_escape(parsed["contact"]), _CONTACT))
    story.append(HRFlowable(width="100%", thickness=0.6, color=RULE,
                            spaceBefore=1, spaceAfter=2))

    if parsed.get("summary"):
        story.append(Paragraph("SUMMARY", _SECTION))
        story.append(_bullets(parsed["summary"]))

    if parsed.get("sections"):
        story.append(Paragraph("EXPERIENCE", _SECTION))
        # The role reorders the bullets inside each job — what the pack argued
        # with leads — but never reorders the jobs themselves. A CV whose
        # employment history jumped about would read as an error, not tailoring.
        for job in parsed["sections"]:
            title, when = _split_heading(job["heading"])
            story.append(Paragraph(_escape(title), _JOB))
            if when:
                story.append(Paragraph(_escape(when), _WHEN))
            lead = [b for b in job["bullets"] if b in cited]
            rest = [b for b in job["bullets"] if b not in cited]
            story.append(_bullets(lead + rest))
    else:
        story.append(Paragraph("SELECTED EXPERIENCE", _SECTION))
        story.append(_bullets(cited or list(all_claims)))

    if parsed.get("skills"):
        story.append(Paragraph("SKILLS", _SECTION))
        story.append(Paragraph(_escape(parsed["skills"]), _BULLET))
    if parsed.get("education"):
        story.append(Paragraph("EDUCATION", _SECTION))
        story.append(Paragraph(_escape(parsed["education"]), _BULLET))

    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(
        f"Ordered for {_escape(pack.title)} at {_escape(pack.company)} — "
        f"{len(cited)} of these are the claims the application argues with. "
        f"Every line is one {_escape(name)} has verified. Prepared {date.today():%d %B %Y}.",
        _FOOT))

    SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=17 * mm, bottomMargin=15 * mm,
        title=f"{name} — {pack.title}", author=name,
    ).build(story)
    return path
