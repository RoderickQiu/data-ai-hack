# Job-hunting agent that gets better every run

Design for the "From Memory to Muscle Memory" hackathon (Sep 11, 2026, 8 hours).
Candidate-side counterpart to the spec's "Recruiting Copilot": one job seeker,
an agent that finds, ranks, and prepares applications, and visibly needs less
reasoning, asks fewer questions, and makes better calls on each successive run.

## 1. The one-line pitch

"Run 1 the agent figures out how to pull jobs, guesses at what you like, asks
you six questions, and drafts your first application. Run 20 it replays what
worked, predicts which roles you will keep before you tell it, asks nothing, and
spends a fraction of the tokens."

The demo is one chart with three lines: **tokens per run falling**, **match
quality per run rising**, and **human touches per run falling to zero**. Cheaper
is the easy half. Better, and needing you less, is the half that proves this is
a compound agent and not a cache.

## 2. Goals, non-goals, and proof moments

### Goals

- **G1. Source.** Pull real roles from real public ATS boards, continuously.
- **G2. Remember.** Hold verified facts about the candidate — claims, skills,
  standard answers — with provenance back to the document they came from.
- **G3. Learn preferences** from keep, skip, and "not for me" signals, with the
  inferred rule confirmed by the human before it changes any ranking.
- **G4. Prepare.** Produce an apply-pack per role using only verified claims.
- **G5. Compound visibly.** Tokens, wall time, and questions-to-human per run
  all trend down while prediction accuracy trends up.
- **G6. Earn trust.** Approvals are the training phase, not the product. Once
  the agent's agreement record is strong enough it asks to run a step alone,
  and the human switches it on.

### Non-goals

Stated up front because each one is a place an 8-hour build goes to die.

- **Auto-submission to a real job board.** Ever. Prepare, log, hand to human.
- **A mock ATS to submit into instead.** Building three fake form pages buys a
  form-filling demo we do not need; the replay story lives in `ingest-ats` and
  `apply-pack`, which are real. See §12.
- **Scraping, LinkedIn, Easy Apply, CAPTCHA, or any credentialed board.** Every
  source is a public keyless API, and it is read exactly once.
- **Fetching anything after hour 0.** The corpus is static. Jobs are released to
  the agent on an assigned day schedule (§5); the demo never touches the
  network, so the venue wifi cannot end the pitch.
- **Writing new resume bullets.** The agent selects and orders verified claims.
  It never invents one. See §6.
- **Multi-candidate.** Schema carries a `candidate_id` so it is not a rewrite
  later, but the product runs for one person and graph depth comes from
  companies, skills, and outcomes rather than from a second user.
- **Outcome tracking from a real inbox.** Gmail polling, rejection-email
  classification, and resume A/B experiments are a good product and the wrong
  hackathon. See §12.

### Proof moments

Every flow below exists to produce one of these. **If a feature feeds none of
them, it gets cut** — this table is the cut test, and it is the reason the
schedule in §9 survives contact with hour 5.

| # | Proof moment | What a judge sees | Layers carrying it |
|---|---|---|---|
| P1 | "It stopped asking me things" | Questions to human per apply-pack: ~6 on run 1, 0 by run ~6 | Cognee, HydraDB |
| P2 | "It learned what I want" | After 3 rejections sharing a reason, Slack proposes a preference rule; confirming it visibly reorders the next digest | Cognee, HydraDB, hotdata |
| P3 | "Muscle memory" | A day's `refresh-and-rank` replays end to end in seconds with near-zero tokens; the hour-0 ingest log shows the same step down across 50-odd boards | Rote, RocketRide |
| P4 | "Partial muscle memory" | A role with a screening question the play has never seen replays the known fields and sends only the novel one to the LLM | Rote, RocketRide |
| P5 | "It never lies" | A role requires a skill with no supporting verified claim; the pack flags the gap instead of claiming it | HydraDB, Cognee |
| P6 | "It earned autonomy" | The agent cites its own agreement record, asks to shortlist unsupervised, the human toggles it on, the next run needs zero clicks | HydraDB, RocketRide |
| P7 | The chart | Tokens, wall time, questions, and prediction accuracy across 20+ real runs | hotdata |

## 3. Why this idea fits the judging criteria

The judges look for (a) all five tools load-bearing, (b) real repeated work
**over the course of the 8 hours**, (c) visible compounding. Job hunting has
all three, provided we fix the one thing it does not have naturally: churn.

- Each job board (Greenhouse, Lever, Ashby) has a different API shape. The first
  encounter needs reasoning; the next 50 are replays. That is muscle memory, and
  it is banked during the hour-0 corpus build rather than on stage.
- **A day's judgment pass is the repeated work.** Rank, predict, digest, absorb
  the response — 20+ times across the build, the same shape every time, getting
  cheaper and sharper on each pass.
- An `apply-pack` is one LLM call wrapped in four deterministic tool calls. That
  is the muscle memory worth showing: plumbing a human would find tedious.
- User feedback ("too senior", "applied", "got a reply") is relationship data
  about the candidate, skills, companies, and outcomes. That is graph memory.
- "What was released today", "median salary for this title", "which company
  just opened five roles" are aggregate questions over a thousand-row corpus.
  That is hotdata.
- Drafting a cover letter, logging to a tracker, sending a digest, scheduling a
  follow-up are real actions. That is RocketRide.

The churn problem, stated plainly: **real Greenhouse boards barely change in a
single day.** An agent that refreshes hourly against live boards would return an
empty delta every time and the compounding story would die. Section 5 is how we
solve that honestly.

## 4. Layer mapping

| Layer | Tool | What it owns in our system |
|---|---|---|
| Structure | Cognee Cloud | Managed tenant. Ingests resume, preference text, job descriptions, and every feedback and tool trace. Extracts typed entities with a custom graph model passed as `graphModel`. |
| Memory | HydraDB | Durable candidate graph: Candidate, Claim, Skill, Job, Company, Application, Outcome, Preference, Play. Multi-hop Cypher for ranking, prediction, and "why". |
| Insight | hotdata.dev | `jobs`, `applications`, `runs` tables over a ~1000 row corpus. SQL for new-since-last-run, salary percentiles, hiring waves, reply rate by source. Vector and BM25 index on job descriptions. |
| Motion | RocketRide | The agent loop. Wave-planning agent with Cognee, HydraDB, and our MCP tools plus Slack and Sheets (Docs, Calendar, Gmail if time). |
| Muscle memory | Modiqo Rote | Plays for `apply-pack`, `refresh-and-rank`, `ingest-ats`. Replayed instead of re-reasoned. Each run logged back into Cognee as a `SkillRunEntry`. |
| Security gate | Snyk | Whitelists and allowlists as design decisions, plus `snyk test` and `snyk code test` in a Makefile target before every commit. |

