"""The three RocketRide pipelines, as data.

**The pipeline JSON shape is not in the OpenAPI spec** — ``POST /task`` takes a
freeform object and reports what is wrong one error at a time — but it *is*
documented at https://docs.rocketride.org/concepts/pipelines and
.../concepts/agents-tools-skills. Read those before changing anything here.

```json
{"pipeline": {"components": [
  {"id": "src",   "provider": "webhook", "config": {...}},
  {"id": "agent", "provider": "agent_rocketride", "config": {...},
   "input": [{"lane": "questions", "from": "src"}]},
  {"id": "llm",   "provider": "llm_openai_api", "config": {...},
   "control": [{"classType": "llm", "from": "agent"}]}
]}}
```

**The one that costs an afternoon: ``control`` lives on the helper, not on the
agent.** An agent has input lanes and nothing else; each LLM, memory and tool
node carries a ``control`` array whose ``from`` points back at the agent that
invokes it. Wiring it the other way round — an ``invoke`` list on the agent — is
accepted at submit and then fails at *run* time with "You must have 1, and only
1 llm node connected to your agent", which reads like a config problem rather
than a shape problem. One helper can serve several invokers: one entry per
invoker in its ``control`` array.

The rest, all validated server-side so a typo fails at submit rather than mid-run:

* ``input`` is an **array of objects**, each ``{"lane": <lane>, "from": <id>}``.
  A bare id is "input entries must be objects"; a missing ``from`` is "'from'
  must be a non-empty string".
* **lanes are checked against the consumer's declared lanes** — an unknown one
  is rejected by name — and so are references: an unknown ``from`` is "input
  references unknown component id".
* ``config`` is the node's own ``Pipe.schema``; a node with a ``dependencies``
  block wants the discriminator *and* its nested object: ``llm_openai_api``
  needs ``{"profile": "custom", "custom": {...}}``, ``mcp_client`` needs
  ``{"transport": "streamable-http", "http": {...}}``.
* **the webhook source classifies by content type.** ``Content-Type: text/plain``
  lands on the ``text`` lane; an ``application/json`` body is accepted with
  ``resultTypes: {}`` and then goes nowhere at all — no error, no output, just a
  completed object that reached no consumer.
* **the webhook emits ``text``, and the agent only accepts ``questions``**, so a
  ``question`` node sits between them. The docs' example wires an LLM straight
  to the source on a ``questions`` lane; on this deployment the webhook does not
  produce one, and without the converter the object is swallowed silently.
* the documented ``/webhook/{project_id}/{source_id}`` route is **404 on
  staging** — it belongs to the local runtime. Use the legacy
  ``/webhook?token=<private token>``, which the docs say stays supported.

The pipelines split at every human gate and never block waiting for a person
(DESIGN §4). A pipeline that sits open waiting for a Slack click times out,
cannot be restarted cleanly, and produces one enormous Rote capture spanning a
human pause.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent.config import ROOT, Settings, settings

# Laid out left to right so the designer draws a readable graph. Without a `ui`
# position every component lands at the origin and the canvas renders six nodes
# stacked on one another with the edges crossing through them — which looks like
# a broken pipeline and is really just a missing layout.
LAYOUT: dict[str, tuple[int, int]] = {
    "src":   (0, 160),
    "q":     (260, 160),
    "agent": (540, 160),
    "out":   (880, 160),
    "llm":   (420, 440),
    "mem":   (640, 440),
    "mcp":   (860, 440),
    "slack": (1080, 440),
}

# The agent's own instructions. Deliberately short: the tool descriptions on the
# MCP server carry the detail, and every token here is paid on every wave.
AGENT_INSTRUCTIONS = """\
You are a job-hunting agent working for one candidate.

Before doing anything, call find_play with the task type — an exact match
replays with no reasoning, a partial match tells you which fields are novel.

