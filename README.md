# data-ai-hack

Knowledge-graph memory over our data using [Cognee Cloud](https://docs.cognee.ai/) —
one managed tenant shared by the whole team, reached over REST. Nothing about the
memory layer runs on your laptop, so there is no embedding stack to configure and no local
graph to drift out of sync with everyone else's.

The full stack, in one line:

```
cognee (memory) -> HydraDB (stores it) -> hotdata.dev (ad-hoc queries) -> RocketRide (acts) -> Rote (replays what worked)
```

## Team checklist: every teammate does this once

Nothing here is shared through git. Keys, logins and CLI installs live on your own
machine, so each of us has to complete every step below. Tick them off in order.

1. **Clone and install.** Follow [Setup](#setup) through `cp .env.example .env`.
   Two small packages, any Python 3.10+, no gcloud. Cognee needs no model key,
   but RocketRide's agent does — see `ANTHROPIC_API_KEY` in `.env.example`.
2. **Join the Cognee Cloud tenant** and copy `COGNEE_BASE_URL` and
   `COGNEE_API_KEY` out of the dashboard's "Connect your agent" panel into `.env`.
   The tenant already holds the team's graph; you are connecting to it, not
   creating your own.
3. **Add the service keys to `.env`.** Ask in the team channel for any you do not have.
   `.env.example` explains each one.
   - `ROCKETRIDE_APIKEY` (dev connection, for running and iterating)
   - `ROCKETRIDE_DEPLOY_APIKEY` (deploy target only, never used for dev runs)
   - `HYDRADB_APIKEY` (plus `HYDRA_DB_API_KEY`, same value, for RocketRide's node)
   - `HOTDATA_API_KEY` — that spelling exactly. The CLI reads no other name, and
     without it every command falls back to the browser session and dies with
     "session expired or revoked".
4. **Verify the Cognee tenant** with the snippet in [Verify the setup](#verify-the-setup).
5. **Set up hotdata.** Install the CLI, then let the script create the database,
   connect the Greenhouse source, and load rows:

   ```bash
   brew install hotdata-dev/tap/cli
   hotdata auth register              # browser, GitHub by default
   sh scripts/hotdata-setup.sh        # database + data source + first ingest
   ```

   Copy the three ids it prints at the end back into `.env`.
6. **Install Rote and join the team org.** This is the Playoffs requirement; the
   installer and sign-in are identity-gated and cannot be done for you. You will
   have received an invite email for the org, which makes you a member on sign-up.

   ```bash
   curl -fsSL https://getrote.dev/playoffs/install.sh | sh   # then open a new shell
   rote login                                                # Google or GitHub, same email as the invite
   rote profile set-handle <your-handle>                     # your public Play namespace
   rote registry org list                                    # must show data-ai-hack
   ROTE_ORG=data-ai-hack sh scripts/rote-setup.sh            # keys into local store, pull adapter, smoke test
   ```

   Do **not** run `rote registry org create`. The org already exists and the free
   plan allows one org per account; creating your own will fail and is not needed.
   If the setup script ever drops into an interactive wizard asking for an
   "environment variable name for API token", press Ctrl-C: it wants the name
   `ROCKETRIDE_APIKEY`, never the key itself.

   Your RocketRide key never leaves your machine; the shared adapter only names it.
   Details and troubleshooting are in [rote/README.md](rote/README.md).
7. **Run the warm-up laps** in a fresh Claude Code conversation, then post
   "warmed up" in the Playoffs Discord:

   ```
   /play what's new
   /play run hello
   ```

Done when `sh scripts/verify-setup.sh` is all green and `/play run hello`
completes.

## Checking you are actually set up

One script hits all five services for real. No check trusts a key just because
it is in `.env`.

```bash
sh scripts/verify-setup.sh                 # all five, ~1 min
SKIP_COGNEE=1 sh scripts/verify-setup.sh   # skip the slow ingest
```

| # | Service | What is checked | How to fix a FAIL |
|---|---------|-----------------|-------------------|
| 1 | RocketRide | bearer `GET /services` returns 200 | re-copy `ROCKETRIDE_APIKEY` from the dashboard |
| 2 | HydraDB | `databases.status()` reports graph, scheduler and both vector stores up | check `HYDRADB_APIKEY` and `HYDRADB_DATABASE` |
| 3 | hotdata | key authenticates, ≥1 data source, rows queryable | `sh scripts/hotdata-setup.sh` |
| 4 | Cognee Cloud | key authenticates, then a real `add` → `cognify` → `search` round trip on the tenant | re-copy `COGNEE_BASE_URL` / `COGNEE_API_KEY`; see [Verify the setup](#verify-the-setup) |
| 5 | Rote | signed in, adapter installed, live authenticated call | `ROTE_ORG=data-ai-hack sh scripts/rote-setup.sh` |

The one thing the script cannot check is the **RocketRide credit balance**. No
endpoint reports it, and a task that fails for lack of credits looks exactly
like a malformed pipeline. Confirm the coupon landed in the dashboard at
[staging.rocketride.ai](https://staging.rocketride.ai).

### Org owner only (one person, already done once)

The org `data-ai-hack` and the shared `rocketride` adapter already exist. Everyone
else pulls. If it ever needs redoing, the sequence is in
[rote/README.md](rote/README.md#org-owner-setup).

**Inviting a teammate.** The org is on the free Community plan (up to 5 members).
Invites go by email and make the person a member as soon as they sign in to Rote
with that email; there is no accept step. Use the `developer` role so they can
push Plays.

```bash
rote registry org invite data-ai-hack <teammate@email> --role developer
rote registry org members data-ai-hack --pending      # see members and open invites
rote registry org invite revoke data-ai-hack <teammate@email>   # undo an invite
rote registry org members role data-ai-hack <teammate@email> admin   # promote later
```

### Sharing Plays

When you record a Play that works, push it to the org so the next run replays for
everyone: `rote registry play push <play-path> data-ai-hack/<play-name> --private`.
Teammates get it with `rote registry play pull data-ai-hack/<play-name>`. Keep
Plays private during the build; the final submission is published publicly under a
personal handle at the end.

## The backend

The half of the build that is not fetching jobs and not the front end:
canonical schema, day clock, the candidate graph, ranking and prediction, the
apply-pack, the three pipelines, and the MCP server RocketRide reaches them
through. `DESIGN.md` is the why; this is where it lives.

```
agent/      schema, day clock, ranking, predict_fit, apply-pack, metrics, the three pipelines
memory/     cognee client + typed model, the candidate graph and its named queries,
            claims + citation validator, the answer ladder, preference induction, autonomy
insight/    hotdata client, the named SQL, the three tables, the corpus projection,
            company headcounts (the one field no ATS board publishes)
mcp_server/ FastMCP server and the Rote wrapper
rocketride/ the three pipelines as .pipe files, the task client, and a CLI
demo/       the timed loop driver and the three-line chart
tests/      78 offline tests — no network, no credentials
```

### Getting it running

```bash
make test                                    # 78 tests, offline, well under a second
python scripts/backend-setup.py --tables     # create applications + runs
python scripts/backend-setup.py --project tech_jobs_release --dry-run   # show the mapping
python scripts/backend-setup.py --project tech_jobs_release             # fill jobs_canonical
python scripts/backend-setup.py --resume candidate/resume.md --prefs candidate/preferences.md
python scripts/backend-setup.py --sync       # Cognee's typed graph -> the candidate graph
make status                                  # what is in each layer right now
```

Then the server RocketRide talks to:

```bash
MCP_BEARER_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))")  # into .env
make serve       # http://127.0.0.1:8787/mcp
make tunnel      # public URL for the staging pipeline; put the bearer in mcp_client
```

`make loop` ticks the day clock through the RocketRide webhook, which is what
makes every point on the chart a real RocketRide run. `make loop-local` runs the
same pass in process — the debugging harness from DESIGN §11, never the demo
path. `make chart` builds `data/chart.html` from the `runs` table.

### RocketRide: getting a pipeline to actually run

The engine runs on **staging**, so it can only reach the MCP server through a
public URL. Three terminals:

```bash
make serve                                   # MCP server on :8787
make tunnel                                  # ngrok (or cloudflared) -> public URL
export MCP_ENDPOINT=https://<tunnel-host>/mcp
make pipes                                   # write the .pipe files with that endpoint
make pipeline-up                             # start P-A, prints token + webhook URL
make pipeline-ask TOKEN=tk_... Q="judge today"
make pipeline-down TOKEN=tk_...              # always: an open source burns credits
```

Verified live on 2026-09-11 — the agent on staging called our tunnelled MCP
server and answered from the real corpus: *"There are 1,886 jobs and 19
companies in the corpus"* and *"63 jobs were released on day 7"*, both through
named queries, both matching what the same queries return locally.

**Four things about the pipeline JSON that cost an afternoon each.** The shape
is documented at `docs.rocketride.org/concepts/pipelines` and
`/concepts/agents-tools-skills` — read those before changing
`rocketride/pipelines.py`, because `POST /task` takes a freeform object and
reports problems one at a time, in its own source file and line.

- **`control` lives on the helper, not the agent.** The agent has input lanes
  and nothing else; each LLM, memory and tool node carries
  `control: [{"classType": "llm", "from": "agent"}]` pointing back at it. An
  `invoke` list on the agent is accepted at submit and then fails at *run* time
  with "You must have 1, and only 1 llm node connected to your agent".
- **The webhook classifies by content type.** `Content-Type: text/plain` lands
  on the `text` lane; a JSON body is accepted with `resultTypes: {}` and then
  reaches no consumer at all — no answer, no error, nothing to debug.
- **The webhook emits `text`, the agent only accepts `questions`**, so a
  `question` node goes between them. Without it the run silently produces
  nothing.
- **`ui.position` per component, or the designer stacks every node at the
  origin** and the canvas looks like one tangled box. The documented
  `/webhook/{project_id}/{source_id}` route is 404 on staging; use the legacy
  `/webhook?token=`.

The `.pipe` files are committed with `${QWEN_API_KEY}`-style placeholders and
filled from `.env` at submit time, so the IDE designer can list and edit them
without a key ever reaching git.

**Still open:** `judge_day` as a single tool call ranks 1,886 rows and extracts
requirements for 30 of them, which is slow enough that the agent's turn can time
out. The pass itself is verified end to end locally and writes real `runs` rows;
splitting it into narrower tools, or pre-warming the requirement extraction, is
the next step before the timed loop runs against it.

### Where the two halves meet

The fetchers own their table; the backend projects it into the canonical schema
once, into **`jobs.public.jobs_canonical`**. `jobs` is left alone — that name
belongs to whatever `scripts/hotdata-setup.sh` and the fetchers last wrote, in
whatever shape the board returned.

As of hour 0 the source is `jobs.public.tech_jobs_release`: 1,886 deduplicated
rows across 19 companies and 3 ATS families, with a `release_day` already on
every row. The projection maps `job_id` → `id`, picks one of the three URL
columns, re-sanitizes the description, and **keeps the source's own schedule** —
a play captured against day 7 has to replay against day 7, so a re-projection
must never re-roll the days. A source with no `release_day` gets one assigned
deterministically instead. Rename a column and re-run `--project`; nothing
downstream changes. `HOTDATA_JOBS_TABLE` points the whole backend at a scratch
copy if you want to try something without touching anything shared.

### Things that cost an afternoon to find out

All verified against the live services on 2026-09-11.

- **HydraDB cloud has no Cypher.** `hydradb-sdk` 2.1.4 exposes
  `context.ingest/list/relations/subgraph/inspect/delete` and nothing that takes
  a query language. DESIGN §4 pre-decided the answer, so: the named queries are
  written as Cypher, `memory/graph.py` evaluates them in process, and HydraDB
  cloud is the durable store (nodes and edges go in as memory items, and pull
  them back with `HydraMirror.pull`). Set `GRAPH_BOLT_URL` and the same queries
  run as real Cypher against the OSS engine (`make graph-up`) — one env var, no
  other change.
- **hotdata's CLI is not uniform.** `databases load` and `search create` take no
  `-o`; `search list` and `tables list` take no `-d` (it is `--database`);
  `-o json` on a query returns `{columns, rows}`, not records; the percentile
  function is `approx_percentile_cont`, not `approx_percentile`.
- **A column's type is fixed by the first load, and an all-null column becomes
  varchar.** The first real integer then fails with "can't change type from
  varchar to int64" — hours later, when there is finally a number to write, and
  `--mode replace` does not fix it because it replaces rows and not types. The
  bootstrap rows that create `applications` and `runs` therefore carry a typed
  value in every column (`RUNS_SEED` in `agent/schema.py`);
  `--recreate-tables` drops and rebuilds if one is already wrong.
- **A hotdata search index parses query syntax.** A bare `-`, `+`, `:`, `"`,
  `(` or `^` anywhere in the text returns *500 internal server error*, so
  pasting a résumé in fails in a way that reads like an outage.
  `insight.hotdata.clean_query` strips them and every search goes through it.
- **Cognee's `graphModel` works, and it must be nested.** Each schema *property*
  becomes an edge from the object that owns it, so a flat root of parallel
  arrays gives you a star with no candidate→skill edge. Also: cognify with a
  graphModel took ~3 minutes for one paragraph against ~17s bare.
- **`remember/entry` is stricter than the four shapes suggest**, and says why
  only in the response body: `qa` needs `session_id`, `feedback` needs a `qa_id`,
  `trace` needs `origin_function`, and `skill_run`'s `selected_skill_id` must be
  the *name of a skill already registered* through `POST /api/v1/skills/` —
  otherwise a bare 400 with nothing pointing at the cause. `memory/remember.py`
  handles all four.
- **`rote play search` answers in prose, not JSON**, including when piped.
  Parsing it line-by-line turns "No registry Plays matched the query." into a
  play that does not exist, and the agent then tries to replay it.

### The one thing the corpus does not carry

No ATS board publishes headcount, and the worked preference rule in DESIGN §6.3
is about company size — the one `candidate/README.md` deliberately leaves out of
the seed so the agent has to infer it. Without a size on the row that rule gets
proposed, confirmed by the human, and then matches nothing, because
`Rule.matches` returns false on an absent field: the shortlist does not move and
P2 fails on stage *while looking like it worked*.

So `insight/companies.py` is a nineteen-line lookup of approximate public
headcounts, applied by the projection. It belongs in the same honesty column as
the release schedule — the jobs are real, the headcounts are ours, and nothing
on the chart depends on them being exact. Verified live: three not-for-mes on
Anthropic, Palantir and Stripe propose *"you avoid roles with company size at
least 2000"*, and confirming it moves the mean score of a big-company role from
0.40 to 0.23 and puts Discord, Perplexity, Linear and Notion at the top.

A test asserts that every field a rule can name is actually selected by the
ranking queries, because that is what made the failure invisible the first time.

### The rules the code enforces

Four things from DESIGN that are checks in code rather than notes in a prompt,
each with a test on it:

- **Named queries only.** `hotdata_query(name, params)` and
  `hydra_query(name, params)` over fixed registries; there is no free-text SQL
  or Cypher tool and there will not be one.
- **Only verified claims reach an apply-pack.** The tailoring call returns
  `claim_id`s; `validate_citations` fails the pack if any citation is missing,
  unverified, or another candidate's, or if a factual sentence carries none.
- **A preference changes nothing until the human confirms it**, and a rejected
  rule is never proposed again.
- **Nothing is ever submitted anywhere**, at any autonomy level. Prepare, log,
  hand to the human.

## Setup

Requires Python 3.10 or newer. The old 3.14 ceiling is gone: the heavyweight
`cognee` package is no longer installed, so its version constraints no longer
apply here.

```bash
git clone <repo-url> && cd data-ai-hack
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Then open `.env` and fill in the two Cognee lines from the dashboard:

```
COGNEE_BASE_URL=https://tenant-<tenant-uuid>.aws.cognee.ai
COGNEE_API_KEY=<your-cognee-api-key>
COGNEE_DATASET=candidate
```

That is the whole memory-layer configuration. The tenant owns its own LLM and
embedding stack, so there is no `gcloud auth application-default login`, no
Vertex project, no `EMBEDDING_DIMENSIONS` to keep in sync with a vector store,
and no per-teammate model drift.

Two things worth knowing before you write any calls:

- **Auth is `X-Api-Key`, not bearer.** `Authorization: Bearer <the same key>`
  returns 401 on every route. This is also why the lightweight `cognee-sdk`
  package on PyPI is unusable for us: it only speaks bearer, and it exposes no
  `remember` / `recall` / `skills` routes. Use `memory/cognee_client.py`.
- **The base URL is per-tenant.** There is no shared `api.cognee.ai` host; the
  tenant UUID is part of the hostname.

Upgrading from the old local setup? Drop the packages you no longer need:

```bash
pip uninstall -y cognee google-cloud-aiplatform     # ~1 GB back
```

## Verify the setup

With the venv active and `.env` filled in, this adds one document, builds the
graph, runs a search, and deletes the scratch dataset again. It takes about
35 seconds.

```bash
python memory/cognee_client.py
```

Expected output: a quota line, a search result naming Ada Lovelace, a node and
edge count, and `smoke dataset removed`.

The same round trip is check 4 of `sh scripts/verify-setup.sh`.

## Things to know

- **Your shell wins over `.env`.** `memory/cognee_client.py` uses
  `os.environ.setdefault`, so an exported variable overrides the file. (The old
  local cognee package inverted this, which surprised everyone at least once.)
- **Storage is the tenant's.** Graph, vector and relational data live in Cognee
  Cloud; nothing is written under your home directory and there is nothing to
  prune locally. Quota is 1.07 GB — check it with `CogneeCloud().quota()`. To
  throw away a dataset:

  ```python
  from memory.cognee_client import CogneeCloud
  with CogneeCloud() as c:
      c.delete_dataset(c.dataset_id("scratch"))
  ```

- **`cognify` is asynchronous unless you ask it not to be.** It returns a
  `pipeline_run_id` immediately and builds the graph server-side; pass
  `wait=True` (the client's default) to block until the graph is ready.
- **`recall` without a dataset only searches `default_dataset`**, not everything
  you can read. Always name the dataset.
- **The dashboard is the debugger.** Sessions, per-model cost, and a live graph
  visualiser are all in the Cognee Cloud UI, which beats reading
  `~/.cognee/logs/` — and works for the demo too.
- **Secrets.** `.env` is gitignored. `.env.example` holds placeholders only; keep it
  that way when adding new keys.