### Owns, and does not own

Overlap between layers is the most common integration failure, and it is what a
judge probes when they ask "why do you need two graphs". Each layer gets an
explicit contract, and every one of the six answers below is falsifiable against
the queries in this document.

| Layer | Owns | Explicitly does not own |
|---|---|---|
| Cognee | Turning messy text — resume, preference paragraphs, job descriptions, free-text feedback — into typed entities with provenance. Skill registry for `find_play`. | Deciding anything. It extracts; app logic and Cypher decide. It is not the long-term store of what the agent *did*. |
| HydraDB | The durable candidate graph and every relationship question over it: claim-to-requirement coverage, prior answers, warm paths, preference rules, play index. | The bulk posting corpus, and analytics over run metrics. A job enters HydraDB only once someone acts on it. |
| hotdata | The firehose: all fetched postings, hard filters, vector and BM25 search, aggregates, and the `runs` metrics table. | Anything relationship-shaped about the candidate. |
| RocketRide | Sequencing. Pipelines, tool calls, LLM calls, Slack and Sheets writes, write-backs to the other layers. | Remembering how a task was done (Rote), or deciding what is worth remembering (Cognee). |
| Rote | Capturing a successful novel path once and replaying it deterministically thereafter, at exact or partial match. | Novel reasoning. Any step with no captured path falls back to the LLM. |
| Snyk | Scanning code and dependencies at three checkpoints through the build, not once at the end. | Nothing at runtime. It is a gate, not a layer. |

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
HydraDB     how it relates to me: claim coverage, warm paths, who replies, what I rejected
   |
Cognee      what I have said I want, recalled as preference context
   |
predict     keep or skip per role, with a reason drawn from all three
   |
RocketRide  acts: ranked digest to Slack, tracker row to Sheets, apply-pack on request
   |
human       keeps, skips, gives a reason, answers a question once, reports a reply
   |
   +--> Cognee (feedback + standard answer) + HydraDB (outcome edge, preference rule)
        + hotdata (applications row) and the next run ranks better and asks less
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
  schema, which is how the typed entities (`Candidate`, `Claim`, `Skill`,
  `Job`, `Company`, `Requirement`, `StandardAnswer`) get their labels instead of
  a generic `Entity`. Typed labels are what make the HydraDB Cypher readable.
  Pass `runInBackground: false` to block; a one-sentence dataset took ~17s.
- `POST /api/v1/remember/entry` for typed memory. The body is discriminated on
  `type` and the four shapes are exactly the ones this design assumes:
  `qa`, `trace`, `feedback` and `skill_run`. `SkillRunEntry` carries
  `selected_skill_id`, `task_text`, `result_summary`, `success_score`,
  `latency_ms` and `tool_trace` — the Rote-play log, with no modelling work on
  our side. `session_id` is required for qa/trace/feedback.
- `POST /api/v1/recall` for "what does this person want", and specifically to
  build the preference context the prediction step (section 7) runs against.
  **Always pass `datasets`**: omitting it silently searches `default_dataset`
  only. Before the first cognify it answers `memory_warming_up`, not an error.
- `GET /api/v1/datasets/{id}/graph?full=true` returns `{nodes, edges}` — the
  bridge to HydraDB (see below), with no export step and no file on disk.
- `POST /api/v1/skills/` registers a SKILL.md body as a `Skill` node, and
  `searchType: SKILLS` retrieves them. This is where `find_play` looks.
- Free for the demo, not yet used: `GET /api/v1/visualize/*` (hosted graph
  visualiser and live event stream) and `GET /api/v1/sessions/cost-by-model`
  (per-model spend, a second source for the cost line in section 7).

`searchType: CYPHER` and `NATURAL_LANGUAGE` are in the enum but untested on our
tenant. Do not build the "why" story on them without checking first.

**Job descriptions are untrusted input to this layer.** The extraction call that
reads a JD has no tool access and returns structured JSON only, because a job
description is a text field a stranger controls. See §8.

### HydraDB

**Decide this before hour 0, not during it.** Two surfaces exist and the entire
"why" narrative depends on which one we get:

1. Cloud memory API (`api.hydradb.com`, `pip install hydradb-sdk`). RocketRide
   has a native `db_hydradb` node for it. Open question: does the cloud key give
   OpenCypher access, or only the SDK's `context`/`search` surface?
2. Open-source engine via Docker: Bolt on 7687, HTTP JSON on 8443. Python
   client is the standard `neo4j` driver. Fully under our control.

**Rule: if the cloud key does not answer a Cypher query by the time we sit
down, we run the OSS engine in Docker and move on.** All the demo queries
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
per run, a token cost that can never compound down, and eight carefully written
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
happened.** Cognee extracts `Candidate`, `Claim`, `Skill`, `Job`, `Company` and
`Requirement` from unstructured text — resume, preference paragraphs, job
descriptions, feedback prose. HydraDB holds those *plus* the edges the agent
writes directly from its own actions, which no text extraction can produce:
`APPLIED_TO` stamped with `day`, `GOT_REPLY`, `REJECTED` with a reason,
`PREDICTED_KEEP` and what the human actually answered, `HOLDS` on a confirmed
preference rule, `EXECUTED_BY` on the play that did the work. Every query below
traverses both halves — claims from the extraction, outcomes from the action log
— which is precisely why neither layer can answer them alone.

#### Nodes worth naming

Only the ones that carry a proof moment. Everything else is an attribute.

