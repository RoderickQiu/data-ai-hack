"""The dashboard feed, offline: rank → respond → roll up → one JSON document.

The page draws whatever this produces, so the honesty rules have to hold *here*
— a metric with no data must survive as ``null`` all the way to the document,
and a day must be one point however many pipelines ran in it.

Reuses the in-memory fixtures from ``tests/test_loop``: same fake hotdata, same
graph, no network.
"""

from __future__ import annotations

import unittest

from agent.feedback import record_response
from agent.metrics import RunMetrics
from agent.rank import record_slate, refresh_and_rank
from demo.dashboard import (
    annotate,
    build,
    cost_basis,
    headline,
    knowledge,
    quality_by_day,
    roll_up,
    shortlist,
)
from tests.test_loop import FakeInsight, fresh_store


class RollUpTest(unittest.TestCase):
    """A day is a roll-up of that day's pipeline invocations, not one row."""

    def test_a_day_sums_its_pipelines_into_one_point(self):
        rows = [
            # P-A: judges the day, deterministic, spends nothing.
            {"started_at": "1a", "day": 1, "mode": "first_run", "shown": 5,
             "tokens_in": 0, "tokens_out": 0, "questions_asked": 0,
             "steps_reasoned": 1, "steps_replayed": 0, "wall_ms": 1200,
             "human_touches": 0},
            # P-B: two packs, one LLM call each, and the question ladder.
            {"started_at": "1b", "day": 1, "mode": "first_run", "shown": 0,
             "tokens_in": 900, "tokens_out": 120, "questions_asked": 3,
             "steps_reasoned": 4, "steps_replayed": 0, "wall_ms": 3400,
             "human_touches": 3},
            {"started_at": "1c", "day": 1, "mode": "first_run", "shown": 0,
             "tokens_in": 800, "tokens_out": 100, "questions_asked": 2,
             "steps_reasoned": 4, "steps_replayed": 0, "wall_ms": 3000,
             "human_touches": 2},
        ]
        points = roll_up(rows, {})
        self.assertEqual(len(points), 1, "three invocations, one day, one point")
        point = points[0]
        self.assertEqual(point["runs"], 3)
        self.assertEqual(point["tokens"], 1920)
        self.assertEqual(point["questions"], 5)
        self.assertEqual(point["shown"], 5, "the slate comes off the judgment pass")
        self.assertEqual(point["wall"], 7.6)

    def test_the_days_mode_is_settled_from_the_whole_day(self):
        def day(num, replayed, reasoned):
            return {"started_at": str(num), "day": num, "shown": 5,
                    "steps_replayed": replayed, "steps_reasoned": reasoned}

        points = roll_up([day(1, 0, 5), day(2, 5, 0), day(3, 4, 1)], {})
        self.assertEqual([p["mode"] for p in points],
                         ["first_run", "full_replay", "partial_replay"])

    def test_a_usage_row_adds_its_tokens_without_becoming_a_run(self):
        """Orchestrator tokens arrive after the run, as their own row. They are a
        cost attachment: the day's total gains them, the invocation count does not."""
        rows = [
            {"started_at": "1a", "day": 1, "run_id": "run-1", "pipeline": "P-A",
             "shown": 5, "tokens_in": 0, "tokens_out": 0, "wall_ms": 1200,
             "steps_replayed": 1, "steps_reasoned": 0, "human_touches": 2},
            {"started_at": "1b", "day": 1, "run_id": "run-1-usage", "pipeline": "P-A",
             "shown": 0, "tokens_in": 4100, "tokens_out": 900, "wall_ms": 0,
             "steps_replayed": 0, "steps_reasoned": 0, "human_touches": 0},
        ]
        point = roll_up(rows, {})[0]
        self.assertEqual(point["tokens"], 5000, "the tokens are the point of the row")
        self.assertEqual(point["runs"], 1, "one invocation happened, not two")
        self.assertEqual(point["run_id"], "run-1")
        self.assertEqual(point["shown"], 5, "the judged row is still the judgment pass")
        self.assertEqual(point["mode"], "full_replay",
                         "a row with no steps must not drag the day off full replay")

    def test_a_usage_row_that_sorts_first_still_is_not_the_run(self):
        """It sorts last today only because it is written after the run. Nothing
        should depend on that — a clock skew must not swap the two."""
        rows = [
            {"started_at": "1a", "day": 1, "run_id": "run-9-usage", "pipeline": "P-A",
             "shown": 0, "tokens_in": 700, "tokens_out": 300},
            {"started_at": "1b", "day": 1, "run_id": "run-9", "pipeline": "P-A",
             "shown": 5, "tokens_in": 0, "tokens_out": 0},
        ]
        point = roll_up(rows, {})[0]
        self.assertEqual(point["run_id"], "run-9")
        self.assertEqual(point["shown"], 5)
        self.assertEqual(point["tokens"], 1000)

    def test_a_day_that_is_only_a_usage_row_still_renders(self):
        rows = [{"started_at": "1a", "day": 1, "run_id": "run-1-usage",
                 "tokens_in": 10, "tokens_out": 5}]
        point = roll_up(rows, {})[0]
        self.assertEqual(point["tokens"], 15)
        self.assertEqual(point["run_id"], "run-1-usage")

    def test_a_pipeline_column_is_used_when_it_exists(self):
        rows = [
            {"started_at": "1a", "day": 1, "pipeline": "P-B", "tokens_in": 500,
             "tokens_out": 0, "shown": 0},
            {"started_at": "1b", "day": 1, "pipeline": "P-A", "shown": 5,
             "tokens_in": 0, "tokens_out": 0},
        ]
        # P-B sorts first here, so the "earliest row" fallback would pick it.
        # The explicit column has to win, or `shown` comes back empty.
        self.assertEqual(roll_up(rows, {})[0]["shown"], 5)


