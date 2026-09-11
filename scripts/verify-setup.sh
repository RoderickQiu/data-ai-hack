#!/usr/bin/env sh
# Check the five hackathon prerequisites end to end, against the live services.
#
#   sh scripts/verify-setup.sh              # all five
#   SKIP_COGNEE=1 sh scripts/verify-setup.sh   # skip the slow one (~35s)
#
# Every check makes a real call. Nothing here trusts a key just because it is
# present in .env.
set -u

cd "$(dirname "$0")/.."

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

envval() { sed -n "s/^$1=//p" .env | tail -n 1; }

pass_count=0
fail_count=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; pass_count=$((pass_count + 1)); }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fail_count=$((fail_count + 1)); }
warn() { printf '  \033[33mMANUAL\033[0m %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

[ -f .env ] || { echo ".env not found. Copy .env.example to .env first." >&2; exit 1; }

# 1. RocketRide --------------------------------------------------------------
head_ "1. RocketRide.ai"
RR_URI="$(envval ROCKETRIDE_URI)"
RR_KEY="$(envval ROCKETRIDE_APIKEY)"
if [ -z "$RR_KEY" ]; then
  fail "ROCKETRIDE_APIKEY missing from .env"
else
  # /services is bearer-authenticated, so a 200 proves account + key together.
  code="$("$PY" - "$RR_URI" "$RR_KEY" <<'EOF'
import sys, urllib.request, urllib.error
req = urllib.request.Request(sys.argv[1].rstrip("/") + "/services",
                             headers={"Authorization": "Bearer " + sys.argv[2]})
try:
    print(urllib.request.urlopen(req, timeout=30).status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print(type(e).__name__)
EOF
)"
  [ "$code" = "200" ] && pass "API key authenticates against $RR_URI/services" \
                      || fail "GET /services returned $code"
fi
# Credits are dashboard-only: no endpoint reports a balance, and a task that
# fails for lack of them is indistinguishable from a malformed pipeline.
warn "credits: confirm the coupon balance at https://staging.rocketride.ai"

# 2. HydraDB -----------------------------------------------------------------
head_ "2. HydraDB"
if ! "$PY" -c 'import hydra_db' 2>/dev/null; then
  fail "hydradb-sdk not installed (pip install -r requirements.txt)"
else
  out="$("$PY" - "$(envval HYDRADB_APIKEY)" "$(envval HYDRADB_DATABASE)" <<'EOF'
import sys
from hydra_db import HydraDB
key, db = sys.argv[1], sys.argv[2] or "default-tenant"
try:
    r = HydraDB(token=key).databases.status(database=db).data
    i = r.infra
    ok = i.graph_status and i.ready_for_ingestion and i.scheduler_status
    print(("OK " if ok else "DOWN ") + f"{r.database} (org {r.org_id})")
except Exception as e:
    print("ERR " + str(e)[:120])
EOF
)"
  case "$out" in
    OK*) pass "instance reachable and ready: ${out#OK }" ;;
    *)   fail "${out}" ;;
  esac
fi

# 3. hotdata.dev -------------------------------------------------------------
head_ "3. hotdata.dev"
if ! command -v hotdata >/dev/null 2>&1; then
  fail "CLI not installed (brew install hotdata-dev/tap/cli)"
else
  HOTDATA_API_KEY="$(envval HOTDATA_API_KEY)"; export HOTDATA_API_KEY
  WS="$(envval HOTDATA_WORKSPACE_ID)"
  CATALOG="$(envval HOTDATA_CATALOG)"; CATALOG="${CATALOG:-jobs}"
  if [ -z "$HOTDATA_API_KEY" ]; then
    fail "HOTDATA_API_KEY missing from .env (note the spelling: the CLI reads no other name)"
  elif ! hotdata workspaces list --no-input -o json >/dev/null 2>&1; then
    fail "key rejected — check HOTDATA_API_KEY"
  else
    pass "registered, CLI authenticates by key"
    n="$(hotdata ingest sources list --no-input -w "$WS" -o json 2>/dev/null \
         | "$PY" -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null)"
    [ "${n:-0}" -ge 1 ] 2>/dev/null \
      && pass "$n data source(s) connected" \
      || fail "no data source connected (run: sh scripts/hotdata-setup.sh)"
    # `-o json` returns columns + rows, not records: the count is rows[0][0].
    rows="$(hotdata query "SELECT count(*) AS n FROM $CATALOG.public.jobs" \
            --no-input -w "$WS" -o json 2>/dev/null \
            | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["rows"][0][0])' 2>/dev/null)"
    [ -n "${rows:-}" ] && pass "$CATALOG.public.jobs holds $rows rows" \
                       || fail "cannot query $CATALOG.public.jobs"
  fi
