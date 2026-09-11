# Job-hunting agent that gets better every run

Design for the "From Memory to Muscle Memory" hackathon (Sep 11, 2026, 8 hours).
Candidate-side counterpart to the spec's "Recruiting Copilot": one job seeker,
an agent that finds, ranks, and prepares applications, and visibly needs less
reasoning on each successive run.

## 1. The one-line pitch

"Run 1 the agent figures out how to pull jobs, learns what you like, and drafts
your first application. Run 5 it replays what worked, ranks by what actually got
you replies, and spends 10x fewer tokens doing it."

The demo is three runs in a row, with a chart of tokens and latency per run.

## 2. Why this idea fits the judging criteria

The judges look for (a) all five tools load-bearing, (b) repeated work over the
day, (c) visible compounding. Job hunting has all three naturally:

- Each job board (Greenhouse, Lever, Ashby) has a different API shape. The first
  encounter needs reasoning; every later one is a replay. That is muscle memory.
- User feedback ("too senior", "applied", "got a reply") is relationship data
  about the candidate, skills, companies, and outcomes. That is graph memory.
- "What is new since last run", "median salary for this title", "which source
  gets replies" are aggregate questions over a live table. That is hotdata.
- Drafting a cover letter, logging to a tracker, sending a digest, scheduling a
  follow-up are real actions. That is RocketRide.

## 3. Layer mapping

| Layer | Tool | What it owns in our system |
|---|---|---|
| Structure | Cognee | Ingests resume, preference text, job descriptions, and every feedback and tool trace. Extracts typed entities with a custom graph model. |
| Memory | HydraDB | Durable candidate graph: Candidate, Skill, Job, Company, Application, Outcome, Preference. Multi-hop Cypher for ranking and "why". |
| Insight | hotdata.dev | `jobs`, `applications`, `runs` tables. SQL for new-since-last-run, salary stats, reply rate by source. Vector and BM25 index on job descriptions. |
| Motion | RocketRide | The agent loop. Wave-planning agent with Cognee, HydraDB, and our MCP tools plus Slack, Sheets, Docs, Calendar, Gmail draft. |
| Muscle memory | Modiqo Rote | Plays for `ingest-ats`, `refresh-and-rank`, `apply-pack`. Replayed instead of re-reasoned. Each run logged back into Cognee as a `SkillRunEntry`. |
| Security | Snyk | `snyk test` and `snyk code test` in a Makefile target before every commit and before submission. |

### Cognee (already wired in this repo, v1.5.4)

Primitives we use, all verified present in the installed package:

- `cognee.remember(data, dataset_name="candidate")` for resume, preferences,
  and job descriptions. Pass a custom `graph_model` so extracted entities are
  typed (`Candidate`, `Skill`, `Job`, `Company`, `Requirement`) instead of
  generic `Entity`. Typed labels are what make the HydraDB Cypher readable.
- `cognee.remember(FeedbackEntry(...))` and `remember(TraceEntry(...))` for
  user feedback and tool outcomes. `remember(SkillRunEntry(...))` after every
  Rote play run. These entry types exist in `cognee.memory` today.
- `cognee.recall(query_text=..., datasets=["candidate"])` for "what does this
  person want" style questions from the agent.
- `cognee.export(dataset, format="json")` as the bridge to HydraDB (see below).

### HydraDB

Two surfaces exist. Decide tonight which one we can reach:

1. Cloud memory API (`api.hydradb.com`, `pip install hydradb-sdk`). RocketRide
   has a native `db_hydradb` node for it. Check whether the cloud key also
   gives Cypher access (the CLI and MCP server expose `graph query`).
2. Open-source engine via Docker: Bolt on 7687, HTTP JSON on 8443. Python
   client is the standard `neo4j` driver. Fully under our control.

Bridge from Cognee, in preference order:

1. Spike (20 min, tonight): point Cognee's `neo4j` graph provider at HydraDB's
   Bolt port. If auth works, Cognee writes to HydraDB directly and there is no
   bridge code. This is the cleanest story for judges.
2. Fallback (pre-write tonight, ~40 lines): `cognee.export(format="json")`,
   then `MERGE` nodes and edges over Bolt or HTTP. Run after every `remember`.
   Deterministic and we control the labels. Cognee's `cypher` export exists
   too but targets Neo4j syntax; do not depend on it.

Cypher queries that vector search cannot answer (these are the demo lines):