class HonestyTest(unittest.TestCase):
    """No data is null, all the way to the document. Never zero, never bridged."""

    def test_a_day_with_no_resolved_predictions_has_a_null_accuracy(self):
        rows = [{"started_at": str(d), "day": d, "shown": 5} for d in (1, 2)]
        quality = {1: {"answered": 5, "kept": 3, "accuracy": 0.6}}
        points = roll_up(rows, quality)
        self.assertEqual(points[0]["acc"], 0.6)
        self.assertIsNone(points[1]["acc"],
                          "a day nobody answered has no accuracy, not an accuracy of 0")
        self.assertIsNone(points[1]["kept"])

    def test_accuracy_is_derived_from_applications_not_read_from_runs(self):
        """The runs row is written before the human answers, so it is always
        NULL on the real path. The applications table is where the answer is."""
        insight = FakeInsight()
        insight.applications = [
            {"job_id": "j1", "event": "seen", "predicted": "skip", "actual": "skip", "day": 1},
            {"job_id": "j2", "event": "seen", "predicted": "skip", "actual": "keep", "day": 1},
            {"job_id": "j3", "event": "seen", "predicted": "keep", "actual": "keep", "day": 1},
        ]
        insight.run = lambda name, params=None: (
            insight.applications if name == "predictions_window" else [])

        quality = quality_by_day(insight)
        self.assertEqual(quality[1]["kept"], 2)
        # Skip class only: j1 and j2 are relevant, one of them is right.
        self.assertEqual(quality[1]["accuracy"], 0.5)

    def test_a_slate_with_no_skips_reports_no_accuracy_rather_than_zero(self):
        insight = FakeInsight()
        insight.run = lambda name, params=None: [
            {"job_id": "j1", "predicted": "keep", "actual": "keep", "day": 1},
        ] if name == "predictions_window" else []
        self.assertIsNone(quality_by_day(insight)[1]["accuracy"])

    def test_a_day_nobody_counted_is_a_gap_not_a_free_day(self):
        """One failed usage poll must not become the best point on the cost chart."""
        rows = [
            {"started_at": "1", "day": 1, "run_id": "r1", "shown": 5,
             "tokens_in": 0, "tokens_out": 0},
            {"started_at": "1u", "day": 1, "run_id": "r1-usage",
             "tokens_in": 3000, "tokens_out": 500},
            {"started_at": "2", "day": 2, "run_id": "r2", "shown": 5,
             "tokens_in": 0, "tokens_out": 0},          # the poll found nothing
        ]
        points = roll_up(rows, {})
        self.assertEqual(points[0]["tokens"], 3500)
        self.assertIsNone(points[1]["tokens"],
                          "an uncounted day is unmeasured, not zero-cost")
        self.assertEqual(cost_basis(points), "tokens")

    def test_cost_basis_says_unavailable_rather_than_drawing_zero(self):
        points = [{"tokens": 0, "tool_calls": 0}, {"tokens": 0, "tool_calls": 0}]
        self.assertEqual(cost_basis(points), "unavailable")
        self.assertEqual(cost_basis([{"tokens": 0, "tool_calls": 12}]), "tool_calls")
        self.assertEqual(cost_basis([{"tokens": 900, "tool_calls": 12}]), "tokens")

    def test_headline_keeps_its_shape_with_nothing_to_report(self):
        stats = headline([], "unavailable")
        self.assertIsNone(stats["cost"]["value"])
        self.assertIsNone(stats["accuracy"]["first"])

    def test_the_callout_is_derived_from_the_mode_not_stored(self):
        points = annotate([
            {"day": 1, "mode": "full_replay"},
            {"day": 2, "mode": "partial_replay"},   # had to reason again
            {"day": 3, "mode": "partial_replay"},   # still reasoning, not news
        ])
        self.assertNotIn("note", points[0])
        self.assertIn("never seen", points[1]["note"])
        self.assertNotIn("note", points[2])


