---
name: cognee
description: Read from and write to this project's Cognee Cloud memory tenant — the shared knowledge graph holding the candidate profile, job corpus, feedback and Rote play runs. Use whenever the task involves remembering something for later, recalling what we already know about a person, job, company or past run, inspecting the graph, or logging a play run. Triggers include "what do you know from cognee", "what do we know about X", "remember this", "recall", "add to memory", "check the knowledge graph", "log this run".
---

# Cognee Cloud

Memory for this project is a **managed Cognee tenant**, not a local library.
The `cognee` Python package is deliberately not installed. Everything is REST.

## Credentials

From the project `.env` (already gitignored):

| Variable | Meaning |
|---|---|
| `COGNEE_BASE_URL` | Per-tenant host, e.g. `https://tenant-<uuid>.aws.cognee.ai`. No trailing slash. There is no shared `api.cognee.ai`. |
| `COGNEE_API_KEY` | Sent as the **`X-Api-Key`** header. |
| `COGNEE_DATASET` | Default dataset name. |

**Auth is `X-Api-Key` only.** `Authorization: Bearer <the same key>` returns 401
on every route. If you see a 401, that is almost always the reason.

Never paste the key into a URL, a log line, or a message to another service.

## Preferred path: the project client

`memory/cognee_client.py` wraps every route below and already encodes the
gotchas. Use it rather than hand-rolling requests.

```python
from memory.cognee_client import CogneeCloud

with CogneeCloud() as c:
    c.add_text("Ada Lovelace wrote the first computer program in 1843.")
    c.cognify(wait=True)                      # blocks until the graph is built
    print(c.search("Who wrote the first computer program?"))
    print(c.recall("What does this candidate want?"))
    graph = c.graph()                         # {"nodes": [...], "edges": [...]}
```

Answering "what do you know from cognee?" is `c.recall(...)` against the
relevant dataset, or `c.datasets()` for the inventory.

## Raw HTTP, when you need a route the client does not wrap

```bash
curl -s -X POST "$COGNEE_BASE_URL/api/v1/recall" \
  -H "X-Api-Key: $COGNEE_API_KEY" -H "Content-Type: application/json" \
  -d '{"query": "What are the main entities?", "datasets": ["candidate"]}'
```

The tenant serves its own OpenAPI spec at `/openapi.json` — fetch that to check
a payload shape rather than guessing.

## Routes that matter

| Route | Use |
|---|---|
| `POST /api/v1/add_text`, `POST /api/v1/add` | Stage text, or upload a file (multipart, field `data`). |
| `POST /api/v1/cognify` | Build the graph. `graphModel` takes a JSON schema for typed entities. |
| `POST /api/v1/search` | Retrieval with an explicit `searchType`. |
| `POST /api/v1/recall` | Agent-facing retrieval; `searchType: null` lets cognee route. |
| `POST /api/v1/remember/entry` | Typed memory: `qa`, `trace`, `feedback`, `skill_run`. |
| `POST /api/v1/skills/` | Register a SKILL.md body as a `Skill` node. |
| `GET /api/v1/datasets/` | Dataset inventory; `id` is what shared-dataset routes need. |
| `GET /api/v1/datasets/{id}/graph?full=true` | Whole graph as `{nodes, edges}` — the HydraDB bridge. |
| `GET /api/v1/visualize/json` | Graph for rendering. |
| `GET /api/v1/quotas/usage` | Storage used against the 1.07 GB limit. |
| `GET /api/v1/sessions/cost-by-model` | Per-model spend. |

`searchType` values: `HYBRID_COMPLETION` (default), `GRAPH_COMPLETION`,
`GRAPH_COMPLETION_DECOMPOSITION`, `RAG_COMPLETION`, `CHUNKS`, `SUMMARIES`,
`TEMPORAL`, `SKILLS`, `CYPHER`, `NATURAL_LANGUAGE`, `AGENTIC_COMPLETION`,
`FEELING_LUCKY`.

## Five things that will cost you an hour otherwise

1. **`cognify` is async by default.** It returns a `pipeline_run_id` and builds
   server-side. Pass `runInBackground: false` (the client's `wait=True`) or poll
   `GET /api/v1/datasets/status`. A one-sentence dataset takes ~17s blocking.
2. **`recall` with no `datasets` searches `default_dataset` only** — not
   everything you can read. Always name the dataset.
3. **Dataset *names* only resolve to datasets you own.** For a dataset shared
   with you, pass `datasetIds` with the UUID from `GET /api/v1/datasets/`.
4. **Before the first `cognify`**, recall returns
   `{"status": "memory_warming_up", "datapoint_count": 0}` rather than an error.
   That means "ingest something", not "broken".
5. **`cognee-sdk` on PyPI does not work here.** It authenticates with
   `Authorization: Bearer` and exposes no `remember`/`recall`/`skills` routes.

## Logging a Rote play run

Every play run gets a `SkillRunEntry` so `recall` can route future similar tasks
to the play instead of re-reasoning:

```python
c.remember_skill_run(
    selected_skill_id="apply-pack",
    run_id=run_id,
    task_text="tailor an application for job 4821",
    result_summary="sheet row + slack post, no reasoning steps",
    success_score=1.0,
    latency_ms=elapsed_ms,
)
```

## Writes are shared

This tenant is the whole team's memory. A `forget`, a `DELETE /datasets/{id}`,
or a bulk ingest affects everyone's graph and the demo. Confirm with the user
before deleting anything you did not create in the same session.
