# Backend plan: making the dashboard's numbers real

The dashboard reads one JSON document, `dashboard/data.json`, built by
`demo/dashboard.py` from the `runs` table, the `applications` table and the
graph. This document is the plan for the backend changes that document depends
on — what each one is, why, and how to know it worked.

Two of the seven fields the dashboard wants are **already answerable today** and
`demo/dashboard.py` reads them now. Four need a backend change. One should never
be added. Read the "Verdict" column of the table before the detail.

---

## 0. The mismatch this is all downstream of

`refresh_and_rank` (`agent/rank.py:86`) is fully deterministic — SQL filters, a
search, graph queries, a scoring function. No model call. `agent/llm.py:130`
says it: the pack's call is "the only LLM call in the pack", and it is the only
one in the repo.

So a P-A run spending zero tokens and asking zero questions is not broken
instrumentation. It is the architecture working. What the dashboard calls "a
day" is three pipeline invocations:

| | P-A `judge_day` | P-B `apply_pack` | P-C `record_feedback` |
|---|---|---|---|
| tokens | 0 by design | one LLM call, counted at `agent/pack.py:94` | 0 |
| questions | none by design | the ladder, counted at `agent/pack.py:81` | 0 |
| accuracy | predicts only | — | the `actual` lands here |
| writes a `runs` row | yes | yes | **no** |

`runs` stores one row per *invocation*; the dashboard shows one point per *day*.
Every change below either makes a day's rows separable, or fills a number that
genuinely has no writer.

---

## The changes, in order of (value / risk)

| # | Change | Verdict | Risk | Unblocks |
|---|---|---|---|---|
| 1 | Widen `runs_series` SELECT | do | none | `shown`, `actual_keep` on the chart |
| 2 | Derive quality at read time | **done** | none | accuracy, kept, touches |
| 3 | `current_slate` graph query | **done** | none | the shortlist panel |
| 4 | `pipeline` column on `runs` | do | low (migration) | rolling a day up |
| 5 | Persist prediction `reasons` | do | none | the "why" line |
| 6 | Wire `RocketRide.usage()` | do, test early | **unknown** | the token line |
| 7 | A `note` column | **don't** | — | — |

Changes 2 and 3 are already implemented — they needed no schema change and the
dashboard would not render without them. The rest are listed as work.

---

### 1. Widen the `runs_series` SELECT

**What.** Add `shown`, `predicted_keep`, `actual_keep`, `pool_size`,
`jobs_released_today` to the SELECT list in `insight/queries.py:206`.

**Why.** The columns already exist in `RUNS_COLUMNS` (`agent/schema.py:49`) and
are already written by `RunMetrics.note_slate` (`agent/metrics.py:110`). They
are simply not selected, so the run log's "Jobs you kept — 4 of 5" column has
nothing to read.

**Files.** `insight/queries.py` only.

**Risk.** None. Additive to a SELECT on columns that exist.

**Verify.** `make chart --json` — or `python -m demo.dashboard --print` — and
confirm `shown` is non-null on rows where the digest had a slate.

**Rollback.** Revert the line.

---

### 2. Derive quality at read time — *already done*

**What.** Compute `prediction_accuracy`, `actual_keep` and `human_touches` per
day from the `applications` table rather than from the `runs` row.

**Why this rather than a write-back.** `judge_the_day` appends the run row at
`agent/pipeline.py:122` — *before* the human has answered. So those three fields
are structurally NULL at write time on the real path. `score_responses`, which
fills them, has exactly one production caller: `demo/loop.py:84`, the
`--local --auto-feedback` debugging harness. The real P-C path
(`record_feedback` → `agent/feedback.py:43`) writes `applications` rows with
`predicted` and `actual` on them, and never touches the run row.

The write-back alternative means re-logging a row that is already in the table,
relying on `load_file(mode="append", key="run_id")` doing an upsert — semantics
we have not verified against the live service. A duplicated run is a duplicated
chart point. The read-time route has no such failure mode and it fills every run
already logged, retroactively.

**How.** `predictions_window` (`insight/queries.py:197`) already returns
`job_id, predicted, actual, day, run_id` for every resolved prediction. Group by
`day`, and pass each group to the **existing** `agent.metrics.skip_class_accuracy`.

> Deliberately computed in Python, not in SQL. `skip_class_accuracy`
> (`agent/metrics.py:176`) returns `None` rather than `0.0` when a slate produced
> no skips, because "no data" and "got them all wrong" are different facts. That
> distinction is worth having in exactly one place.

**Verify.** `python -m demo.dashboard --print` shows non-null `acc` on days that
had feedback, and `null` on days that did not. Covered by
`tests/test_dashboard.py::test_accuracy_is_derived_not_stored`.

---

### 3. `current_slate` graph query — *already done*

**What.** A named read query returning the predictions the human has not
answered yet (`memory/graph.py`).

**Why.** `agreement_record` deliberately returns only *resolved* predictions —
both halves or it is not a record (`memory/graph.py:532`). Today's shortlist is
by definition unresolved, so nothing could read it. The alternative was to have
the dashboard call `judge_the_day(advance=False)`, which writes a `runs` row and
would put a phantom point on its own chart every time someone refreshed a
browser tab.

**Risk.** None — read-only, additive, registered through the same `_register`
decorator as the other sixteen. No caller of an existing query changes.

---

### 4. A `pipeline` column on `runs`

