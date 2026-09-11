"""Headcount and industry for the companies in the corpus.

**Why this file exists.** The worked example in DESIGN §6.3 is the company-size
rule — "I think you prefer companies under 2,000 people, three not-for-mes in a
row" — and `candidate/README.md` deliberately leaves that preference out of the
seed so the agent has to infer it. But no ATS board publishes headcount, so
without this table the rule is proposed, confirmed by the human, and then
matches nothing: :meth:`memory.prefs.Rule.matches` returns ``False`` when the
field is absent, so the shortlist does not move and P2 quietly fails on stage.
An inert preference is worse than no preference, because it looks like it worked.

**What it is, and how to say it.** Public, approximate headcounts as of 2026,
rounded to the granularity a human would say out loud. They are ours, like the
release schedule, and belong in the same honesty column: the jobs are real, the
headcounts are a lookup we wrote, and nothing on the chart depends on them being
exact — a rule about "under 2,000 people" needs to sort Linear from Stripe, not
to know either number to three digits.

Adding a company costs one line. A company with no entry simply has no
``company_size``, and a size rule will not match it — which is the right
behaviour: not knowing is not the same as knowing it is small.
"""

from __future__ import annotations

from typing import Any, Mapping

# company (as it appears in the corpus) -> (approximate headcount, industry)
COMPANIES: dict[str, tuple[int, str]] = {
    "openai": (3000, "ai"),
    "anthropic": (2000, "ai"),
    "databricks": (7000, "data infrastructure"),
    "stripe": (8000, "fintech"),
    "cloudflare": (4000, "infrastructure"),
    "palantir": (4000, "enterprise software"),
    "reddit": (2000, "consumer social"),
    "robinhood": (2000, "fintech"),
    "mongodb": (5000, "data infrastructure"),
    "figma": (1500, "design tools"),
    "perplexity": (500, "ai"),
    "ramp": (1000, "fintech"),
    "vanta": (900, "security"),
    "replit": (200, "developer tools"),
    "notion": (900, "productivity"),
    "discord": (1000, "consumer social"),
    "duolingo": (900, "education"),
    "spotify": (7000, "consumer media"),
    "linear": (100, "developer tools"),
}


def profile(company: str) -> dict[str, Any]:
    """``{company_size, industry}`` for a company, or ``{}`` if we do not know."""
    entry = COMPANIES.get((company or "").strip().lower())
    if entry is None:
        return {}
    size, industry = entry
    return {"company_size": size, "industry": industry}


def enrich(row: Mapping[str, Any]) -> dict[str, Any]:
    """Add the profile to a job row, without overwriting anything already there."""
    enriched = dict(row)
    for key, value in profile(str(row.get("company") or "")).items():
        enriched.setdefault(key, value)
    return enriched


def coverage(companies: list[str]) -> dict[str, Any]:
    """Which companies in a corpus have no entry. Run it after every re-ingest:
    a new board with no headcount is a silent hole in the preference rules."""
    missing = sorted({c for c in companies if (c or "").strip().lower() not in COMPANIES})
    known = len(companies) - len([c for c in companies
                                  if (c or "").strip().lower() not in COMPANIES])
    return {"known": known, "missing": missing}