| Node | Key fields | Why it exists |
|---|---|---|
| `Candidate` | candidate_id, base_location, targets | One in v1; the id is on every node and every query takes it as a parameter |
| `Claim` | claim_id, text, kind (bullet, summary, fact), status (verified, unverified, retired), source_doc | Resume bullets are claims. **Only `verified` claims may appear in an apply-pack** (P5) |
| `Evidence` | source_doc, excerpt_ref | Provenance for a claim; what makes "verified" mean something |
| `Skill` | name, canonical_name | Normalized so "LLM evals" and "AI evals" merge |
| `StandardAnswer` | key, text, confirmed_at | Work authorization, notice period, salary band, relocation. Asked once, never again (P1) |
| `Question` | question_id, canonical_text, scope | A screening question seen in the wild, canonicalized so a near-match reuses the answer |
| `Job` / `Company` | id, title, company, source, ats_type, url | Jobs enter only when acted on |
| `Requirement` | text, kind (required, preferred), skill_ref | Extracted by Cognee from the JD |
| `Gap` | skill_ref, job_ref | A requirement with no supporting verified claim. Surfaced, never papered over |
| `Signal` | kind (keep, skip, not_for_me), reason_tags, reason_text, day | The raw episodic event. See the taxonomy in §6 |
| `Preference` | rule (structured), status (hypothesis, active, rejected), confidence, evidence_count | See §6. Rules are structured, never prose |
| `Play` | play_id, rote_ref, task_type, fingerprint, success_count, last_used | The index Rote replays through |
| `AutonomyState` | domain, state, readiness_value, decisions_since_prompt, recent_overrides | See §8 |

Key edges:

```
(Candidate)-[:HAS_CLAIM]->(Claim)-[:SUPPORTED_BY]->(Evidence)
(Claim)-[:DEMONSTRATES]->(Skill)
(Candidate)-[:HAS_SKILL]->(Skill)
(Company)-[:POSTED]->(Job)-[:REQUIRES]->(Requirement)-[:ABOUT]->(Skill)
(Job)-[:HAS_GAP]->(Gap)
(Candidate)-[:GAVE]->(Signal)-[:ON]->(Job)
(Preference)-[:INFERRED_FROM]->(Signal)
(Candidate)-[:HOLDS]->(Preference)
(Candidate)-[:APPLIED_TO|REJECTED|PREDICTED_KEEP]->(Job)
(Job)-[:GOT_REPLY]->(Outcome)
(Application)-[:USED_CLAIM]->(Claim)
(Application)-[:ANSWERED]->(Question)-[:WITH]->(Answer)
(Application)-[:EXECUTED_BY]->(Play)
```

#### The queries

We stay **single-candidate**. Graph depth comes from routing through companies,
skills, claims, and outcomes rather than from a second person. Every query takes
`$candidate_id`; it is elided below for readability.

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

// Follow-ups due: applied, no reply, older than 7 days
MATCH (c:Candidate)-[a:APPLIED_TO]->(j:Job)<-[:POSTED]-(co:Company)
WHERE NOT (j)-[:GOT_REPLY]->() AND a.day < $cutoff_day
RETURN co.name, j.title, a.day

// Claim coverage: which requirements have verified evidence, and which are gaps.
// This is the query that makes P5 structural rather than a prompt instruction.
MATCH (j:Job {id: $job_id})-[:REQUIRES]->(r:Requirement)-[:ABOUT]->(s:Skill)
OPTIONAL MATCH (c:Candidate)-[:HAS_CLAIM]->(cl:Claim {status: 'verified'})-[:DEMONSTRATES]->(s)
RETURN r.text, r.kind, collect(cl.claim_id) AS supporting_claims

// Reuse a prior approved answer to a similar screening question. Every hit here
// is one question the human is not asked again — line 3 of the chart.
MATCH (c:Candidate)<-[:BY]-(a:Application)-[:ANSWERED]->(q:Question)-[:WITH]->(ans:Answer {approved: true}),
      (a)-[:FOR]->(:Job)-[:POSTED_BY]->(co:Company)
WHERE q.question_id IN $similar_question_ids
RETURN ans.text, q.scope, co.name ORDER BY a.day DESC LIMIT 3

