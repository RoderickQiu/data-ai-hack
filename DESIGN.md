# Job-hunting agent that gets better every run

Design for the "From Memory to Muscle Memory" hackathon (Sep 11, 2026, 8 hours).
Candidate-side counterpart to the spec's "Recruiting Copilot": one job seeker,
an agent that finds, ranks, and prepares applications, and visibly needs less
reasoning and makes better calls on each successive run.

## 1. The one-line pitch

"Run 1 the agent figures out how to pull jobs, guesses at what you like, and
drafts your first application. Run 20 it replays what worked, predicts which
roles you will keep before you tell it, and spends a fraction of the tokens."

The demo is one chart with two lines: **tokens per run falling** and **match
quality per run rising**. Cheaper is the easy half. Better is the half that
proves this is a compound agent and not a cache.

## 2. Why this idea fits the judging criteria

The judges look for (a) all five tools load-bearing, (b) real repeated work
**over the course of the 8 hours**, (c) visible compounding. Job hunting has
all three, provided we fix the one thing it does not have naturally: churn.

- Each job board (Greenhouse, Lever, Ashby) has a different API shape. The first
  encounter needs reasoning; every later one is a replay. That is muscle memory.
- An `apply-pack` is one LLM call wrapped in four deterministic tool calls. That
  is the muscle memory worth showing: plumbing a human would find tedious.
- User feedback ("too senior", "applied", "got a reply") is relationship data
  about the candidate, skills, companies, and outcomes. That is graph memory.
- "What is new since last run", "median salary for this title", "which company
  just opened five roles" are aggregate questions over a large live corpus.
  That is hotdata.
- Drafting a cover letter, logging to a tracker, sending a digest, scheduling a
  follow-up are real actions. That is RocketRide.

The churn problem, stated plainly: **real Greenhouse boards barely change in a
single day.** An agent that refreshes hourly against live boards would return an
empty delta every time and the compounding story would die. Section 4 is how we
solve that honestly.

## 3. Layer mapping

| Layer | Tool | What it owns in our system |
|---|---|---|
| Structure | Cognee Cloud | Managed tenant. Ingests resume, preference text, job descriptions, and every feedback and tool trace. Extracts typed entities with a custom graph model passed as `graphModel`. |
| Memory | HydraDB | Durable candidate graph: Candidate, Skill, Job, Company, Application, Outcome, Preference. Multi-hop Cypher for ranking, prediction, and "why". |
| Insight | hotdata.dev | `jobs`, `applications`, `runs` tables over a ~1000 row corpus. SQL for new-since-last-run, salary percentiles, hiring waves, reply rate by source. Vector and BM25 index on job descriptions. |
| Motion | RocketRide | The agent loop. Wave-planning agent with Cognee, HydraDB, and our MCP tools plus Slack and Sheets (Docs, Calendar, Gmail if time). |
| Muscle memory | Modiqo Rote | Plays for `apply-pack`, `refresh-and-rank`, `ingest-ats`. Replayed instead of re-reasoned. Each run logged back into Cognee as a `SkillRunEntry`. |
| Security gate | Snyk | Whitelists and allowlists as design decisions, plus `snyk test` and `snyk code test` in a Makefile target before every commit. |

Snyk is deliberately the odd row. The spec's only stated mechanism for it is
subtractive — "security vulnerabilities found will reduce points from the final
score" — and it is absent from the five-column table that every suggested
project is mapped against. It is a gate, not a layer, so it gets gate-shaped
effort: scan early, keep the tree small, do not architect around it. The five
rows above it are the ones that must be load-bearing.

### The loop, and why no layer is decorative

Each layer's output is the next layer's input, and the cycle closes on itself.
That is the whole claim, and it is what "all five load-bearing" has to mean in
practice:

```
hotdata     what is new today, and how this role prices against 1000 live postings
   |
HydraDB     how it relates to me: skill overlap, warm paths, who replies, what I rejected
   |
Cognee      what I have said I want, recalled as preference context
   |
predict     keep or skip per role, with a reason drawn from all three
   |
RocketRide  acts: ranked digest to Slack, tracker row to Sheets, apply-pack on request
   |
human       keeps, skips, reports a reply
   |
   +--> Cognee (feedback entry) + HydraDB (outcome edge) + hotdata (applications row)
        and the next run ranks better
```

Rote sits across every step rather than inside one. Before any task `find_play`
asks Rote's registry and Cognee's skill memory; a hit replays, a miss reasons
and crystallizes. The run is written back to Cognee as a `SkillRunEntry`, so
Rote's output becomes Cognee's memory and routes the *next* `find_play`. That
edge is what makes the system compound rather than merely persist.

