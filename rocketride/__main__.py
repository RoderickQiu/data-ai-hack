"""Drive the RocketRide pipelines from the command line.

    python -m rocketride write   --endpoint https://<tunnel>/mcp   # .pipe files
    python -m rocketride up      --pipe rocketride/pa.pipe         # start it
    python -m rocketride ask     --token tk_... "judge today"      # talk to it
    python -m rocketride status  --token tk_...
    python -m rocketride down    --token tk_...                    # or --all

``up`` prints the token and the webhook URL; ``demo/loop.py`` takes that URL and
ticks it on a timer, which is what makes every point on the chart a real
RocketRide run rather than a local script we later describe as one.

**Stop what you start.** A webhook or chat source sits open waiting for input and
keeps consuming credits; there is no usage route to watch the balance with, so
an abandoned pipeline is invisible until a run fails for lack of them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent.config import env, settings
from agent.net import http_url
from rocketride.client import RocketRide, RocketRideError
from rocketride.pipelines import PIPELINES, load_pipe, write_pipe_files

ROOT = Path(__file__).resolve().parent.parent


def _endpoint(args: argparse.Namespace) -> str:
    endpoint = args.endpoint or env("MCP_ENDPOINT")
    if not endpoint:
        raise SystemExit(
            "no MCP endpoint. Start the server and a tunnel, then pass\n"
            "  --endpoint https://<tunnel-host>/mcp\n"
            "or set MCP_ENDPOINT in .env. RocketRide runs on staging, so it can "
            "only reach the server through a public URL.")
    # This value is not fetched here — it is written into a pipeline that
    # RocketRide's own servers then call. A scheme they would resolve on their
    # side, from our account, is worth refusing on ours.
    try:
        return http_url(endpoint, "MCP endpoint")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _pipe_path(value: Path) -> Path:
    """A ``--pipe`` file, confined to the checkout.

    ``write`` puts these under the repo and ``up`` reads them straight back, so
    nothing legitimate points outside it; confining means a stray ``..`` fails
    here rather than reading somewhere unexpected.
    """
    path = Path(value).expanduser()
    path = (path if path.is_absolute() else ROOT / path).resolve()
    if not path.is_relative_to(ROOT):
        raise SystemExit(f"{str(value)!r} resolves outside the repository ({path})")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rocketride", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    write = sub.add_parser("write", help="write the three .pipe files for the IDE designer")
    write.add_argument("--endpoint", default="")
    write.add_argument("--dir", type=Path, default=None)

    up = sub.add_parser("up", help="start a pipeline and print its token")
    up.add_argument("--pipe", type=Path, help="a .pipe file to run")
    up.add_argument("--name", choices=sorted(PIPELINES), help="build one instead")
    up.add_argument("--endpoint", default="")

    ask = sub.add_parser("ask", help="send one question to a running pipeline")
    ask.add_argument("--token", required=True)
    ask.add_argument("question", nargs="+")
    ask.add_argument("--timeout", type=float, default=600.0)

    status = sub.add_parser("status", help="show a task's state, errors and usage")
    status.add_argument("--token", required=True)

    down = sub.add_parser("down", help="stop a pipeline")
    down.add_argument("--token", nargs="*", default=[])

    args = parser.parse_args(argv)

    if args.command == "write":
        config = settings()
        paths = write_pipe_files(_endpoint(args), config.mcp_bearer, directory=args.dir)
        for path in paths:
            print(f"wrote {path}")
        print("\nOpen these in the IDE's RocketRide panel under PIPELINES. Secrets are\n"
              "placeholders; `up --pipe` fills them from .env at submit time.")
        return 0

    client = RocketRide()

    if args.command == "up":
        if args.pipe:
            # `--pipe` names a file this same CLI wrote, read under the operator's
            # own uid; `_pipe_path` confines it to the checkout regardless.
            # deepcode ignore PT: confined to the repo by _pipe_path, no trust boundary crossed
            pipeline = load_pipe(_pipe_path(args.pipe), mcp_endpoint=_endpoint(args))
        elif args.name:
            pipeline = PIPELINES[args.name](_endpoint(args))
        else:
            raise SystemExit("pass --pipe <file> or --name P-A")
        # The URL opened is ROCKETRIDE_URI, scheme-pinned in `RocketRide.__init__`.
        # `--endpoint` travels in the request body, and `_endpoint` checked it.
        # deepcode ignore Ssrf: host is ROCKETRIDE_URI, pinned in RocketRide.__init__
        task = client.submit(pipeline)
        client.wait_ready(task.token, timeout=120)
        print(f"token:   {task.token}")
        print(f"webhook: {task.webhook_url}")
        if task.chat_url:
            print(f"chat:    {task.chat_url}?auth={task.public_key}")
        print("\nstop it with:  python -m rocketride down --token " + task.token)
        return 0

    if args.command == "ask":
        # Same pinned host; `RocketRide.ask` urlencodes the token into a query
        # parameter, so it reaches neither the host nor the path.
        # deepcode ignore Ssrf: host is ROCKETRIDE_URI, pinned in RocketRide.__init__
        result = client.ask(args.token, " ".join(args.question), timeout=args.timeout)
        for answer in result["answers"]:
            print(answer)
        if not result["answers"]:
            print(f"(no answer; resultTypes={result['result_types']})", file=sys.stderr)
            return 1
        return 0

    if args.command == "status":
        data = client.status(args.token)
        print(json.dumps({
            "status": data.get("status"), "errors": data.get("errors"),
            "completed": data.get("completedCount"), "failed": data.get("failedCount"),
            "credits": data.get("tokens"), "usage": RocketRide.usage(data),
        }, indent=2, default=str))
        return 0

    if args.command == "down":
        if not args.token:
            raise SystemExit("pass --token tk_... (one or more)")
        for token in args.token:
            try:
                client.cancel(token)
                print(f"stopped {token}")
            except RocketRideError as exc:
                print(f"{token}: {str(exc)[:120]}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
