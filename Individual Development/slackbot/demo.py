"""Post fixture payloads through the running API.

Doubles as the integration example for teammates: this is exactly the HTTP call
a RocketRide pipeline or the MCP server makes.

    python -m slackbot.demo cold      # first-run digest, then a pack that asks 6 questions
    python -m slackbot.demo warm      # replayed digest with reasons, pack that asks nothing
    python -m slackbot.demo preference
    python -m slackbot.demo autonomy
    python -m slackbot.demo claims
    python -m slackbot.demo question
"""

import json
import sys
import urllib.error
import urllib.request

from . import fixtures
from .config import load_config

SEQUENCES = {
    "cold": [("/digest", fixtures.DIGEST_COLD), ("/pack", fixtures.PACK_COLD)],
    "warm": [("/digest", fixtures.DIGEST_WARM), ("/pack", fixtures.PACK_WARM)],
    "digest": [("/digest", fixtures.DIGEST_WARM)],
    "pack": [("/pack", fixtures.PACK_WARM)],
    "preference": [("/preference", fixtures.PREFERENCE)],
    "autonomy": [("/autonomy", fixtures.AUTONOMY)],
    "claims": [("/claims", fixtures.CLAIMS)],
    "question": [("/question", fixtures.QUESTION)],
}


def call(base: str, token: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read())


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "warm"
    if name not in SEQUENCES:
        print(f"unknown sequence {name!r}; try one of: {', '.join(SEQUENCES)}")
        return 1

    cfg = load_config()
    base = f"http://{cfg.api_host}:{cfg.api_port}"
    for path, payload in SEQUENCES[name]:
        try:
            res = call(base, cfg.api_token, path, payload)
        except urllib.error.URLError as exc:
            print(f"cannot reach {base}{path} - is 'python -m slackbot.app' running? ({exc})")
            return 1
        print(f"{path} -> {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
