"""Realistic payloads so the Slack surface can be built and rehearsed before any
other layer exists. Shapes here are the contract teammates post against."""

DIGEST_COLD = {
    "run_id": "run-001",
    "day": "1",
    "mode": "first_run",
    "tokens": 14820,
    "wall_ms": 21400,
    "questions_asked": 6,
    "jobs": [
        {
            "job_id": "gh-stripe-4411",
            "title": "Senior Product Manager, Payments",
            "company": "Stripe",
            "location": "San Francisco · Hybrid",
            "salary": "$196k–$248k",
            "url": "https://stripe.com/jobs/listing/4411",
            "why": "Matches your title and metro. I don't know much about you yet.",
            "prediction": "keep",
            "confidence": 0.41,
        },
        {
            "job_id": "lev-ramp-882",
            "title": "Group PM, Risk Platform",
            "company": "Ramp",
            "location": "New York · Remote OK",
            "salary": "$210k–$260k",
            "why": "Title keyword overlap only.",
            "prediction": "keep",
            "confidence": 0.38,
        },
        {
            "job_id": "ash-vanta-210",
            "title": "Staff Product Manager, Compliance",
            "company": "Vanta",
            "location": "Remote (US)",
            "salary": "$205k–$245k",
            "why": "Seniority is one step above your last role.",
            "prediction": "keep",
            "confidence": 0.35,
        },
    ],
}

DIGEST_WARM = {
    "run_id": "run-019",
    "day": "14",
    "mode": "full_replay",
    "tokens": 1240,
    "wall_ms": 3100,
    "questions_asked": 0,
    "jobs": [
        {
            "job_id": "gh-linear-77",
            "title": "Senior PM, Integrations",
            "company": "Linear",
            "location": "Remote (US/EU)",
            "salary": "$190k–$230k",
            "why": (
                "You have verified claims covering 4 of 5 requirements, the warmest path runs through "
                "the Stripe role that replied to you on day 6, and it sits under your 2,000-person "
                "company-size rule."
            ),
            "prediction": "keep",
            "confidence": 0.88,
            "coverage": "4 of 5 requirements have verified claims",
        },
        {
            "job_id": "lev-mercury-31",
            "title": "Principal PM, Payments Infrastructure",
            "company": "Mercury",
            "location": "San Francisco",
            "salary": "$235k–$280k",
            "why": (
                "Strong claim coverage, but you marked the staff-level variant of this title "
                "'too senior' on day 6 and again on day 9."
            ),
            "prediction": "skip",
            "confidence": 0.79,
            "coverage": "5 of 6 requirements have verified claims",
        },
        {
            "job_id": "ash-deel-914",
            "title": "Product Manager, Payroll",
            "company": "Deel",
            "location": "Remote (Global)",
            "salary": "$160k–$195k",
            "why": "Pay sits at the 31st percentile for this title across 1,043 live postings.",
            "prediction": "skip",
            "confidence": 0.72,
            "off_slate": True,
        },
    ],
}

PACK_COLD = {
    "run_id": "run-001",
    "job_id": "gh-stripe-4411",
    "title": "Senior Product Manager, Payments",
    "company": "Stripe",
    "play": "apply-pack v1 (first run)",
    "tokens": 8900,
    "summary": (
        "Eight years shipping payments and risk products, most recently owning a ledger migration "
        "that moved $2.1B in annualised volume without a reconciliation break. `[claim:C-104]` "
        "Led the team that cut chargeback rate by 38% in four quarters. `[claim:C-117]`"
    ),
    "claims": [
        {"claim_id": "C-104", "text": "Owned ledger migration moving $2.1B annualised volume, zero reconciliation breaks"},
        {"claim_id": "C-117", "text": "Cut chargeback rate 38% over four quarters"},
    ],
    "gaps": [
        {"skill": "Kubernetes", "note": "no verified claim — the pack leads with your adjacent Terraform work instead"}
    ],
    "questions": [
        {"question_id": "q-work-auth", "text": "Are you authorised to work in the US without sponsorship?"},
        {"question_id": "q-notice", "text": "What notice period are you on?"},
        {"question_id": "q-comp", "text": "What base salary band are you targeting?"},
    ],
    "answers_from_memory": 0,
}

PACK_WARM = {
    "run_id": "run-019",
    "job_id": "gh-linear-77",
    "title": "Senior PM, Integrations",
    "company": "Linear",
    "play": "apply-pack v3 (full replay)",
    "tokens": 1180,
    "summary": (
        "Eight years shipping integration and platform surfaces, most recently an API partner programme "
        "that took third-party integrations from 12 to 96 in a year. `[claim:C-131]` Ran the migration "
        "that consolidated four bespoke webhook stacks into one. `[claim:C-142]`"
    ),
    "claims": [
        {"claim_id": "C-131", "text": "Grew third-party integrations 12 → 96 in one year via an API partner programme"},
        {"claim_id": "C-142", "text": "Consolidated four bespoke webhook stacks into a single delivery service"},
    ],
    "gaps": [{"skill": "Kubernetes", "note": "still unclaimed — flagged rather than implied"}],
    "questions": [],
    "answers_from_memory": 6,
    "sheet_url": "https://docs.google.com/spreadsheets/d/EXAMPLE",
}

PREFERENCE = {
    "run_id": "run-012",
    "preference_id": "pref-size-2000",
    "rule_text": "You prefer companies under 2,000 people",
    "rule": {"field": "company_size_bucket", "op": "lt", "value": 2000, "effect": "penalty", "weight": 0.5},
    "evidence": [
        "Staff PM, Compliance at Vanta — 'company too large' (day 8)",
        "Group PM, Risk Platform at Ramp — 'company too large' (day 10)",
        "Senior PM, Billing at Workday — 'company too large' (day 11)",
    ],
    "confidence": 0.82,
}

AUTONOMY = {
    "run_id": "run-019",
    "domain": "shortlisting",
    "headline": "I'd like to prepare packs for my own top picks without asking first.",
    "agreed": 13,
    "total": 15,
    "accuracy": 0.87,
}

CLAIMS = {
    "run_id": "run-001",
    "claims": [
        {
            "claim_id": "C-201",
            "text": "Comfortable operating as the sole PM on an infrastructure team",
            "source_doc": "preferences.txt",
        },
        {
            "claim_id": "C-202",
            "text": "Has run pricing experiments end to end, including the analysis",
            "source_doc": "cover-letter-2025.pdf",
        },
    ],
}

QUESTION = {
    "run_id": "run-003",
    "question_id": "q-relocate",
    "text": "Would you relocate for the right role?",
    "job_id": "gh-stripe-4411",
    "context": "Asked because the Stripe role is hybrid in San Francisco and your base is Seattle.",
}
