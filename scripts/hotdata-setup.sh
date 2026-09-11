#!/usr/bin/env sh
# Bring a hotdata.dev account up to the hackathon checklist: CLI authenticated,
# one database, one connected data source, rows actually loaded.
#
#   sh scripts/hotdata-setup.sh
#
# Prerequisite: the CLI, and HOTDATA_API_KEY in .env.
#   brew install hotdata-dev/tap/cli
#
# Idempotent: an existing database or datasource with the same name is reused,
# and re-running the ingest just replaces the rows.
set -eu

cd "$(dirname "$0")/.."

say() { printf '\n==> %s\n' "$*"; }
envval() { sed -n "s/^$1=//p" .env | tail -n 1; }

if ! command -v hotdata >/dev/null 2>&1; then
  echo "hotdata CLI not found. Install it:  brew install hotdata-dev/tap/cli" >&2
  exit 1
fi

[ -f .env ] || { echo ".env not found. Copy .env.example to .env first." >&2; exit 1; }

# The CLI reads this name and no other. Without it, it falls back to the browser
# session in ~/.hotdata, which expires and then fails every command.
HOTDATA_API_KEY="$(envval HOTDATA_API_KEY)"
[ -n "$HOTDATA_API_KEY" ] || { echo "HOTDATA_API_KEY is empty in .env" >&2; exit 1; }
export HOTDATA_API_KEY

CATALOG="$(envval HOTDATA_CATALOG)"
CATALOG="${CATALOG:-jobs}"

# Authenticating by key stores no default workspace, so -w is required on every
# command. Take it from .env if set, otherwise the first workspace on the account.
WS="$(envval HOTDATA_WORKSPACE_ID)"
if [ -z "$WS" ]; then
  WS="$(hotdata workspaces list --no-input -o json | python3 -c \
    'import json,sys; print(json.load(sys.stdin)[0]["public_id"])')"
fi
say "Workspace: $WS"

# 1. Database ----------------------------------------------------------------
# The catalog alias is globally unique, so a second run must reuse, not create.
DB="$(hotdata databases list --no-input -w "$WS" -o json | python3 -c \
  'import json,sys
rows = json.load(sys.stdin)
hit = next((r for r in rows if r.get("default_catalog") == sys.argv[1]), None)
print(hit["id"] if hit else "")' "$CATALOG" 2>/dev/null || true)"

if [ -n "$DB" ]; then
  say "Database with catalog '$CATALOG' already exists: $DB"
else
  say "Creating database (catalog '$CATALOG')"
  DB="$(hotdata databases create --name data-ai-hack --catalog "$CATALOG" \
        --no-input -w "$WS" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
  echo "  $DB"
fi

# 2. Data source -------------------------------------------------------------
# Greenhouse's public job board API: no credentials, real rows, and the same
# shape the ingest-ats play will use for the other ATS families.
SOURCE_NAME=greenhouse_stripe
CONFIG='{"base_url":"https://boards-api.greenhouse.io/v1/boards/stripe/","auth_type":"none","source_name":"'"$SOURCE_NAME"'"}'

DS="$(hotdata ingest sources list --no-input -w "$WS" -o json | python3 -c \
  'import json,sys
rows = json.load(sys.stdin)
hit = next((r for r in rows if r.get("display_name") == "Greenhouse (Stripe board)"), None)
print(hit["datasource_id"] if hit else "")' 2>/dev/null || true)"

if [ -n "$DS" ]; then
  say "Datasource already exists: $DS"
else
  say "Validating the source config"
  hotdata ingest sources test --family rest --config "$CONFIG" --no-input -w "$WS"

  say "Creating datasource"
  DS="$(hotdata ingest sources add --family rest --display-name "Greenhouse (Stripe board)" \
        --config "$CONFIG" --no-input -w "$WS" -o json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["datasource_id"])')"
  echo "  $DS"
fi

# 3. Ingest ------------------------------------------------------------------
# One-time and write_mode=replace, so re-running is safe.
say "Loading jobs into $CATALOG.public.jobs"
ING="$(hotdata ingest create --datasource-id "$DS" --type one-time --no-input -w "$WS" -o json \
       --selector '{"resources":[{"name":"jobs","endpoint":{"path":"jobs","data_selector":"jobs"},"primary_key":"id"}],"limit":200}' \
       --destination '{"database_id":"'"$DB"'","schema":"public"}' \
       | python3 -c 'import json,sys; print(json.load(sys.stdin)["ingest_id"])')"
echo "  ingest $ING"

# The run is dispatched asynchronously; it finishes in ~15s.
printf '  waiting for the run'
i=0
while [ "$i" -lt 24 ]; do
  printf '.'
  sleep 5
  STATUS="$(hotdata ingest show "$ING" --no-input -w "$WS" -o json | python3 -c \
    'import json,sys; r = json.load(sys.stdin).get("latest_run") or {}; print(r.get("status",""))')"
  case "$STATUS" in
    succeeded) printf ' %s\n' "$STATUS"; break ;;
    failed|cancelled) printf ' %s\n' "$STATUS"; hotdata ingest show "$ING" --no-input -w "$WS"; exit 1 ;;
  esac
  i=$((i + 1))
done

say "Query it"
hotdata query "SELECT count(*) AS jobs FROM $CATALOG.public.jobs" --no-input -w "$WS"

cat <<EOF

Add these to .env so the rest of the tooling can skip the lookups:
  HOTDATA_WORKSPACE_ID=$WS
  HOTDATA_DATABASE_ID=$DB
  HOTDATA_CATALOG=$CATALOG
EOF