// Active preference rules, applied deterministically by predict_fit
MATCH (c:Candidate)-[:HOLDS]->(p:Preference {status: 'active'})
RETURN p.rule, p.confidence
```

The skill-adjacency, company-outcome and claim-coverage queries are the three a
vector store cannot fake, and they are all single-candidate. They also feed the
prediction step directly: "this role wants Kubernetes, which you have no
verified claim for, and the last three roles wanting it you skipped."

### hotdata.dev

Cloud only, CLI plus Python SDK (`pip install hotdata`) plus REST. Tables:

- `jobs`: canonical schema across ATS sources. Columns: `id, source, company,
  title, location, remote, salary_min, salary_max, description, url,
  posted_at, release_day, first_seen_run, last_seen_run`.
- `applications`: event log. `job_id, event (seen|predicted_keep|predicted_skip|
  shortlisted|applied|replied|rejected|skipped|not_for_me), reason_tags, reason,
  at, day, run_id`.
- `runs`: the compounding proof, one row per run. See section 7 for the full
  column list; it carries the cost, quality, and human-effort metrics together.

Indexes: vector on `jobs.description`, BM25 on `title` and `description`.

**hotdata needs volume to be irreducible.** Over 200 rows, a salary percentile
is noise and a judge is right to ask why it is not a graph query. Over 1000+
rows from 50 to 100 boards it becomes the only layer that can answer:

- "p50 for your title in your metro is $X across N live postings, you are
  anchoring low"
- "three of your target companies opened 5+ matching roles this week,
  that is a hiring wave"
- semantic top-k over descriptions for the candidate summary
- reply rate by source, once the application log has depth

Ranking is deliberately two-stage so the two data layers do distinct work and
neither is doing the other's job:

1. **hotdata narrows.** SQL hard filters (title class, location or radius,
   released within N days, drop disqualifying sponsorship language, drop
   anything already signalled on), then top 30 by vector similarity to the
   candidate profile embedding.
2. **HydraDB judges.** Claim coverage, warm path, company outcome memory and
   active preference rules over those 30 only.

Running the graph queries against 1000 rows would be slow and pointless;
running the percentile in Cypher would be worse. Division of labour rule for the
pitch: **hotdata answers "what is out there and how much of it", HydraDB answers
"how does it relate to me and my history".**

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

**Pipelines split at every human gate, and never block waiting for a person.**
A pipeline that sits open waiting for a Slack click is a pipeline that times
out, cannot be restarted cleanly, and produces one enormous Rote capture
spanning a human pause. Each decision starts a *new* pipeline instead:

| Pipeline | Trigger | Ends with |
|---|---|---|
| **P-A: judge the day** | Timer (the hour-3 loop) or "next day" in chat | Clock advances one day; today's releases plus the unjudged carry-over are ranked and predicted; digest to Slack; `runs` row written |
| **P-B: apply-pack** | Human taps a role in the digest, or D1 autonomy selects it | Pack in Slack and Sheets, questions asked if any remain unanswered |
| **P-C: record feedback** | Human answers, keeps, skips, or reports a reply | Cognee feedback entry, HydraDB edges, `applications` rows, preference check |

Runs stay short, restartable, and each is a clean unit for Rote to capture.

Architecture decision: write one small MCP server (Python, FastMCP) exposing
`hotdata_query`, `hotdata_search`, `hydra_query`, `find_play`, `run_play`,
`record_step`, `predict_fit`, `ask_human`, `log_run_metrics`. RocketRide's
`mcp_client` connects to it.

**This is not a sixth integration; it is the only bridge to two of the five.**
The 140-node catalog has no hotdata node and no Rote node — neither layer can be
reached from RocketRide at all without it. Cognee and Slack/Sheets have good
native nodes and stay native; we do not wrap them. The server is ~250 lines and
has three consumers — the RocketRide pipeline, the hour-3 loop driver, and the
debugging harness in §11 — so the glue gets written once instead of three times.

Two rules on the tool surface, both settled before hour 1 because neither can be
retrofitted once the loop is producing chart rows:

- **Named queries, never query text.** `hydra_query(name, params)` over the
  Cypher above and `hotdata_query(name, params)` over the fixed SQL, not
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

- **Committed, working by hour 3:** ranked digest with reasons and predictions
  posted to Slack; application tracker row appended to a Google Sheet.
- **Additive, only if the committed pair is green:** tailored cover letter into
  a Google Doc, then Calendar follow-up at applied day plus 7, then recruiter
  outreach as a Gmail draft.
- **Never:** auto-submission to a real job board. Prepare, log, hand to human.

**Slack go/no-go at hour 3.** Interactive Slack buttons are the single riskiest
piece of motion, because a button click has to round-trip back into a pipeline.
If a click has not round-tripped by hour 3, degrade to the `chat` source plus a
numbered reply ("keep 1,3; not-for-me 2 too senior") and keep the Sheet as the
written record. The demo script does not change, only the surface does.

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

1. `apply-pack`, param `job_id`. Pull the job, the claim-coverage result and the
   standard answers from HydraDB and Cognee, call the LLM **once** to select and
   order claims and draft the tailored text (**the only reasoning step**), run
   the citation validator, write the Sheet row, post to Slack, and the
   Doc/Calendar steps if built.
2. `refresh-and-rank`, the daily judgment pass. Advance the day clock, run the
   hotdata released-today query, pull the unjudged carry-over, run the HydraDB
   overlap, warm-path, company-outcome and preference-rule queries, merge,
   predict, write the digest.
3. `ingest-ats` with params `ats`, `company`. Fetch, normalize to canonical
   schema, load into hotdata. One play per ATS family.

**Fingerprint and match mode.** A play is not selected by name alone but by a
fingerprint of the shape it was captured against —
`hash(task_type + ats_family + sorted(normalized_field_names))` for ingest,
`hash(task_type + required_inputs)` for apply-pack. The match mode decides how
much reasoning the run costs:

| Match | Condition | Mode | Proof |
|---|---|---|---|
| Exact | Same fingerprint | Full replay, zero LLM calls on the plumbing | P3 |
| Partial | Same family, ≥70% field overlap | Replay the known steps, send only the novel fields to the LLM, then capture the extended path as a new play version | P4 |
| None | Otherwise | First run: reason, `record_step`, crystallize | baseline |

Partial replay is the more interesting demo beat than exact replay, because
exact replay is what a judge expects a cache to do and partial replay is not.
It is also what keeps the system honest as boards drift: a play whose replay
fails is marked `stale` and the next run falls back to the novel path rather
than producing a wrong result quietly.

Selection logic before any task: `find_play(task_pattern, fingerprint)` checks
the Rote registry and Cognee's skill memory. Hit means `rote play run ...` and a
`SkillRunEntry` write. Miss means reason, record, crystallize, then write the
`SkillRunEntry`. Cognee's `recall` then routes future similar tasks to the play.

Public, keyless job sources for the ingest plays, read once at hour 0 to build
the static corpus (no scraping, no ToS risk):

- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`
- Lever: `https://api.lever.co/v0/postings/{slug}?mode=json`
- Ashby: `https://api.ashbyhq.com/posting-api/job-board/{slug}`
- Hacker News "Who is hiring" via the Firebase API, as a stretch.

Keep every raw payload. It is what lets an ingest play be re-run offline later,
and it is the only copy — after hour 0 nothing re-fetches.

### Snyk, and the guardrails it scores

Security is scored, and design decisions read better than a clean scan report.
State both. The mitigations below are in the build from the first commit, not
bolted on after a finding:

| Risk | Mitigation |
|---|---|
| Fabricated experience | The tailoring LLM returns `claim_id`s, not prose facts. Every factual sentence in generated text carries a `[claim:ID]` tag; a deterministic validator checks each id exists, is `verified`, and belongs to this candidate, and fails the pack otherwise. Gaps are shown, never papered over (P5) |
| Prompt injection in job descriptions | JD text is data, not instruction. The extraction call that reads a JD has no tool access and returns structured JSON only. No JD text ever reaches the planning agent unfiltered |
| Injection through our own tool surface | **The MCP server is what `snyk code test` will flag**, not the dependencies. A surface taking raw SQL, raw Cypher and a shell-out to the `rote` CLI is three findings waiting to happen, in code we have not written yet. The named-query rule removes two by construction; `record_step` takes an argument list, never a command string |
| Unwanted outward action | No auto-submission exists to exploit. `tool_http_request` gets a strict URL whitelist covering only the three ATS hosts; `tool_python` gets a minimal `allowedModules` list |
| Runaway autonomy | Autonomy is per domain, earned from the human's agreement record, granted only by an explicit toggle, auto-revoked after repeated overrides, and never extends to submission or to facts about the candidate (§8) |
| Credential exposure | The agent holds no job-board credentials because every source is keyless. `.env` out of git (already done) |
| PII leakage | Resume content stays in our stores. `runs` rows carry counts, never content. No PII in URLs or logs |
| XSS from board HTML | JD HTML is sanitized before storage and before it is rendered anywhere |
| Dependency vulnerabilities | `make security` runs `snyk test` and `snyk code test`; dependencies pinned in a lock file; scans at hours 1, 5 and 7, not once at the end |

