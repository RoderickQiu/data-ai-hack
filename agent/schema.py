"""The canonical job schema, the ATS normalizers, and the three hotdata tables.

This is the boundary between whoever is fetching boards and everything
downstream. A fetcher hands us the raw payload an ATS returned; ``normalize``
turns it into a ``Job``, and ``Job.row()`` is exactly what lands in
``jobs.public.jobs``. Nothing downstream ever sees a raw ATS shape, which is
what lets the ranking, the graph and the plays stay ATS-agnostic.

Three things are deliberate:

* ``description`` is sanitized on the way in, not on the way out. Board HTML is
  a string a stranger controls and it reaches Slack, a Sheet and a Doc; strip it
  once, at the only place every row passes through (DESIGN §4, XSS row).
* ``release_day`` is assigned by us and ``posted_at`` is kept untouched beside
  it, so the replay schedule is auditable and swappable for the true dates
  (DESIGN §5).
* Every id is stable across re-ingest: ``{source}:{company}:{native_id}``. Plays
  replay against stored payloads, and a re-run must not produce new ids.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Any, Iterable, Mapping

ATS_SOURCES = ("greenhouse", "lever", "ashby")

# Columns, in order, for the three hotdata tables (DESIGN §4 and §7).
JOBS_COLUMNS = (
    "id", "source", "ats_type", "company", "company_slug", "title", "location",
    "remote", "salary_min", "salary_max", "description", "url", "posted_at",
    "release_day", "first_seen_run", "last_seen_run",
    # No ATS board publishes these, and the worked preference rule in DESIGN
    # §6.3 is about company size. They come from insight/companies.py, and a job
    # whose company is not in that table simply has neither — which is correct:
    # a size rule must not match a company whose size we do not know.
    "company_size", "industry", "seniority",
)

APPLICATIONS_COLUMNS = (
    "id", "job_id", "event", "reason_tags", "reason", "at", "day", "run_id",
    "decided_by", "predicted", "actual",
)

RUNS_COLUMNS = (
    # `pipeline` is what makes a day separable from a row. P-A judges the day,
    # each P-B prepares a pack, and a day is all of them — without this column
    # `day` is not a key and nothing can roll a day up (docs/dashboard-backend-plan.md).
    "run_id", "started_at", "day", "mode", "pipeline",
    "wall_ms", "tokens_in", "tokens_out", "steps_reasoned", "steps_replayed", "plays_used",
    # The MCP-boundary proxy for cost, counted always and used when the
    # orchestrator does not report token usage. Stored beside the real thing
    # rather than instead of it, so a proxy can never be read as a token count.
    "tool_calls", "tool_bytes",
    "questions_asked", "human_touches",
    "values_from_memory", "values_replayed", "values_reasoned",
    "jobs_released_today", "pool_size", "shown", "predicted_keep", "actual_keep",
    "precision_at_5", "prediction_accuracy",
)

PIPELINES = ("P-A", "P-B", "P-C")

# Orchestrator token usage is read back from RocketRide *after* the run that
# spent it has already written its row, so it arrives as its own row keyed
# `<run_id>-usage` rather than as an edit to that row — correct whether hotdata
# treats `append` with a key as an upsert or as a plain append, which is not
# something we have verified (docs/dashboard-backend-plan.md §2 and §6).
#
# It is a cost attachment, not a run: every other counter on it is zero. Anything
# rolling runs up has to add its tokens but not count it as an invocation, so the
# suffix lives here rather than being spelled out in the writer and the reader
# separately — two copies of a convention like this drift, and the failure is a
# silently double-counted day.
USAGE_RUN_SUFFIX = "-usage"


def is_usage_row(row: Mapping[str, Any]) -> bool:
    """True for a cost-attachment row written by ``demo.loop.attribute_usage``."""
    return str(row.get("run_id") or "").endswith(USAGE_RUN_SUFFIX)

# hotdata infers a column's type from the first load, and a column that arrives
# as all-nulls becomes varchar — after which the first real integer is rejected
# with "can't change type from varchar to int64". So the bootstrap row that
# creates a table has to carry a correctly typed value in *every* column, not
# just the ones it has something to say about.
#
# The seed only fixes the *first* load; `COLUMN_TYPES` below is what fixes every
# load after it.
RUNS_SEED: dict[str, Any] = {
    "run_id": "bootstrap", "started_at": "1970-01-01T00:00:00+00:00", "day": 0,
    "mode": "first_run", "pipeline": "P-A",
    "wall_ms": 0, "tokens_in": 0, "tokens_out": 0,
    "steps_reasoned": 0, "steps_replayed": 0, "plays_used": "",
    "tool_calls": 0, "tool_bytes": 0,
    "questions_asked": 0, "human_touches": 0,
    "values_from_memory": 0, "values_replayed": 0, "values_reasoned": 0,
    "jobs_released_today": 0, "pool_size": 0, "shown": 0, "predicted_keep": 0,
    "actual_keep": 0, "precision_at_5": 0.0, "prediction_accuracy": 0.0,
}

APPLICATIONS_SEED: dict[str, Any] = {
    "id": "bootstrap", "job_id": "bootstrap", "event": "seen", "reason_tags": "",
    "reason": "", "at": "1970-01-01T00:00:00+00:00", "day": 0, "run_id": "bootstrap",
    "decided_by": "human", "predicted": "", "actual": "",
}

# The declared type of every column across the three tables.
#
# hotdata infers a column's type from the file it is loaded from, and that
# inference is per-load, not per-table: a column that arrives all-null in *one*
# load is inferred as varchar and the load is then rejected against the real
# column ("can't change type from float64 to varchar"). That is not a rare case
# — `precision_at_5` and `prediction_accuracy` are legitimately null on every
# run the human has not answered yet, which is most of them, so the run that
# should have started the chart is exactly the run that fails to log.
#
# Omitting the null columns is not a way out: hotdata rejects a short upload
# with "an append must carry every column the table has". So the load file has
# to *declare* its types instead of having them guessed, which means writing
# parquet rather than JSON (insight/store.py). This map is the declaration.
COLUMN_TYPES: dict[str, str] = {
    # jobs
    "id": "str", "source": "str", "ats_type": "str", "company": "str",
    "company_slug": "str", "title": "str", "location": "str", "remote": "bool",
    "salary_min": "int", "salary_max": "int", "description": "str", "url": "str",
    "posted_at": "str", "release_day": "int", "first_seen_run": "str",
    "last_seen_run": "str", "company_size": "int", "industry": "str",
    "seniority": "str",
    # applications
    "job_id": "str", "event": "str", "reason_tags": "str", "reason": "str",
    "at": "str", "day": "int", "run_id": "str", "decided_by": "str",
    "predicted": "str", "actual": "str",
    # runs
    "started_at": "str", "mode": "str", "pipeline": "str", "wall_ms": "int",
    "tokens_in": "int", "tokens_out": "int", "steps_reasoned": "int",
    "steps_replayed": "int", "plays_used": "str", "tool_calls": "int",
    "tool_bytes": "int", "questions_asked": "int", "human_touches": "int",
    "values_from_memory": "int", "values_replayed": "int", "values_reasoned": "int",
    "jobs_released_today": "int", "pool_size": "int", "shown": "int",
    "predicted_keep": "int", "actual_keep": "int",
    # The two that exposed all of this. Null on every unanswered run, and null
    # is the honest value — see agent/metrics.py on why neither may be 0.0.
    "precision_at_5": "float", "prediction_accuracy": "float",
}

APPLICATION_EVENTS = (
    "seen", "predicted_keep", "predicted_skip", "shortlisted", "applied",
    "replied", "rejected", "skipped", "not_for_me",
)

# The reason chips behind the only signal that teaches (DESIGN §6.3). Free text
# is allowed alongside them; Cognee turns it into more of these tags.
REASON_TAGS = (
    "too_senior", "too_junior", "company_too_large", "company_too_small",
    "wrong_domain", "location", "comp", "stack", "other",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_REMOTE_RE = re.compile(r"\bremote\b", re.I)
_SALARY_RE = re.compile(
    r"(?:[$£€]\s?)(\d{2,3})(?:[,.]?(\d{3}))?\s?(k\b)?\s*(?:-|–|to)\s*(?:[$£€]\s?)?(\d{2,3})(?:[,.]?(\d{3}))?\s?(k\b)?",
    re.I,
)


def sanitize_html(raw: str | None) -> str:
    """Strip board HTML down to plain text before it is ever stored.

    Scripts and styles go body-and-all; everything else loses its tags and keeps
    its text. Entities are unescaped once, after tag removal, so an encoded
    ``&lt;script&gt;`` cannot re-enter as markup.
    """
    if not raw:
        return ""
    text = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    text = _TAG_RE.sub(" ", text)  # anything an entity decoded back into markup
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def job_id(source: str, company_slug: str, native_id: Any) -> str:
    return f"{source}:{company_slug}:{native_id}"


def _parse_salary(*texts: str | None) -> tuple[int | None, int | None]:
    """Best-effort band from free text. ``None`` when the posting does not say.

    Deliberately conservative: a wrong band poisons the salary percentile, which
    is one of the three things only hotdata can answer.
    """
    for text in texts:
        if not text:
            continue
        match = _SALARY_RE.search(text)
        if not match:
            continue
        def _num(whole: str | None, thousands: str | None, k: str | None) -> int | None:
            if not whole:
                return None
            value = int(whole + (thousands or ""))
            if k or value < 1000:
                value *= 1000
            return value
        lo = _num(match.group(1), match.group(2), match.group(3))
        hi = _num(match.group(4), match.group(5), match.group(6) or match.group(3))
        if lo and hi and lo <= hi and 10_000 <= lo <= 1_000_000:
            return lo, hi
    return None, None


@dataclass
class Job:
    """One posting, in the only shape the rest of the system knows about."""

    id: str
    source: str            # greenhouse | lever | ashby | ...
    ats_type: str          # the ATS family, kept separate: a board can move host
    company: str
    company_slug: str
    title: str
    location: str = ""
    remote: bool = False
    salary_min: int | None = None
    salary_max: int | None = None
    description: str = ""
    url: str = ""
    posted_at: str | None = None      # real, from the board
    release_day: int | None = None    # ours, assigned by agent.clock
    first_seen_run: str | None = None
    last_seen_run: str | None = None
    company_size: int | None = None   # ours, from insight/companies.py
    industry: str | None = None       # ours, same
    seniority: str | None = None      # derived from the title
    raw: dict[str, Any] | None = None  # kept in data/raw, never loaded to hotdata

    def row(self) -> dict[str, Any]:
        """The hotdata row. ``raw`` is dropped; it lives in the parquet dump."""
        data = asdict(self)
        data.pop("raw", None)
        return {column: data[column] for column in JOBS_COLUMNS}

    def text_for_embedding(self) -> str:
        parts = [self.title, self.company, self.location, self.description[:4000]]
        return "\n".join(part for part in parts if part)

    def fingerprint(self) -> str:
        return hashlib.sha256(
            "|".join([self.source, self.company_slug, self.title]).encode()
        ).hexdigest()[:16]


# --- ATS normalizers -------------------------------------------------------
#
# One function per family, each taking the raw item exactly as the board's
# public API returns it. These are what `ingest-ats` replays: the first board of
# a family costs reasoning, the next fifty are a play (DESIGN §4, Rote).

def from_greenhouse(item: dict[str, Any], company: str, slug: str) -> Job:
    """``boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true``."""
    description = sanitize_html(item.get("content"))
    location = (item.get("location") or {}).get("name", "") or item.get("location__name", "") or ""
    lo, hi = _parse_salary(description)
    return Job(
        id=job_id("greenhouse", slug, item.get("id")),
        source="greenhouse",
        ats_type="greenhouse",
        company=company or item.get("company_name", "") or slug,
        company_slug=slug,
        title=item.get("title", ""),
        location=location,
        remote=bool(_REMOTE_RE.search(location) or _REMOTE_RE.search(item.get("title", ""))),
        salary_min=lo,
        salary_max=hi,
        description=description,
        url=item.get("absolute_url", ""),
        posted_at=item.get("first_published") or item.get("updated_at"),
        raw=item,
    )


def from_lever(item: dict[str, Any], company: str, slug: str) -> Job:
    """``api.lever.co/v0/postings/{slug}?mode=json``."""
    categories = item.get("categories") or {}
    description = sanitize_html(
        item.get("descriptionPlain") or item.get("description") or ""
    )
    location = categories.get("location", "") or ""
    lo, hi = _parse_salary(categories.get("commitment"), description)
    posted = item.get("createdAt")
    if isinstance(posted, (int, float)):  # Lever hands back epoch milliseconds
        posted = datetime.fromtimestamp(posted / 1000, tz=timezone.utc).isoformat()
    return Job(
        id=job_id("lever", slug, item.get("id")),
        source="lever",
        ats_type="lever",
        company=company or slug,
        company_slug=slug,
        title=item.get("text", ""),
        location=location,
        remote=bool(_REMOTE_RE.search(location) or _REMOTE_RE.search(item.get("workplaceType", "") or "")),
        salary_min=lo,
        salary_max=hi,
        description=description,
        url=item.get("hostedUrl", "") or item.get("applyUrl", ""),
        posted_at=posted,
        raw=item,
    )


def from_ashby(item: dict[str, Any], company: str, slug: str) -> Job:
    """``api.ashbyhq.com/posting-api/job-board/{slug}``."""
    description = sanitize_html(
        item.get("descriptionPlain") or item.get("descriptionHtml") or ""
    )
    compensation = item.get("compensation") or {}
    lo, hi = _parse_salary(compensation.get("compensationTierSummary"), description)
    return Job(
        id=job_id("ashby", slug, item.get("id")),
        source="ashby",
        ats_type="ashby",
        company=company or slug,
        company_slug=slug,
        title=item.get("title", ""),
        location=item.get("location", "") or "",
        remote=bool(item.get("isRemote")),
        salary_min=lo,
        salary_max=hi,
        description=description,
        url=item.get("jobUrl", "") or item.get("applyUrl", ""),
        posted_at=item.get("publishedAt") or item.get("updatedAt"),
        raw=item,
    )


_NORMALIZERS = {
    "greenhouse": from_greenhouse,
    "lever": from_lever,
    "ashby": from_ashby,
}


def normalize(source: str, item: dict[str, Any], company: str = "", slug: str = "") -> Job:
    """Turn one raw ATS item into a ``Job``. Raises on an unknown family."""
    normalizer = _NORMALIZERS.get(source)
    if normalizer is None:
        raise ValueError(f"no normalizer for source {source!r} (have {sorted(_NORMALIZERS)})")
    return normalizer(item, company or slug, slug or _slug(company))


def normalize_many(source: str, items: Iterable[dict[str, Any]], company: str = "",
                   slug: str = "") -> list[Job]:
    jobs, seen = [], set()
    for item in items:
        job = normalize(source, item, company, slug)
        if not job.title or job.id in seen:
            continue
        seen.add(job.id)
        jobs.append(job)
    return jobs


def ingest_fingerprint(source: str, sample: dict[str, Any]) -> str:
    """The Rote play fingerprint for an ingest run (DESIGN §4).

    ``hash(task_type + ats_family + sorted(normalized_field_names))``: a board
    that answers with the same field set replays, a board that answers with a
    different one is a partial match and only the novel fields cost tokens.
    """
    fields = ",".join(sorted(sample.keys()))
    return hashlib.sha256(f"ingest-ats|{source}|{fields}".encode()).hexdigest()[:16]


@dataclass
class ApplicationEvent:
    """One row of the event log. The chart's quality line is built from these."""

    job_id: str
    event: str
    at: str
    day: int
    run_id: str
    reason_tags: list[str] = field(default_factory=list)
    reason: str = ""
    decided_by: str = "human"          # human | agent  (DESIGN §8, inform-don't-consult)
    predicted: str | None = None       # keep | skip, what the agent guessed
    actual: str | None = None          # keep | skip, what the human did

    def row(self) -> dict[str, Any]:
        return {
            "id": hashlib.sha256(
                f"{self.run_id}|{self.job_id}|{self.event}|{self.at}".encode()
            ).hexdigest()[:24],
            "job_id": self.job_id,
            "event": self.event,
            "reason_tags": ",".join(self.reason_tags),
            "reason": self.reason[:500],
            "at": self.at,
            "day": self.day,
            "run_id": self.run_id,
            "decided_by": self.decided_by,
            "predicted": self.predicted,
            "actual": self.actual,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