Ask for queries by name with hotdata_query and hydra_query; call list_queries if
you need the names. Never compose SQL or Cypher: there is no tool that takes it.
Tools return ids and one-line summaries. Call job_detail only when you genuinely
need a job's text.

Judging a day is judge_day. Preparing a pack is apply_pack. Recording what the
human said is record_feedback. Prefer one of those to assembling the steps
yourself — they are the paths that replay.

Never submit an application anywhere. Prepare the pack, log it, hand it over.
"""


def _component(component_id: str, provider: str, config: Mapping[str, Any],
               input_: Sequence[Mapping[str, str]] | None = None,
               control: Sequence[Mapping[str, str]] | None = None,
               name: str = "") -> dict[str, Any]:
    x, y = LAYOUT.get(component_id, (0, 0))
    component: dict[str, Any] = {
        "id": component_id, "provider": provider,
        "config": {"type": provider, **config},
        "ui": {"position": {"x": x, "y": y}},
    }
    if name:
        component["name"] = name
    if input_:
        component["input"] = [dict(entry) for entry in input_]
    if control:
        component["control"] = [dict(entry) for entry in control]
    return component


def _controlled_by(agent_id: str, class_type: str) -> list[dict[str, str]]:
    """The helper's side of an invoke connection."""
    return [{"classType": class_type, "from": agent_id}]


# --- the nodes -------------------------------------------------------------

def source(component_id: str = "src", kind: str = "webhook") -> dict[str, Any]:
    """``webhook`` for the timed loop, ``chat`` for a UI to demo from.

    The webhook source is what makes the hour-3 loop honest: cron curls it, so
    every point on the chart is a real RocketRide run rather than a local script
    we later describe as one.
    """
    return _component(component_id, kind, {"mode": "Source", "hideForm": True})


def llm(config: Settings, component_id: str = "llm",
        agent_id: str = "agent") -> dict[str, Any]:
    """Qwen through ``llm_openai_api`` (profile ``custom``), not ``llm_qwen``.

    ``llm_qwen`` expects a regional DashScope host and its keys are not
    interchangeable between regions; ours is a workspace host with a
    ``/compatible-mode/v1`` base URL.

    ``enable_thinking: false`` is passed through in ``custom`` even though the
    node's form schema does not expose it — a component's config is a freeform
    object, so an extra key may reach the client. It zeroes ~87 reasoning tokens
    a call with tool calling intact, and reasoning tokens vary run to run on an
    identical prompt, which is noise on the y-axis of the headline chart. If the
    node rejects it, drop this one key: the fallback is not an ``-instruct``
    model, it is picking the model that thinks least (DESIGN §10).
    """
    return _component(component_id, "llm_openai_api", control=_controlled_by(agent_id, "llm"),
                      config={
        "profile": "custom",
        "custom": {
            "apikey": config.qwen_api_key,
            "base_url": config.qwen_base_url,
            "model": config.qwen_model,
            "modelSource": "custom",
            "modelTotalTokens": 131072,
            "enable_thinking": False,
        },
    })


def memory(component_id: str = "mem", agent_id: str = "agent") -> dict[str, Any]:
    """``agent_rocketride`` requires exactly one memory connection.

    ``memory_internal`` is keyless, run-scoped, and sufficient. ``db_hydradb``
    would double as memory and graph store, which is tidier but couples the two
    — and the graph is reached through our MCP server anyway, because the
    ``db_hydradb`` node has no query language on it.
    """
    return _component(component_id, "memory_internal", {},
                      control=_controlled_by(agent_id, "memory"))


def mcp(endpoint: str, bearer: str, component_id: str = "mcp",
        agent_id: str = "agent") -> dict[str, Any]:
    """The only bridge to hotdata and Rote — no node in the catalog reaches them.

    Streamable HTTP through a tunnel, because the engine runs on staging and
    stdio is not available to us. The tunnel is a public URL fronting SQL, a
    graph and a Rote shell-out, so it carries a bearer.
    """
    return _component(component_id, "mcp_client", {
        "serverName": "data-ai-hack",
        "transport": "streamable-http",
        "http": {"endpoint": endpoint, "bearer": bearer, "headers": {}},
    }, control=_controlled_by(agent_id, "tool"))