**Smallest dependency tree we can defend.** Moving Cognee to a managed tenant
took the `cognee` package (and litellm, lancedb, diskcache with it) out of the
build. That deleted the entire `.snyk` ignore list — five suppressed findings
with no upstream fix, all transitive under cognee. The project now tests clean
at 12 dependencies with an empty policy file, verified 2026-09-11. "We removed
the vulnerable subtree" is a better answer to a judge than "we justified it".
That clean tree was verified *before* FastMCP, the neo4j driver, the hotdata SDK
and pyarrow land at hour 0, which is why the scan re-runs once dependencies
settle rather than only at hour 1.

Run the first scan at hour 1, not hour 7. A high or critical finding discovered
with an hour left is a score reduction we will not have time to fix.

## 5. The static corpus and the day clock

This is the mechanic that makes an all-day compounding loop possible, and it is
the part to get right first.

**The problem.** The spec's judging note asks for real repeated work across the
whole 8 hours. But live ATS boards are near-static on a one-day horizon, so an
agent refreshing against them returns an empty delta and has nothing to compound
on. Refreshing 20 times against unchanged data proves nothing. And fetching live
during the demo buys nothing except a way for the venue wifi to end the pitch.

**The fix.** One static database, built at hour 0 and never written to again.
Each job carries an assigned **release day**, and the agent does one full
judgment pass per day.

1. **Freeze the corpus.** At hour 0, fetch ~1000 jobs from 50 to 100 public
   boards across all three ATS families. Store as Parquet, load into hotdata,
   and stop. **Nothing fetches anything after hour 0.** Every row is real: real
   company, real title, real description, real requirements.
2. **Assign release days.** Stamp each row with a `release_day` — 9.1, 9.2, 9.3
   and so on — spread across ~30 days so every day carries a non-trivial batch.
   The assignment is ours: it is a schedule for replaying a static corpus, not a
   claim about when anything was really posted. The real `posted_at` stays on
   the row as a separate column, so the schedule is auditable and swappable for
   the true dates later if we want them.
3. **One run is one day.** Run `n` is day `T_n`, and it sees only
   `release_day <= T_n`. `new_today` is the rows with
   `release_day == T_n`. Non-empty, different every day, and entirely offline.
4. **Judgment happens every day, not just on the new rows.** A day's pass is:
   pull today's releases, add any earlier role still unjudged, rank the pooled
   set, predict keep or skip on each, post the digest, take the human's
   response, write it back. Yesterday's memory changes today's ranking of a
   role that has been sitting in the pool since day 3 — which is exactly the
   compounding we are claiming, and it is visible without a single new row.
5. **Say so on the slide.** "One static corpus of a thousand real jobs, released
   to the agent on a schedule, one day per run." Judges reward that framing;
   they punish discovering it themselves.

What this buys:

- **The demo cannot fail on the network.** No rate limits, no board changing
  under us, no captive wifi killing the closing run. For an 8-hour build this
  reliability is worth more than any live-data bragging right.
- **The day delta is guaranteed non-trivial**, because we chose it. Deriving it
  from real `posted_at` risks a day with four postings and a day with two
  hundred, which makes the chart lumpy for reasons that have nothing to do with
  the agent.
- **hotdata aggregates become meaningful** because 1000+ rows is enough for a
  real percentile and a real hiring-wave signal.
- **The compounding claim gets more honest, not less.** The story is not "new
  jobs appeared" but **"same corpus, better judgment"**: the ranking improves
  because preference memory grew, not because the data moved. That is the
  stronger claim and it is the one we can actually prove.

**What the clock is not for.** It supplies the daily delta. It does **not**
carry the learning proof. P1 through P6 are all functions of repetition the
human drives directly: questions drop because answers accumulate, preferences
form because three rejections shared a reason, replay gets faster because a play
exists, autonomy is earned because an agreement record grew. None of them waits
on the clock. That separation matters, because it means **if the clock breaks at
hour 4 the compounding story survives**: re-judge the same day repeatedly and
every line on the chart still moves.

**Where the honesty line sits, and how to say it.** Two of the three things on
screen are real and one is ours, so name which is which before anyone asks:

| Thing | Real or ours |
|---|---|
| The jobs — companies, titles, descriptions, requirements, salary bands | Real, pulled from live public ATS APIs at hour 0 |
| The release schedule — which job the agent sees on which day | Ours, assigned to replay a static corpus as a timeline |
| Every number on the chart — tokens, questions, predictions, accuracy | Real, logged by runs that actually happened |

The assigned schedule is the only synthetic element in the build, it affects
what the agent *sees* and never what it *does*, and nothing on the chart depends
on it being true.

**Where Rote is exercised, now that nothing fetches live.** The ingest plays are
still real repeated work, just earlier: building the corpus means running
`ingest-ats` across 50 to 100 boards at hour 0, so the Greenhouse play is
captured once and replayed dozens of times against genuinely different company
boards, with the token counts logged. That is stronger evidence than the two
replays a live demo beat would have shown; it simply happened before the pitch
rather than during it, so the chart has to carry it. The raw payloads are kept,
so a play can be re-run offline in front of the room against a stored response.

During the demo the replay proof rides on the two plays that run every day
anyway — `refresh-and-rank` (20+ replays by demo time) and `apply-pack` — plus
the partial-replay beat in §9, which comes from a screening-question shape the
play has not seen rather than from a new board.

## 6. What the agent knows, and how it learns

Three kinds of memory, and the rules that keep each honest. This section is what
separates "the agent remembers things" from "the agent got better", and most of
it is cheap — the expensive part is deciding it before the loop starts writing
chart rows.

### 6.1 Verified claims, and the rule that the agent never invents

Every resume bullet enters as a `Claim` with `status: verified` — the candidate
wrote it and stands behind it. Everything the extraction pipeline infers from
looser material (preference transcripts, past cover letters) enters as
`unverified` and appears in a one-time Slack verification digest with
Confirm / Fix / Discard.

**Tailoring means selection and ordering of verified claims. The agent never
writes a new one.** The LLM step in `apply-pack` returns a list of `claim_id`s
plus connective prose; the validator in §4 rejects any factual sentence without
a citation to a verified claim. Two things fall out of this, and both are worth
saying on stage:

- It is the cheapest possible anti-hallucination story: structural, not a prompt
  instruction, and demonstrable in one screenshot (P5).
- It holds the token line down. The LLM emits ids and a few sentences rather
  than a whole document, so the output cost of `apply-pack` does not grow as the
  claim graph does.

Where a requirement has no supporting verified claim, the pack writes a `Gap`
and says so: "this role asks for Kubernetes depth; you have no verified claim
for it — the pack leads with your adjacent Terraform work instead."

### 6.2 Standard answers, and why the question count goes to zero