Read as repeated work across the 8 hours: hotdata is queried every run against a
clock that moves every run; HydraDB is read every run and written on every human
response; Cognee is recalled every run and written on every feedback and every
skill run; RocketRide executes every run; Rote either replays or records on
every step of every run. None of them is a day-0 import.

### Cognee Cloud (managed tenant, wired in this repo)

Cognee runs as a hosted tenant, not a local library. Nothing imports the
`cognee` package; `memory/cognee_client.py` is a thin `X-Api-Key` client over
the tenant's REST API. Three consequences worth stating in the pitch:

- **One graph, five laptops.** Everyone reads and writes the same memory during
  the build, so the demo graph is the graph we have been filling all day.
- **RocketRide reaches it natively.** The `tool_cognee` node takes a `base_url`
  and an `api_key` and speaks to Cognee Cloud directly. With a local library
  this would have needed a tunnel from RocketRide staging to somebody's laptop.
- **No model configuration at all.** The tenant owns its LLM and embedding
  stack, so there is no Vertex project, no ADC token to expire mid-demo, and no
  embedding dimension to keep in sync with a vector store.

Routes we use, all verified against the live tenant on 2026-09-11:

- `POST /api/v1/add_text` and `POST /api/v1/add` (multipart) for resume,
  preferences and job descriptions.
- `POST /api/v1/cognify` to build the graph. It takes a `graphModel` JSON
  schema, which is how the typed entities (`Candidate`, `Skill`, `Job`,
  `Company`, `Requirement`) get their labels instead of a generic `Entity`.
  Typed labels are what make the HydraDB Cypher readable. Pass
  `runInBackground: false` to block; a one-sentence dataset took ~17s.
- `POST /api/v1/remember/entry` for typed memory. The body is discriminated on
  `type` and the four shapes are exactly the ones this design assumes:
  `qa`, `trace`, `feedback` and `skill_run`. `SkillRunEntry` carries
  `selected_skill_id`, `task_text`, `result_summary`, `success_score`,
  `latency_ms` and `tool_trace` — the Rote-play log, with no modelling work on
  our side. `session_id` is required for qa/trace/feedback.
- `POST /api/v1/recall` for "what does this person want", and specifically to
  build the preference context the prediction step (section 5) runs against.
  **Always pass `datasets`**: omitting it silently searches `default_dataset`
  only. Before the first cognify it answers `memory_warming_up`, not an error.
- `GET /api/v1/datasets/{id}/graph?full=true` returns `{nodes, edges}` — the
  bridge to HydraDB (see below), with no export step and no file on disk.
- `POST /api/v1/skills/` registers a SKILL.md body as a `Skill` node, and
  `searchType: SKILLS` retrieves them. This is where `find_play` looks.
- Free for the demo, not yet used: `GET /api/v1/visualize/*` (hosted graph
  visualiser and live event stream) and `GET /api/v1/sessions/cost-by-model`
  (per-model spend, a second source for the cost line in section 5).

`searchType: CYPHER` and `NATURAL_LANGUAGE` are in the enum but untested on our
tenant. Do not build the "why" story on them without checking first.

### HydraDB

**Decide this before hour 0, not during it.** Two surfaces exist and the entire
"why" narrative depends on which one we get:

1. Cloud memory API (`api.hydradb.com`, `pip install hydradb-sdk`). RocketRide
   has a native `db_hydradb` node for it. Open question: does the cloud key give
   OpenCypher access, or only the SDK's `context`/`search` surface?
2. Open-source engine via Docker: Bolt on 7687, HTTP JSON on 8443. Python
   client is the standard `neo4j` driver. Fully under our control.

**Rule: if the cloud key does not answer a Cypher query by the time we sit
down, we run the OSS engine in Docker and move on.** All five demo queries
below are Cypher. Rewriting them against `graph.relations` and `graph.subgraph`
at hour 2 is not a trade we should be willing to make. Docker also unlocks the
cleanest bridge story, below.

**Whichever surface we get, the queries run through our MCP server, not through
a RocketRide node.** Two findings from the node catalog decide this. `db_hydradb`
describes its own retrieval as "HydraDB's native server-side search — no
embeddings or query language required": there is no Cypher on that node whatever
the cloud key supports. And `graph_neo4j`, which would reach an OSS instance over
Bolt, "accepts natural-language questions, translates them to Cypher queries via
an LLM" and requires an `llm` invoke connection — one LLM call per graph query
per run, a token cost that can never compound down, and six carefully written
multi-hop queries reduced to per-run guesses on stage.

Bridge from Cognee — one option now, which is a simplification, not a loss:

`GET /api/v1/datasets/{id}/graph?full=true` returns the whole graph as
`{"nodes": [...], "edges": [...]}` with our typed labels on it. `MERGE` those
over Bolt or HTTP after every `remember`. Roughly 40 lines, deterministic, and
we control the labels.