def slack(webhook_url: str = "", token: str = "", component_id: str = "slack",
          agent_id: str = "agent") -> dict[str, Any]:
    return _component(component_id, "tool_slack",
                      {"webhookUrl": webhook_url, "token": token},
                      control=_controlled_by(agent_id, "tool"))


def question(component_id: str = "q", source_id: str = "src") -> dict[str, Any]:
    """``text`` in, ``questions`` out. The adapter the webhook needs.

    It changes nothing about the payload — it just re-labels the lane — but
    without it the agent has no input it will accept and the run produces
    nothing at all, with no error to explain why.
    """
    return _component(component_id, "question", {},
                      input_=[{"lane": "text", "from": source_id}])


def agent(instructions: str, max_waves: int = 12, component_id: str = "agent",
          source_id: str = "q") -> dict[str, Any]:
    """Input lanes only. The helpers point their ``control`` at this id."""
    return _component(
        component_id, "agent_rocketride",
        {"instructions": instructions, "max_waves": max_waves,
         "agent_description": "Job-hunting agent with graph and corpus memory"},
        input_=[{"lane": "questions", "from": source_id}],
    )


def answers(component_id: str = "out", source_id: str = "agent") -> dict[str, Any]:
    """Collect the agent's answers so the task result carries them."""
    return _component(component_id, "response_json", {"laneName": "answers"},
                      input_=[{"lane": "answers", "from": source_id}])


# --- the pipelines ---------------------------------------------------------

def build(mcp_endpoint: str, mcp_bearer: str = "", instructions: str = "",
          source_kind: str = "webhook", max_waves: int = 12,
          slack_webhook: str = "", config: Settings | None = None,
          name: str = "", description: str = "") -> dict[str, Any]:
    """One agent, one model, one memory, our tools. The shape all three share."""
    config = config or settings()
    components = [
        source("src", source_kind),
        question(),
        agent(instructions or AGENT_INSTRUCTIONS, max_waves=max_waves),
        llm(config),
        memory(),
        mcp(mcp_endpoint, mcp_bearer or config.mcp_bearer),
    ]
    if slack_webhook:
        components.append(slack(slack_webhook))
    components.append(answers())
    return {
        "name": name or "data-ai-hack",
        "description": description or "Job-hunting agent: judge, prepare, learn.",
        "version": 1,
        "source": "src",
        "components": components,
        "viewport": {"x": 0, "y": 0, "zoom": 0.8},
        "snapToGrid": True,
        "snapGridSize": [20, 20],
        "editorMode": "design",
    }


P_A = """\
Judge today. Call judge_day, then post the digest it returns, unchanged, as your
answer. If it returns an autonomy prompt, include that too. Do not re-rank or
re-word anything: the ranking is deterministic and the digest is the record."""

P_B = """\
Prepare an apply-pack for the job id in the question. Call apply_pack. If it
comes back with ok=false, say so and give the failure reason verbatim — a pack
that failed its citation check must never be presented as a draft. List any
questions it still needs from the human."""

P_C = """\
Record the human's response. Parse it into signals — keep, skip, or not_for_me
with a reason — and call record_feedback once with all of them. If it returns a
preference hypothesis, ask the human to confirm it, quoting the evidence."""


def pipeline_a(mcp_endpoint: str, **kwargs: Any) -> dict[str, Any]:
    """P-A: judge the day. Timer tick, or "next day" in chat."""
    kwargs.setdefault("name", "P-A judge the day")
    kwargs.setdefault("description", "Advance the clock, rank the pool, post the digest.")
    return build(mcp_endpoint, instructions=AGENT_INSTRUCTIONS + "\n" + P_A, **kwargs)


