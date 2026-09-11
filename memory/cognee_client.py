"""Thin HTTP client for our Cognee Cloud tenant.

Cognee is a managed service here, not a local library: nothing in this repo
imports the `cognee` package. Everything goes over REST against
``COGNEE_BASE_URL`` with an ``X-Api-Key`` header.

Facts verified against the live tenant on 2026-09-11, each of which will cost
someone an hour if rediscovered the hard way:

* Auth is ``X-Api-Key`` only. ``Authorization: Bearer <same key>`` is a 401 on
  every route, which is also why the ``cognee-sdk`` package on PyPI is unusable.
* ``cognify`` returns immediately with a ``pipeline_run_id`` unless you pass
  ``runInBackground=False``. Blocking took ~17s for a one-sentence dataset.
* ``recall`` with no ``datasets`` silently searches ``default_dataset`` only,
  not everything you can read. Always name the dataset.
* Dataset *names* only resolve to datasets you own. For a dataset shared with
  you, pass ``datasetIds``.
* Before the first ``cognify``, recall answers with
  ``{"status": "memory_warming_up"}`` rather than an error.

Run it directly for a live round trip:  python memory/cognee_client.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

import httpx

DEFAULT_TIMEOUT = 300.0


def _load_env() -> None:
    """Read the project .env without taking a dependency on python-dotenv.

    Shell variables win, matching normal 12-factor precedence. (The old local
    cognee package inverted this and let .env override the shell, which is the
    opposite of what everyone expects.)
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


class CogneeCloud:
    """Every call is a real HTTP request; there is no local state to prune."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        dataset: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        _load_env()
        self.base_url = (base_url or os.environ.get("COGNEE_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("COGNEE_API_KEY", "")
        self.dataset = dataset or os.environ.get("COGNEE_DATASET") or "main_dataset"
        if not self.base_url or not self.api_key:
            raise RuntimeError("COGNEE_BASE_URL and COGNEE_API_KEY must be set (see .env.example)")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"X-Api-Key": self.api_key},
            # The cloud serves collection routes with a trailing slash and
            # answers the other form with a 307.
            follow_redirects=True,
        )

    def __enter__(self) -> "CogneeCloud":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    # -- ingest ----------------------------------------------------------

    def add_text(self, texts: str | Iterable[str], dataset: str | None = None,
                 node_set: list[str] | None = None) -> dict:
        """Stage raw text. Creates the dataset if it does not exist yet."""
        if isinstance(texts, str):
            texts = [texts]
        body: dict[str, Any] = {
            "textData": list(texts),
            "datasetName": dataset or self.dataset,
        }
        if node_set:
            body["nodeSet"] = node_set
        return self._call("POST", "/api/v1/add_text", json=body)

    def add_file(self, path: str | Path, dataset: str | None = None) -> dict:
        """Upload a real file (resume PDF, job description, ...)."""
        path = Path(path)
        with path.open("rb") as handle:
            return self._call(
                "POST",
                "/api/v1/add",
                files={"data": (path.name, handle)},
                data={"datasetName": dataset or self.dataset},
            )

    def cognify(self, dataset: str | None = None, wait: bool = True,
                graph_model: dict | None = None) -> dict:
        """Build the knowledge graph.

        ``graph_model`` is a JSON schema describing our typed entities
        (Candidate, Skill, Job, Company, Requirement) — the cloud equivalent of
        passing a custom pydantic graph model to the local library.
        """
        body: dict[str, Any] = {
            "datasets": [dataset or self.dataset],
            "runInBackground": not wait,
        }
        if graph_model:
            body["graphModel"] = graph_model
        return self._call("POST", "/api/v1/cognify", json=body)

    # -- retrieve --------------------------------------------------------

    def search(self, query: str, dataset: str | None = None,
               search_type: str = "GRAPH_COMPLETION") -> list:
        return self._call("POST", "/api/v1/search", json={
            "query": query,
            "searchType": search_type,
            "datasets": [dataset or self.dataset],
        })

    def recall(self, query: str, dataset: str | None = None,
               search_type: str | None = "HYBRID_COMPLETION") -> list:
        """Agent-facing retrieval. Pass search_type=None to let cognee route."""
        return self._call("POST", "/api/v1/recall", json={
            "query": query,
            "searchType": search_type,
            "datasets": [dataset or self.dataset],
        })

    # -- memory entries --------------------------------------------------

    def remember_entry(self, entry: dict, dataset: str | None = None,
                       session_id: str | None = None) -> dict:
        """Write one typed memory entry.

        ``entry`` is discriminated on its ``type`` field: ``qa``, ``trace``,
        ``feedback`` or ``skill_run``. ``session_id`` is required for the first
        three and optional for ``skill_run``.
        """
        body: dict[str, Any] = {"entry": entry, "dataset_name": dataset or self.dataset}
        if session_id:
            body["session_id"] = session_id
        return self._call("POST", "/api/v1/remember/entry", json=body)

    def remember_skill_run(self, selected_skill_id: str, **fields: Any) -> dict:
        """Log a Rote play run. Optional fields: run_id, task_text,
        result_summary, success_score, latency_ms, tool_trace, error_type."""
        return self.remember_entry(
            {"type": "skill_run", "selected_skill_id": selected_skill_id, **fields}
        )

    def ingest_skill(self, skills_text: str, skill_name: str,
                     dataset: str | None = None) -> dict:
        """Register a skill (a SKILL.md body) as a Skill node in the graph."""
        return self._call("POST", "/api/v1/skills/", json={
            "skills_text": skills_text,
            "skill_name": skill_name,
            "dataset_name": dataset or self.dataset,
        })

    # -- datasets and export ---------------------------------------------

    def datasets(self) -> list:
        return self._call("GET", "/api/v1/datasets/")

    def dataset_id(self, name: str | None = None) -> str | None:
        name = name or self.dataset
        return next((d["id"] for d in self.datasets() if d["name"] == name), None)

    def graph(self, dataset_id: str | None = None, full: bool = True) -> dict:
        """Export the graph as ``{"nodes": [...], "edges": [...]}``.

        This is the HydraDB bridge: no local export step, no file on disk.
        """
        dataset_id = dataset_id or self.dataset_id()
        if dataset_id is None:
            raise RuntimeError(f"no dataset named {self.dataset!r}")
        return self._call("GET", f"/api/v1/datasets/{dataset_id}/graph",
                          params={"full": str(full).lower()})

    def delete_dataset(self, dataset_id: str) -> Any:
        return self._call("DELETE", f"/api/v1/datasets/{dataset_id}")

    def quota(self) -> dict:
        return self._call("GET", "/api/v1/quotas/usage")


if __name__ == "__main__":
    with CogneeCloud(dataset="smoke") as cognee:
        print("quota:", cognee.quota())
        cognee.add_text("Ada Lovelace wrote the first computer program in 1843.")
        cognee.cognify(wait=True)
        print("search:", cognee.search("Who wrote the first computer program?"))
        graph = cognee.graph()
        print(f"graph: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")
        cognee.delete_dataset(cognee.dataset_id())
        print("smoke dataset removed")
