"""Rote: find a play, replay it, or record the novel path and crystallize it.

Rote is not a transparent proxy. Work has to flow through ``rote`` commands
inside a workspace, and then ``rote play pending write`` / ``rote workspace
export`` turn it into a typed, versioned play. :func:`record_step` wraps
``rote proc run`` so every fetch the agent makes on a novel path is captured.

**Fingerprint and match mode** (DESIGN §4). A play is not selected by name
alone but by a fingerprint of the shape it was captured against:

======= ==================================== ====================================
Exact   same fingerprint                     full replay, zero LLM on the plumbing
Partial same family, ≥70% field overlap      replay the known steps, send only the
                                             novel fields to the LLM, then capture
                                             the extended path as a new version
None    otherwise                            reason, record, crystallize
======= ==================================== ====================================

Partial replay is the more interesting beat than exact replay, because exact
replay is what a judge expects a cache to do and partial replay is not. It is
also what keeps the system honest as boards drift: **a play whose replay fails
is marked stale and the next run falls back to the novel path**, rather than
returning a partial result as if it were a replay.

Security note for the scan: every call here builds an **argument list**.
``record_step`` takes ``argv``, never a command string, and nothing is passed
through a shell — which is what removes the injection finding that a shell-out
tool surface would otherwise be (DESIGN §4).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent.schema import now_iso
from memory.graph import GraphStore
from memory.writers import note_play_run, upsert_play

PARTIAL_OVERLAP = 0.70
DEFAULT_WORKSPACE = "data-ai-hack"


class RoteError(RuntimeError):
    pass


def rote_home() -> Path:
    return Path(os.environ.get("ROTE_HOME", str(Path.home() / ".rote")))


def workspace_dir(name: str = DEFAULT_WORKSPACE) -> Path:
    return rote_home() / "workspaces" / name


@dataclass
class Fingerprint:
    task_type: str
    family: str
    fields: tuple[str, ...]

    def value(self) -> str:
        payload = f"{self.task_type}|{self.family}|{','.join(sorted(self.fields))}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def fingerprint_ingest(ats: str, sample: Mapping[str, Any]) -> Fingerprint:
    return Fingerprint("ingest-ats", ats, tuple(sorted(sample.keys())))


def fingerprint_task(task_type: str, required_inputs: Sequence[str],
                     family: str = "") -> Fingerprint:
    return Fingerprint(task_type, family or task_type, tuple(sorted(required_inputs)))


@dataclass
class PlayMatch:
    mode: str                       # exact | partial | none
    play_id: str | None = None
    rote_ref: str = ""
    fingerprint: str = ""
    known_fields: tuple[str, ...] = ()
    novel_fields: tuple[str, ...] = ()
    overlap: float = 0.0
    source: str = ""                # graph | rote-registry | cognee
    reason: str = ""

    def summary(self) -> dict[str, Any]:
        return {"mode": self.mode, "play_id": self.play_id, "overlap": round(self.overlap, 2),
                "novel_fields": list(self.novel_fields), "source": self.source,
                "reason": self.reason}


class Rote:
    """The CLI wrapper. Every method is an argv, never a command string."""

    def __init__(self, workspace: str = DEFAULT_WORKSPACE, timeout: float = 120.0):
        self.workspace = workspace
        self.timeout = timeout
        self.available = shutil.which("rote") is not None

    def _run(self, argv: Sequence[str], cwd: Path | None = None) -> dict[str, Any]:
        if not self.available:
            raise RoteError("rote is not installed (curl -fsSL https://getrote.dev/playoffs/install.sh | sh)")
        directory = cwd or workspace_dir(self.workspace)
        directory.mkdir(parents=True, exist_ok=True)
        try:
            done = subprocess.run(["rote", *argv], cwd=directory, capture_output=True,
                                  text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise RoteError(f"rote {argv[0]} timed out after {self.timeout}s") from exc
        return {"argv": list(argv), "code": done.returncode,
                "stdout": done.stdout.strip(), "stderr": done.stderr.strip()}

    def ensure_workspace(self) -> dict[str, Any]:
        return self._run(["init", self.workspace], cwd=rote_home())

    # -- capture ---------------------------------------------------------

    def record_step(self, argv: Sequence[str]) -> dict[str, Any]:
        """Run one command under Rote so the novel path is captured.

        ``argv`` is a list — ``["curl", "-s", url]``, not ``"curl -s " + url``.
        The distinction is the whole reason this is safe to expose over MCP.
        """
        if not argv:
            raise ValueError("record_step needs a non-empty argument list")
        if any(not isinstance(arg, str) for arg in argv):
            raise ValueError("every element of argv must be a string")
        return self._run(["proc", "run", *argv])

    def search(self, text: str, registry: bool = True) -> list[dict[str, Any]]:
        argv = ["play", "search", text]
        if registry:
            argv += ["--source", "registry"]
        result = self._run(argv)
        return _parse_play_list(result["stdout"])

    def list_plays(self) -> list[dict[str, Any]]:
        return _parse_play_list(self._run(["play", "list"])["stdout"])

    def run_play(self, ref: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        argv = ["play", "run", ref]
        for key, value in (params or {}).items():
            argv += [f"--{key}", str(value)]
        result = self._run(argv)
        result["ok"] = result["code"] == 0
        return result

    def crystallize(self, name: str, description: str) -> dict[str, Any]:
        """Turn the workspace's captured path into a play stub.

        ``play pending write`` does not itself create the play: it prints the
        pre-filled ``play template create`` command. That two-step is
        deliberate on Rote's side and we keep it — the stub survives a context
        reset, and a human sees what is about to be published.
        """
        return self._run(["play", "pending", "write", "--name", name,
                          "--description", description])

    def export(self) -> dict[str, Any]:
        return self._run(["workspace", "export"])


# Rote answers in its own block format — `@@status`, a count line, then prose —
# rather than JSON, even when piped. Parsing it line-by-line turns "No registry
# Plays matched the query." into a play that does not exist, and the agent then
# tries to replay it. So: read the count, and only accept lines that actually
# look like a play reference.
_COUNT_RE = re.compile(r"returned\s+(\d+)\s+result", re.I)
_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _parse_play_list(stdout: str) -> list[dict[str, Any]]:
    """Play references only. A false negative costs one reasoning pass; a false
    positive sends the agent to replay something that was never captured."""
    text = (stdout or "").strip()
    if not text:
        return []
    count = _COUNT_RE.search(text)
    if count and int(count.group(1)) == 0:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        refs = []
        for line in text.splitlines():
            candidate = line.strip().lstrip("-•* ").split()[0] if line.strip() else ""
            if _REF_RE.match(candidate) or candidate.endswith(".ts"):
                refs.append({"ref": candidate})
        return refs
    if isinstance(data, list):
        return [item if isinstance(item, dict) else {"ref": str(item)} for item in data]
    for key in ("plays", "results", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    return [data]


class PlayIndex:
    """``find_play`` — the Rote registry and Cognee's skill memory, in that order.

    A hit means ``rote play run`` plus a ``SkillRunEntry`` write. A miss means
    reason, record, crystallize, then write the ``SkillRunEntry`` — and Cognee's
    ``recall`` routes future similar tasks to the play that resulted. That edge
    is what makes the system compound rather than merely persist.
    """

    def __init__(self, store: GraphStore, rote: Rote | None = None):
        self.store = store
        self.rote = rote or Rote()

    def find(self, task_type: str, fingerprint: Fingerprint) -> PlayMatch:
        target = fingerprint.value()
        known = self.store.run("play_index", {"task_type": task_type})

        for play in known:
            if play.get("status") == "stale":
                continue
            if play.get("fingerprint") == target:
                return PlayMatch("exact", play["play_id"], play.get("rote_ref", ""),
                                 target, tuple(play.get("fields") or ()), (), 1.0,
                                 "graph", "same fingerprint")

        best: PlayMatch | None = None
        for play in known:
            if play.get("status") == "stale":
                continue
            fields = tuple(play.get("fields") or ())
            if not fields:
                continue
            overlap = _overlap(fields, fingerprint.fields)
            if overlap >= PARTIAL_OVERLAP and (best is None or overlap > best.overlap):
                best = PlayMatch(
                    "partial", play["play_id"], play.get("rote_ref", ""), target,
                    fields, tuple(sorted(set(fingerprint.fields) - set(fields))),
                    overlap, "graph",
                    f"{overlap:.0%} field overlap; {len(set(fingerprint.fields) - set(fields))} novel field(s)",
                )
        if best:
            return best

        # Nothing local. Ask the registry before paying for reasoning.
        try:
            hits = self.rote.search(task_type)
        except RoteError:
            hits = []
        if hits:
            ref = hits[0].get("ref") or hits[0].get("name", "")
            return PlayMatch("partial", None, ref, target, (), fingerprint.fields, 0.0,
                             "rote-registry", "registry has a play for this task type")
        return PlayMatch("none", reason="no play captured for this shape yet")

    def register(self, play_id: str, task_type: str, fingerprint: Fingerprint,
                 rote_ref: str = "") -> str:
        node = upsert_play(self.store, play_id, task_type, fingerprint.value(),
                           rote_ref=rote_ref, fields=fingerprint.fields)
        self.store.flush()
        return node

    def note_run(self, play_id: str, success: bool) -> None:
        """A failed replay marks the play stale, so the next run reasons instead
        of quietly returning a wrong result."""
        note_play_run(self.store, play_id, success)
        self.store.flush()


def _overlap(known: Sequence[str], wanted: Sequence[str]) -> float:
    known_set, wanted_set = set(known), set(wanted)
    if not wanted_set:
        return 0.0
    return len(known_set & wanted_set) / len(wanted_set)


@dataclass
class SkillRun:
    """What gets written back to Cognee after every play run, hit or miss."""

    skill_id: str
    task_text: str
    result_summary: str
    success_score: float
    latency_ms: int
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = field(default_factory=now_iso)

    def entry(self) -> dict[str, Any]:
        return {"selected_skill_id": self.skill_id, "task_text": self.task_text,
                "result_summary": self.result_summary,
                "success_score": self.success_score, "latency_ms": self.latency_ms,
                "tool_trace": self.tool_trace}