The old plan had a 20-minute spike to point Cognee's `neo4j` graph provider at
HydraDB's Bolt port and skip the bridge entirely. **That is off the table on a
managed tenant**: storage is Cognee's and no graph provider is configurable.
Cut the spike from the schedule. The JSON sync was the pre-written fallback
anyway, and the graph endpoint is a better version of it — no export call, no
intermediate file, and a `query` + `neighborhood_depth` mode if we ever want to
sync a subgraph instead of the lot.

**What stops HydraDB being a mirror of Cognee.** The sync copies typed entities
across, so a judge can fairly ask what the second graph is *for*. The answer is a
clean split of provenance: **Cognee holds what was said, HydraDB holds what
happened.** Cognee extracts `Candidate`, `Skill`, `Job`, `Company` and
`Requirement` from unstructured text — resume, preference paragraphs, job
descriptions, feedback prose. HydraDB holds those *plus* the edges the agent
writes directly from its own actions, which no text extraction can produce:
`APPLIED_TO` stamped with `virtual_day`, `GOT_REPLY`, `REJECTED` with a reason,
`PREDICTED_KEEP` and what the human actually answered. Every query below
traverses both halves — skills from the extraction, outcomes from the action log
— which is precisely why neither layer can answer them alone.

We stay **single-candidate**. Graph depth comes from routing through companies,
skills, and outcomes rather than from a second person:

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

// Company-level outcome memory: who actually replies to this candidate
MATCH (c:Candidate)-[:APPLIED_TO]->(j:Job)<-[:POSTED]-(co:Company)
OPTIONAL MATCH (j)-[:GOT_REPLY]->(o)
RETURN co.name, count(j) AS applied, count(o) AS replies
ORDER BY replies DESC

// Skill adjacency: what do roles that want my skills also want
MATCH (c:Candidate)-[:HAS_SKILL]->(s1:Skill)<-[:REQUIRES]-(j:Job)-[:REQUIRES]->(s2:Skill)
WHERE NOT (c)-[:HAS_SKILL]->(s2)
RETURN s2.name, count(DISTINCT j) AS demand ORDER BY demand DESC LIMIT 10

// Why was this rejected last time, and does the new role repeat the reason
MATCH (c:Candidate)-[r:REJECTED]->(j:Job)-[:REQUIRES]->(req)
RETURN j.title, r.reason, collect(req.name)

