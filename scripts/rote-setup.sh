#!/usr/bin/env sh
# Wire Modiqo Rote to RocketRide for this project.
#
# Prerequisite: install Play/Rote once (this is the Playoffs installer):
#   curl -fsSL https://getrote.dev/playoffs/install.sh | sh
#
# Then run:  sh scripts/rote-setup.sh
#
# What this does, idempotently:
#   1. checks the rote CLI is on PATH and you are signed in (opens `rote login` if not)
#   2. loads ROCKETRIDE_* from .env into rote's local token store
#      (credentials stay on this machine; a published Play only lists the names)
#   3. builds/refreshes the `rocketride` adapter from rote/rocketride.openapi.json
#   4. smoke-tests the adapter against the live engine
set -eu

cd "$(dirname "$0")/.."

say() { printf '\n==> %s\n' "$*"; }

# 1. CLI + sign-in -----------------------------------------------------------
if ! command -v rote >/dev/null 2>&1; then
  echo "rote CLI not found on PATH." >&2
  echo "Install it first:  curl -fsSL https://getrote.dev/playoffs/install.sh | sh" >&2
  echo "Then open a new shell (the installer may add rote to PATH) and rerun this script." >&2
  exit 1
fi
say "rote $(rote --version 2>/dev/null || echo '(version unknown)')"

if ! rote whoami >/dev/null 2>&1; then
  say "Not signed in. Opening browser sign-in (Google or GitHub); pick a memorable handle."
  rote login
fi
say "Signed in as: $(rote whoami)"

# 2. Credentials -------------------------------------------------------------
if [ ! -f .env ]; then
  echo ".env not found. Copy .env.example to .env and fill in ROCKETRIDE_* first." >&2
  exit 1
fi

# Read a key from .env without exporting the whole file.
envval() { sed -n "s/^$1=//p" .env | tail -n 1; }

for name in ROCKETRIDE_URI ROCKETRIDE_APIKEY ROCKETRIDE_DEPLOY_URI ROCKETRIDE_DEPLOY_APIKEY; do
  value="$(envval "$name")"
  if [ -z "$value" ]; then
    echo "  skip $name (empty or absent in .env)"
    continue
  fi
  printf '%s' "$value" | rote token set "$name" --stdin
  echo "  stored $name in rote token store"
done

# 3. Adapter -----------------------------------------------------------------
SPEC=rote/rocketride.openapi.json
if [ ! -f "$SPEC" ]; then
  say "Spec missing; fetching from RocketRide"
  python3 scripts/refresh-rocketride-spec.py
fi

# Already installed? Do nothing. `rote adapter new` refuses to overwrite, and
# rerunning it without --yes drops into an interactive wizard that can write a
# wrong auth config. `adapter info` exits 0 only when the adapter exists.
if rote adapter info rocketride >/dev/null 2>&1; then
  say "Adapter 'rocketride' already installed; leaving it alone"
  echo "  To rebuild from scratch: rote adapter delete rocketride && sh scripts/rote-setup.sh"

# Teammates: pull the adapter the org owner published instead of building it.
# Set ROTE_ORG=data-ai-hack to enable.
elif [ -n "${ROTE_ORG:-}" ] && rote registry adapter pull "$ROTE_ORG/rocketride"; then
  say "Pulled adapter '$ROTE_ORG/rocketride' from the registry"

else
  say "Creating adapter 'rocketride' from $SPEC"
  # --yes is required: it skips the wizard and takes auth from --config-json.
  # Rote would otherwise default the bearer variable to ROCKETRIDE_API_TOKEN,
  # which is not the name we store. Never type a key at the wizard's
  # "environment variable name" prompt; it wants the NAME, not the secret.
  if ! rote adapter new rocketride "$SPEC" --yes \
       --config-json '{"auth":{"type":"bearer","token_env":"ROCKETRIDE_APIKEY"}}'; then
    echo "" >&2
    echo "'rote adapter new' failed. Check the flags for this CLI version:" >&2
    rote adapter new --help >&2 || true
    exit 1
  fi
fi

# 4. Smoke test --------------------------------------------------------------
# Adapter calls only run inside a rote workspace (kept under ~/.rote, not the repo).
WS=rocketride-smoke
rote init "$WS" >/dev/null 2>&1 || true
WS_DIR="${ROTE_HOME:-$HOME/.rote}/workspaces/$WS"

say "Smoke test: unauthenticated version call"
( cd "$WS_DIR" && rote rocketride_call getVersion '{}' | head -3 ) \
  || echo "  FAILED: adapter cannot reach $(envval ROCKETRIDE_URI)"

say "Smoke test: authenticated services listing (uses ROCKETRIDE_APIKEY)"
( cd "$WS_DIR" && rote rocketride_call listServices '{}' | head -3 ) \
  || echo "  FAILED: check ROCKETRIDE_APIKEY with: rote token list"

say "Done. In Claude Code run:  /play what's new   then   /play run hello"
