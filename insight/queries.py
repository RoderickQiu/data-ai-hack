"""The named SQL registry. The agent asks for a name; it never writes SQL.

``hotdata_query(name, params)`` over this table, never ``hotdata_sql(text)``.
Three reasons, all pointing the same way (DESIGN §4):

* the agent stops spending reasoning tokens composing a query on every run,
  which is line 1 of the chart;
* a named query is trivially a Rote play — same name, same params, same result;
* ``snyk code test`` never gets an LLM-driven injection surface to flag.

hotdata's CLI takes SQL text and has no bind-parameter form, so the templates
below are rendered with :func:`_literal`, which is the only place a caller's
value becomes SQL. It accepts strings, numbers, booleans, ``None`` and flat
lists of those, and nothing else — a dict, an object or a nested list raises
rather than being coerced. That is the whole injection surface, in one function,
with a test on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from agent.config import TUNABLES

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _literal(value: Any) -> str:
    """Render one Python value as a SQL literal. The only SQL-building step."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite float is not a SQL literal")
        return repr(value)
    if isinstance(value, str):
        cleaned = _CONTROL_RE.sub("", value)
        return "'" + cleaned.replace("'", "''") + "'"
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if not items:
            # An empty IN () is a syntax error in most engines; NULL never
            # matches, which is the semantics every caller here wants.
            return "(NULL)"
        if any(isinstance(item, (list, tuple, set, dict)) for item in items):
            raise ValueError("nested sequences are not SQL literals")
        return "(" + ", ".join(_literal(item) for item in items) + ")"
    raise ValueError(f"{type(value).__name__} is not renderable as a SQL literal")


@dataclass(frozen=True)
class NamedQuery:
    name: str
    sql: str
    params: tuple[str, ...]
    defaults: Mapping[str, Any]
    summary: str

    def render(self, params: Mapping[str, Any] | None = None) -> str:
        values = {**self.defaults, **(params or {})}
        missing = [p for p in self.params if p not in values]
        if missing:
            raise KeyError(f"query {self.name!r} needs {missing}")
        extra = [k for k in values if k not in self.params]
        if extra:
            raise KeyError(f"query {self.name!r} does not take {extra}")
        return self.sql.format(**{key: _literal(values[key]) for key in self.params})


def _q(name: str, sql: str, params: tuple[str, ...] = (), summary: str = "",
       **defaults: Any) -> NamedQuery:
    return NamedQuery(name=name, sql=" ".join(sql.split()), params=params,
                      defaults=defaults, summary=summary)