Preparing a pack needs facts the resume does not carry: work authorization,
notice period, salary band, relocation, "why this company". On run 1 the agent
does not have them and asks, through `ask_human`. Each answer is written once as
a `StandardAnswer` in Cognee and HydraDB and **never asked again**.

Resolution order, per question, and the whole of P1 lives in this ladder:

1. **StandardAnswer** exists → filled, zero tokens, zero human touches.
2. **Prior approved Answer** to a canonically similar `Question` → reused as a
   draft, flagged as reused.
3. **Derivable from verified claims** → generated, marked generated, needs one
   approval.
4. **Unknown fact about the human** → asked. Always. This is the one thing the
   agent never guesses and never earns autonomy over (§8).

`questions_asked` per run is logged to `runs`, and it is the most legible line
on the chart because nobody needs the metric explained.

### 6.3 Preference learning: signals, hypotheses, confirmed rules

**The signal taxonomy matters more than it looks.** Three responses, and only
one of them teaches:

- **keep** — positive signal, feeds `precision_at_5` and the D1 agreement record.
- **skip** — "not now". Carries no preference weight at all. Without this,
  every busy afternoon becomes fake evidence that the candidate dislikes
  something.
- **not for me** — the teaching signal, and the only one that asks for a reason.
  Reason chips ("too senior", "company too large", "wrong domain", "location",
  "comp") plus optional free text, which Cognee turns into structured tags.

The learning loop, which is P2 end to end:

1. HydraDB counts signals sharing a reason tag or an attribute (size bucket,
   industry, seniority, location) across the last 20 signals.
2. At `evidence_count >= 3`, with no previously rejected rule matching, create a
   `Preference {status: hypothesis}`.
3. Slack asks: "I think you prefer companies under 2,000 people — three
   not-for-mes in a row (X, Y, Z). Confirm?" **Confirm** / **Not quite**.
4. Confirm sets `active`. Not quite sets `rejected`, permanently, so the same
   wrong guess is never proposed twice.
5. The current shortlist re-ranks immediately and the reordered top 5 is posted.
   That visible reorder *is* the proof moment.

**Preferences never change behaviour before the human confirms them**, and rules
are **structured, never prose**:
`{field: company_size_bucket, op: lt, value: 2000, effect: penalty, weight: 0.5}`.
Structured rules can be applied in SQL or Cypher deterministically, which is
what keeps `predict_fit` free of LLM calls (§7) and what lets the digest explain
a ranking in one line.

## 7. Proving it compounds: three lines, not one

A falling token count alone is a cache, and a judge can say so. The `runs` table
carries cost, quality and human effort side by side:

```
run_id, started_at, day, mode,
wall_ms, tokens_in, tokens_out, steps_reasoned, steps_replayed, plays_used,
questions_asked, human_touches,
values_from_memory, values_replayed, values_reasoned,
jobs_released_today, pool_size, shown, predicted_keep, actual_keep, precision_at_5, prediction_accuracy
```

`mode` is `first_run | partial_replay | full_replay`, so the chart's points can
be coloured by it and the step down at the first replay is visible rather than
narrated.

**Line 1, cost falling.** `tokens_in + tokens_out` and `wall_ms` per run, with
`steps_replayed / (steps_reasoned + steps_replayed)` as the supporting ratio.

**Line 2, quality rising.** Before showing the digest, the agent **predicts**
for each candidate job whether the human will keep it or skip it. The prediction
runs off Cognee `recall` of stated preferences, the HydraDB rejection-reason,
claim-coverage and company-outcome queries, the active preference rules, and
hotdata's salary percentile for the title — so all three memory layers are
load-bearing in the quality line, not only in the display. Both the prediction
and the human's actual response are logged to `applications`.

**Line 3, human effort falling.** `questions_asked` and `human_touches`
(approvals, edits, and answers given for this run). This is the line that needs
no explanation to a non-technical judge, and the line that closes into §8: when
it reaches zero for a domain, the agent has earned the right to ask for
autonomy.

**`predict_fit` is a deterministic scorer, not an LLM call.** This is the
decision that stops the lines fighting each other. An LLM prediction would take
a `recall` context that grows with the graph, so tokens per run would *rise*
exactly as memory improved. A weighted score over graph and aggregate features —
`0.45 * requirement_coverage + 0.30 * vector_similarity + 0.25 * preference_fit`
as the starting weights, tuned against real reactions late in the day — holds
the model fixed and lets the inputs get richer. That is also the stronger claim:
the agent did not get a better brain, it got a better memory.

Two metrics fall out:

- `precision_at_5`: of the five roles shown, how many the human did not reject.
- `prediction_accuracy`: how often the agent's guess matched the human,
  **reported on the skip class**. Measured across all five shown roles the
  number is dishonest: as ranking improves the slate becomes uniformly good, the
  human keeps nearly everything, and "keep" turns trivially predictable —
  accuracy rises because the base rate moved, not because judgment sharpened.
  Two roles of every five are therefore drawn from *outside* the top ranking to
  hold slate difficulty fixed, and the headline number is how often the agent
  called a skip correctly. Settle this at hour 1; retrofitting it at hour 6
  means re-running the whole loop.

These are not two independent proofs — on an all-top-5 slate they measure nearly
the same thing, which is the other reason the slate is mixed.

"Run 1 it guessed your taste 40% of the time and asked you six questions. Run 20,
90% and none." That is memory becoming judgment, it needs almost no typing from
the human during the demo, and it is the line that separates this from a cache
with a chart.

**Honesty rule: only numbers the system actually logged go on the chart.** No
interpolated points, no back-filled runs, no "representative" values. Hours 3
through 7 exist to produce real rows.

**Seeding the curve honestly.** From hour 3 the loop runs on a timer through the
rest of the build, one day per tick, with a teammate giving real
feedback as digests arrive. By demo time the chart has 20+ genuine points
produced by real runs with real human feedback, and the live demo run is simply
the last point on an existing curve. Nothing is fabricated; the feedback was
just given earlier in the day than the pitch.

## 8. Earning autonomy

The three lines in §7 describe an agent that needs the human less. Autonomy is
what that turns into as a product, and it is the closing beat of the demo (P6).

**Readiness comes from the agreement record, not from model confidence.** LLM
self-reported confidence is poorly calibrated and a judge knows it. "You agreed
with 13 of my last 15 picks" is a fact the human can check, and we already log
every number it is made of.

Two domains, deliberately, because two is what fits in the schedule and both
already have their metrics:

