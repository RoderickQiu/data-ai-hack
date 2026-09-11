"""Cognee: what was said, how it was recalled, and what the agent did about it.

Thin layer over :mod:`memory.cognee_client` that adds the three things every
caller in this repo needs and none of which belong in the HTTP client:

* **the dataset is always named.** ``recall`` without ``datasets`` silently
  searches ``default_dataset`` only — not everything you can read — so every
  call here passes one.
* **payloads are capped.** Cognee's ``recall`` returns *more* as the graph
  grows. Left uncapped, the memory layer would make the cost line *rise* over
  the day, which would be the exact opposite of the thing being demonstrated
  (DESIGN §4). :data:`TUNABLES.max_recall_chars` is the ceiling.
* **every skill run is logged back.** ``SkillRunEntry`` carries
  ``selected_skill_id``, ``task_text``, ``result_summary``, ``success_score``,
  ``latency_ms`` and ``tool_trace`` — the Rote play log, with no modelling work
  on our side. Writing it is what makes Rote's output become Cognee's memory and
  route the *next* ``find_play``. That edge is what makes the system compound
  rather than merely persist.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterable, Mapping, Sequence

from agent.config import TUNABLES, settings
from memory.cognee_client import CogneeCloud
from memory.graph_model import graph_model


class Remember:
    """One session's worth of Cognee traffic."""

    def __init__(self, client: CogneeCloud | None = None, dataset: str | None = None,
                 session_id: str | None = None):
        config = settings()
        self.client = client or CogneeCloud(dataset=dataset or config.cognee_dataset)
        self.dataset = dataset or self.client.dataset
        self.session_id = session_id or f"sess-{uuid.uuid4().hex[:12]}"
        self._registered_skills: set[str] = set()

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Remember":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- ingest ----------------------------------------------------------

    def add_documents(self, texts: Sequence[str], node_set: Sequence[str] | None = None,
                      cognify: bool = True) -> dict[str, Any]:
        """Stage text and build the graph against our typed model.

        ``cognify`` blocks by default (``runInBackground: false``). A
        one-sentence dataset took ~17s against the live tenant; a resume takes
        longer, which is why the corpus ingest does it once at hour 0 rather
        than per run.
        """
        added = self.client.add_text(list(texts), dataset=self.dataset,
                                     node_set=list(node_set) if node_set else None)
        result: dict[str, Any] = {"added": len(texts), "response": added}
        if cognify:
            result["cognify"] = self.client.cognify(dataset=self.dataset, wait=True,
                                                    graph_model=graph_model())
        return result

    def add_job_description(self, job_id: str, text: str, cognify: bool = False) -> dict[str, Any]:
        """JD text into Cognee, tagged so it can be pulled back per job.

        Untrusted input: it is stored and extracted from, and it never reaches
        the planning agent unfiltered (DESIGN §4).
        """
        return self.add_documents([f"[job:{job_id}]\n{text}"],
                                  node_set=["job_description", job_id], cognify=cognify)

    # -- recall ----------------------------------------------------------

    def recall(self, query: str, cap: int | None = None) -> dict[str, Any]:
        """Preference context for the prediction step, capped.

        Returns ``{"text": ..., "truncated": bool}`` rather than the raw
        response so no caller can accidentally splice an unbounded payload into
        a prompt.
        """
        limit = cap or TUNABLES.max_recall_chars
        try:
            raw = self.client.recall(query, dataset=self.dataset)
        except Exception as exc:  # recall is not worth failing a run over
            return {"text": "", "truncated": False, "error": str(exc)[:200]}
        text = _flatten_text(raw)
        if text.strip() in ("memory_warming_up", ""):
            # Answered before the first cognify. Not an error, and not a reason
            # to block a run: the deterministic scorer works without it.
            return {"text": "", "truncated": False, "warming_up": True}
        return {"text": text[:limit], "truncated": len(text) > limit}

    def search(self, query: str, search_type: str = "GRAPH_COMPLETION",
               cap: int | None = None) -> dict[str, Any]:
        raw = self.client.search(query, dataset=self.dataset, search_type=search_type)
        text = _flatten_text(raw)
        limit = cap or TUNABLES.max_recall_chars
        return {"text": text[:limit], "truncated": len(text) > limit}

    def find_skills(self, query: str) -> list[dict[str, Any]]:
        """Where ``find_play`` looks second, after the Rote registry."""
        try:
            raw = self.client.search(query, dataset=self.dataset, search_type="SKILLS")
        except Exception:
            return []
        return raw if isinstance(raw, list) else [{"result": raw}]

    def register_skill(self, name: str, body: str) -> dict[str, Any]:
        return self.client.ingest_skill(body, name, dataset=self.dataset)

    # -- typed memory entries --------------------------------------------

    def qa(self, question: str, answer: str, **fields: Any) -> dict[str, Any]:
        """One question and its answer. The response carries an ``entry_id``,
        which is the ``qa_id`` a later feedback entry has to reference."""
        return self.client.remember_entry(
            {"type": "qa", "question": question, "answer": answer, **fields},
            dataset=self.dataset, session_id=self.session_id)

    def feedback(self, text: str, qa_id: str | None = None,
                 question: str = "", answer: str = "", **fields: Any) -> dict[str, Any]:
        """Free-text human feedback. Cognee turns it into structured tags.

        The tenant requires ``qa_id``: feedback is *about* an exchange, not a
        standalone note. When the caller has no qa entry to hang it on — which
        is the common case, because a Not-for-me is a click and a sentence — one
        is written first from ``question``/``answer`` and the feedback attached
        to it. That keeps the caller's API to "here is what they said".
        """
        if qa_id is None:
            parent = self.qa(question or "How was this role?", answer or text)
            qa_id = parent.get("entry_id")
        return self.client.remember_entry(
            {"type": "feedback", "qa_id": qa_id, "content": text, **fields},
            dataset=self.dataset, session_id=self.session_id)

    def trace(self, summary: str, origin_function: str = "agent", **fields: Any) -> dict[str, Any]:
        """A step the agent took. ``origin_function`` is required by the tenant."""
        return self.client.remember_entry(
            {"type": "trace", "content": summary, "origin_function": origin_function, **fields},
            dataset=self.dataset, session_id=self.session_id)

    def register_skill_once(self, name: str, body: str = "") -> str:
        """Register a skill unless we already have this session.

        ``skill_run`` rejects a ``selected_skill_id`` that is not a registered
        skill *name*, with a bare 400. Registering is idempotent on the tenant
        and cheap, so the play log never fails for a reason the caller could not
        have seen coming.
        """
        if name not in self._registered_skills:
            try:
                self.client.ingest_skill(body or f"# {name}\nRote play: {name}.",
                                         name, dataset=self.dataset)
            except Exception:
                pass                    # already registered, or the route is busy
            self._registered_skills.add(name)
        return name

    def skill_run(self, skill_id: str, task_text: str, result_summary: str,
                  success_score: float, latency_ms: int,
                  tool_trace: Sequence[Mapping[str, Any]] = (),
                  run_id: str | None = None, error_type: str | None = None) -> dict[str, Any]:
        """The Rote play log. Rote's output becoming Cognee's memory is the edge
        that makes the system compound rather than merely persist.

        Verified against the live tenant: this writes ``SkillRun`` and
        ``CandidateSkill`` nodes beside the registered ``Skill``, which is what
        a later ``searchType: SKILLS`` lookup routes ``find_play`` through.
        """
        entry: dict[str, Any] = {
            "type": "skill_run",
            "selected_skill_id": self.register_skill_once(skill_id),
            "task_text": task_text[:1000],
            "result_summary": result_summary[:TUNABLES.max_summary_chars],
            "success_score": round(float(success_score), 3),
            "latency_ms": int(latency_ms),
            "tool_trace": [dict(step) for step in tool_trace][:50],
        }
        if run_id:
            entry["run_id"] = run_id
        if error_type:
            entry["error_type"] = error_type
        return self.client.remember_entry(entry, dataset=self.dataset)

    # -- export ----------------------------------------------------------

    def graph(self) -> dict[str, Any]:
        """``{nodes, edges}`` with our typed labels. The bridge to HydraDB."""
        return self.client.graph()

    def quota(self) -> dict[str, Any]:
        return self.client.quota()


def _flatten_text(raw: Any) -> str:
    """Cognee returns a list of hits, a dict, or a string depending on route."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping):
        for key in ("answer", "result", "text", "content", "status"):
            if key in raw:
                return _flatten_text(raw[key])
        return json.dumps(raw, default=str)
    if isinstance(raw, Iterable):
        return "\n".join(_flatten_text(item) for item in raw)
    return str(raw)