```cypher
// Skill overlap, excluding jobs already applied to
MATCH (c:Candidate)-[:HAS_SKILL]->(s:Skill)<-[:REQUIRES]-(j:Job)<-[:POSTED]-(co:Company)
WHERE NOT (c)-[:APPLIED_TO]->(j)
RETURN j.id, co.name, count(s) AS overlap ORDER BY overlap DESC LIMIT 10

// Warm path: skills shared with jobs that previously got a reply
MATCH (c:Candidate)-[:APPLIED_TO]->(j1:Job)-[:GOT_REPLY]->(),
      (j1)-[:REQUIRES]->(s:Skill)<-[:REQUIRES]-(j2:Job)
WHERE NOT (c)-[:APPLIED_TO]->(j2)
RETURN j2.id, count(DISTINCT s) AS signal ORDER BY signal DESC

// Why was this rejected last time
MATCH (c:Candidate)-[r:REJECTED]->(j:Job)-[:REQUIRES]->(req)
RETURN j.title, r.reason, collect(req.name)

// Follow-ups due: applied, no reply, older than 7 days
MATCH (c:Candidate)-[a:APPLIED_TO]->(j:Job)<-[:POSTED]-(co:Company)
WHERE NOT (j)-[:GOT_REPLY]->() AND a.at < $cutoff
RETURN co.name, j.title, a.at
```

### hotdata.dev

Cloud only, CLI plus Python SDK (`pip install hotdata`) plus REST. Tables:

- `jobs`: canonical schema across ATS sources. Columns: `id, source, company,
  title, location, remote, salary_min, salary_max, description, url,
  posted_at, first_seen_run, last_seen_run`.
- `applications`: event log. `job_id, event (seen|shortlisted|applied|replied|
  rejected|skipped), reason, at, run_id`.
- `runs`: `run_id, started_at, wall_ms, tokens_in, tokens_out, steps_reasoned,
  steps_replayed, plays_used`. This table is the compounding proof.

Indexes: vector on `jobs.description`, BM25 on `title` and `description`.
Queries the agent runs: new jobs since last run, semantic top-k for the
candidate summary, salary percentiles by title and location, reply rate by
source, companies with 3+ open matching roles.

Division of labour rule for the pitch: hotdata answers "what is out there and
how much of it", HydraDB answers "how does it relate to me and my history".

### RocketRide

Findings from the local `.rocketride/` catalog:

- `agent_rocketride` is a wave-planning agent that takes an `llm`, a `memory`
  node, and any number of `tool` nodes via invoke connections.
- Native tool nodes we want: `tool_cognee` (talks to a Cognee server),
  `db_hydradb` (cloud memory API), `tool_http_request` (URL whitelisted),
  `tool_python` (sandboxed), `tool_slack`, `tool_sheets`, `tool_docs`,
  `tool_calendar`, `tool_gmail`, `mcp_client` (stdio or streamable HTTP).
- Sources: `chat` gives a UI for the demo, `webhook` for scripted runs.

Architecture decision: write one small MCP server (Python, FastMCP) that
exposes our own tools: `hotdata_sql`, `hotdata_search`, `hydra_cypher`,
`find_play`, `run_play`, `record_step`, `log_run_metrics`. RocketRide's
`mcp_client` node connects to it. Benefits: all glue code is local and
testable without RocketRide, the same tools can be driven from Claude Code
while debugging, and the RocketRide pipeline stays a thin JSON file.

If RocketRide runs in the cloud (staging), the MCP server must be reachable
over streamable HTTP through a tunnel (`cloudflared tunnel` or ngrok). If we
run the engine locally via Docker on port 5565, stdio works. Prefer local for
development, switch to staging for the final demo only if it is stable.

Motion actions, all human-in-the-loop, no auto-submission to real job boards:

- Tailored cover letter and resume bullets written to a Google Doc.
- Application tracker row appended to a Google Sheet.
- Ranked digest posted to Slack.
- Follow-up reminder created in Calendar at applied date plus 7 days.
- Recruiter outreach saved as a Gmail draft, never sent.

### Modiqo Rote

Rote is not a transparent proxy. Work must flow through `rote` commands
(`rote proc run curl ...`, `rote query @1 ...`) inside a workspace, then
`rote play pending write` and `rote workspace export` crystallize it into a
typed, versioned `main.ts` play. Our MCP `record_step` tool wraps
`rote proc run` so every fetch the agent makes during a novel path is
captured. Plays to produce:

1. `ingest-ats` with params `ats`, `company`. Fetch, normalize to canonical
   schema, load into hotdata. One play per ATS family.
2. `refresh-and-rank`. Run the hotdata new-jobs query, the HydraDB overlap and
   warm-path queries, merge, write the digest.
3. `apply-pack` with param `job_id`. Pull job and candidate context, call the
   LLM for the tailored text (the only reasoning step), write Doc, Sheet row,
   Calendar reminder.

Selection logic before any task: `find_play(task_pattern)` checks the Rote
registry and Cognee's skill memory. Hit means `rote play run ...` and a
`SkillRunEntry` write. Miss means reason, record, crystallize, then write the
`SkillRunEntry`. Cognee's `recall` then routes future similar tasks to the play.

Public, keyless job sources for the ingest plays (no scraping, no ToS risk):

- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`
- Lever: `https://api.lever.co/v0/postings/{slug}?mode=json`
- Ashby: `https://api.ashbyhq.com/posting-api/job-board/{slug}`
- Hacker News "Who is hiring" via the Firebase API, as a stretch.

### Snyk

`make security` runs `snyk test` and `snyk code test`. Pin dependencies in a
lock file. Keep `.env` out of git (already done). `tool_http_request` gets a
strict URL whitelist. `tool_python` gets a minimal `allowedModules` list.
Fix anything high or critical before submission; the scan reduces score.

## 4. The demo script (three runs, one chart)

Run 1, cold. Drop the resume PDF and a paragraph of preferences. Cognee builds
the candidate graph, HydraDB stores it. Ask "find me jobs at Stripe". The agent
has no play for Greenhouse: it reasons about the API, fetches, normalizes,
loads hotdata, runs the overlap query, presents five jobs. Rote captures the
ingest play. Metrics row written to `runs`.

Feedback. "Skip the second one, too senior. I applied to the first." Cognee
records a `FeedbackEntry`, HydraDB gets `APPLIED_TO` and `REJECTED {reason}`
edges, hotdata gets two `applications` rows.

Run 2. "Add Airbnb on Lever and refresh." Greenhouse ingest replays as a play
with no reasoning. Lever is novel, so it reasons and captures a second play.
Ranking now excludes senior roles and the applied job, and the Cypher "why"
query shows the path. Slack digest posted. Tokens visibly lower.

Run 3. "Refresh." Everything replays. hotdata returns only jobs first seen this
run. Calendar reminder created for the Stripe follow-up. The `runs` chart
shows tokens and latency falling across the three runs. That chart is the
closing slide.

## 5. Schedule

Tonight, before the event (the spec asks for setup today):

- Accounts and keys for all five plus Snyk. Rote hello play, post "ready &
  warmed up" in Discord.
- HydraDB: pick cloud vs Docker, confirm a Cypher round trip.
- Spike: Cognee `neo4j` provider against HydraDB Bolt. Timebox 20 minutes.
  Pre-write the JSON export sync as fallback regardless.
- hotdata: `hotdata auth`, create a database, load one CSV, run one query.
- RocketRide: staging key, run the hello pipeline, confirm `mcp_client`
  reaches a local FastMCP server.

Event day, by hour:

| Hour | Work | Done when |
|---|---|---|
| 0 to 1 | Repo layout, canonical job schema, fetchers for three ATS APIs, seed five companies, load hotdata | `hotdata query` returns jobs from all three sources |
| 1 to 3 | Cognee custom graph model, resume and prefs ingest, HydraDB sync, the four Cypher queries, hotdata queries, MCP server | Every tool callable from a Python REPL |
| 3 to 5 | RocketRide pipeline: agent, LLM, MCP client, Slack, Sheets, Docs, Calendar. Full run 1 end to end | Run 1 of the demo script works |
| 5 to 6.5 | Rote: record and crystallize the three plays, `find_play` and replay path, `SkillRunEntry`, `runs` metrics | Run 2 and run 3 replay with lower token counts |
| 6.5 to 7.5 | Rehearse all three runs twice, chart from `runs`, Snyk scan and fixes, README | Clean scan, chart renders |
| 7.5 to 8 | Submission and pitch | Submitted |

Suggested split for three people: one on memory (Cognee, HydraDB, sync), one
on insight and orchestration (hotdata, MCP server, RocketRide pipeline), one on
motion and muscle memory (Google tools, Rote plays, metrics, demo). A fourth
person takes the demo, README, Snyk, and chart from hour 5.

## 6. Risks and pre-decided cuts

- Cognee to HydraDB direct write fails: use the JSON sync. Same demo.
- RocketRide agent to tool wiring is undocumented: fall back to
  `agent_langchain` with the same `mcp_client`. Keep a local Python driver of
  the same MCP tools as a debugging harness, never as the demo path, since
  judges need RocketRide load-bearing.
- Rote cannot see the agent's native calls: it does not need to. All novel
  fetches go through `record_step`, which shells out to `rote proc run`.
- hotdata cloud outage: DuckDB over the same Parquet files keeps the demo
  alive, but say so openly. Only a last resort.
- Time overrun: cut Gmail drafts and Calendar first, then Docs. Keep Slack
  digest and Sheets tracker, they are cheap and visible.
- Never auto-submit applications. Prepare, log, and hand to the human.

## 7. Repo layout to create tomorrow morning

```
agent/            # canonical schema, ATS fetchers, ranking merge
memory/           # cognee graph model, remember helpers, hydra sync + cypher
insight/          # hotdata client, table loaders, queries
mcp_server/       # FastMCP server exposing the tools above plus rote wrappers
rocketride/       # pipeline .pipe JSON and node configs
plays/            # crystallized Rote plays (main.ts + deps.toml)
demo/             # run scripts for run 1, 2, 3 and the metrics chart
Makefile          # setup, security (snyk), demo targets
```
