"""Hour 0: freeze the corpus, assign the release schedule, close the network.

The fetching itself is not here — whoever is pulling boards hands us raw ATS
payloads and this turns them into the frozen corpus. The contract is one
function: :func:`build_corpus` takes ``{source, company, slug, items}`` batches,
normalizes them, stamps the schedule, writes the parquet, and loads hotdata.

**Nothing fetches anything after this runs.** The raw payloads are kept under
``data/raw/`` because they are the only copy: an ingest play can be re-run
offline against a stored response, which is what lets a replay be demonstrated
in front of the room without touching the network.

Honesty, stated here because it has to be stated on the slide too (DESIGN §5):

===================================== ===============================
the jobs, titles, descriptions, bands real, from live public ATS APIs
the release schedule                  ours, to replay a static corpus
every number on the chart             real, logged by runs that happened
===================================== ===============================

The schedule is the only synthetic element in the build. It affects what the
agent *sees* and never what it *does*, and no metric depends on it being true.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agent.clock import DEFAULT_HORIZON, assign_release_days, release_histogram
from agent.config import RAW_DIR, DATA_DIR
from agent.schema import Job, normalize_many
from insight.store import Insight


@dataclass
class CorpusReport:
    jobs: int
    companies: int
    sources: dict[str, int]
    per_day: dict[int, int]
    thinnest_day: tuple[int, int]
    with_salary: int
    parquet: str | None = None
    loaded: dict[str, Any] | None = None

    def ok(self, min_jobs: int = 1000, min_per_day: int = 10) -> bool:
        """Hour-0 gate: enough rows for a real percentile, and no thin day.

        Under 200 rows a salary percentile is noise and a judge is right to ask
        why it is not a graph query. The per-day floor is what stops the chart
        being lumpy for reasons that have nothing to do with the agent.
        """
        return self.jobs >= min_jobs and self.thinnest_day[1] >= min_per_day

    def summary(self) -> str:
        day, count = self.thinnest_day
        return (f"{self.jobs} jobs from {self.companies} companies across "
                f"{len(self.sources)} ATS families; thinnest day is day {day} "
                f"with {count}; {self.with_salary} carry a salary band")


def save_raw(source: str, slug: str, payload: Any) -> Path:
    """Keep the raw payload. It is the only copy and it makes offline replay real."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"{source}__{slug}.json"
    path.write_text(json.dumps(payload, default=str))
    return path


def load_raw() -> list[tuple[str, str, Any]]:
    """Every stored payload, for re-running the build without the network."""
    out = []
    for path in sorted(RAW_DIR.glob("*.json")):
        source, _, slug = path.stem.partition("__")
        out.append((source, slug, json.loads(path.read_text())))
    return out


def jobs_from_batches(batches: Iterable[Mapping[str, Any]]) -> list[Job]:
    """``{source, company, slug, items}`` in, canonical jobs out.

    Deduplicated on the canonical id, so re-running against a board that has not
    changed produces the same corpus rather than a doubled one.
    """
    jobs: dict[str, Job] = {}
    for batch in batches:
        source = batch["source"]
        slug = batch.get("slug") or batch.get("company", "")
        company = batch.get("company") or slug
        for job in normalize_many(source, batch.get("items") or [], company, slug):
            jobs[job.id] = job
    return list(jobs.values())


def write_parquet(jobs: Sequence[Job], path: Path | None = None) -> Path | None:
    """Freeze the corpus to disk. Falls back to JSON if pyarrow is absent."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = path or DATA_DIR / "corpus.parquet"
    rows = [job.row() for job in jobs]
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        fallback = target.with_suffix(".json")
        fallback.write_text(json.dumps(rows, default=str))
        return fallback
    pq.write_table(pa.Table.from_pylist(rows), target)
    return target


def build_corpus(batches: Iterable[Mapping[str, Any]], insight: Insight | None = None,
                 horizon: int = DEFAULT_HORIZON, load: bool = True,
                 keep_raw: bool = True) -> CorpusReport:
    """The whole hour-0 step, in one call."""
    batches = list(batches)
    if keep_raw:
        for batch in batches:
            save_raw(batch["source"], batch.get("slug") or batch.get("company", ""),
                     batch.get("items") or [])

    jobs = jobs_from_batches(batches)
    if not jobs:
        raise ValueError("no jobs normalized — check the source names and payload shapes")
    assign_release_days(jobs, horizon=horizon)

    per_day = release_histogram(jobs)
    sources: dict[str, int] = {}
    for job in jobs:
        sources[job.source] = sources.get(job.source, 0) + 1

    report = CorpusReport(
        jobs=len(jobs),
        companies=len({job.company_slug for job in jobs}),
        sources=sources,
        per_day=per_day,
        thinnest_day=min(per_day.items(), key=lambda item: item[1]) if per_day else (0, 0),
        with_salary=sum(1 for job in jobs if job.salary_max),
        parquet=str(write_parquet(jobs)),
    )

    if load:
        insight = insight or Insight()
        report.loaded = insight.load_corpus(jobs)
        insight.ensure_indexes()
    return report
