# data-ai-hack

Knowledge-graph memory over our data using [Cognee Cloud](https://docs.cognee.ai/) —
one managed tenant shared by the whole team, reached over REST. Nothing about the
memory layer runs on your laptop, so there is no model to configure and no local
graph to drift out of sync with everyone else's.

The full stack, in one line:

```
cognee (memory) -> HydraDB (stores it) -> hotdata.dev (ad-hoc queries) -> RocketRide (acts) -> Rote (replays what worked)
```

## Team checklist: every teammate does this once

Nothing here is shared through git. Keys, logins and CLI installs live on your own
machine, so each of us has to complete every step below. Tick them off in order.

1. **Clone and install.** Follow [Setup](#setup) through `cp .env.example .env`.
   Two small packages, any Python 3.10+, no gcloud and no LLM keys of your own.
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
