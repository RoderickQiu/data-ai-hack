"""Adopt an externally built jobs table into the canonical schema.

The fetchers are somebody else's half of the build, and their table is theirs:
as of hour 0 it is ``jobs.public.tech_jobs_20260911``, with ``job_id`` where we
say ``id``, three URL columns where we say one, and no ``release_day`` at all.

Rather than making every query downstream negotiate with whatever the fetcher
happened to name things, this projects their table once into the canonical
shape, assigns the release schedule, and loads ``jobs.public.jobs``. After that
the contract in :mod:`agent.schema` holds everywhere and the two halves can move
independently — they can rename a column, we re-run the projection.

The synonyms below are a mapping, not a guess: anything unmatched is reported
rather than silently defaulted, because a silently-null ``salary_max`` turns the
salary percentile into a lie.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agent.clock import DEFAULT_HORIZON, assign_release_days, release_histogram
from agent.schema import Job, sanitize_html
from insight.companies import coverage, profile
from memory.extract import seniority_of
from insight.hotdata import Hotdata
from insight.store import Insight

# canonical column -> the names a fetcher might have used, best first.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "id": ("id", "job_id"),
    "source": ("source", "ats", "ats_type", "provider"),
    "ats_type": ("ats_type", "source", "ats"),
    "company": ("company", "company_name", "org"),
    "company_slug": ("company_slug", "source_board", "board", "slug"),
    "title": ("title", "role", "name"),
    "location": ("location", "location__name", "city"),
    "remote": ("remote", "is_remote"),
    "salary_min": ("salary_min", "compensation_min", "min_salary"),
    "salary_max": ("salary_max", "compensation_max", "max_salary"),
    "description": ("description", "content", "descriptionPlain", "body"),
    "url": ("url", "apply_url", "absolute_url", "hostedUrl", "source_url"),
    "posted_at": ("posted_at", "first_published", "published_at", "created_at",
                  "source_created_at"),
    # If the fetcher already assigned the schedule, it is the team's schedule and
    # this must not quietly re-roll it: a play captured against day 7 replays
    # against day 7. Only a table without one gets days assigned here.
    "release_day": ("release_day",),
    "company_size": ("company_size", "headcount", "employees"),
    "industry": ("industry", "sector"),
}

# Fields no ATS board publishes. If the source happens to carry one it is used,
# but its absence is normal and not worth reporting as a gap: insight/companies
# fills them, and that file reports its own coverage.
DERIVED = ("company_size", "industry")


@dataclass
class ProjectionReport:
    source_table: str
    rows: int
    mapped: dict[str, str]
    unmapped: list[str]
    per_day: dict[int, int]
    schedule: str = ""
    company_coverage: dict[str, Any] | None = None
    loaded: dict[str, Any] | None = None

    def summary(self) -> str:
        thin = min(self.per_day.items(), key=lambda kv: kv[1]) if self.per_day else (0, 0)
        missing = f"; no column for {', '.join(self.unmapped)}" if self.unmapped else ""
        gaps = ""
        if self.company_coverage and self.company_coverage["missing"]:
            gaps = ("; no headcount for "
                    + ", ".join(self.company_coverage["missing"][:4])
                    + " (a size preference cannot match them)")
        return (f"{self.rows} rows projected from {self.source_table}; "
                f"{self.schedule}; thinnest day is day {thin[0]} with "
                f"{thin[1]}{missing}{gaps}")


def detect_mapping(columns: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    present = {column.lower(): column for column in columns}
    mapped, unmapped = {}, []
    for canonical, candidates in SYNONYMS.items():
        for candidate in candidates:
            if candidate.lower() in present:
                mapped[canonical] = present[candidate.lower()]
                break
        else:
            unmapped.append(canonical)
    return mapped, unmapped


def _release_day(row: Mapping[str, Any], mapping: Mapping[str, str]) -> int | None:
    column = mapping.get("release_day")
    if not column:
        return None
    return _int(row.get(column))


def _job_from(row: Mapping[str, Any], mapping: Mapping[str, str]) -> Job | None:
    def value(field: str, default: Any = None) -> Any:
        column = mapping.get(field)
        return row.get(column, default) if column else default

    job_id = value("id")
    title = value("title")
    if not job_id or not title:
        return None
    source = str(value("source") or str(job_id).split(":", 1)[0] or "unknown")
    company = str(value("company") or "")
    known = profile(company)
    slug = str(value("company_slug") or company.lower().replace(" ", "-"))
    return Job(
        id=str(job_id), source=source, ats_type=str(value("ats_type") or source),
        company=company, company_slug=slug, title=str(title),
        location=str(value("location") or ""),
        remote=bool(value("remote") or False),
        salary_min=_int(value("salary_min")), salary_max=_int(value("salary_max")),
        # Sanitized again on the way in: we do not know what the fetcher stored,
        # and this is the last point every row passes through.
        description=sanitize_html(value("description") or ""),
        url=str(value("url") or ""), posted_at=value("posted_at"),
        release_day=_release_day(row, mapping),
        # The source rarely carries these; the lookup fills what it can and
        # leaves the rest genuinely unknown.
        company_size=_int(value("company_size")) or known.get("company_size"),
        industry=str(value("industry") or known.get("industry") or "") or None,
        seniority=seniority_of(str(title)),
    )


def _int(value: Any) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def project(source_table: str, insight: Insight | None = None,
            horizon: int = DEFAULT_HORIZON, limit: int = 100_000,
            load: bool = True) -> ProjectionReport:
    """Read their table, write ours. Idempotent: the schedule is deterministic."""
    insight = insight or Insight()
    client: Hotdata = insight.client
    qualified = source_table if "." in source_table else client.qualified(source_table)

    sample = client.query(f"SELECT * FROM {qualified} LIMIT 1")
    if not sample:
        raise ValueError(f"{qualified} is empty — nothing to project")
    mapping, unmapped = detect_mapping(list(sample[0]))
    unmapped = [field for field in unmapped if field not in DERIVED]
    if "id" not in mapping or "title" not in mapping:
        raise ValueError(f"{qualified} has no id/title column; found {sorted(sample[0])}")

    columns = ", ".join(sorted(set(mapping.values())))
    rows = client.query(f"SELECT {columns} FROM {qualified} LIMIT {int(limit)}")
    jobs = [job for job in (_job_from(row, mapping) for row in rows) if job]
    unscheduled = [job for job in jobs if job.release_day is None]
    if unscheduled:
        # Either the source has no schedule at all, or it is partial. Assigning
        # over the whole set keeps one deterministic deal rather than mixing two.
        assign_release_days(jobs, horizon=horizon)
        report_note = f"assigned a schedule to {len(jobs)} rows"
    else:
        report_note = "kept the source's own release schedule"

    report = ProjectionReport(
        source_table=qualified, rows=len(jobs), mapped=mapping, unmapped=unmapped,
        per_day=release_histogram(jobs), schedule=report_note,
        company_coverage=coverage([job.company for job in jobs]),
    )
    if load:
        report.loaded = insight.load_corpus(jobs)
    return report
