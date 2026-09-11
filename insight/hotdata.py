"""hotdata.dev client: SQL, search, and table loading.

The CLI is the transport. It is what the setup script, the verify script and
the team's shells already use, it handles auth from ``HOTDATA_API_KEY``
directly, and it means one fewer HTTP surface to keep in sync with a service
that is still moving. Every call is ``subprocess.run`` with an **argument
list** — never a shell string — so nothing here can be talked into running a
second command.

Four things the service does that will bite anyone who assumes otherwise, each
verified against the live workspace on 2026-09-11:

* ``-o json`` on a query returns ``{"columns": [...], "rows": [[...]]}``, not a
  list of records. ``rows()`` below zips them back into dicts.
* Authenticating by key stores no default workspace, so **every** command needs
  ``-w``. A missing ``-w`` fails with a workspace error that reads like an auth
  failure.
* ``databases load`` and ``search create`` take no ``-o``; ``search list`` and
  ``search create`` take no ``-d``. Only the query forms are database-scoped.
* **A search index parses query syntax.** A bare ``-``, ``+``, ``:``, ``"``,
  ``(`` or ``^`` anywhere in the text returns *500 internal server error*, not a
  parse error — so pasting a résumé straight in fails in a way that reads like
  an outage. :func:`clean_query` strips them, and every search goes through it.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence

from agent.config import Settings, settings
from agent.metrics import note_boundary_call


class HotdataError(RuntimeError):
    pass


# A catalog or table name that is going to be interpolated into SQL text. The
# query API takes a SQL string, not bound identifiers, so a name that reaches a
# query is checked against this first and rejected if it is anything but a plain
# identifier. Table names normally come from config; ``--project`` and
# ``--source-table`` let an operator name one on the command line.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def identifier(name: str, what: str = "identifier") -> str:
    """Return ``name`` unchanged if it is a plain SQL identifier, else raise."""
    if not _IDENTIFIER.fullmatch(name or ""):
        raise HotdataError(f"{name!r} is not a plain {what}")
    return name


def qualify(table: str, catalog: str, schema: str = "public") -> str:
    """``catalog.public.table`` from a bare name, or a checked dotted name."""
    parts = (table or "").split(".")
    if len(parts) == 1:
        parts = [catalog, schema, parts[0]]
    if len(parts) != 3:
        raise HotdataError(f"{table!r} is not a table name: expected `table` "
                           "or `catalog.schema.table`")
    return ".".join(identifier(part, "part of a table name") for part in parts)


# Everything the index treats as an operator. Stripped rather than escaped:
# BM25 over a job description wants terms, and a résumé has no query intent.
_QUERY_SYNTAX = re.compile(r"[-+:^~*?\\/\"\'()\[\]{}<>|&!#=,;]")
_WHITESPACE = re.compile(r"\s+")


def clean_query(text: str, max_terms: int = 40, max_chars: int = 400) -> str:
    """Reduce free text to plain search terms, deduplicated and bounded.

    Bounded because a hundred-term BM25 query is both slow and meaningless: past
    a point every extra term pulls the ranking back toward the corpus average.
    Order is preserved, so the most distinctive words — which is where a profile
    puts them — survive the cut.
    """
    words = _WHITESPACE.sub(" ", _QUERY_SYNTAX.sub(" ", text or "")).split()
    seen, terms = set(), []
    for word in words:
        key = word.lower()
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        terms.append(word)
        if len(terms) >= max_terms:
            break
    return " ".join(terms)[:max_chars].strip()


class Hotdata:
    def __init__(self, config: Settings | None = None, timeout: float = 180.0):
        self.config = config or settings()
        self.timeout = timeout
        if not self.config.hotdata_api_key:
            raise HotdataError("HOTDATA_API_KEY is not set (see .env.example)")
        self.catalog = self.config.hotdata_catalog or "jobs"

    # -- transport -------------------------------------------------------

    def _run(self, args: Sequence[str], parse_json: bool = True) -> Any:
        command = ["hotdata", *args, "--no-input", "--api-key", self.config.hotdata_api_key]
        if "-w" not in args and "--workspace-id" not in args and self.config.hotdata_workspace:
            command += ["-w", self.config.hotdata_workspace]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout)
        except FileNotFoundError as exc:
            raise HotdataError("hotdata CLI not found (brew install hotdata-dev/tap/cli)") from exc
        except subprocess.TimeoutExpired as exc:
            raise HotdataError(f"hotdata timed out after {self.timeout}s: {' '.join(args[:2])}") from exc
        if done.returncode != 0:
            raise HotdataError(
                f"hotdata {' '.join(args[:2])} failed ({done.returncode}): "
                f"{(done.stderr or done.stdout).strip()[:400]}"
            )
        # Every call out of the process, counted for whichever run is open.
        # This is the transport, so nothing can reach hotdata without passing
        # here — which is the only way the cost line is a count rather than an
        # estimate (agent/metrics.py:note_boundary_call).
        note_boundary_call(len(done.stdout or ""))
        if not parse_json:
            return done.stdout
        try:
            return json.loads(done.stdout)
        except json.JSONDecodeError as exc:
            raise HotdataError(f"hotdata returned non-JSON: {done.stdout[:200]}") from exc

    # -- query -----------------------------------------------------------

    def query(self, sql: str) -> list[dict[str, Any]]:
        """Run SQL and return records. Callers go through insight.queries."""
        args = ["query", sql, "-o", "json"]
        if self.config.hotdata_database:
            args += ["-d", self.config.hotdata_database]
        payload = self._run(args)
        return self.rows(payload)

    @staticmethod
    def rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        columns = payload.get("columns") or []
        return [dict(zip(columns, row)) for row in payload.get("rows") or []]

    def scalar(self, sql: str, default: Any = None) -> Any:
        records = self.query(sql)
        if not records:
            return default
        return next(iter(records[0].values()), default)

    # -- search ----------------------------------------------------------

    def search(self, text: str, index: str, limit: int = 10,
               select: str | None = None) -> list[dict[str, Any]]:
        """Vector or BM25 search over an index created by ``ensure_indexes``.

        ``--select`` leaves the generated embedding column out by default, which
        is what we want: a returned 1536-float vector would blow the token
        budget for every downstream consumer.
        """
        query = clean_query(text)
        if not query:
            return []
        args = ["search", query, "--index", index, "--limit", str(limit), "-o", "json"]
        if self.config.hotdata_database:
            args += ["-d", self.config.hotdata_database]
        if select:
            args += ["--select", select]
        payload = self._run(args)
        if isinstance(payload, dict):
            return self.rows(payload)
        return payload

    def search_indexes(self, table: str | None = None) -> list[dict[str, Any]]:
        # `search list` takes no -d: indexes are listed per workspace and
        # filtered by table. Only the query form is database-scoped.
        args = ["search", "list", "-o", "json"]
        if table:
            args += ["--table", table]
        payload = self._run(args)
        if isinstance(payload, list):
            return payload
        return payload.get("indexes") or payload.get("rows") or []

    def create_index(self, name: str, table: str, column: str, kind: str = "text",
                     provider: str | None = None) -> Any:
        # `search create` reports in prose and takes no -o.
        args = ["search", "create", name, "--type", kind, "--from", table,
                "--column", column]
        if provider:
            args += ["--provider", provider]
        return {"output": self._run(args, parse_json=False).strip()[:300]}

    # -- loading ---------------------------------------------------------

    def load_file(self, path: str | Path, table: str, mode: str = "replace",
                  key: str | None = None, fmt: str | None = None) -> Any:
        """Load a local csv/json/parquet file into ``catalog.public.<table>``.

        ``databases load`` is the one command with no ``-o json``: it reports in
        prose. The stdout is returned as-is rather than parsed, because the only
        thing a caller needs from it is that it did not raise.
        """
        path = Path(path)
        args = ["databases", "load", "--catalog", self.catalog, "--table", table,
                "--file", str(path), "--mode", mode]
        if fmt:
            args += ["--format", fmt]
        if key:
            args += ["--key", key]
        return {"output": self._run(args, parse_json=False).strip()[:300]}

    def tables(self) -> list[dict[str, Any]]:
        args = ["databases", "tables", "list", "-o", "json"]
        if self.config.hotdata_database:
            args += ["--database", self.config.hotdata_database]
        payload = self._run(args)
        if isinstance(payload, list):
            return payload
        return payload.get("tables") or payload.get("rows") or []

    def table_columns(self, table: str) -> list[str]:
        args = ["databases", "tables", "show", table, "-o", "json"]
        if self.config.hotdata_database:
            args += ["--database", self.config.hotdata_database]
        try:
            payload = self._run(args)
        except HotdataError:
            return []
        columns = payload.get("columns") if isinstance(payload, dict) else payload
        return [c["name"] if isinstance(c, dict) else str(c) for c in columns or []]

    def declare_table(self, table: str, key: str | None = None) -> Any:
        args = ["databases", "tables", "add", table, "-o", "json"]
        if self.config.hotdata_database:
            args += ["--database", self.config.hotdata_database]
        if key:
            args += ["--key", key]
        return self._run(args)

    def drop_table(self, table: str) -> Any:
        """Delete a table. Needed because ``--mode replace`` replaces the *rows*
        and keeps the column types: a column that was inferred wrong on the
        first load can only be fixed by dropping and recreating."""
        args = ["databases", "tables", "remove", table]
        if self.config.hotdata_database:
            args += ["--database", self.config.hotdata_database]
        return {"output": self._run(args, parse_json=False).strip()[:200]}

    def qualified(self, table: str) -> str:
        return qualify(table, self.catalog)
