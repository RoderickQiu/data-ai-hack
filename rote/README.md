# Rote (Modiqo) + RocketRide

Rote sits underneath the RocketRide execution loop. The first time a task
succeeds, Rote captures the working path (the RocketRide API calls, their order
and parameters) as a deterministic Play. Repeat runs replay the Play instead of
re-reasoning from scratch.

```
cognee  ->  HydraDB  ->  hotdata.dev  ->  RocketRide (acts)  ->  Rote (captures)  ->  replay
```

## Files

| File | Purpose |
|------|---------|
| `rote/rocketride.openapi.json` | Adapter-ready OpenAPI spec for RocketRide (base URL, bearer auth, 9 curated operations) |
| `scripts/refresh-rocketride-spec.py` | Regenerates the spec from the live `https://api.rocketride.ai/openapi.json` |
| `scripts/rote-setup.sh` | Signs in, loads `ROCKETRIDE_*` from `.env` into Rote's token store, builds the `rocketride` adapter, smoke-tests it |

## Org owner setup

Done once by one person. Everyone else follows the teammate checklist in the root
README. Requires Rote installed and `rote login` completed.

```sh
# 1. identity
rote profile set-handle <your-handle>

# 2. team org (Community plan: free, up to 5 members). Ours is `data-ai-hack`.
rote registry org create --slug data-ai-hack --name "Data AI Hack"
rote registry org usage data-ai-hack             # confirms plan and quota

# 3. build the RocketRide adapter locally (reads ROCKETRIDE_APIKEY from the token store)
sh scripts/rote-setup.sh

# 4. publish it privately to the org. The slug is the org only; rote appends the adapter id.
rote registry adapter publish rocketride data-ai-hack --private
rote registry adapter info data-ai-hack/rocketride

# 5. invite the team (they become members on sign-up, no accept step)
rote registry org invite data-ai-hack <teammate@email> --role developer
rote registry org members data-ai-hack --pending
```

**Steps 1 to 4 are already done.** As of 2026-09-11: handle `roderickqiu`, org
`data-ai-hack` (owner, free plan, 1 of 5 members), adapter
`data-ai-hack/rocketride` v1.0.0 published private. Only the invites in step 5
remain. Do not rerun steps 1 to 4.

Roles: `admin` can manage members, `developer` can push adapters and Plays,
`reader` can only pull. Give teammates `developer`.

## Troubleshooting

**`You've reached your organizations limit`** on `org create`. The free plan
allows one org and you already own `data-ai-hack`. Nothing to fix. Confirm with
`rote registry org list`. Teammates never run this command; they are invited into
the existing org instead.

**`adapter 'rocketride' already exists`** from the setup script. Expected on a
rerun. The script now detects this and skips the build. To genuinely rebuild:

```sh
rote adapter delete rocketride && sh scripts/rote-setup.sh
```

**The setup script opens an interactive wizard.** That happens only if `--yes` is
missing. The wizard's second step asks for the "environment variable name for API
token" and it wants the literal name `ROCKETRIDE_APIKEY`, not the key itself.
Typing the key there stores your secret as a variable name and picks the wrong
auth scheme. Press Ctrl-C and rerun the script instead.

**`No workspace active`** on any `rocketride_call`. Adapter calls need a Rote
workspace. Run `rote init <name>` then `cd ~/.rote/workspaces/<name>`.

**Checking the adapter is wired correctly:**

```sh
rote adapter info rocketride | sed -n '/Authentication/,/Files/p'
```

Expect `Type: Bearer Token` and `Environment variable: ROCKETRIDE_APIKEY`.

## One-time setup (Playoffs warm-up)

1. Install Play/Rote (macOS, Linux, WSL). Pinned to release v0.4.98 by the selector script:

   ```sh
   curl -fsSL https://getrote.dev/playoffs/install.sh | sh
   ```

   The installer may end with `READY — SIGN IN TO CONTINUE`. That is success.

2. Wire it to RocketRide:

   ```sh
   ROTE_ORG=<team-org-slug> sh scripts/rote-setup.sh
   ```

   This runs `rote login` if needed, then `rote token set ROCKETRIDE_APIKEY --stdin`
   (and the deploy key) so Plays can authenticate without the key ever leaving
   this machine, then pulls the shared `rocketride` adapter from the org. Without
   `ROTE_ORG` it builds the adapter locally from `rote/rocketride.openapi.json`
   instead.

3. Warm-up laps, in Claude Code:

   ```
   /play what's new
   /play run hello
   ```

   Then post "warmed up" in the Playoffs Discord.

## Using the adapter

Rote generates two tools per adapter: `rocketride_probe` for semantic search over
operations and `rocketride_call` to execute one. Calls only run inside a rote
workspace, which lives under `~/.rote/workspaces/`, never in this repo:

```sh
rote init my-task                      # once
cd ~/.rote/workspaces/my-task
rote rocketride_probe "run a pipeline"
rote rocketride_call getVersion '{}'   # response cached as @1
rote @1 '$'                            # print it
rote rocketride_call listServices '{}'
```

Verified 2026-09-11: `getVersion` answers in ~0.5 s, `listServices` returns the
full RocketRide node catalog with the stored key. Operations exposed:

| operationId | Method | Path | Auth |
|-------------|--------|------|------|
| `getStatus` | GET | `/status` | none |
| `getVersion` | GET | `/version` | none |
| `listServices` | GET | `/services` | bearer |
| `executeTask` | POST | `/task` | bearer |
| `getTaskStatus` | GET | `/task?token=` | bearer |
| `cancelTask` | DELETE | `/task?token=` | bearer |
| `uploadTaskData` | POST | `/task/data?token=` | bearer |
| `postWebhook` | POST | `/webhook?token=` | bearer |
| `listMarketplaceApps` | GET | `/marketplace/apps` | bearer |

A Play that calls the adapter must list it under `requires_endpoints` in its
manifest, otherwise `rote registry play push` rejects it with
"undeclared endpoints".

## Dev vs deploy

`.env` carries two RocketRide connections. Use `ROCKETRIDE_APIKEY` for the dev
loop (run, validate, iterate). `ROCKETRIDE_DEPLOY_APIKEY` is only for
`deploy.*`, schedules and `publishApp`/`submitApp`. Never deploy through the dev
connection. Both point at the same host today, so the adapter is shared and the
key selects the environment.
