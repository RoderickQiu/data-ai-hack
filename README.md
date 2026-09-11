# data-ai-hack

Knowledge-graph memory over our data using [cognee](https://docs.cognee.ai/), backed by
either Vertex AI (Gemini) or any OpenAI-compatible API.

The full stack, in one line:

```
cognee (memory) -> HydraDB (stores it) -> hotdata.dev (ad-hoc queries) -> RocketRide (acts) -> Rote (replays what worked)
```

## Team checklist: every teammate does this once

Nothing here is shared through git. Keys, logins and CLI installs live on your own
machine, so each of us has to complete every step below. Tick them off in order.

1. **Clone and install cognee.** Follow [Setup](#setup) through `cp .env.example .env`.
2. **Pick an LLM backend** and fill in `.env`: [Option A](#option-a-vertex-ai-gemini)
   needs Google Cloud access, [Option B](#option-b-openai-compatible-api-no-gcloud)
   needs only an API key.
3. **Add the service keys to `.env`.** Ask in the team channel for any you do not have.
   `.env.example` explains each one.
   - `ROCKETRIDE_APIKEY` (dev connection, for running and iterating)
   - `ROCKETRIDE_DEPLOY_APIKEY` (deploy target only, never used for dev runs)
   - `HYDRADB_APIKEY` (plus `HYDRA_DB_API_KEY`, same value, for RocketRide's node)
   - `HOTDATA_API_KEY` — that spelling exactly. The CLI reads no other name, and
     without it every command falls back to the browser session and dies with
     "session expired or revoked".
4. **Verify cognee** with the snippet in [Verify the setup](#verify-the-setup).
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
| 4 | cognee | real `add` → `cognify` → `search` round trip | see [Verify the setup](#verify-the-setup) |
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

Requires Python 3.10 to 3.14 (cognee does not yet support 3.15; developed on 3.14).

```bash
git clone <repo-url> && cd data-ai-hack
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Then open `.env` and pick **one** of the two LLM options below. Everything else in
the file is explained inline.

### Option A: Vertex AI (Gemini)

Uses Gemini 3.8 Flash for the LLM and Gemini Embedding 2 for embeddings. Needs a
Google Cloud project with the Vertex AI API enabled and a one-time login on your
machine:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project <your-gcp-project-id>
```

In `.env` set `VERTEXAI_PROJECT=<your-gcp-project-id>` and leave the rest of the
Vertex block as it is. Notes:

- `VERTEXAI_LOCATION` must stay `global`. These models are not served from regional
  endpoints and will 404 elsewhere.
- `LLM_API_KEY` and `EMBEDDING_API_KEY` are placeholders. Cognee requires a non-empty
  value, but Vertex authenticates with your gcloud credentials and ignores it.
- `EMBEDDING_BATCH_SIZE=1` is required. Gemini Embedding 2 on Vertex fuses every
  input in a request into a single vector, so larger batches break indexing.
- Do not set `LLM_TEMPERATURE`. Gemini 3.6 Flash and later reject it.

### Option B: OpenAI-compatible API (no gcloud)

For anyone without Google Cloud access. Works with the official OpenAI API,
OpenRouter, Groq, DeepSeek, Together, a LiteLLM proxy, vLLM, LM Studio, or
Ollama's `/v1` endpoint.

In `.env`, comment out the Vertex `LLM_*` and `EMBEDDING_*` lines and uncomment
the Option B block, filling in:

```
LLM_PROVIDER=custom
LLM_MODEL=openai/<model-name>        # keep the openai/ prefix
LLM_ENDPOINT=https://<host>/v1       # base URL up to and including /v1
LLM_API_KEY=<key>

EMBEDDING_PROVIDER=openai_compatible
EMBEDDING_MODEL=<embedding-model>
EMBEDDING_ENDPOINT=https://<host>/v1
EMBEDDING_API_KEY=<key>
EMBEDDING_DIMENSIONS=1536            # must match the embedding model
```

`EMBEDDING_DIMENSIONS` is required here. Cognee cannot detect the size of an
unknown model and would silently assume 3072. Changing it later needs a fresh
vector database.

## Verify the setup

With the venv active and `.env` filled in, this adds one document, builds the
graph, and runs a search. It takes about half a minute on Vertex.

```bash
python - <<'EOF'
import asyncio, cognee
from cognee.api.v1.search import SearchType

async def main():
    await cognee.add("Ada Lovelace wrote the first computer program in 1843.")
    await cognee.cognify()
    print(await cognee.search(
        query_type=SearchType.GRAPH_COMPLETION,
        query_text="Who wrote the first computer program?",
    ))

asyncio.run(main())
EOF
```

Expected output ends with a search result naming Ada Lovelace.

## Things to know

- **`.env` overrides your shell.** Cognee loads the project `.env` with override
  enabled and finds it by walking up from the venv, so exporting a variable in the
  terminal has no effect. Edit the file instead.
- **Storage is local by default.** Graph, vector, and relational data live under
  cognee's package directory using Ladybug, LanceDB, and SQLite. No extra services
  are needed. To wipe everything:

  ```python
  await cognee.prune.prune_data()
  await cognee.prune.prune_system(metadata=True)
  ```

- **Logs** are written to `~/.cognee/logs/`. Set `LITELLM_LOG=DEBUG` in `.env` to see
  the raw requests sent to the model provider.
- **Secrets.** `.env` is gitignored. `.env.example` holds placeholders only; keep it
  that way when adding new keys.
