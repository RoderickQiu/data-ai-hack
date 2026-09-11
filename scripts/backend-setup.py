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
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.clock import DayClock                      # noqa: E402
from agent.config import DATA_DIR, settings           # noqa: E402
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


# Probe rows written into the live `runs` table while working out the parquet
# load and the HydraDB caps. They are real rows the table really holds, so they
# are dropped here — in the one destructive pass a user has to approve anyway —
# rather than filtered out by the chart. A reader that hides rows a writer wrote
# is the quiet correction this project's chart rules exist to prevent.
#
# Named explicitly rather than matched by a rule: "delete every row that looks
# like junk" is not something to hand a migration over a live table.
DISCARD_RUN_IDS = frozenset({
    "run-c9b14489f3",   # day 97, probe row
    "run-9402281cf0",   # day 99, probe row
})


def discard_plan(rows: Sequence[Mapping[str, Any]],
                 horizon: int) -> tuple[list, list, list]:
    """Split the dumped rows into (keep, drop, suspicious). Prints nothing.

    A row whose ``day`` is past the clock horizon cannot have come from
    ``DayClock.advance``, so it comes back in ``suspicious`` — but it is never
    dropped on that basis alone. The horizon is a canary for junk nobody has
    noticed yet, not a deletion rule.

    Deliberately pure. An earlier version printed the warning itself, and the
    unit test covering the canary then emitted a line indistinguishable from a
    live one — which sent another session hunting a phantom day-98 row that only
    ever existed in a fixture. A function that reports by printing cannot be
    tested without lying to whoever reads the output.
    """
    keep, drop = [], []
    for row in rows:
        (drop if row.get("run_id") in DISCARD_RUN_IDS else keep).append(row)
    suspicious = [row for row in keep
                  if isinstance(row.get("day"), int) and row["day"] > horizon]
    return keep, drop, suspicious


def migrate_runs(insight: Insight, dry_run: bool = True) -> dict:
    """Add new columns to a live ``runs`` table without losing the run history.

    ``ensure_tables(recreate=True)`` drops the table, and the history *is* the
    chart — dropping it to gain a column costs the thing the column was for.
    hotdata infers columns on load and ``append`` never adds one, so the only
    route is dump → drop → recreate from the seed → re-append.

    The dump is written to disk *before* anything is dropped, and it contains
    every row including the ones this pass discards, so nothing here is
    unrecoverable. A migration that loses the curve is worse than a missing
    column.

    Duplicate ``bootstrap`` rows need no handling: the dump comes from
    ``runs_series``, which excludes them, and the recreate writes exactly one
    fresh seed row — so they are gone by construction rather than by a rule.
    """
    from agent.schema import RUNS_COLUMNS, RUNS_SEED

    rows = insight.run("runs_series", {"limit": 10000})
    present = set(rows[0]) if rows else set()
    missing = [column for column in RUNS_COLUMNS if column not in present]
    horizon = DayClock.load().horizon
    keep, drop, suspicious = discard_plan(rows, horizon)
    report = {"rows": len(rows), "missing_columns": missing, "migrated": False,
              "keep": len(keep), "drop": [r.get("run_id") for r in drop],
              "suspicious": [r.get("run_id") for r in suspicious]}
    for row in suspicious:
        _tick(False, f"day {row['day']} is past the clock horizon of {horizon} "
                     f"({row.get('run_id')}) — kept, but it will plot")

    # Both reasons to run, checked together. Checking only for missing columns
    # would strand the probe rows the moment someone else recreates the table:
    # the cleanup was folded in here precisely so it rides the one destructive
    # pass, and it cannot ride a pass that already returned.
    if not missing and not drop:
        _tick(True, f"runs has all {len(RUNS_COLUMNS)} columns and nothing to discard")
        return report

    backup = DATA_DIR / f"runs-backup-{int(time.time())}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(rows, indent=2, default=str))
    report["backup"] = str(backup)
    _tick(True, f"wrote {backup} ({len(rows)} row(s), including any discarded below)")

    if missing:
        print(f"  adding {', '.join(missing)}")
    print(f"  keeping {len(keep)} run row(s)")
    for row in drop:
        print(f"  DROPPING {row.get('run_id')} (day {row.get('day')}) — probe row")

    if dry_run:
        print("  --dry-run: stopping before the write. Re-run without it to apply.")
        return report

    restored = [{column: row.get(column, RUNS_SEED[column])
                 for column in RUNS_COLUMNS} for row in keep]
    if missing:
        # Columns are the only thing that needs the table gone: hotdata infers
        # them on load and `append` never adds one.
        insight.client.drop_table("runs")
        insight._load_rows("runs", [RUNS_SEED], RUNS_COLUMNS, mode="replace",
                           key=RUNS_COLUMNS[0])
        if restored:
            insight._load_rows("runs", restored, RUNS_COLUMNS, mode="append",
                               key=RUNS_COLUMNS[0])
    else:
        # Discard-only: `replace` replaces rows, not column types, so the table
        # never has to be dropped at all. Strictly less destructive, and the
        # types that took a while to get right are not re-inferred.
        insight._load_rows("runs", [RUNS_SEED] + restored, RUNS_COLUMNS,
                           mode="replace", key=RUNS_COLUMNS[0])

    after = insight.run("runs_series", {"limit": 10000})
    report["migrated"] = True
    report["rows_after"] = len(after)
    _tick(len(after) == len(keep),
          f"runs now has {len(RUNS_COLUMNS)} columns and {len(after)} row(s); "
          f"kept {len(keep)}, discarded {len(drop)}")
    return report


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


