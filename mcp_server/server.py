"""The MCP server. Not a sixth integration — the only bridge to two of the five.

The 140-node RocketRide catalog has no hotdata node and no Rote node, so
neither layer can be reached from a pipeline at all without this. Cognee and
Slack/Sheets have good native nodes and stay native; we do not wrap them. The
server has three consumers — the RocketRide pipeline, the timed loop driver, and
the debugging harness — so the glue gets written once instead of three times.

Two rules on the tool surface, both settled before hour 1 because neither can be
retrofitted once the loop is producing chart rows (DESIGN §4):

* **Named queries, never query text.** ``hydra_query(name, params)`` and
  ``hotdata_query(name, params)`` over fixed registries. The agent stops
  spending reasoning tokens composing queries (line 1 of the chart), a named
  query is trivially a Rote play, and ``snyk code test`` never gets an
  LLM-driven injection surface to flag.
* **Return ids and one-line summaries, never payloads.** Tokens per run are
  dominated by what tools hand *back* into context, not by planning. Full text
  only on an explicit single-job fetch, and a hard cap on everything else.

Transport is streamable HTTP behind a tunnel, because the RocketRide engine runs
on staging and stdio is not available to us. The tunnel is a public URL fronting
SQL, a graph and a Rote shell-out, so it does not go out unauthenticated: set
``MCP_BEARER_TOKEN`` and give the same value to ``mcp_client``'s bearer field.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from fastmcp import FastMCP

from agent.clock import DayClock
from agent.config import TUNABLES, settings, state_path
from agent.feedback import (
    answer_question as _answer_question,
    confirm_preference as _confirm_preference,
    record_applied,
    record_reply,
    record_response,
)
from agent.pipeline import judge_the_day, prepare_pack
from agent.predict import predict
from agent.rank import _title_class
from insight.queries import catalogue as sql_catalogue
from insight.store import Insight
from memory.autonomy import DOMAINS, decline, evaluate, grant, guardrails, state_of
from memory.answers import resolve as resolve_question
from memory.graph import GraphStore, query_catalogue
from memory.prefs import active_rules
from mcp_server.rote import PlayIndex, Rote, fingerprint_task

PENDING_QUESTIONS = "pending_questions.json"

mcp = FastMCP(
    name="data-ai-hack",
    instructions=(
        "Job-hunting agent memory. Ask for queries by name — never write SQL or "
        "Cypher. Tools return ids and one-line summaries; call job_detail when "
        "you genuinely need a job's text. Never submit an application anywhere: "
        "prepare the pack, log it, and hand it to the human."
    ),
)


class _Context:
    """Built on first use, so the server starts with a half-filled .env."""

    def __init__(self) -> None:
        self._insight: Insight | None = None
        self._store: GraphStore | None = None
        self._remember: Any = None
        self._plays: PlayIndex | None = None

    @property
    def insight(self) -> Insight:
        if self._insight is None:
            self._insight = Insight()
        return self._insight

    @property
    def store(self) -> GraphStore:
        if self._store is None:
            self._store = GraphStore()
        return self._store

    @property
    def remember(self):
        if self._remember is None:
            try:
                from memory.remember import Remember
                self._remember = Remember()
            except Exception:
                self._remember = False      # tried once; do not retry per call
        return self._remember or None

    @property
    def plays(self) -> PlayIndex:
        if self._plays is None:
            self._plays = PlayIndex(self.store, Rote())
        return self._plays


ctx = _Context()


def _cap(rows: Sequence[Mapping[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    """Row cap, plus a per-field character cap. The payload rule, enforced once."""
    limit = limit or TUNABLES.max_rows_returned
    capped = []
    for row in rows[:limit]:
        item = {}
        for key, value in row.items():
            if isinstance(value, str) and len(value) > TUNABLES.max_summary_chars:
                item[key] = value[:TUNABLES.max_summary_chars] + "…"
            else:
                item[key] = value
        capped.append(item)
    return capped


# --- insight ---------------------------------------------------------------

@mcp.tool
def hotdata_query(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one of the named SQL queries over the job corpus.

    Call list_queries() for the names and their parameters. There is no
    free-text SQL tool and there will not be one.
    """
    rows = ctx.insight.run(name, params or {})
    return {"query": name, "rows": len(rows), "results": _cap(rows)}


@mcp.tool
def hotdata_search(text: str, limit: int = 10) -> dict[str, Any]:
    """Semantic / BM25 search over job descriptions. Returns ids and titles only."""
    hits = ctx.insight.search(text, limit=min(limit, TUNABLES.max_rows_returned))
    return {"hits": len(hits), "results": _cap(hits, limit)}


@mcp.tool
def job_detail(job_id: str) -> dict[str, Any]:
    """The one call that returns a full job description. Use it deliberately."""
    rows = ctx.insight.run("job", {"job_id": job_id})
    if not rows:
        return {"error": f"no job {job_id!r}"}
    job = dict(rows[0])
    job["description"] = (job.get("description") or "")[:6000]
    return job