| Domain | What the agent does alone | Readiness metric | Ready at |
|---|---|---|---|
| **D1 Shortlisting** | Decides which roles make the digest and prepares apply-packs for its own top picks, without being asked per role | `prediction_accuracy` on the skip class over the last 15 shown roles | ≥ 85% |
| **D2 Preference rules** | Activates an inferred preference rule without a confirmation prompt | Hypotheses confirmed over the last 5 proposals | ≥ 4 of 5 |

States are `Supervised → Ready → Autonomous`, with downgrade. In `Supervised`
every decision goes through the human and each response updates the metric. At
`Ready` the agent posts **one** prompt and keeps asking until answered:

> "You've agreed with 13 of my last 15 calls on Senior PM roles (87%). I'd like
> to prepare packs for my own top picks without asking first. You'll get the
> digest either way, and you can turn it off anytime. **Turn it on** / **Not yet**"

- **Anti-nag:** after "Not yet", do not re-prompt that domain until 10 more
  decisions are logged.
- **Inform, don't consult:** every autonomous action is logged `decided_by:
  agent`, appears in the digest, and reversible ones carry an Undo.
- **Downgrade:** 2 overrides (undo, or a post-hoc "not for me" on something the
  agent picked itself) in the last 10 autonomous decisions returns the domain to
  Supervised, with the reason stated out loud.

**Never autonomous, whatever the record says.** The agent can earn autonomy over
*judgment*, never over *facts* — facts about a person cannot be learned by
watching them click:

- Adding or changing a `Claim` or a `StandardAnswer`. An unknown fact is always
  asked (§6.2, rung 4).
- Marking an unverified claim verified.
- **Submitting anything, to anything.** This is a non-goal at any autonomy
  level, and stating it as a hard ceiling is a better answer to "isn't this
  dangerous" than any amount of guardrail prose.

Thresholds live on the `Candidate` node and are lowered for the demo run so
readiness is reachable inside one session — using the same config values that
would hold the real numbers, not a hardcoded demo branch.

## 9. The demo script

The chart already exists when the demo starts. The live portion shows the three
regimes that produced it.

**Cold (recorded at hour 0 and hour 3, replayed as the first points).** Two
things happened here and both are on tape. At hour 0, no play exists for
Greenhouse: the agent reasons about the API shape, fetches, normalizes, loads
hotdata, and Rote crystallizes `ingest-ats` — which then replays across the
remaining boards with the token count dropping to the floor on run 2 and staying
there for 50-odd boards (P3). At hour 3, day 1 of the clock: resume and a
paragraph of preferences go in, Cognee extracts claims, HydraDB stores them, the
verification digest confirms a handful. The agent judges day 1's releases and
presents five roles. Predictions are near-random. The first `apply-pack` flags a
gap out loud and asks six questions.

**Live day A, novel path and partial replay.** "Next day." The clock advances,
hotdata returns that day's releases, and `refresh-and-rank` replays end to end
with zero reasoning on the plumbing (P3). Then `apply-pack` on a role whose
screening set contains a question shape the play has never seen: the known
fields replay from memory and only the novel one goes to the LLM, after which
Rote crystallizes the extended path as a new play version in front of the room
(P4). Nothing in this beat touches the network.

**Live day B, learning.** Mark two roles "not for me" with the same reason. The
third matching signal fires the preference hypothesis; confirm it; the shortlist
visibly reorders (P2). Then `apply-pack` on a role: this time it asks nothing,
because every standard answer is already known (P1), and the pack cites only
verified claims with one gap flagged (P5).

**Live day C, warm path.** "Next day." Everything replays. The digest now leads
with **why**, rendered from the Cypher path:

> Shown because you have verified claims covering 4 of 5 requirements, the
> warmest path runs through the role at Stripe that replied to you, and you
> rejected the staff-level variant of this title on day 6 — this one is senior.

That paragraph is the most screenshot-able thing we will build, and it is the
thing a job board structurally cannot do. Agent predicts keep/skip for all five;
the human confirms; accuracy lands on the chart live.

**Close, in two parts.** First the autonomy prompt fires, citing the real
agreement count from the session; toggle it on; one more refresh runs end to end
with zero clicks (P6). Then the chart from `runs`: tokens down, questions down,
accuracy up, 20+ real points, one per day of the replayed schedule (P7).

## 10. Schedule

Pre-event setup:

- ~~Accounts and keys for all five plus Snyk.~~ Done; `sh scripts/verify-setup.sh`
  re-checks all five against the live services. Rote hello play and the Discord
  post still to do.
- ~~HydraDB: pick cloud vs Docker.~~ **Cloud provisioned.** `default-tenant` on
  `api.hydradb.com` (org `ww6ndjr2d5`) is deployed and `ready_for_ingestion`.
  **Blocking question, answer before hour 0:** does the cloud key run Cypher? If
  not, start the OSS engine in Docker and use that. Do not spend hour 2
  rewriting the queries.
- ~~Spike: Cognee `neo4j` provider against HydraDB Bolt.~~ **Dropped.** Cognee
  is a managed tenant now and its storage is not configurable. Write the
  `GET /datasets/{id}/graph` → `MERGE` sync directly; it was the fallback anyway.
- ~~Cognee: pick a local LLM backend.~~ **Gone entirely.** The tenant owns its
  model stack. `sh scripts/verify-setup.sh` proves the round trip.
- ~~hotdata: create a database, load one CSV, run one query.~~ Done via REST:
  `scripts/hotdata-setup.sh` connects the Greenhouse board API as a `rest`
  datasource and loads 200 rows into `jobs.public.jobs`. Still open: the
  canonical `jobs` schema with `release_day`, and the `applications` and `runs`
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
| 0 to 1 | Canonical job schema with `release_day`. Fetch the ~1000 job corpus from 50 to 100 boards across all three ATS families, keep the raw payloads, assign the release schedule, load hotdata, **then close the network**. **Tunnel up, one stub MCP tool callable from a staging pipeline, token usage confirmed in the task response.** | `hotdata query` returns 1000+ rows from three sources, every row carries a `release_day`, each day of the schedule has a non-trivial batch, the salary percentile is not noise, and a staging pipeline has called one of our tools through the tunnel |
| 1 | **Snyk scan #1** | Clean, or the one finding is triaged |
| 1 to 3 | Cognee custom graph model, resume and prefs ingest with claim verification, HydraDB sync, the Cypher queries, hotdata queries, `predict_fit`, MCP server | Every tool callable from a Python REPL; a prediction comes back with a reason; claim coverage returns a gap |
| 3 | **Slack go/no-go.** Has a button click round-tripped into a pipeline? | Yes, or approvals move to numbered chat replies and the script is unchanged |
| 3 to 4.5 | RocketRide pipelines P-A/P-B/P-C: agent, LLM, MCP client, Slack, Sheets. First full run end to end. **Start the timed loop.** | Cold run works, questions get asked and stored, and the loop is ticking one day per interval |
| 4.5 to 6 | Rote: record and crystallize `apply-pack` then `refresh-and-rank` then `ingest-ats`. Fingerprint match modes including partial. `find_play` and replay path. `SkillRunEntry`. Metrics into `runs`. | Later runs replay with visibly lower token counts and zero questions while the loop keeps running |
| 5 | **Snyk scan #2** (dependencies have settled by now) | Highs and criticals fixed |
| 6 to 7 | Preference learning loop and the confirm prompt. Autonomy readiness prompt for D1. "Why" rendering in the digest. Three-line chart from `runs`. Rehearse the live runs twice. Additive Google nodes only if green. README. | Chart renders from real rows; a preference hypothesis has fired and been confirmed; all live beats rehearsed |
| 7 | **Snyk scan #3**, submission and pitch | Clean, submitted |

The loop started at hour 3 keeps producing rows through every later block. By
hour 7 the chart is a curve, not three dots.

Suggested split for three people: one on memory (Cognee, HydraDB, sync, claims,
preference learning, prediction), one on insight and orchestration (corpus,
day clock, hotdata, MCP server, RocketRide pipelines), one on motion and
muscle memory (Slack, Sheets, Rote plays, fingerprinting, metrics, demo). A
fourth takes the README, Snyk, chart, autonomy, and pitch from hour 5.

## 11. Risks and pre-decided cuts

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
  is a proxy. Lines 2 and 3 are unaffected either way, which is the other reason
  for having three.
- **RocketRide agent to tool wiring is undocumented:** fall back to
  `agent_langchain` with the same `mcp_client`. Keep a local Python driver of the
  same MCP tools as a debugging harness, never as the demo path, since judges
  need RocketRide load-bearing.
- **Slack interactivity does not round-trip:** decided at the hour-3 go/no-go.
  Degrade to the `chat` source with numbered replies; the Sheet stays the
  written record and the demo script is unchanged.
- **Rote cannot see the agent's native calls:** it does not need to. All novel
  fetches go through `record_step`, which shells out to `rote proc run`.
- **A crystallized play breaks on an unexpected payload shape:** the replay is
  marked `stale` and the run falls back to the novel path. Never return a
  partial result as if it were a replay. With a static corpus this can only
  happen at hour 0, which is the right time for it to happen.
- **hotdata cloud outage:** DuckDB over the same frozen Parquet keeps the demo
  alive, and say so openly. Last resort only.
- **The day clock is wrong or the corpus is thin:** re-judge the same day
  repeatedly. Every line on the chart still moves, because none of them
  depends on time passing (§5).
- **A judge reads the assigned release dates as faked data:** answer it before
  they ask, with the table in §5. The jobs are real and every number on the
  chart is real; the schedule is a replay order for a static corpus, it changes
  only what the agent sees on a given day, and no metric depends on it being
  true. Volunteering this reads as rigour; being caught on it reads as the
  opposite.
- **Prediction accuracy does not improve:** it is still an honest chart, and the
  token and question lines still stand. Do not tune the metric to make the line
  go up; a flat quality line with a real explanation beats a curve we cannot
  defend.
- **Time overrun, cut in this order:** Gmail draft, Calendar, Docs, the third
  ATS family, autonomy D2, autonomy D1 entirely, partial replay (keep exact
  replay), preference learning.
- **Never cut:** the day clock, verified-claims-only packs, the
  human-in-the-loop gate on every outward action, the Slack digest, the Sheets
  tracker, and the chart from real `runs` rows. Those are the project.
- **Never auto-submit applications.** Prepare, log, and hand to the human.

## 12. Considered and deliberately not taken

An alternative design for the same brief proposed three things we are not
building. Recording why, because each will come up in questions:

- **A mock ATS and browser form-filling.** Three fake form pages would give Rote
  a crisp replay demo, but the pages are ours, so the "muscle memory" is against
  a fixture we wrote — and it costs an afternoon we would take out of the memory
  layer. `ingest-ats` replays against three *real* APIs with genuinely different
  shapes — 50-odd times during the hour-0 corpus build — which is the same proof
  against harder material. The partial-replay idea from that design was worth
  keeping and is in §4; the mock ATS was not.
- **Outcome tracking from a real inbox, and resume A/B experiments.** Gmail
  polling, rejection classification and Thompson sampling over resume variants
  make a genuinely better product, and none of it can be shown in 8 hours
  without a recruiter simulator sending fake emails to a fake inbox on a
  compressed clock. That is a simulation of the very thing the day clock
  already gives us honestly. The candidate reports a reply, an edge is written,
  the company-outcome query uses it — same signal, no fiction.
- **Fetching a live board during the demo.** An earlier draft kept one real API
  call on stage, to be able to say the ingest was live. It buys a sentence and
  costs the whole run if the venue wifi drops, and it is not needed: the ingest
  plays were genuinely exercised against live APIs at hour 0, with the token
  counts logged, which is the evidence a judge actually wants. The corpus is
  frozen and the demo is offline end to end.
- **A second approval stack (approval service plus web dashboard).** An
  interface-agnostic approval API with Slack and a dashboard as two clients is
  the right architecture for a product and the wrong one for a day. Slack is the
  surface; the hour-3 go/no-go decides whether it survives; the fallback is the
  chat source, not a second UI.

## 13. Repo layout

```
agent/            # canonical schema, corpus build, day clock, ranking merge, predict_fit
memory/           # cognee cloud client, graph model, remember helpers, hydra sync + cypher
                  #   claims.py (verification + citation validator), prefs.py (rule induction)
insight/          # hotdata client, table loaders, queries, corpus build
mcp_server/       # FastMCP server exposing the tools above plus rote wrappers
rocketride/       # pipeline .pipe JSON (P-A, P-B, P-C) and node configs
plays/            # crystallized Rote plays (main.ts + deps.toml), fingerprint index
demo/             # loop driver, run scripts, the three-line metrics chart
data/             # frozen job corpus (parquet), gitignored
Makefile          # setup, security (snyk), corpus, loop, demo targets
```