# ``{J}``/``{A}``/``{R}`` are filled by :func:`bind_catalog` at import of the
# registry, not by a caller — table names are never a parameter.
_TEMPLATES: tuple[NamedQuery, ...] = (
    _q(
        "released_today",
        """SELECT id, source, company, title, location, remote, salary_min, salary_max,
                  company_size, industry, seniority, url
           FROM {J} WHERE release_day = {day} ORDER BY company, title LIMIT {limit}""",
        ("day", "limit"), "Roles the schedule releases on this day.", limit=200,
    ),
    _q(
        "pool",
        """SELECT id, source, company, title, location, remote, salary_min, salary_max,
                  company_size, industry, seniority, release_day, url
           FROM {J}
           WHERE release_day <= {day} AND id NOT IN {judged}
           ORDER BY release_day DESC, company LIMIT {limit}""",
        ("day", "judged", "limit"),
        "Everything released so far that the human has not judged yet.",
        limit=400,
    ),
    _q(
        "narrow",
        """SELECT id, source, company, title, location, remote, salary_min, salary_max,
                  company_size, industry, seniority, release_day, url,
                  substr(description, 1, 400) AS blurb
           FROM {J}
           WHERE release_day <= {day}
             AND id NOT IN {judged}
             AND ({title_any} = '' OR lower(title) LIKE {title_like})
             AND ({remote_only} = FALSE OR remote = TRUE)
             AND ({location} = '' OR lower(location) LIKE {location_like})
             AND lower(coalesce(description,'')) NOT LIKE '%no visa sponsorship%'
             AND lower(coalesce(description,'')) NOT LIKE '%not able to sponsor%'
           ORDER BY release_day DESC, salary_max DESC NULLS LAST
           LIMIT {limit}""",
        ("day", "judged", "title_any", "title_like", "remote_only", "location",
         "location_like", "limit"),
        "Stage 1 of ranking: SQL hard filters. HydraDB judges what survives.",
        title_any="", title_like="%", remote_only=False, location="", location_like="%",
        limit=TUNABLES.narrow_top_k * 4,
    ),
    _q(
        "job",
        """SELECT id, source, ats_type, company, title, location, remote,
                  salary_min, salary_max, company_size, industry, seniority,
                  url, posted_at, release_day, description
           FROM {J} WHERE id = {job_id}""",
        ("job_id",),
        "Full text for one job. The only query that returns a description.",
    ),
    _q(
        "jobs_by_id",
        """SELECT id, source, company, title, location, remote, salary_min, salary_max,
                  company_size, industry, seniority, release_day, url,
                  substr(description, 1, 600) AS blurb
           FROM {J} WHERE id IN {job_ids}""",
        ("job_ids",), "Summaries for a set of ids, no full descriptions.",
    ),
    _q(
        "descriptions_by_id",
        """SELECT id, substr(description, 1, 6000) AS description
           FROM {J} WHERE id IN {job_ids}""",
        ("job_ids",),
        "Descriptions for the shortlist, for the one-time requirement extraction. "
        "Internal to the ranking pass; never returned to the agent.",
    ),
    _q(
        "salary_percentile",
        """SELECT count(*) AS postings,
                  approx_percentile_cont(salary_max, 0.5) AS p50,
                  approx_percentile_cont(salary_max, 0.25) AS p25,
                  approx_percentile_cont(salary_max, 0.75) AS p75
           FROM {J}
           WHERE salary_max IS NOT NULL AND lower(title) LIKE {title_like}
             AND ({location} = '' OR lower(location) LIKE {location_like})""",
        ("title_like", "location", "location_like"),
        "What this title pays across the live corpus. Needs volume to mean anything.",
        location="", location_like="%",
    ),
    _q(
        "hiring_wave",
        """SELECT company, count(*) AS opened, min(release_day) AS since
           FROM {J}
           WHERE release_day BETWEEN {day} - {window} AND {day}
           GROUP BY company HAVING count(*) >= {min_roles}
           ORDER BY opened DESC LIMIT {limit}""",
        ("day", "window", "min_roles", "limit"),
        "Companies that opened several matching roles at once.",
        window=7, min_roles=5, limit=10,
    ),
    _q(
        "reply_rate_by_source",
        """SELECT j.source,
                  count(DISTINCT CASE WHEN a.event = 'applied' THEN a.job_id END) AS applied,
                  count(DISTINCT CASE WHEN a.event = 'replied' THEN a.job_id END) AS replied
           FROM {A} a JOIN {J} j ON j.id = a.job_id
           GROUP BY j.source ORDER BY replied DESC""",
        (), "Which board actually answers this candidate. Needs application depth.",
    ),
    _q(
        "judged_ids",
        """SELECT DISTINCT job_id FROM {A}
           WHERE run_id <> 'bootstrap'
             AND event IN ('keep','shortlisted','applied','skipped','not_for_me','rejected')""",
        (), "Every job the human has already responded to.",
    ),
    _q(
        "signals",
        """SELECT job_id, event, reason_tags, reason, day, run_id
           FROM {A} WHERE event IN ('skipped','not_for_me','shortlisted','applied')
           ORDER BY day DESC, at DESC LIMIT {limit}""",
        ("limit",), "Recent human signals, newest first.", limit=TUNABLES.pref_signal_window,
    ),
    _q(
        "predictions_window",
        """SELECT job_id, predicted, actual, day, run_id
           FROM {A} WHERE event = 'seen' AND predicted IS NOT NULL AND actual IS NOT NULL
           ORDER BY day DESC, at DESC LIMIT {limit}""",
        ("limit",),
        "The agreement record autonomy readiness is computed from.",
        limit=TUNABLES.d1_window,
    ),
    _q(
        "runs_series",
        """SELECT run_id, started_at, day, mode, wall_ms, tokens_in, tokens_out,
                  steps_reasoned, steps_replayed, questions_asked, human_touches,
                  precision_at_5, prediction_accuracy
           FROM {R} WHERE run_id <> 'bootstrap'
           ORDER BY started_at LIMIT {limit}""",
        ("limit",),
        "Every point on the three-line chart. The bootstrap row that created the "
        "table is not a run and is excluded: only numbers a run produced go on "
        "the chart.",
        limit=500,
    ),
    _q(
        "corpus_stats",
        """SELECT count(*) AS jobs, count(DISTINCT company) AS companies,
                  count(DISTINCT source) AS sources, max(release_day) AS horizon,
                  sum(CASE WHEN salary_max IS NOT NULL THEN 1 ELSE 0 END) AS with_salary
           FROM {J}""",
        (), "Hour-0 sanity check: is the corpus big enough for a real percentile.",
    ),
    _q(
        "release_histogram",
        """SELECT release_day, count(*) AS jobs FROM {J}
           GROUP BY release_day ORDER BY release_day""",
        (), "Rows per day. No day should be trivially small.",
    ),
)


def bind_catalog(catalog: str, jobs_table: str = "jobs") -> dict[str, NamedQuery]:
    """Bind the registry to a catalog. Table names come from config, not callers."""
    for identifier in (catalog, jobs_table):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
            raise ValueError(f"{identifier!r} is not a plain identifier")
    tables = {
        "J": f"{catalog}.public.{jobs_table}",
        "A": f"{catalog}.public.applications",
        "R": f"{catalog}.public.runs",
    }
    bound = {}
    for query in _TEMPLATES:
        sql = query.sql
        for key, table in tables.items():
            sql = sql.replace("{" + key + "}", table)
        bound[query.name] = NamedQuery(query.name, sql, query.params, query.defaults,
                                       query.summary)
    return bound


def catalogue() -> list[dict[str, str]]:
    """What the agent is allowed to ask for, for the tool description."""
    return [
        {"name": q.name, "params": ", ".join(q.params) or "-", "summary": q.summary}
        for q in _TEMPLATES
    ]