# --- memory ----------------------------------------------------------------

@mcp.tool
def hydra_query(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one of the named graph queries over the candidate graph.

    Call list_queries() for the names. These are the multi-hop questions a
    vector store cannot fake: claim coverage, warm paths, company outcomes.
    """
    rows = ctx.store.run(name, params or {})
    return {"query": name, "rows": len(rows), "results": _cap(rows)}


@mcp.tool
def list_queries() -> dict[str, Any]:
    """Every query name the agent may ask for, with its parameters."""
    return {"hotdata": sql_catalogue(), "hydra": query_catalogue()}


@mcp.tool
def predict_fit(job_id: str) -> dict[str, Any]:
    """Score one role: keep or skip, with the reasons that produced the number.

    Deterministic — a weighted score over graph and aggregate features, never an
    LLM call. An LLM prediction would take a recall context that grows with the
    graph, so tokens per run would rise exactly as memory improved.
    """
    rows = ctx.insight.run("jobs_by_id", {"job_ids": [job_id]})
    if not rows:
        return {"error": f"no job {job_id!r}"}
    job = dict(rows[0])
    coverage = ctx.store.run("claim_coverage", {"job_id": job_id})
    warm = {row["job_id"]: row for row in ctx.store.run("warm_path", {"limit": 50})}
    outcomes = {row["company"]: row for row in ctx.store.run("company_outcomes")}
    salary = ctx.insight.salary_percentile(_title_class((job.get("title") or "").lower()))
    prediction = predict(job, coverage, active_rules(ctx.store),
                         warm=warm.get(job_id),
                         company_outcome=outcomes.get(job.get("company")),
                         salary=salary)
    return prediction.row()


# --- muscle memory ---------------------------------------------------------

@mcp.tool
def find_play(task_type: str, fields: list[str] | None = None) -> dict[str, Any]:
    """Ask Rote and the play index whether this shape has been done before.

    Returns the match mode: `exact` replays with zero reasoning on the plumbing,
    `partial` replays the known steps and names the novel fields, `none` means
    reason, record with record_step, then crystallize.
    """
    fingerprint = fingerprint_task(task_type, fields or [])
    return ctx.plays.find(task_type, fingerprint).summary()


@mcp.tool
def run_play(ref: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Replay a crystallized play by reference."""
    result = ctx.plays.rote.run_play(ref, params or {})
    return {"ref": ref, "ok": result["ok"],
            "output": result["stdout"][:TUNABLES.max_summary_chars],
            "error": result["stderr"][:TUNABLES.max_summary_chars]}


@mcp.tool
def record_step(argv: list[str]) -> dict[str, Any]:
    """Run one command under Rote so a novel path gets captured.

    Takes an argument list — ["curl", "-s", url] — never a command string, and
    nothing is passed through a shell.
    """
    result = ctx.plays.rote.record_step(argv)
    return {"code": result["code"],
            "output": result["stdout"][:TUNABLES.max_summary_chars],
            "error": result["stderr"][:TUNABLES.max_summary_chars]}


# --- the human -------------------------------------------------------------

@mcp.tool
def ask_human(question: str, context: str = "") -> dict[str, Any]:
    """Resolve a question down the ladder, and only queue it if it is genuinely new.

    1 a stored standard answer, 2 a prior approved answer to a similar question,
    3 a derivation from verified claims, 4 ask. Rungs 1 and 2 cost nothing and
    touch nobody, which is why questions per run falls to zero.
    """
    resolution = resolve_question(ctx.store, question)
    if not resolution.asked:
        return {"answered": True, **resolution.summary()}
    pending = _load_pending()
    if not any(item["question"] == question for item in pending):
        pending.append({"question": question, "context": context[:400]})
        _save_pending(pending)
    return {"answered": False, "question": question, "queued": True,
            "pending": len(pending)}


@mcp.tool
def pending_questions() -> dict[str, Any]:
    """What the next digest has to ask. Empty is the goal (P1)."""
    return {"questions": _load_pending()}


@mcp.tool
def answer_question(question: str, answer: str, day: int = 0) -> dict[str, Any]:
    """Store one answer from the human. Asked once, never again.

    Writing a fact about the candidate is never autonomous, whatever the
    agreement record says.
    """
    result = _answer_question(ctx.store, question, answer, day=day,
                              remember=ctx.remember)
    _save_pending([item for item in _load_pending() if item["question"] != question])
    return result


def _load_pending() -> list[dict[str, Any]]:
    path = state_path(PENDING_QUESTIONS)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return []


def _save_pending(items: Sequence[Mapping[str, Any]]) -> None:
    state_path(PENDING_QUESTIONS).write_text(json.dumps(list(items), indent=2))


# --- pipelines -------------------------------------------------------------

@mcp.tool
def judge_day(advance: bool = True, title_like: str = "", location: str = "",
              remote_only: bool = False) -> dict[str, Any]:
    """P-A: advance the clock, rank the pool, predict, and write the runs row."""
    result = judge_the_day(ctx.insight, ctx.store, DayClock.load(), advance=advance,
                           remember=ctx.remember, plays=ctx.plays,
                           title_like=title_like, location=location,
                           remote_only=remote_only)
    payload = result.summary()
    payload["digest"] = result.digest_text
    if result.autonomy_prompt:
        payload["autonomy_prompt_text"] = result.autonomy_prompt
    return payload


@mcp.tool
def apply_pack(job_id: str, day: int = 0, screening_questions: list[str] | None = None,
               decided_by: str = "human") -> dict[str, Any]:
    """P-B: prepare an apply-pack. Never submits anything, at any autonomy level."""
    pack, metrics = prepare_pack(ctx.insight, ctx.store, job_id, day=day,
                                 screening_questions=screening_questions or [],
                                 remember=ctx.remember, plays=ctx.plays,
                                 decided_by=decided_by)
    payload = pack.summary()
    payload.update({"copy": pack.copy, "gap_note": pack.gap_note,
                    "provenance": pack.provenance,
                    "questions": pack.questions_to_ask,
                    "tokens": {"in": metrics.tokens_in, "out": metrics.tokens_out},
                    "mode": metrics.settle_mode()})
    return payload


@mcp.tool
def record_feedback(signals: list[dict[str, Any]], day: int = 0, run_id: str = "",
                    predictions: dict[str, str] | None = None) -> dict[str, Any]:
    """P-C: absorb keeps, skips and not-for-mes, then check for a new preference.

    Each signal is {job_id, kind, reason_text?, reason_tags?} where kind is
    keep | skip | not_for_me. Only not_for_me teaches; skip means "not now".
    """
    return record_response(ctx.store, ctx.insight, day=day, run_id=run_id,
                           signals=signals, remember=ctx.remember,
                           predictions=predictions or {})


@mcp.tool
def confirm_preference(preference_id: str, confirmed: bool) -> dict[str, Any]:
    """Confirm an inferred preference, or reject it permanently.

    A rejected rule is never proposed again. A confirmed one re-ranks the
    shortlist immediately — that visible reorder is the proof.
    """
    return _confirm_preference(ctx.store, preference_id, confirmed)


@mcp.tool
def mark_applied(job_id: str, day: int = 0, run_id: str = "",
                 claim_ids: list[str] | None = None) -> dict[str, Any]:
    """The human applied. We prepared it; they sent it."""
    return record_applied(ctx.store, ctx.insight, job_id, day=day, run_id=run_id,
                          claim_ids=claim_ids or [])


@mcp.tool
def mark_outcome(job_id: str, kind: str = "replied", day: int = 0, run_id: str = "",
                 detail: str = "") -> dict[str, Any]:
    """The candidate reports a reply or a rejection. Feeds the warm-path query."""
    return record_reply(ctx.store, ctx.insight, job_id, day=day, run_id=run_id,
                        kind=kind, detail=detail)


# --- autonomy and metrics --------------------------------------------------

@mcp.tool
def autonomy_status(domain: str = "") -> dict[str, Any]:
    """Where each domain stands, and what the agent would say if it were ready."""
    domains = [domain] if domain else list(DOMAINS)
    out = {}
    for name in domains:
        readiness = evaluate(ctx.store, name)
        out[name] = {**state_of(ctx.store, name), "reason": readiness.reason,
                     "ready": readiness.ready, "prompt": readiness.prompt()}
    return {"domains": out, **guardrails()}


@mcp.tool
def autonomy_set(domain: str, on: bool) -> dict[str, Any]:
    """The human's toggle. The only way a domain becomes Autonomous."""
    return grant(ctx.store, domain) if on else decline(ctx.store, domain)


@mcp.tool
def log_run_metrics(row: dict[str, Any]) -> dict[str, Any]:
    """Append one runs row. Only numbers a run actually produced belong here."""
    result = ctx.insight.log_run(row)
    return {"logged": result.get("rows", 0), "run_id": row.get("run_id")}


@mcp.tool
def chart_data(limit: int = 200) -> dict[str, Any]:
    """The three lines, straight out of the runs table."""
    rows = ctx.insight.run("runs_series", {"limit": limit})
    return {"runs": len(rows), "series": _cap(rows, limit)}


def main() -> None:
    config = settings()
    auth = None
    if config.mcp_bearer:
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
        auth = StaticTokenVerifier(tokens={config.mcp_bearer: {"client_id": "rocketride"}})
    else:
        print("warning: MCP_BEARER_TOKEN is unset — do not expose this through a tunnel")
    mcp.auth = auth
    mcp.run(transport="http", host=config.mcp_host, port=config.mcp_port)


if __name__ == "__main__":
    main()