def pipeline_b(mcp_endpoint: str, **kwargs: Any) -> dict[str, Any]:
    """P-B: apply-pack. The human taps a role, or D1 autonomy picks one."""
    kwargs.setdefault("name", "P-B apply pack")
    kwargs.setdefault("description", "One LLM call wrapped in four deterministic tool calls.")
    return build(mcp_endpoint, instructions=AGENT_INSTRUCTIONS + "\n" + P_B, **kwargs)


def pipeline_c(mcp_endpoint: str, **kwargs: Any) -> dict[str, Any]:
    """P-C: record feedback. The human answers, keeps, skips, reports a reply."""
    kwargs.setdefault("name", "P-C record feedback")
    kwargs.setdefault("description", "Absorb the response; check for a preference rule.")
    return build(mcp_endpoint, instructions=AGENT_INSTRUCTIONS + "\n" + P_C, **kwargs)


PIPELINES = {"P-A": pipeline_a, "P-B": pipeline_b, "P-C": pipeline_c}

PIPE_DIR = ROOT / "rocketride"


def write_pipe_files(mcp_endpoint: str, mcp_bearer: str = "",
                     directory: Path | None = None, redact: bool = True) -> list[Path]:
    """Write the three pipelines as ``.pipe`` files the IDE designer can open.

    The extension lists ``.pipe`` files in the workspace under PIPELINES; a
    pipeline submitted over the API appears only under AD-HOC and cannot be
    edited. Writing them makes the graph openable, editable and reviewable.

    **Keys are redacted by default.** A ``.pipe`` file holds the LLM's API key
    and the MCP bearer in plain text, and these live in a git repository — so
    what lands on disk is ``${QWEN_API_KEY}`` style placeholders, and
    ``load_pipe`` fills them at submit time from ``.env``.
    """
    directory = directory or PIPE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for label, factory in PIPELINES.items():
        pipeline = factory(mcp_endpoint, mcp_bearer=mcp_bearer)
        if redact:
            pipeline = _redact(pipeline)
        path = directory / f"{label.lower().replace('-', '')}.pipe"
        path.write_text(json.dumps(pipeline, indent=2) + "\n")
        written.append(path)
    return written


_PLACEHOLDERS = {
    "apikey": "${QWEN_API_KEY}",
    "base_url": "${QWEN_BASE_URL}",
    "model": "${QWEN_MODEL}",
    "bearer": "${MCP_BEARER_TOKEN}",
    "endpoint": "${MCP_ENDPOINT}",
}


def _redact(pipeline: Mapping[str, Any]) -> dict[str, Any]:
    """Swap secrets for ``${VAR}`` placeholders before anything touches disk."""
    out = json.loads(json.dumps(pipeline))
    for component in out["components"]:
        for block in (component.get("config") or {}).values():
            if isinstance(block, dict):
                for key, placeholder in _PLACEHOLDERS.items():
                    if key in block:
                        block[key] = placeholder
    return out


def load_pipe(path: str | Path, config: Settings | None = None,
              mcp_endpoint: str = "") -> dict[str, Any]:
    """Read a ``.pipe`` file and fill the ``${VAR}`` placeholders from .env."""
    config = config or settings()
    values = {
        "${QWEN_API_KEY}": config.qwen_api_key,
        "${QWEN_BASE_URL}": config.qwen_base_url,
        "${QWEN_MODEL}": config.qwen_model,
        "${MCP_BEARER_TOKEN}": config.mcp_bearer,
        "${MCP_ENDPOINT}": mcp_endpoint,
    }
    text = Path(path).read_text()
    for placeholder, value in values.items():
        if placeholder in text and not value:
            raise ValueError(f"{path} needs {placeholder} but it is empty in .env")
        text = text.replace(placeholder, value)
    return json.loads(text)
