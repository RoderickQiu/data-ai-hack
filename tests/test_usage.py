"""Change 6: whether the engine reports tokens, and what happens either way.

The shapes below are guesses — there is no usage route in the OpenAPI, so
``RocketRide.usage`` scrapes an undocumented payload and ``reported`` is the
only thing a caller may branch on. These tests pin the two facts that a wrong
guess would quietly get wrong on a chart: a credit *balance* must never be read
as a token count, and a run's usage must land on its day exactly once.
"""

from __future__ import annotations

import unittest

from demo.loop import attribute_usage, token_of
from rocketride.client import RocketRide


class FakeInsight:
    """Just the two methods ``attribute_usage`` uses."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.logged = []

    def run(self, name, params=None):
        assert name == "runs_series"
        return list(self.rows)

    def log_run(self, row):
        self.logged.append(row)
        self.rows.append(row)
        return {"rows": 1}


class FakeClient:
    def __init__(self, status):
        self._status = status
        self.asked = []

    def status(self, token):
        self.asked.append(token)
        return self._status


RUN = {"run_id": "run-abc", "started_at": "2026-09-11T10:00:00+00:00", "day": 3,
       "mode": "partial_replay", "pipeline": "P-A", "tokens_in": 0, "tokens_out": 0}


class UsageShapes(unittest.TestCase):
    def test_credit_balance_is_not_a_token_count(self):
        """`status.tokens` is the credit balance — a falling line that means the
        opposite of what the chart claims. It comes back as `credits`, and
        `reported` stays false so the cost panel is suppressed."""
        usage = RocketRide.usage({"tokens": 48210,
                                  "pipeflow": {"byPipe": {"chat": {"count": 3}}}})
        self.assertFalse(usage["reported"])
        self.assertEqual(usage["credits"], 48210)
        self.assertEqual((usage["tokens_in"], usage["tokens_out"]), (0, 0))

    def test_per_node_counters_split_by_direction(self):
        usage = RocketRide.usage({"pipeflow": {"byPipe": {
            "agent": {"promptTokens": 10, "completionTokens": 90}}}})
        self.assertTrue(usage["reported"])
        self.assertEqual((usage["tokens_in"], usage["tokens_out"]), (10, 90))

    def test_direction_reads_the_leaf_not_the_path(self):
        """`prompt.completionTokens` is output. Reading the whole dotted path
        would call it input and put the bigger half on the wrong line."""
        usage = RocketRide.usage({"pipeflow": {"byPipe": {
            "prompt": {"completionTokens": 90}}}})
        self.assertEqual((usage["tokens_in"], usage["tokens_out"]), (0, 90))

    def test_nested_usage_object_counts_every_number_inside(self):
        usage = RocketRide.usage({"pipeflow": {"byPipe": {
            "agent": {"tokenUsage": {"in": 4, "out": 6}}}}})
        self.assertEqual((usage["tokens_in"], usage["tokens_out"]), (4, 6))

    def test_a_flag_named_tokens_is_not_a_counter(self):
        self.assertFalse(RocketRide.usage({"usage": {"tokensEnabled": True}})["reported"])


class Attribution(unittest.TestCase):
    def test_nothing_is_written_when_the_engine_reports_nothing(self):
        """The whole point of the probe. No counters means no row — the cost
        panel is hidden rather than drawn flat at zero."""
        insight = FakeInsight([RUN])
        usage = attribute_usage(insight, FakeClient({"status": "Done"}), "tk_1")
        self.assertFalse(usage["reported"])
        self.assertEqual(insight.logged, [])

    def test_usage_lands_on_the_run_s_day_exactly_once(self):
        insight = FakeInsight([RUN])
        status = {"pipeflow": {"byPipe": {"a": {"promptTokens": 12,
                                                "completionTokens": 34}}}}
        usage = attribute_usage(insight, FakeClient(status), "tk_1")
        self.assertEqual(usage["attached_to"], "run-abc")
        row, = insight.logged
        self.assertEqual(row["day"], 3)
        self.assertEqual((row["tokens_in"], row["tokens_out"]), (12, 34))

    def test_the_usage_row_carries_no_other_counter(self):
        """It is a second row, not an edit, because append-with-key upsert
        semantics are unverified. That is only safe if every other field is
        zero — the dashboard sums a day, so anything non-zero here would be
        double-counted against the run it belongs to."""
        insight = FakeInsight([RUN])
        status = {"usage": {"input_tokens": 1, "output_tokens": 2}}
        attribute_usage(insight, FakeClient(status), "tk_1")
        row, = insight.logged
        self.assertNotEqual(row["run_id"], RUN["run_id"])
        for field in ("wall_ms", "questions_asked", "human_touches", "shown",
                      "steps_reasoned", "steps_replayed", "actual_keep"):
            self.assertEqual(row[field], 0, field)

    def test_it_attaches_to_the_newest_run_not_the_last_row(self):
        older = {**RUN, "run_id": "run-old", "started_at": "2026-09-11T09:00:00+00:00"}
        insight = FakeInsight([RUN, older])          # deliberately out of order
        status = {"usage": {"input_tokens": 1}}
        usage = attribute_usage(insight, FakeClient(status), "tk_1")
        self.assertEqual(usage["attached_to"], "run-abc")

    def test_no_runs_yet_writes_nothing(self):
        insight = FakeInsight([])
        usage = attribute_usage(insight, FakeClient({"usage": {"input_tokens": 1}}), "tk_1")
        self.assertIsNone(usage["attached_to"])
        self.assertEqual(insight.logged, [])


class Token(unittest.TestCase):
    def test_token_comes_out_of_the_webhook_url_the_loop_already_has(self):
        self.assertEqual(
            token_of("https://api.example.com/webhook?token=tk_abc123"), "tk_abc123")

    def test_a_url_without_a_token_yields_empty_rather_than_raising(self):
        self.assertEqual(token_of("https://api.example.com/webhook"), "")


if __name__ == "__main__":
    unittest.main()