fi

# 4. Cognee Cloud -------------------------------------------------------------
head_ "4. Cognee.ai (cloud tenant)"
CG_URL="$(envval COGNEE_BASE_URL)"
CG_KEY="$(envval COGNEE_API_KEY)"
if [ -z "$CG_URL" ] || [ -z "$CG_KEY" ]; then
  fail "COGNEE_BASE_URL / COGNEE_API_KEY missing from .env"
elif ! "$PY" -c 'import httpx' 2>/dev/null; then
  fail "httpx not installed (pip install -r requirements.txt)"
else
  # A 200 on a key-authenticated route proves tenant + key together. Bearer is
  # not accepted, so this also catches a key pasted into the wrong header.
  out="$("$PY" - <<'EOF' 2>&1
from memory.cognee_client import CogneeCloud
try:
    with CogneeCloud() as c:
        used = c.quota()["storageUsedInBytes"]
        print(f"OK {len(c.datasets())} dataset(s), {used/1e6:.1f} MB stored")
except Exception as e:
    print("ERR " + str(e)[:140])
EOF
)"
  case "$out" in
    OK*) pass "tenant authenticates: ${out#OK }" ;;
    *)   fail "${out}" ;;
  esac

  if [ -n "${SKIP_COGNEE:-}" ]; then
    warn "ingest skipped (SKIP_COGNEE set)"
  else
    printf '  running a real ingest against the tenant, this takes ~35s\n'
    out="$("$PY" - <<'EOF' 2>/dev/null
from memory.cognee_client import CogneeCloud
with CogneeCloud(dataset="verify-setup") as c:
    c.add_text("Ada Lovelace wrote the first computer program in 1843.")
    c.cognify(wait=True)
    hits = c.search("Who wrote the first computer program?")
    print("ADA" if "Ada" in str(hits) else "MISS")
    c.delete_dataset(c.dataset_id())
EOF
)"
    [ "$out" = "ADA" ] && pass "add + cognify + search round trip on the tenant" \
                       || fail "round trip did not return the expected answer ($out)"
  fi
fi

# 5. Rote --------------------------------------------------------------------
head_ "5. Modiqo.ai (Rote)"
if ! command -v rote >/dev/null 2>&1; then
  fail "rote not installed (curl -fsSL https://getrote.dev/playoffs/install.sh | sh)"
else
  rote whoami >/dev/null 2>&1 && pass "signed in as $(rote whoami 2>/dev/null | sed -n 's/^ok: //p')" \
                              || fail "not signed in (rote login)"
  if rote adapter info rocketride >/dev/null 2>&1; then
    pass "rocketride adapter installed"
    # A live call is the only proof the adapter reaches a target API with the
    # key from rote's own token store. Workspaces live under ~/.rote, never here.
    WSDIR="${ROTE_HOME:-$HOME/.rote}/workspaces/verify-setup"
    rote init verify-setup >/dev/null 2>&1
    # A rote call reports `ok` as long as the request was made, so the HTTP
    # result has to be read back out of the cached response.
    out="$(cd "$WSDIR" && rote rocketride_call listServices '{}' 2>&1)"
    rid="$(printf '%s\n' "$out" | sed -n 's/^response: *//p' | tail -1)"
    body="$(cd "$WSDIR" && rote "$rid" '$' 2>&1)"
    if [ -n "$rid" ] && ! printf '%s\n' "$body" | grep -q '"is_error": *true'; then
      pass "authenticated listServices call through the adapter"
    else
      fail "adapter call failed (check: rote token list | grep ROCKETRIDE_APIKEY)"
    fi
  else
    fail "rocketride adapter missing (ROTE_ORG=data-ai-hack sh scripts/rote-setup.sh)"
  fi
fi

printf '\n\033[1m%d passed, %d failed\033[0m\n' "$pass_count" "$fail_count"
[ "$fail_count" -eq 0 ]
