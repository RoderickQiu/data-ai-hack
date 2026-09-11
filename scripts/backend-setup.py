#!/usr/bin/env python3
"""Bring the backend up: tables, candidate memory, the graph bridge.

Idempotent. Run it as often as you like.

    python scripts/backend-setup.py --status              # check, change nothing
    python scripts/backend-setup.py --tables              # applications + runs
    python scripts/backend-setup.py --resume me.md --prefs prefs.md
    python scripts/backend-setup.py --sync                # Cognee -> the graph

It deliberately does **not** touch ``jobs``. The corpus is loaded once, at hour
0, by ``agent.corpus.build_corpus`` from whatever the fetchers hand it — see
``--corpus`` below for the offline re-run against the stored raw payloads.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.clock import DayClock                      # noqa: E402
from agent.config import settings                     # noqa: E402
from agent.corpus import build_corpus, load_raw       # noqa: E402
from insight.hotdata import HotdataError              # noqa: E402
from insight.store import Insight                     # noqa: E402
from memory.claims import claims_from_resume, pending_verification, store_claims  # noqa: E402
from memory.graph import GraphStore                   # noqa: E402


def _tick(ok: bool, text: str) -> None:
    print(("  \033[32mok\033[0m   " if ok else "  \033[31mfail\033[0m ") + text)


def ensure_tables(insight: Insight, recreate: bool = False) -> None:
    """Create ``applications`` and ``runs`` by loading one seed row into each.

    hotdata declares a table on first load, so a zero-row table is not a thing
    that exists. The seed rows are marked ``run_id='bootstrap'`` and are
    harmless: every query that matters filters on an event or a real run.

    The seeds carry a typed value in every column (see ``RUNS_SEED``). A column
    seeded as null becomes varchar, and the first real run then fails to load
    with "can't change type from varchar to int64" — which only shows up hours
    later, when there is finally a number to write.

    Pass ``recreate=True`` to rebuild a table whose columns got the wrong types.
    """
    from agent.schema import (APPLICATIONS_COLUMNS, APPLICATIONS_SEED,
                              RUNS_COLUMNS, RUNS_SEED)

    # `tables list` reports the name under "table"; older builds used "name".
    existing = {t.get("table") or t.get("name") for t in insight.client.tables()}
    for table, columns, seed in (("applications", APPLICATIONS_COLUMNS, APPLICATIONS_SEED),
                                 ("runs", RUNS_COLUMNS, RUNS_SEED)):
        if table in existing and not recreate:
            _tick(True, f"{table} table already present")
            continue
        if table in existing:
            # `replace` replaces rows, not column types. A column that was
            # inferred as varchar stays varchar until the table is dropped.
            insight.client.drop_table(table)
        insight._load_rows(table, [seed], columns, mode="replace",
                           key=columns[0])
        _tick(True, f"{'recreated' if table in existing else 'created'} "
                    f"{insight.client.catalog}.public.{table}")


def ingest_candidate(store: GraphStore, resume: Path | None, prefs: Path | None) -> None:
    """Resume bullets enter verified; preference prose enters through Cognee."""
    from memory.remember import Remember

    documents = []
    if resume and resume.exists():
        text = resume.read_text()
        claims = claims_from_resume(text, source_doc=resume.name)
        counts = store_claims(store, claims)
        store.flush()
        _tick(True, f"{counts.get('verified', 0)} verified claim(s) from {resume.name}")
        documents.append(text)
    if prefs and prefs.exists():
        documents.append(prefs.read_text())
        _tick(True, f"staged {prefs.name} for extraction")

    if not documents:
        return
    try:
        with Remember() as remember:
            result = remember.add_documents(documents)
        _tick(True, f"cognified {result['added']} document(s) against the typed model")
    except Exception as exc:
        _tick(False, f"Cognee ingest failed: {str(exc)[:160]}")


def sync(store: GraphStore) -> None:
    from memory.sync import link_candidate_skills, sync_from_cognee

    try:
        result = sync_from_cognee(store)
        linked = link_candidate_skills(store)
        _tick(True, f"merged {result['nodes_merged']} node(s), {result['edges_merged']} "
                    f"edge(s); linked {linked} candidate skill(s)")
    except Exception as exc:
        _tick(False, f"sync failed: {str(exc)[:160]}")


def status(insight: Insight | None, store: GraphStore) -> None:
    config = settings()
    print(f"\ncandidate: {config.candidate_id}   day: {DayClock.load().day}")
    print(f"graph backend: {'bolt' if config.graph_bolt_url else 'local + HydraDB mirror'}")
    print("\ngraph")
    stats = store.graph.stats()
    print(f"  {stats['nodes']} nodes, {stats['edges']} edges")
    for label, count in sorted(stats["labels"].items(), key=lambda kv: -kv[1])[:10]:
        print(f"    {label:<16} {count}")
    unverified = pending_verification(store)
    if unverified:
        print(f"  {len(unverified)} claim(s) awaiting verification")

    print("\nhotdata")
    if insight is None:
        _tick(False, "not configured")
        return
    for query, label in (("corpus_stats", "corpus"), ("runs_series", "runs")):
        try:
            rows = insight.run(query, {"limit": 1} if query == "runs_series" else None)
            print(f"  {label}: {json.dumps(rows[0] if rows else {}, default=str)[:160]}")
        except HotdataError as exc:
            _tick(False, f"{label}: {str(exc)[:120]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tables", action="store_true", help="create applications and runs")
    parser.add_argument("--recreate-tables", action="store_true",
                        help="rebuild applications and runs (drops the rows in them)")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--prefs", type=Path)
    parser.add_argument("--sync", action="store_true", help="Cognee graph -> candidate graph")
    parser.add_argument("--corpus", action="store_true",
                        help="rebuild the corpus from data/raw (offline; replaces jobs)")
    parser.add_argument("--project", metavar="TABLE",
                        help="project a fetcher's table into the canonical jobs table")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --project: report the mapping, write nothing")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    store = GraphStore()
    try:
        insight: Insight | None = Insight()
    except Exception as exc:
        print(f"hotdata unavailable: {str(exc)[:160]}")
        insight = None

    did_something = False
    if (args.tables or args.recreate_tables) and insight:
        print("\ntables")
        ensure_tables(insight, recreate=args.recreate_tables)
        did_something = True
    if args.resume or args.prefs:
        print("\ncandidate memory"); ingest_candidate(store, args.resume, args.prefs)
        did_something = True
    if args.sync:
        print("\nsync"); sync(store); did_something = True
    if args.corpus and insight:
        print("\ncorpus (offline, from data/raw)")
        batches = [{"source": source, "slug": slug, "company": slug, "items": items}
                   for source, slug, items in load_raw()]
        if not batches:
            _tick(False, "no raw payloads under data/raw — the fetchers have not run yet")
        else:
            report = build_corpus(batches, insight, keep_raw=False)
            _tick(report.ok(), report.summary())
        did_something = True

    if args.project and insight:
        from insight.project import project
        print(f"\nprojection {args.project} -> {insight.client.catalog}.public.{insight.jobs_table}")
        report = project(args.project, insight, load=not args.dry_run)
        _tick(not report.unmapped, report.summary())
        if args.dry_run:
            print("  dry run — nothing written. Mapping:")
            for canonical, column in sorted(report.mapped.items()):
                print(f"    {canonical:<14} <- {column}")
        did_something = True

    if args.status or not did_something:
        status(insight, store)


if __name__ == "__main__":
    main()
