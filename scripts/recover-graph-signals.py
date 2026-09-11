#!/usr/bin/env python3
"""Rebuild the graph's human-signal half from hotdata. Idempotent.

The ``applications`` table is the durable copy of every answer the human gave.
The graph half — Signal nodes, GAVE/ON/REJECTED edges, resolved
PREDICTED_KEEP — is derived from the same call and can be lost: a stale writer
used to stamp over ``graph.json``, and any future write-loss has the shape.

Nothing here is invented. Every row replayed carries the day, run_id,
reason_tags and reason the human actually produced.

**Idempotent by construction.** ``memory.writers.record_signal`` mixes
``now_iso()`` into the signal id, so replaying through it twice creates two
nodes for one answer — which silently doubles the evidence count that
preference induction (P2, "three rejections sharing a reason") thresholds on.
Here the signal id is derived from the application row's own stable id, so a
re-run MERGEs onto the same node. Existing signals are cleared first, so a
graph already holding duplicates is repaired rather than added to.

Run it last, after whatever was losing writes has stopped.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402

from agent.config import state_path  # noqa: E402
from memory.graph import GRAPH_FILE, Graph, nid  # noqa: E402
from memory.writers import record_answer_to_prediction  # noqa: E402
from memory.graph import GraphStore  # noqa: E402
from insight.store import Insight  # noqa: E402

KIND = {"shortlisted": "keep", "skipped": "skip", "not_for_me": "not_for_me"}


def main() -> None:
    insight = Insight()
    rows = insight.client.query(
        "SELECT id, job_id, event, reason_tags, reason, at, day, run_id, "
        "predicted, actual FROM jobs.public.applications "
        "WHERE run_id <> 'bootstrap' ORDER BY at")
    print(f"application rows: {len(rows)}")

    # Rewrite the file directly rather than through `flush`: flush merges onto
    # disk (correctly — that is what stopped the clobber), so a node dropped
    # in memory would simply come back. Clearing duplicates needs the write.
    path = state_path(GRAPH_FILE)
    graph = Graph.from_dict(json.loads(path.read_text())) if path.exists() else Graph()

    stale = [n for n in graph.nodes.values() if n.label == "Signal"]
    for node in stale:
        for edge in list(graph.out(node.id)) + list(graph.into(node.id)):
            graph.drop_edge(edge.src, edge.type, edge.dst)
        graph.nodes.pop(node.id, None)
    print(f"cleared {len(stale)} existing signal node(s)")

    candidate = nid("candidate", insight.client.config.candidate_id)
    graph.merge_node(candidate, "Candidate",
                     candidate_id=insight.client.config.candidate_id)

    signals = 0
    for row in rows:
        kind = KIND.get(row["event"])
        if not kind:
            continue
        tags = [t for t in (row.get("reason_tags") or "").split(",") if t]
        # The application row id is already a hash of run_id|job_id|event|at —
        # stable across re-runs, which is exactly what the signal id needs.
        signal = nid("signal", row["id"])
        graph.merge_node(signal, "Signal", signal_id=row["id"], kind=kind,
                         reason_tags=tags, reason_text=row.get("reason") or "",
                         day=int(row.get("day") or 0), run_id=row.get("run_id") or "",
                         at=row.get("at") or "")
        graph.merge_edge(candidate, "GAVE", signal)
        graph.merge_edge(signal, "ON", nid("job", row["job_id"]))
        if kind == "not_for_me":
            graph.merge_edge(candidate, "REJECTED", nid("job", row["job_id"]),
                             reason=row.get("reason") or "", reason_tags=tags,
                             day=int(row.get("day") or 0))
        signals += 1

    path.write_text(json.dumps(graph.to_dict(), indent=1, default=str))

    # Resolutions go through the writer: PREDICTED_KEEP is keyed (candidate,
    # job), so re-asserting it is already idempotent, and this way the mirror
    # sees them.
    store = GraphStore()
    resolved = 0
    for row in rows:
        if row.get("predicted") and row.get("actual"):
            record_answer_to_prediction(store, row["job_id"], row["actual"],
                                        row["predicted"])
            resolved += 1
    store.flush(wait=True)

    print(f"replayed {signals} signal(s), {resolved} resolved prediction(s)")
    stats = store.graph.stats()
    print(f"graph now: {stats['nodes']} nodes, {stats['edges']} edges")
    for label in ("Signal", "Claim", "Preference"):
        print(f"  {label:<12} {stats['labels'].get(label, 0)}")
    for edge in ("GAVE", "REJECTED"):
        print(f"  {edge:<12} {stats['edge_types'].get(edge, 0)}")


if __name__ == "__main__":
    main()