**What.** Add `pipeline` (`P-A | P-B | P-C`) to `RUNS_COLUMNS` and `RUNS_SEED`
in `agent/schema.py`, set it in `RunMetrics`, pass it at the two `log_run` call
sites (`agent/pipeline.py:122` and `:167`).

**Why.** `day` is not a key. A day with three pack preparations writes four rows
sharing one `day` value, and nothing distinguishes them. Rolling a day up —
which is the only way tokens and questions can ever appear on a per-day chart —
requires knowing which row is the judgment pass.

**The trap.** `agent/schema.py:58` documents it: hotdata infers a column's type
from the first load, and a column that arrives all-null becomes `varchar`, after
which the first real value is rejected. A new column **must** carry a correctly
typed value in `RUNS_SEED`, not just in the live rows.

**Migration.** The column is additive, so rows written before it exists come
back NULL. `demo/dashboard.py` already falls back to the `shown > 0` heuristic
for those, so old rows keep working and no backfill is needed.

**Verify.** `make tables` against a scratch `HOTDATA_JOBS_TABLE`, one judge run
and one pack run, then confirm both rows carry the right value and the seed row
did not turn the column into a varchar.

**Rollback.** The column is additive and the reader tolerates NULL, so reverting
the writers is safe on its own.

---

### 5. Persist prediction `reasons`

**What.** Add `reasons` to the edge props in `record_prediction`
(`memory/writers.py:129`).

**Why.** The "why" line — the most screenshot-able thing in the build, per
`agent/predict.py:144` — is computed by `explain()` at digest time from the live
`Prediction` object and then thrown away. The edge keeps `predicted`, `score`,
`day` and `run_id`, but not the reasons. Nothing can reconstruct the sentence
the human actually saw.

**Meanwhile.** `demo/dashboard.py` falls back to composing a weaker line from
what *is* in the graph — claim coverage, gaps, company size. It is honest about
being a reconstruction. The stored reasons are strictly better because they are
the text that was shown.

**Risk.** None. One more prop on an edge that is already merged.

---

### 6. Wire `RocketRide.usage()` into the run row

**What.** After `tick_webhook` fires (`demo/loop.py:66`), poll `GET /task`,
call `RocketRide.usage(status)`, and post the counters through the existing
`log_run_metrics` MCP tool (`mcp_server/server.py:380`).

**Why.** This is the number the pitch is actually about: the orchestrator agent
reasoning across waves, and doing less of it as plays accumulate. It is spent on
the far side of the MCP boundary, so `RunMetrics` cannot see it. Both halves of
the wiring already exist and **neither has a caller** —
`RocketRide.usage()` (`rocketride/client.py:148`) was written for exactly this
and returns a `reported` boolean for exactly this reason.

**The risk, and it is the real one.** Whether the engine reports per-node token
counters at all is unknown until a live run. `rocketride/client.py:9` notes
there is no usage route in the OpenAPI. `usage()` scrapes `pipeflow.byPipe` for
any key matching `token` or `usage`, which is a guess about an undocumented
shape.

**If `reported` is false**, fall back to the MCP-boundary proxy:
`RunMetrics.note_tool_call` (`agent/metrics.py:89`) exists but has no callers,
and `tool_calls` / `tool_bytes` are not in `RUNS_COLUMNS`, so they cannot yet be
persisted — that is a second small schema change, with the same seed-typing
trap as change 4.

**And then the label has to move.** `demo/dashboard.py` emits `cost_basis`
(`tokens` | `tool_calls` | `unavailable`) and the dashboard titles the panel
from it, so a proxy is never captioned as a token count. When `cost_basis` is
`unavailable` the panel is hidden rather than drawn flat at zero.

> This is why the chart has three lines. Lines 2 and 3 do not depend on this
> one, and both are answerable today.

**Test it first.** One live P-A run and a `print(RocketRide.usage(status))` —
fifteen minutes, and it decides whether the headline axis says "tokens".

---

### 7. A `note` column — don't

The day-12 "hit a question it had never seen" callout has no column, and should
not get one. It is derivable: a `partial_replay` following a `full_replay` *is*
that event, and `mode` and `steps_reasoned` already encode it. `demo/dashboard.py`
computes it in the reader.

A free-text column on `runs` is a place for a person to write a better story
than the run had. The whole table is built on the opposite rule
(`agent/metrics.py:16`, `demo/chart.py:13`, `insight/store.py:143`).

Also not doing: `note_llm` in P-A. There is no model call to count, and a
synthesized number would be the exact failure all three of those docstrings
forbid.

---

## Sequencing

1. **Change 6 first**, as a fifteen-minute probe, not as an implementation — it
   is the only unknown, and the answer changes an axis label.
2. Changes 1 and 5 — minutes each, no risk.
3. Change 4 — the one migration. Do it against a scratch table first.
4. Change 6 properly, on whichever basis the probe established.

Changes 2 and 3 are in. Nothing else is blocking: the dashboard renders real
runs, a real shortlist and real knowledge counters today, with the cost panel
suppressed until 6 lands.

## The contract

`demo/dashboard.py` is the only thing that reads the tables, and
`dashboard/data.json` is the only thing the page reads. If a backend change
alters a shape, it alters `demo/dashboard.py` — never the page. The page's
fallback sample data is clearly labelled as such and the "Example numbers" chip
is driven by whether the fetch succeeded, so a dashboard that cannot reach its
feed says so rather than quietly showing fiction.
