"""Submit a pipeline, watch it, ask it a question, read the token usage back.

``POST /task`` returns a **token** and the pipeline starts running; it does not
block. ``GET /task?token=...`` is the status, and it is also where the answers
and the per-node metrics come back. ``DELETE /task?token=...`` stops it — worth
being disciplined about, because a `chat` or `webhook` source sits open waiting
for input and every abandoned one is a pipeline still on the account.

On credits: there is no usage route anywhere in the OpenAPI, so nothing can
watch the balance for us and a task that fails for lack of credits looks exactly
like a malformed pipeline. Check the dashboard at the first sign, not at hour 6
with a live run pending (DESIGN §11).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

from agent.config import Settings, settings
from agent.net import http_url


class RocketRideError(RuntimeError):
    pass


@dataclass
class Task:
    token: str
    public_key: str = ""
    chat_url: str = ""
    webhook_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class RocketRide:
    def __init__(self, config: Settings | None = None, timeout: float = 120.0):
        self.config = config or settings()
        self.key = self.config.rocketride_api_key
        self.timeout = timeout
        if not (self.config.rocketride_uri or "").strip() or not self.key:
            raise RocketRideError("ROCKETRIDE_URI / ROCKETRIDE_APIKEY are not set")
        # Pinned before the first request, because every request below attaches
        # the API key: a ROCKETRIDE_URI that is a typo, or a scheme urllib is
        # willing to open but we never meant to, would send that key somewhere
        # of someone else's choosing.
        try:
            self.base = http_url(self.config.rocketride_uri, "ROCKETRIDE_URI").rstrip("/")
        except ValueError as exc:
            raise RocketRideError(str(exc)) from exc

    def _call(self, method: str, path: str, body: Any = None,
              **query: Any) -> dict[str, Any]:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:500]
            raise RocketRideError(f"{method} {path} -> {exc.code}: {detail}") from exc
        if payload.get("status") == "Error":
            # The engine reports one problem at a time, with the file and line
            # in its own source. Quoting it verbatim is the fastest way through.
            raise RocketRideError(json.dumps(payload.get("error"))[:400])
        return payload.get("data") or {}

    # -- lifecycle -------------------------------------------------------

    def submit(self, pipeline: Mapping[str, Any], trace: bool = False) -> Task:
        query = {"trace": "true"} if trace else {}
        data = self._call("POST", "/task", {"pipeline": dict(pipeline)}, **query)
        token = data.get("token")
        if not token:
            raise RocketRideError(f"no task token in response: {json.dumps(data)[:200]}")
        task = Task(token=token, raw=data)
        return self._decorate(task)

    def status(self, token: str) -> dict[str, Any]:
        return self._call("GET", "/task", token=token)

    def cancel(self, token: str) -> dict[str, Any]:
        return self._call("DELETE", "/task", token=token)

    def _decorate(self, task: Task) -> Task:
        """Pull the public key and the URLs out of the status ``notes`` block."""
        try:
            data = self.status(task.token)
        except RocketRideError:
            return task
        for note in data.get("notes") or []:
            task.public_key = note.get("auth-key", "") or task.public_key
            link = (note.get("url-link") or "").replace("{host}", self.base)
            if "chat" in link:
                task.chat_url = link
        task.webhook_url = f"{self.base}/webhook?token={task.token}"
        return task

    def wait_ready(self, token: str, timeout: float = 90.0,
                   poll: float = 2.0) -> dict[str, Any]:
        """Block until the source is accepting input, or the task has failed.

        ``state`` alone is not enough to trust: a pipeline can reach a running
        state and then report a wiring error, so the errors list is checked on
        every poll rather than at the end.
        """
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.status(token)
            if last.get("errors"):
                raise RocketRideError(f"pipeline errors: {json.dumps(last['errors'])[:400]}")
            status_text = (last.get("status") or "").lower()
            if "ready" in status_text or last.get("completed"):
                return last
            time.sleep(poll)
        raise RocketRideError(f"task not ready after {timeout}s: {last.get('status')!r}")

    # -- talking to a running pipeline ------------------------------------

    def ask(self, token: str, question: str, timeout: float = 300.0) -> dict[str, Any]:
        """Send one question and return the answers.

        **The body goes as ``text/plain``.** A JSON body is accepted and then
        silently dropped: the webhook classifies by content type, and an
        unclassified object reaches no consumer, producing no answer and no
        error. This one detail is the difference between a working pipeline and
        an hour of debugging a pipeline that looks fine.

        The POST blocks until the pipeline answers, so the answers come back in
        this response rather than needing a poll.
        """
        url = f"{self.base}/webhook?{urllib.parse.urlencode({'token': token})}"
        request = urllib.request.Request(
            url, data=question.encode(), method="POST",
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "text/plain"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise RocketRideError(f"webhook -> {exc.code}: {exc.read().decode()[:400]}") from exc
        body = ((payload.get("data") or {}).get("objects") or {}).get("body") or {}
        if isinstance(body, Mapping) and body.get("status") == "Error":
            raise RocketRideError(json.dumps(body.get("error"))[:400])
        return {"answers": body.get("answers") or [],
                "result_types": (payload.get("data") or {}).get("resultTypes") or {},
                "raw": payload}

    # -- what the chart needs ---------------------------------------------

    @staticmethod
    def usage(status: Mapping[str, Any]) -> dict[str, Any]:
        """Token usage for one run, if the engine reports it.

        Line 1 of the chart depends on this. If it is not here, count at the MCP
        boundary instead and say on the slide that the cost line is a proxy —
        lines 2 and 3 are unaffected, which is the other reason for having three
        (DESIGN §11). ``pipeflow.byPipe`` is where per-node counters live.

        There is no usage route in the OpenAPI, so every key below is a guess
        about an undocumented shape and ``reported`` is the only thing a caller
        should branch on. Two places are searched: the per-node ``byPipe``
        counters, and a top-level ``usage`` object if one exists. Splitting the
        total into ``tokens_in``/``tokens_out`` is best-effort — a counter that
        does not say which direction it measures lands in ``tokens_out``,
        because a run's output is the part that grows, and a wrong split still
        sums right.

        **The bare top-level ``tokens`` field is deliberately not harvested.**
        ``python -m rocketride status`` reports it as the credit balance, and a
        balance on the token axis would be a falling line that means the exact
        opposite of what the chart claims. It is returned as ``credits`` instead,
        so a live probe can show it without it reaching a run row.
        """
        flow = status.get("pipeflow") or {}
        by_pipe = flow.get("byPipe") or {}
        found: dict[str, Any] = {}

        def _harvest(prefix: str, source: Any, inside: bool = False) -> None:
            """``inside`` means we are already within a ``usage``-shaped object,
            where every number counts — ``{"tokenUsage": {"in": 4, "out": 6}}``
            names the direction on the leaf and the unit on the parent."""
            if not isinstance(source, Mapping):
                return
            for key, value in source.items():
                name = f"{prefix}{key}" if prefix else str(key)
                counts = inside or _counts_tokens(key)
                if isinstance(value, Mapping):
                    if counts:
                        _harvest(f"{name}.", value, inside=True)
                elif counts and _is_count(value):
                    found[name] = value

        _harvest("", status.get("usage"), inside=True)
        for pipe_name, pipe in by_pipe.items():
            _harvest(f"{pipe_name}.", pipe)

        tokens_in = sum(int(v) for k, v in found.items() if _direction(k) == "in")
        tokens_out = sum(int(v) for k, v in found.items() if _direction(k) != "in")
        return {
            "reported": bool(found),
            "counters": found,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "credits": status.get("tokens"),
            "words": status.get("wordsCount"),
            "total_pipes": flow.get("totalPipes"),
            "wall_ms": int(((status.get("endTime") or time.time())
                            - (status.get("startTime") or 0)) * 1000),
        }


_IN_WORDS = ("in", "input", "prompt", "request")


def _counts_tokens(key: Any) -> bool:
    lowered = str(key).lower()
    return "token" in lowered or "usage" in lowered


def _is_count(value: Any) -> bool:
    """A token counter is a number. ``True`` is not one, and neither is a name."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _direction(key: str) -> str:
    """Which half of the token count a counter name refers to.

    Only the last word segment is read: ``chat.promptTokens`` is input and
    ``prompt.completionTokens`` is not, and looking at the whole dotted path
    would get both of those backwards.
    """
    tail = re.split(r"[._\-]|(?<=[a-z])(?=[A-Z])", key)[-2:]
    return "in" if any(part.lower() in _IN_WORDS for part in tail) else "out"


def _answer_count(status: Mapping[str, Any]) -> int:
    return int(status.get("completedCount") or 0)


def answers(status: Mapping[str, Any]) -> list[Any]:
    """The agent's answers, wherever the engine put them in this build."""
    for key in ("results", "answers", "output", "data"):
        value = status.get(key)
        if isinstance(value, list) and value:
            return value
    flow = (status.get("pipeflow") or {}).get("byPipe") or {}
    out = []
    for pipe in flow.values():
        if isinstance(pipe, Mapping) and pipe.get("answers"):
            out.append(pipe["answers"])
    return out