def restore(store: GraphStore) -> None:
    """Pull the graph out of HydraDB and make it this machine's local graph.

    ``--mirror`` already pulls, but only to compare: it throws the result away.
    This is the half that was missing, and it is what a second laptop runs.
    Without it a new machine starts blank — ``data/state/`` is gitignored and
    ``GraphStore`` only ever loads the local file — so the resume ingest has to
    be repeated and accumulated clicks never travel.

    Merge rather than replace. HydraDB's ingest is asynchronous, so a pull can
    legitimately come back short (882 -> 592 -> 352 was measured on one push),
    and replacing would turn that into data loss on the machine that had the
    rows. Merging means a second run converges instead of destroying, so
    running this twice is always safe.
    """
    from memory.graph import GRAPH_FILE, HydraMirror, state_path

    try:
        hydra = HydraMirror(settings())
    except Exception as exc:
        _tick(False, f"HydraDB unavailable: {str(exc)[:160]}")
        return
    try:
        remote = hydra.pull()
    except Exception as exc:
        _tick(False, f"pull failed: {str(exc)[:200]}")
        return

    graph = store.graph
    before_nodes, before_edges = len(graph.nodes), len(graph.edges)
    for node in remote.nodes.values():
        graph.merge_node(node.id, node.label, **node.props)
    for edge in remote.edges.values():
        graph.merge_edge(edge.src, edge.type, edge.dst, **edge.props)

    # Written straight to the local file rather than through store.flush(),
    # which would mark every restored item dirty and push the whole graph back
    # to the database it just came from.
    path = state_path(GRAPH_FILE)
    path.write_text(json.dumps(graph.to_dict(), indent=1, default=str))

    _tick(True, f"pulled {len(remote.nodes)} node(s), {len(remote.edges)} edge(s); "
                f"local graph {before_nodes}/{before_edges} -> "
                f"{len(graph.nodes)}/{len(graph.edges)} node(s)/edge(s)")
    if not remote.nodes:
        print("  nothing in HydraDB yet — run the ingest, then `make mirror` "
              "on the machine that has the data")


