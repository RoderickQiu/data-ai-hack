"""The insight layer's front door: named queries in, ids and summaries out.

Everything above this line asks for a query by name and gets records back.
Nothing above this line builds SQL, names a table, or sees a job description
unless it asked for one job by id.

The write side lives here too, because the three tables have exactly three
writers: the corpus build (``load_corpus``, once, at hour 0), the feedback
pipeline (``log_event``) and the run accountant (``log_run``). A fourth writer
would be a bug.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agent.config import TUNABLES, Settings
from agent.schema import (
    APPLICATIONS_COLUMNS,
    JOBS_COLUMNS,
    RUNS_COLUMNS,
    ApplicationEvent,
    Job,
)
from insight.hotdata import Hotdata, HotdataError
from insight.queries import bind_catalog, catalogue

# Index names are derived from the table, so pointing HOTDATA_JOBS_TABLE at a
# scratch copy does not collide with the team's indexes.
def _index_names(table: str) -> tuple[str, str]:
    return f"{table}_description_text", f"{table}_description_vector"


class Insight:
    def __init__(self, config: Settings | None = None, client: Hotdata | None = None):
        self.client = client or Hotdata(config)
        self.jobs_table = self.client.config.hotdata_jobs_table or "jobs"
        self.text_index, self.vector_index = _index_names(self.jobs_table)
        self.queries = bind_catalog(self.client.catalog, self.jobs_table)

    # -- read ------------------------------------------------------------

    def run(self, name: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        query = self.queries.get(name)
        if query is None:
            raise KeyError(f"unknown query {name!r}; have {sorted(self.queries)}")
        return self.client.query(query.render(params))

    def catalogue(self) -> list[dict[str, str]]:
        return catalogue()

    def judged_ids(self) -> list[str]:
        try:
            return [row["job_id"] for row in self.run("judged_ids")]
        except HotdataError:
            return []  # the applications table does not exist until the first signal

    def released_today(self, day: int) -> list[dict[str, Any]]:
        return self.run("released_today", {"day": day})

    def narrow(self, day: int, title_like: str = "", location: str = "",
               remote_only: bool = False, limit: int | None = None) -> list[dict[str, Any]]:
        """Stage 1 of the two-stage rank: SQL hard filters (DESIGN §4).

        Running the graph queries against a thousand rows would be slow and
        pointless; this is the narrowing that makes the graph's work cheap.
        """
        judged = self.judged_ids() or ["__none__"]
        return self.run("narrow", {
            "day": day,
            "judged": judged,
            "title_any": title_like,
            "title_like": f"%{title_like.lower()}%" if title_like else "%",
            "remote_only": remote_only,
            "location": location,
            "location_like": f"%{location.lower()}%" if location else "%",
            "limit": limit or TUNABLES.narrow_top_k * 4,
        })

    def search(self, text: str, limit: int = TUNABLES.narrow_top_k,
               index: str | None = None) -> list[dict[str, Any]]:
        """Semantic/BM25 top-k over descriptions, with the payload left behind.

        Falls back from the vector index to the BM25 one: an embedding provider
        may not be configured on the workspace, and a missing index must not
        take the ranking pass down with it.
        """
        candidates = [index] if index else [self.vector_index, self.text_index]
        last: Exception | None = None
        for name in candidates:
            try:
                hits = self.client.search(text, index=name, limit=limit,
                                          select="id,company,title,location")
                return hits[:limit]
            except HotdataError as exc:
                last = exc
        if last:
            raise last
        return []

    def salary_percentile(self, title: str, location: str = "") -> dict[str, Any]:
        rows = self.run("salary_percentile", {
            "title_like": f"%{title.lower()}%",
            "location": location,
            "location_like": f"%{location.lower()}%" if location else "%",
        })
        return rows[0] if rows else {"postings": 0, "p50": None, "p25": None, "p75": None}

    # -- write -----------------------------------------------------------

    def load_corpus(self, jobs: Sequence[Job], mode: str = "replace") -> dict[str, Any]:
        """Load the frozen corpus into ``jobs``. Called once, at hour 0.

        Written as newline-free JSON through a temp file rather than row-by-row:
        a thousand single-row loads is a thousand round trips, and the corpus
        build is the one place where the whole table is known at once.
        """
        rows = [job.row() for job in jobs]
        missing_day = [row["id"] for row in rows if row.get("release_day") is None]
        if missing_day:
            raise ValueError(
                f"{len(missing_day)} job(s) have no release_day — run "
                f"agent.clock.assign_release_days first (e.g. {missing_day[0]})"
            )
        return self._load_rows(self.jobs_table, rows, JOBS_COLUMNS, mode=mode, key="id")

    def log_event(self, event: ApplicationEvent) -> dict[str, Any]:
        return self.log_events([event])

    def log_events(self, events: Iterable[ApplicationEvent]) -> dict[str, Any]:
        rows = [event.row() for event in events]
        if not rows:
            return {"loaded": 0}
        return self._load_rows("applications", rows, APPLICATIONS_COLUMNS,
                               mode="append", key="id")

    def log_run(self, run_row: Mapping[str, Any]) -> dict[str, Any]:
        """One row per run. This table *is* the proof (DESIGN §7).

        Honesty rule: only numbers the system actually logged go in here. No
        interpolated points, no back-filled runs.
        """
        row = {column: run_row.get(column) for column in RUNS_COLUMNS}
        return self._load_rows("runs", [row], RUNS_COLUMNS, mode="append", key="run_id")

    def _load_rows(self, table: str, rows: Sequence[Mapping[str, Any]],
                   columns: Sequence[str], mode: str, key: str | None) -> dict[str, Any]:
        payload = [{column: row.get(column) for column in columns} for row in rows]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle, default=str)
            path = Path(handle.name)
        try:
            result = self.client.load_file(path, table=table, mode=mode, key=key)
        except HotdataError as exc:
            # `append` onto a table that does not exist yet is the first-signal
            # case; declare it and retry once rather than making every caller
            # handle a cold start.
            if mode == "append" and "not found" in str(exc).lower():
                self.client.load_file(path, table=table, mode="replace", key=key)
                result = {"created": table, "rows": len(payload)}
            else:
                raise
        finally:
            path.unlink(missing_ok=True)
        return {"table": table, "rows": len(payload), "result": result}

    # -- indexes ---------------------------------------------------------

    def ensure_indexes(self, embedding_provider: str | None = None) -> dict[str, str]:
        """BM25 always, vector when the workspace has an embedding provider.

        Both are on ``jobs.description``. The BM25 one is what the candidate
        summary search falls back to, so it is created first and never skipped.
        """
        # `search list` reports the name under "index_name".
        existing = {index.get("index_name") or index.get("name") for index in
                    self.client.search_indexes(table=self.jobs_table)}
        table = self.client.qualified(self.jobs_table)
        created = {}
        if self.text_index not in existing:
            self.client.create_index(self.text_index, table, "description", kind="text")
            created[self.text_index] = "created"
        if embedding_provider and self.vector_index not in existing:
            self.client.create_index(self.vector_index, table, "description",
                                     kind="vector", provider=embedding_provider)
            created[self.vector_index] = "created"
        return created or {"indexes": "already present"}