// Follow-ups due: applied, no reply, older than 7 virtual days
MATCH (c:Candidate)-[a:APPLIED_TO]->(j:Job)<-[:POSTED]-(co:Company)
WHERE NOT (j)-[:GOT_REPLY]->() AND a.virtual_day < $cutoff_day
RETURN co.name, j.title, a.virtual_day
```

The skill-adjacency and company-outcome queries are the two that a vector store
cannot fake, and they are both single-candidate. They also feed the prediction
step directly: "this role wants Kubernetes, which you do not have, and the last
three roles wanting it you skipped."

### hotdata.dev

Cloud only, CLI plus Python SDK (`pip install hotdata`) plus REST. Tables:

- `jobs`: canonical schema across ATS sources. Columns: `id, source, company,
  title, location, remote, salary_min, salary_max, description, url,
  posted_at, virtual_day, first_seen_run, last_seen_run`.
- `applications`: event log. `job_id, event (seen|predicted_keep|predicted_skip|
  shortlisted|applied|replied|rejected|skipped), reason, at, virtual_day, run_id`.
- `runs`: the compounding proof, one row per run. See section 5 for the full
  column list; it carries both the cost metrics and the quality metrics.

Indexes: vector on `jobs.description`, BM25 on `title` and `description`.

**hotdata needs volume to be irreducible.** Over 200 rows, a salary percentile
is noise and a judge is right to ask why it is not a graph query. Over 1000+
rows from 50 to 100 boards it becomes the only layer that can answer:

- "p50 for your title in your metro is $X across N live postings, you are
  anchoring low"
- "three of your target companies opened 5+ matching roles this virtual week,
  that is a hiring wave"
- semantic top-k over descriptions for the candidate summary
- reply rate by source, once the application log has depth

Division of labour rule for the pitch: **hotdata answers "what is out there and
how much of it", HydraDB answers "how does it relate to me and my history".**

### RocketRide

Findings from the local `.rocketride/` catalog:

- `agent_rocketride` is a wave-planning agent that takes an `llm`, a `memory`
  node, and any number of `tool` nodes via invoke connections.
- Native tool nodes we want: `tool_cognee` (point `base_url` at our tenant
  and `api_key` at `COGNEE_API_KEY`; it sends `X-Api-Key` like we do),
  `db_hydradb` (cloud memory API), `tool_http_request` (URL whitelisted),
  `tool_python` (sandboxed), `tool_slack`, `tool_sheets`, `mcp_client`
  (streamable HTTP; stdio is not available to us, see below). `tool_docs`,
  `tool_calendar`, `tool_gmail` are additive only.
- Sources: `chat` gives a UI for the demo, `webhook` for scripted and timed runs.

Architecture decision: write one small MCP server (Python, FastMCP) exposing
`hotdata_query`, `hotdata_search`, `hydra_query`, `find_play`, `run_play`,
`record_step`, `predict_fit`, `log_run_metrics`. RocketRide's `mcp_client`
connects to it.

**This is not a sixth integration; it is the only bridge to two of the five.**
The 140-node catalog has no hotdata node and no Rote node — neither layer can be
reached from RocketRide at all without it. Cognee and Slack/Sheets have good
native nodes and stay native; we do not wrap them. The server is ~200 lines and
has three consumers — the RocketRide pipeline, the hour-3 loop driver, and the
debugging harness in §8 — so the glue gets written once instead of three times.

Two rules on the tool surface, both settled before hour 1 because neither can be
retrofitted once the loop is producing chart rows:

- **Named queries, never query text.** `hydra_query(name, params)` over the six
  Cypher queries above and `hotdata_query(name, params)` over the fixed SQL, not
  `hydra_cypher(text)`. Three reasons that all point the same way: the agent
  stops spending reasoning tokens composing queries on every run, which is line
  1 of the chart; a named query is trivially a Rote play; and `snyk code test`
  never gets an LLM-driven injection surface to flag.
- **Return ids and one-line summaries, never payloads.** Tokens per run are
  dominated by what tools hand *back* into context, not by planning. A query
  that returns 200 job descriptions swamps every saving Rote produces, and
  Cognee `recall` returns *more* as the graph grows — left uncapped, the memory
  layer would make the cost line rise over the day. Full text only on an
  explicit single-job fetch, and a hard cap on recall payload.

**Transport is decided: staging only.** The coupon credits live there, so the
engine is not ours to run locally and stdio is off the table. Consequences, all
of which land at hour 0 rather than hour 4:

- The MCP server is reached over **streamable HTTP through a tunnel**
  (`cloudflared tunnel`). It goes up with the first pipeline, not with the first
  rehearsal: what we practise on has to be what we demo on.
- A quick tunnel's URL changes on every restart. Use a named tunnel, or keep the
  URL in `.env` and have the pipeline read it from there, or spend the last hour
  re-pasting URLs into node configs.
- `mcp_client` takes a `bearer` token. The tunnel is a public URL fronting SQL,
  Cypher and a Rote shell-out; it does not go out unauthenticated.
- The `webhook` source is public, which is what makes the hour-3 timed loop
  honest: `cron` or a `while` loop `curl`s the webhook, so every point on the
  chart is a real RocketRide run rather than a local script we later describe as
  one.

**Motion scope is pre-committed, not deferred.** Four Google integrations is
where 8-hour builds die: OAuth consent per node can eat 90 minutes on its own.

- **Committed, working by hour 3:** ranked digest with reasons posted to Slack;
  application tracker row appended to a Google Sheet.
- **Additive, only if the committed pair is green:** tailored cover letter into
  a Google Doc, then Calendar follow-up at applied day plus 7, then recruiter
  outreach as a Gmail draft.
- **Never:** auto-submission to a real job board. Prepare, log, hand to human.

### Modiqo Rote

Rote is not a transparent proxy. Work must flow through `rote` commands
(`rote proc run curl ...`, `rote query @1 ...`) inside a workspace, then
`rote play pending write` and `rote workspace export` crystallize it into a
typed, versioned `main.ts` play. Our MCP `record_step` tool wraps `rote proc run`
so every fetch the agent makes during a novel path is captured.

**Lead the Rote story with `apply-pack`, not with the fetchers.** "We cached an
HTTP shape" invites the response that a human could have written the fetcher in
twenty minutes. "One LLM call wrapped in four deterministic tool calls, and run
two does zero reasoning on any of the plumbing" is the muscle memory that lands.

Plays to produce, in the order they matter:

1. `apply-pack`, param `job_id`. Pull job and candidate context from HydraDB and
   Cognee, call the LLM for the tailored text (**the only reasoning step**),
   write the Sheet row, post to Slack, and the Doc/Calendar steps if built.
2. `refresh-and-rank`. Advance the virtual clock, run the hotdata new-jobs
   query, run the HydraDB overlap, warm-path and company-outcome queries, merge,
   predict, write the digest.
3. `ingest-ats` with params `ats`, `company`. Fetch, normalize to canonical
   schema, load into hotdata. One play per ATS family.

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

Security is scored, and design decisions read better than a clean scan report.
State both:

- `tool_http_request` gets a strict URL whitelist covering only the three ATS
  hosts. `tool_python` gets a minimal `allowedModules` list. Neither is a
  reaction to a finding; both are in the pipeline from the first commit.
- The agent never holds job-board credentials because every source is keyless.
- No auto-submission means no destructive outward action exists to be exploited.
- **The MCP server is what `snyk code test` will flag**, not the dependencies. A
  tool surface taking raw SQL, raw Cypher and a shell-out to the `rote` CLI is
  three injection findings waiting to happen, in code we have not written yet.
  The named-query rule above removes two by construction; `record_step` takes an
  argument list, never a command string.
- The clean 12-dependency tree was verified *before* FastMCP, the neo4j driver,
  the hotdata SDK and pyarrow land at hour 0. Re-run the scan once dependencies
  settle, not only at hour 1.
- `make security` runs `snyk test` and `snyk code test`. Dependencies pinned in
  a lock file, `.env` out of git (already done).
- **Smallest dependency tree we can defend.** Moving Cognee to a managed tenant
  took the `cognee` package (and litellm, lancedb, diskcache with it) out of the
  build. That deleted the entire `.snyk` ignore list — five suppressed findings
  with no upstream fix, all transitive under cognee. The project now tests clean
  at 12 dependencies with an empty policy file, verified 2026-09-11. "We removed
  the vulnerable subtree" is a better answer to a judge than "we justified it".

Run the scan at hour 1, not hour 7. A high or critical finding discovered with
an hour left is a score reduction we will not have time to fix.

## 4. The corpus and the virtual clock

This is the mechanic that makes an all-day compounding loop possible, and it is
the part to get right first.

**The problem.** The spec's judging note asks for real repeated work across the
whole 8 hours. But live ATS boards are near-static on a one-day horizon, so an
agent refreshing against them returns an empty delta and has nothing to compound
on. Refreshing 20 times against unchanged data proves nothing.

**The fix.** Pre-fetch once, then replay real posting history as accelerated
time.

1. **Freeze a corpus.** At hour 0, fetch ~1000 jobs from 50 to 100 public boards
   across all three ATS families. Store locally as Parquet and load into hotdata.
   Every row is real: real company, real title, real description, real
   `posted_at`.
2. **Derive a virtual clock.** The corpus spans roughly 90 days of real posting
   dates. Map them onto virtual days and stamp each row with `virtual_day`.
   Choose the window so each virtual day carries a non-trivial delta, so
   "today" genuinely has more new postings than "yesterday".
3. **Each run advances the clock by one virtual day.** Run `n` sees only
   `posted_at <= T_n`. `new_since_last_run` is the rows in
   `T_(n-1) < posted_at <= T_n`. Non-empty, real, and different every time.
4. **Say so on the slide.** "We replay 90 days of real postings at one day per
   run." Judges reward that framing; they punish discovering it themselves.

What this buys beyond churn:

- **The demo does not touch the network.** No rate limits, no a board changed
  under us, no captive wifi at the venue killing the closing run. For an 8-hour
  build this reliability is worth more than the live-data bragging right.
- **hotdata aggregates become meaningful** because 1000+ rows is enough for a
  real percentile and a real hiring-wave signal.
- **The compounding claim gets more honest, not less.** The story stops being
  "new jobs appeared" and becomes **"same corpus, better judgment"**: the agent's
  ranking improves because preference memory grew, not because the data moved.
  That is the stronger claim and it is the one we can actually prove.

**Live ingest still happens, and is still real.** During the demo the agent is
asked to add a board it has never seen. That fetch hits the real API, takes the
novel path, and gets crystallized by Rote into a play. So `ingest-ats` is
genuinely exercised against a live service; the frozen corpus is the backdrop
that makes everything else meaningful. Both halves are real, neither is fragile.

## 5. Proving it compounds: two lines, not one

A falling token count alone is a cache, and a judge can say so. The `runs` table
carries cost and quality side by side:

```
run_id, started_at, virtual_day,
wall_ms, tokens_in, tokens_out, steps_reasoned, steps_replayed, plays_used,
jobs_new, shown, predicted_keep, actual_keep, precision_at_5, prediction_accuracy
```

**Line 1, cost falling.** `tokens_in + tokens_out` and `wall_ms` per run, with
`steps_replayed / (steps_reasoned + steps_replayed)` as the supporting ratio.

**Line 2, quality rising.** Before showing the digest, the agent **predicts**
for each candidate job whether the user will keep it or skip it. The prediction
runs off Cognee `recall` of stated preferences, the HydraDB rejection-reason and
company-outcome queries, and hotdata's salary percentile for the title — so all
three memory layers are load-bearing in the quality line, not only in the
display. Both the prediction and the user's actual response are logged to
`applications`.

**`predict_fit` is a deterministic scorer, not an LLM call.** This is the
decision that stops the two lines fighting each other. An LLM prediction would
take a `recall` context that grows with the graph, so tokens per run would
*rise* exactly as memory improved. A weighted score over graph and aggregate
features holds the model fixed and lets the inputs get richer, which is also the
stronger claim: the agent did not get a better brain, it got a better memory.

Two metrics fall out:

- `precision_at_5`: of the five roles shown, how many the user did not reject.
- `prediction_accuracy`: how often the agent's guess matched the human,
  **reported on the skip class**. Measured across all five shown roles the
  number is dishonest: as ranking improves the slate becomes uniformly good, the
  user keeps nearly everything, and "keep" turns trivially predictable —
  accuracy rises because the base rate moved, not because judgment sharpened.
  Two roles of every five are therefore drawn from *outside* the top ranking to
  hold slate difficulty fixed, and the headline number is how often the agent
  called a skip correctly. Settle this at hour 1; retrofitting it at hour 6
  means re-running the whole loop.

These are not two independent proofs — on an all-top-5 slate they measure nearly
the same thing, which is the other reason the slate is mixed.

"Run 1 it guessed your taste 40% of the time. Run 20, 90%." That is memory
becoming judgment, it needs almost no typing from the human during the demo, and
it is the line that separates this from a cache with a chart.

**Seeding the curve honestly.** From hour 3 the loop runs on a timer through the
rest of the build, one virtual day per tick, with a teammate giving real
feedback as digests arrive. By demo time the chart has 20+ genuine points
produced by real runs with real human feedback, and the live demo run is simply
the last point on an existing curve. Nothing is fabricated; the feedback was
just given earlier in the day than the pitch.

## 6. The demo script

The chart already exists when the demo starts. The live portion shows the three
regimes that produced it.

**Cold (recorded at hour 3, replayed as the first points).** Resume PDF and a
paragraph of preferences go in. Cognee builds the candidate graph, HydraDB
stores it. No play exists for Greenhouse: the agent reasons about the API,
fetches, normalizes, loads hotdata, runs the overlap query, presents five jobs.
Predictions are near-random. Rote captures the ingest play.

**Live run A, novel path.** "Add Airbnb on Lever and refresh." Greenhouse ingest
replays as a play with zero reasoning. Lever is genuinely new, so the agent
reasons, hits the real API, and Rote crystallizes a second play in front of the
room. The virtual clock advances; hotdata returns the day's new postings.

**Live run B, warm path.** "Refresh." Everything replays. The digest now leads
with **why**, rendered from the Cypher path:

> Shown because you have 4 of 5 required skills, the warmest path runs through
> the role at Stripe that replied to you, and you rejected the staff-level
> variant of this title on day 6.

That paragraph is the most screenshot-able thing we will build, and it is the
thing a job board structurally cannot do. Agent predicts keep/skip for all five;
the user confirms; accuracy lands on the chart live.

**Close.** The two-line chart from `runs`. Tokens down, accuracy up, 20+ real
points across the working day.

## 7. Schedule

Pre-event setup:

- ~~Accounts and keys for all five plus Snyk.~~ Done; `sh scripts/verify-setup.sh`
  re-checks all five against the live services. Rote hello play and the Discord
  post still to do.
- ~~HydraDB: pick cloud vs Docker.~~ **Cloud provisioned.** `default-tenant` on
  `api.hydradb.com` (org `ww6ndjr2d5`) is deployed and `ready_for_ingestion`.
  **Blocking question, answer before hour 0:** does the cloud key run Cypher? If
  not, start the OSS engine in Docker and use that. Do not spend hour 2
  rewriting six queries.
- ~~Spike: Cognee `neo4j` provider against HydraDB Bolt.~~ **Dropped.** Cognee
  is a managed tenant now and its storage is not configurable. Write the
  `GET /datasets/{id}/graph` → `MERGE` sync directly; it was the fallback anyway.
- ~~Cognee: pick a local LLM backend.~~ **Gone entirely.** The tenant owns its
  model stack. `sh scripts/verify-setup.sh` proves the round trip.
- ~~hotdata: create a database, load one CSV, run one query.~~ Done via REST:
  `scripts/hotdata-setup.sh` connects the Greenhouse board API as a `rest`
  datasource and loads 200 rows into `jobs.public.jobs`. Still open: the
  canonical `jobs` schema with `virtual_day`, and the `applications` and `runs`
  tables.
- ~~An API key for the agent's model.~~ **Done: Qwen `qwen3.8-max` on an
  Alibaba Cloud MaaS workspace endpoint, in `.env`.** RocketRide does not front
  a model — every `llm_*` node requires its own `apikey`, and
  `agent_rocketride.invoke.llm` is `min 1, max 1`, so without a key there is no
  agent and no pipeline. The "no model configuration at all" claim is true of
  Cognee and false of RocketRide. Cognee cannot cover for it either: its tenant
  exposes 70 routes and the only LLM-ish ones are `/llm/custom-prompt` (a
  cognify helper that generates an extraction prompt from a `graphModel`) and
  `/llm/infer-schema`. There is no chat or completions surface.
  Verified live on 2026-09-11:
    - The node is **`llm_openai_api`, profile `custom`** — not `llm_qwen`, which
      expects a regional DashScope host and whose keys are not interchangeable
      between regions. Ours is a workspace host with a `/compatible-mode/v1`
      base URL.
    - Multi-turn tool calling works: on turn 1 it selected the right tool with
      the right enum value and params; on turn 2, fed a tool result, it chained
      to the next tool carrying an id out of the first result. That was the real
      risk in swapping models and it is cleared.
    - `qwen3.8-max` is a reasoning model, ~87 reasoning tokens per call,
      which is variance on the exact metric the demo rests on. It can be turned
      off: `enable_thinking: false` (flat in the body — DashScope wants it
      un-nested, unlike self-hosted vLLM which wants
      `chat_template_kwargs`) drops reasoning to zero with tool calling intact,
      verified at 673 -> 603 tokens on the same prompt.
      Getting it *through* RocketRide is a five-minute test rather than a known
      quantity: the node's form schema exposes only `apikey`, `base_url`,
      `model` and `modelTotalTokens`, but a pipeline component's `config` is
      typed `Record<string, unknown>`, so an extra `enable_thinking` key may
      pass straight through. `POST /task` reports bad fields one at a time, so
      trying it costs nothing once a pipeline exists.
    - If it does not pass through, the fallback is **not** an `-instruct`
      model. That naming belongs to the 2025-era open-weight checkpoints on
      this endpoint; every current model here thinks by default. The fallback
      is to pick the model that thinks *least*. Measured on the same
      three-tool prompt, one call each:

      | model | reasoning | total | thinking off | tool call |
      |---|---|---|---|---|
      | qwen3.8-flash | 81 | 678 | 572 | correct |
      | qwen3.8-max | 87 | 681 | 558 | correct |
      | kimi-k3 | 156 | 608 | 385 | correct |
      | deepseek-v4-pro | 175 | 767 | — | correct |
      | qwen3.7-plus | 192 | 759 | 603 | correct |
      | qwen3.7-max | 266 | 825 | — | correct |
      | glm-5.2 | 307 | 744 | — | correct |
      | qwen3.7-flash | 511 | 1064 | — | correct |
      | qwen3.6-flash | 542 | 1103 | — | correct |

      All nine selected the right tool with the right enum and params, so tool
      calling is not the discriminator here — reasoning overhead is.
      `qwen3.8-max` is both newer than `qwen3.7-plus` and cheaper on it, and
      `enable_thinking: false` zeroes reasoning on every model tested with tool
      calling intact.
    - **Reasoning tokens vary run to run on an identical prompt**:
      `qwen3.7-plus` returned 114 then 192, `glm-5.2` 307 then 397. That is
      noise on the y-axis of the headline chart, which is the real argument for
      turning thinking off rather than for any particular model.
    - `modelTotalTokens` is required by the node and is the context window.
      Take the real number from the Aliyun console.
- **`agent_rocketride` also requires exactly one `memory` connection**
  (`invoke.memory` is `min 1, max 1`). The node list below names tools but never
  a memory node. `memory_internal` is keyless and sufficient; `db_hydradb`
  would double as memory and graph store, which is tidier but couples the two.
- RocketRide: staging key works against `/services`. **Decided: staging only**,
  since the coupon credits live there — so the tunnel is mandatory and goes up at
  hour 0, not at rehearsal time. Still open, in priority order: does a task
  response carry token usage (line 1 of the chart depends on it), does
  `mcp_client` reach a tunnelled FastMCP server, what does one run cost against
  the coupon, and the hello pipeline. The pipeline JSON shape is not in
  the OpenAPI spec: `POST /task` takes a freeform object and only tells you what
  is missing, one error at a time.

Event day, by hour:

| Hour | Work | Done when |
|---|---|---|
| 0 to 1 | Canonical job schema with `virtual_day`. Fetch the ~1000 job corpus from 50 to 100 boards across all three ATS families. Load hotdata. Snyk scan running. **Tunnel up, one stub MCP tool callable from a staging pipeline, token usage confirmed in the task response.** | `hotdata query` returns 1000+ rows from three sources and a salary percentile that is not noise, and a staging pipeline has called one of our tools through the tunnel |
| 1 to 3 | Cognee custom graph model, resume and prefs ingest, HydraDB sync, the six Cypher queries, hotdata queries, `predict_fit`, MCP server | Every tool callable from a Python REPL; a prediction comes back with a reason |
| 3 to 4.5 | RocketRide pipeline: agent, LLM, MCP client, Slack, Sheets. First full run end to end. **Start the timed loop.** | Cold run works and the loop is ticking one virtual day per interval |
| 4.5 to 6 | Rote: record and crystallize `apply-pack` then `refresh-and-rank` then `ingest-ats`. `find_play` and replay path. `SkillRunEntry`. Metrics into `runs`. | Later runs replay with visibly lower token counts while the loop keeps running |
| 6 to 7 | "Why" rendering in the digest. Two-line chart from `runs`. Rehearse the live runs twice. Additive Google nodes only if green. Snyk clean. README. | Chart renders from real rows; both live runs rehearsed |
| 7 to 8 | Submission and pitch | Submitted |

The loop started at hour 3 keeps producing rows through every later block. By
hour 7 the chart is a curve, not three dots.

Suggested split for three people: one on memory (Cognee, HydraDB, sync,
prediction), one on insight and orchestration (corpus, virtual clock, hotdata,
MCP server, RocketRide pipeline), one on motion and muscle memory (Slack,
Sheets, Rote plays, metrics, demo). A fourth takes the README, Snyk, chart, and
pitch from hour 5.

## 8. Risks and pre-decided cuts

- **HydraDB cloud has no Cypher:** switch to OSS in Docker. Decided pre-event,
  not discovered at hour 2.
- **Cognee to HydraDB sync is slower than expected:** sync a subgraph instead
  of the whole graph — the graph endpoint takes `query` and `neighborhood_depth`.
- **Cognee Cloud outage or quota exhaustion:** quota is 1.07 GB and a text-only
  corpus will not come close, but the tenant is a single point of failure that a
  local library was not. Mitigation is the HydraDB copy: once the sync has run,
  every demo query except fresh ingestion reads from HydraDB anyway.
- **RocketRide credits run out mid-afternoon:** the coupon grants 5,000 org
  tokens and organizers top up on request, so this is a logistics item rather
  than a design constraint — ask at the first sign, not at hour 6 with a live
  run pending. Note also that the balance is dashboard-only: there is no usage
  route anywhere in the OpenAPI, so nothing can watch it for us.
- **RocketRide does not return token usage:** then line 1 has no data source.
  Cognee's `sessions/cost-by-model` measures tenant LLM spend, not the agent's,
  and is not a substitute. Since inference runs on our own key we are at least
  not wholly dependent on RocketRide reporting it, though per-run attribution
  from the provider side is awkward. Check it in the first ten minutes,
  alongside the Cypher question. If the answer is no, count at the MCP boundary
  (tool calls and returned bytes) and say openly on the slide that the cost line
  is a proxy.
- **RocketRide agent to tool wiring is undocumented:** fall back to
  `agent_langchain` with the same `mcp_client`. Keep a local Python driver of the
  same MCP tools as a debugging harness, never as the demo path, since judges
  need RocketRide load-bearing.
- **Rote cannot see the agent's native calls:** it does not need to. All novel
  fetches go through `record_step`, which shells out to `rote proc run`.
- **hotdata cloud outage:** DuckDB over the same frozen Parquet keeps the demo
  alive, and say so openly. Last resort only.
- **Prediction accuracy does not improve:** it is still an honest chart, and the
  token line still stands. Do not tune the metric to make the line go up; a
  flat quality line with a real explanation beats a curve we cannot defend.
- **Time overrun:** cut in this order. Gmail draft, then Calendar, then Docs,
  then the third ATS family. Keep Slack digest, Sheets tracker, the virtual
  clock, and the two-line chart. Those four are the project.
- **Never auto-submit applications.** Prepare, log, and hand to the human.

## 9. Repo layout

```
agent/            # canonical schema, ATS fetchers, virtual clock, ranking merge
memory/           # cognee cloud client, graph model, remember helpers, hydra sync + cypher
insight/          # hotdata client, table loaders, queries, corpus build
mcp_server/       # FastMCP server exposing the tools above plus rote wrappers
rocketride/       # pipeline .pipe JSON and node configs
plays/            # crystallized Rote plays (main.ts + deps.toml)
demo/             # loop driver, run scripts, the two-line metrics chart
data/             # frozen job corpus (parquet), gitignored
Makefile          # setup, security (snyk), corpus, loop, demo targets
```