def mirror(store: GraphStore) -> None:
    """Push the whole graph to HydraDB, then read it back and compare.

    A run only pushes what it dirtied, so anything written before the mirror
    worked is on this machine and nowhere else. This is also the only check that
    the round trip *restores* rather than merely accepts: HydraDB returns
    `description` empty, so the props ride in `metadata` (memory/graph.py), and
    a push that lands while the pull comes back blank is the failure this
    catches.
    """
    from memory.graph import HydraMirror

    try:
        hydra = HydraMirror(settings())
    except Exception as exc:
        _tick(False, f"HydraDB unavailable: {str(exc)[:160]}")
        return

    graph = store.graph
    dirty = ([("node", node_id) for node_id in graph.nodes]
             + [("edge", key) for key in graph.edges])
    try:
        pushed = hydra.push(graph, dirty)
    except Exception as exc:
        _tick(False, f"push failed: {str(exc)[:200]}")
        return
    _tick(True, f"pushed {pushed['pushed']} item(s) to "
                f"{pushed['database']}/{pushed['collection']}")

    # Ingest is asynchronous (202), so a pull straight after a push legitimately
    # comes back short. The count is reported, never asserted.
    try:
        restored = hydra.pull()
    except Exception as exc:
        _tick(False, f"pull failed: {str(exc)[:200]}")
        return
    _tick(len(restored.nodes) > 0,
          f"pulled back {len(restored.nodes)} node(s), {len(restored.edges)} edge(s) "
          f"(local: {len(graph.nodes)} / {len(graph.edges)})")

    # An edge deficit straight after a push is eventual consistency, not a bug:
    # successive pulls converge (882 -> 592 -> 352 -> 234 -> 128 was measured on
    # one push). Say so, or the next person spends an hour chasing it.
    if len(restored.edges) < len(graph.edges):
        print(f"  {len(graph.edges) - len(restored.edges)} edge(s) not back yet — "
              f"ingest is async (202); re-run to watch it converge")

    # A *propless* node is the failure that does not converge. Batching turned
    # an all-or-nothing push into a partial one, so a batch rejected for one bad
    # item leaves its nodes in HydraDB as id-and-label only — present, listed,
    # and useless to restore from. Named individually: "the mirror holds it" has
    # to mean the props too.
    blank = [node.id for node in restored.nodes.values()
             if not node.props and graph.nodes.get(node.id) and graph.nodes[node.id].props]
    _tick(not blank, f"{len(blank)} node(s) came back without the props they have locally"
                     + (f" (e.g. {blank[0]})" if blank else ""))


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
    parser.add_argument("--migrate-runs", action="store_true",
                        help="add new columns to runs without losing the history "
                             "(dumps to data/ first; pair with --dry-run to preview)")
    parser.add_argument("--recreate-tables", action="store_true",
                        help="rebuild applications and runs (drops the rows in them)")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--prefs", type=Path)
    parser.add_argument("--sync", action="store_true", help="Cognee graph -> candidate graph")
    parser.add_argument("--mirror", action="store_true",
                        help="push the whole graph to HydraDB, then pull it back and compare")
    parser.add_argument("--pull", action="store_true",
                        help="restore the candidate graph from HydraDB onto this machine")
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
    if args.migrate_runs and insight:
        print(f"\nmigrate {insight.client.catalog}.public.runs")
        migrate_runs(insight, dry_run=args.dry_run)
        did_something = True

    if (args.tables or args.recreate_tables) and insight:
        print("\ntables")
        ensure_tables(insight, recreate=args.recreate_tables)
        did_something = True
    if args.resume or args.prefs:
        print("\ncandidate memory"); ingest_candidate(store, args.resume, args.prefs)
        did_something = True
    if args.sync:
        print("\nsync"); sync(store); did_something = True
    if args.mirror:
        print("\nHydraDB mirror"); mirror(store); did_something = True
    if args.pull:
        print("\nrestore from HydraDB"); restore(store); did_something = True
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