class LiveDocumentTest(unittest.TestCase):
    """One real judgment pass, one real response, one document out the far end."""

    def setUp(self):
        self.insight = FakeInsight()
        self.store = fresh_store()

    def judge(self, day=1, run_id="run-1"):
        result = refresh_and_rank(self.insight, self.store, day)
        record_slate(self.store, result, run_id)
        return result

    def test_the_open_slate_is_readable_without_re_running_the_ranking(self):
        result = self.judge()
        roles = shortlist(self.insight, self.store, day=1)
        self.assertEqual({r["job_id"] for r in roles},
                         {p.job_id for p in result.slate})
        self.assertTrue(all(r["title"] and r["company"] for r in roles))
        self.assertTrue(all(r["predicted"] in ("keep", "skip") for r in roles))
        self.assertTrue(all(r["why"] for r in roles))
        # record_prediction persists the reasons `explain` composed, so the page
        # shows the sentence the human actually read.
        self.assertTrue(all(r["why_source"] == "stored" for r in roles))

    def test_a_slate_without_stored_reasons_says_it_is_a_reconstruction(self):
        """Edges written before reasons were persisted still have to render —
        and must not pass a reconstruction off as the original wording."""
        self.judge()
        # Strip the prop on the graph directly: merge_edge drops None values by
        # design, so a prop cannot be cleared by merging one over it.
        graph = self.store.backend.graph
        for edge in graph.out(self.store.candidate_node(), "PREDICTED_KEEP"):
            edge.props.pop("reasons", None)
        self.store.flush()
        roles = shortlist(self.insight, self.store, day=0)
        self.assertTrue(roles)
        self.assertTrue(all(r["why_source"] == "reconstructed" for r in roles))
        self.assertTrue(all(r["why"] for r in roles),
                        "a reconstruction is still a sentence, not an empty string")

    def test_an_answered_role_leaves_the_slate(self):
        result = self.judge()
        answered = result.slate[0]
        record_response(self.store, self.insight, day=1, run_id="run-1",
                        signals=[{"job_id": answered.job_id, "kind": "skip"}],
                        predictions={answered.job_id: answered.predicted})
        self.store.flush()
        open_ids = {r["job_id"] for r in shortlist(self.insight, self.store, day=1)}
        self.assertNotIn(answered.job_id, open_ids,
                         "current_slate is the mirror of agreement_record")

    def test_knowledge_counts_come_out_of_the_graph(self):
        self.judge()
        k = knowledge(self.store)
        self.assertEqual(k["claims"], 3, "the three verified claims in the fixture")
        self.assertIn(k["autonomy"]["shortlisting"], ("Supervised", "Ready", "Autonomous"))
        self.assertIsNone(k["readiness"]["value"],
                          "nothing resolved yet, so there is no readiness to report")

    def test_knowledge_does_not_write_to_the_graph(self):
        """A page refresh must not move the thing it is displaying."""
        self.judge()
        before = self.store.backend.graph.to_dict()
        knowledge(self.store)
        self.assertEqual(self.store.backend.graph.to_dict(), before)

    def test_build_produces_a_document_the_page_can_draw(self):
        result = self.judge()
        metrics = RunMetrics(day=1)
        metrics.note_reasoned()
        metrics.note_slate(result.pool_size, result.released_today, result.digest_rows())
        self.insight.log_run(metrics.row())
        record_response(
            self.store, self.insight, day=1, run_id=metrics.run_id,
            signals=[{"job_id": p.job_id, "kind": "skip"} for p in result.slate],
            predictions={p.job_id: p.predicted for p in result.slate})

        document = build(self.insight, self.store)
        for key in ("generated_at", "day", "cost_basis", "runs", "stats",
                    "shortlist", "knowledge"):
            self.assertIn(key, document)
        self.assertEqual(len(document["runs"]), 1)
        self.assertEqual(document["cost_basis"], "unavailable",
                         "the judgment pass calls no model, so there is no cost line yet")
        self.assertIsNone(document["stats"]["cost"]["value"])
        self.assertEqual(document["runs"][0]["kept"], 0, "the human skipped everything")


if __name__ == "__main__":
    unittest.main(verbosity=2)
