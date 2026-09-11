"""One place for environment, tunables and state paths.

Everything that reads a credential or a threshold reads it from here, so a
demo-day change is one edit rather than a grep. Two rules carried over from
``memory/cognee_client.py`` and worth keeping consistent across the repo:

* ``.env`` is read without a dependency on python-dotenv, and **the shell wins**
  (``os.environ.setdefault``). An exported variable overrides the file.
* Nothing here raises on a missing key. A tool that needs one raises when it is
  used, so importing the package never fails on a half-filled ``.env``.

Thresholds live on the ``Candidate`` node in the graph in the real product
(DESIGN §8). ``TUNABLES`` below is the default they are seeded from, and the
demo lowers them through the same config values rather than a demo-only branch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_DIR = DATA_DIR / "state"
RAW_DIR = DATA_DIR / "raw"


def load_env() -> None:
    """Populate os.environ from the project .env. Shell variables win."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def env(name: str, default: str = "") -> str:
    load_env()
    return os.environ.get(name, default)


def require(name: str) -> str:
    value = env(name)
    if not value or value.startswith("<"):
        raise RuntimeError(f"{name} is not set in .env (see .env.example)")
    return value


@dataclass(frozen=True)
class Tunables:
    """Scoring weights and the readiness thresholds from DESIGN §7 and §8."""

    # predict_fit is a deterministic weighted score, never an LLM call: an LLM
    # prediction would take a recall context that grows with the graph, so the
    # token line would rise exactly as memory improved (DESIGN §7).
    w_coverage: float = 0.45
    w_similarity: float = 0.30
    w_preference: float = 0.25
    keep_threshold: float = 0.55

    # Two of every five shown roles are drawn from outside the top ranking, to
    # hold slate difficulty fixed so prediction_accuracy cannot drift up just
    # because the slate got uniformly good (DESIGN §7).
    slate_size: int = 10
    slate_off_ranking: int = 4
    narrow_top_k: int = 30

    # Preference induction (DESIGN §6.3).
    pref_evidence_threshold: int = 3
    pref_signal_window: int = 20

    # Autonomy (DESIGN §8). Lowered for the demo through these values, not by a
    # hardcoded branch; D1 is measured on the skip class only.
    d1_accuracy_threshold: float = 0.85
    d1_window: int = 15
    d2_confirm_threshold: int = 4
    d2_window: int = 5
    autonomy_renag_after: int = 10
    autonomy_downgrade_overrides: int = 2
    autonomy_downgrade_window: int = 10

    # Tool returns are ids and one-line summaries; payload caps stop the memory
    # layer making the cost line rise as the graph grows (DESIGN §4).
    max_summary_chars: int = 240
    max_recall_chars: int = 2000
    max_rows_returned: int = 50


TUNABLES = Tunables()


@dataclass(frozen=True)
class Settings:
    candidate_id: str = field(default_factory=lambda: env("CANDIDATE_ID", "cand-001"))

    cognee_base_url: str = field(default_factory=lambda: env("COGNEE_BASE_URL"))
    cognee_api_key: str = field(default_factory=lambda: env("COGNEE_API_KEY"))
    cognee_dataset: str = field(default_factory=lambda: env("COGNEE_DATASET", "candidate"))

    hydradb_api_key: str = field(default_factory=lambda: env("HYDRADB_APIKEY"))
    hydradb_database: str = field(default_factory=lambda: env("HYDRADB_DATABASE", "default-tenant"))
    hydradb_collection: str = field(default_factory=lambda: env("HYDRADB_COLLECTION", "default"))

    # Set GRAPH_BOLT_URL to run the named queries as real Cypher against the
    # HydraDB OSS engine (or any Bolt-speaking graph) instead of evaluating
    # them in process. See memory/graph.py for why both paths exist.
    graph_bolt_url: str = field(default_factory=lambda: env("GRAPH_BOLT_URL"))
    graph_bolt_user: str = field(default_factory=lambda: env("GRAPH_BOLT_USER", "neo4j"))
    graph_bolt_password: str = field(default_factory=lambda: env("GRAPH_BOLT_PASSWORD"))

    hotdata_api_key: str = field(default_factory=lambda: env("HOTDATA_API_KEY"))
    hotdata_workspace: str = field(default_factory=lambda: env("HOTDATA_WORKSPACE_ID"))
    hotdata_database: str = field(default_factory=lambda: env("HOTDATA_DATABASE_ID"))
    hotdata_catalog: str = field(default_factory=lambda: env("HOTDATA_CATALOG", "jobs"))
    # The canonical table the named SQL reads. Point it somewhere else to run
    # against a scratch copy without touching the team's corpus table.
    hotdata_jobs_table: str = field(
        default_factory=lambda: env("HOTDATA_JOBS_TABLE", "jobs_canonical"))

    rocketride_uri: str = field(default_factory=lambda: env("ROCKETRIDE_URI"))
    rocketride_api_key: str = field(default_factory=lambda: env("ROCKETRIDE_APIKEY"))

    qwen_api_key: str = field(default_factory=lambda: env("QWEN_API_KEY"))
    qwen_base_url: str = field(default_factory=lambda: env("QWEN_BASE_URL"))
    qwen_model: str = field(default_factory=lambda: env("QWEN_MODEL", "qwen3.8-max"))

    mcp_bearer: str = field(default_factory=lambda: env("MCP_BEARER_TOKEN"))
    mcp_host: str = field(default_factory=lambda: env("MCP_HOST", "127.0.0.1"))
    mcp_port: int = field(default_factory=lambda: int(env("MCP_PORT", "8787")))


def settings() -> Settings:
    load_env()
    return Settings()


def state_path(name: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / name
