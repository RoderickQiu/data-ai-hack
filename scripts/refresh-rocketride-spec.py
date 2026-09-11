#!/usr/bin/env python3
"""Fetch RocketRide's live OpenAPI spec and write an adapter-ready copy.

RocketRide publishes https://api.rocketride.ai/openapi.json without a
`servers` block or a `securitySchemes` entry, and it mixes real API operations
with static-asset and OAuth-bounce routes. Rote builds an adapter straight from
an OpenAPI document, so this script produces rote/rocketride.openapi.json with:

  * servers        -> the ROCKETRIDE_URI base URL
  * securitySchemes-> HTTP bearer, applied globally (Authorization: Bearer <key>)
  * paths          -> only the operations an agent should call

Run it again whenever RocketRide ships a new engine version.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "rote" / "rocketride.openapi.json"

BASE_URL = os.environ.get("ROCKETRIDE_URI", "https://api.rocketride.ai:443").rstrip("/")
SPEC_URL = f"{BASE_URL}/openapi.json"

# Operations worth exposing to an agent. Everything else in the live spec is
# either a static SPA route, an OAuth/Stripe bounce, or marked deprecated.
KEEP: dict[str, set[str]] = {
    "/status": {"get"},
    "/version": {"get"},
    "/services": {"get"},
    "/task": {"post", "get", "delete"},
    "/task/data": {"post"},
    "/webhook": {"post"},
    "/marketplace/apps": {"get"},
}

# Friendlier operationIds than the FastAPI defaults (e.g. task_Execute_task_post).
RENAME: dict[tuple[str, str], str] = {
    ("/status", "get"): "getStatus",
    ("/version", "get"): "getVersion",
    ("/services", "get"): "listServices",
    ("/task", "post"): "executeTask",
    ("/task", "get"): "getTaskStatus",
    ("/task", "delete"): "cancelTask",
    ("/task/data", "post"): "uploadTaskData",
    ("/webhook", "post"): "postWebhook",
    ("/marketplace/apps", "get"): "listMarketplaceApps",
}


def main() -> int:
    with urllib.request.urlopen(SPEC_URL, timeout=30) as resp:  # noqa: S310
        spec = json.load(resp)

    paths: dict[str, dict] = {}
    for path, methods in KEEP.items():
        src = spec["paths"].get(path)
        if src is None:
            print(f"warning: {path} missing from live spec, skipping", file=sys.stderr)
            continue
        kept = {}
        for method in methods:
            op = src.get(method)
            if op is None:
                print(f"warning: {method.upper()} {path} missing, skipping", file=sys.stderr)
                continue
            op = dict(op)
            op["operationId"] = RENAME.get((path, method), op.get("operationId"))
            # The live spec models the bearer key as an explicit header parameter.
            # Drop it so the security scheme below is the single source of auth.
            op["parameters"] = [
                p for p in op.get("parameters", [])
                if not (p.get("in") == "header" and p.get("name", "").lower() == "authorization")
            ]
            if not op["parameters"]:
                op.pop("parameters")
            kept[method] = op
        if kept:
            paths[path] = kept

    out = {
        "openapi": spec["openapi"],
        "info": {
            "title": "RocketRide Web Services",
            "version": spec["info"].get("version", "0.1.0"),
            "description": (
                "RocketRide orchestrates and executes pipelines (\"tasks\"). "
                "Submit a pipeline with executeTask, poll it with getTaskStatus, "
                "stream inputs with uploadTaskData, and stop it with cancelTask. "
                "Every call authenticates with `Authorization: Bearer <ROCKETRIDE_APIKEY>`."
            ),
        },
        "servers": [{"url": BASE_URL, "description": "RocketRide managed cloud engine"}],
        "security": [{"bearerAuth": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "RocketRide API key. Supplied at runtime from ROCKETRIDE_APIKEY.",
                }
            },
            "schemas": spec.get("components", {}).get("schemas", {}),
        },
    }
    # Public, unauthenticated probes.
    for path in ("/status", "/version"):
        if path in out["paths"]:
            out["paths"][path]["get"]["security"] = []

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    ops = sum(len(m) for m in paths.values())
    print(f"wrote {OUT.relative_to(ROOT)}: engine {out['info']['version']}, {ops} operations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
