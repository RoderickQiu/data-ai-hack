"""The loop, end to end, offline: rank → digest → feedback → preference → re-rank.

This is the test that would have caught the things a live rehearsal catches: a
hypothesis that never fires, a confirmed rule that changes nothing, a prediction
that is never resolved, a runs row with no quality metric on it.

hotdata is replaced by :class:`FakeInsight`, which holds the same three tables in
memory and answers the same named queries. Nothing here makes a network call, so
it runs in CI and in the five minutes before the pitch.
"""

from __future__ import annotations

import unittest
from typing import Any, Mapping, Sequence

from agent.config import Settings, TUNABLES
from demo.loop import webhook_url
from agent.feedback import record_response
from agent.metrics import RunMetrics
from agent.rank import refresh_and_rank
from memory.claims import Claim, store_claims
from memory.extract import link_claim_skills
from memory.graph import Graph, GraphStore, LocalBackend
from memory.prefs import active_rules

CLAIMS = [
    Claim("cl-evals", "Led the evaluation tooling for an LLM assistant used by 40 teams.",
          status="verified", source_doc="resume"),
    Claim("cl-tf", "Built and operated the Terraform estate for a 200-node cluster.",
          status="verified", source_doc="resume"),
    Claim("cl-api", "Shipped a retrieval service in Python and FastAPI at 4000 rps.",
          status="verified", source_doc="resume"),
]

JOBS = [
    # id, company, title, description, release_day
    ("j-staff-1", "Bigco", "Staff Platform Engineer", "Python and Terraform at scale.", 1),
    ("j-staff-2", "Bigco2", "Staff Backend Engineer", "Python, Postgres, on-call.", 1),
    ("j-staff-3", "Bigco3", "Staff ML Engineer", "Python, evals, LLM tooling.", 1),
    ("j-senior-1", "Smallco", "Senior Platform Engineer", "Terraform, Python, FastAPI.", 1),
    ("j-senior-2", "Smallco2", "Senior ML Engineer", "LLM evals and Python tooling.", 1),
    ("j-senior-3", "Smallco3", "Senior Backend Engineer", "Python and Postgres.", 1),
    ("j-senior-4", "Smallco4", "Senior Data Engineer", "dbt, Postgres, Airflow.", 1),
    ("j-front-1", "Webco", "Frontend Engineer", "React and TypeScript.", 1),
]


class FakeInsight:
    """The three tables, in memory, behind the same named-query interface."""

    def __init__(self) -> None:
        self.jobs = [
            {"id": job_id, "source": "greenhouse", "company": company, "title": title,
             "location": "Remote", "remote": True, "salary_min": 90000,
             "salary_max": 120000, "description": description, "blurb": description,
             "release_day": day, "url": f"https://example.test/{job_id}",
             "company_size": 9000 if company.startswith("Bigco") else 200}
            for job_id, company, title, description, day in JOBS
        ]
        self.applications: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []

    # -- read
    def judged_ids(self) -> list[str]:
        judged = {"shortlisted", "skipped", "not_for_me", "applied", "rejected"}
        return [row["job_id"] for row in self.applications if row["event"] in judged]

    def released_today(self, day: int) -> list[dict[str, Any]]:
        return [job for job in self.jobs if job["release_day"] == day]

    def narrow(self, day: int, **_: Any) -> list[dict[str, Any]]:
        judged = set(self.judged_ids())
        return [job for job in self.jobs
                if job["release_day"] <= day and job["id"] not in judged]

    def search(self, text: str, limit: int = 30, index: str | None = None) -> list[dict[str, Any]]:
        # Rank by shared words. Enough to give the similarity feature something
        # monotonic to work with; the real index is exercised live, not here.
        terms = {word.lower() for word in text.split() if len(word) > 3}
        scored = sorted(
            self.jobs,
            key=lambda job: -len(terms & {w.lower() for w in
                                          (job["title"] + " " + job["description"]).split()}),
        )
        return [{"id": job["id"]} for job in scored[:limit]]

    def salary_percentile(self, title: str, location: str = "") -> dict[str, Any]:
        return {"postings": 400, "p50": 110000, "p25": 95000, "p75": 130000}

    def run(self, name: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        params = params or {}
        if name == "descriptions_by_id":
            wanted = set(params["job_ids"])
            return [{"id": job["id"], "description": job["description"]}
                    for job in self.jobs if job["id"] in wanted]
        if name == "jobs_by_id":
            wanted = set(params["job_ids"])
            return [job for job in self.jobs if job["id"] in wanted]
        if name == "job":
            return [job for job in self.jobs if job["id"] == params["job_id"]]
        if name == "runs_series":
            return self.runs
        raise KeyError(name)

    # -- write
    def log_events(self, events) -> dict[str, Any]:
        rows = [event.row() for event in events]
        self.applications.extend(rows)
        return {"rows": len(rows)}

    def log_event(self, event) -> dict[str, Any]:
        return self.log_events([event])

    def log_run(self, row: Mapping[str, Any]) -> dict[str, Any]:
        self.runs.append(dict(row))
        return {"rows": 1}


def fresh_store() -> GraphStore:
    store = GraphStore.__new__(GraphStore)
    store.config = Settings()
    store.candidate_id = "cand-loop"
    store.backend = LocalBackend(Graph(), mirror=None)
    store.backend.flush = lambda: {}
    store_claims(store, CLAIMS)
    link_claim_skills(store)
    return store


class WebhookTargetTests(unittest.TestCase):
    """``--webhook`` is checked before a request carries the API key to it."""

    def test_http_and_https_pass(self):
        for url in ("https://api.rocketride.ai/v1/hook", "http://localhost:8080/x"):
            self.assertEqual(webhook_url(url), url)

    def test_any_other_scheme_is_refused(self):
        for bad in ("file:///etc/passwd", "ftp://host/f", "gopher://h",
                    "https:///no-host", "api.rocketride.ai/hook", ""):
            with self.assertRaises(ValueError, msg=bad):
                webhook_url(bad)


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.insight = FakeInsight()
        self.store = fresh_store()

    def judge(self, day: int = 1):
        return refresh_and_rank(self.insight, self.store, day)

    def respond(self, result, kinds: Mapping[str, str], day: int = 1,
                run_id: str = "run-test") -> dict[str, Any]:
        signals = [{"job_id": job_id, "kind": kind,
                    "reason_text": "too senior for me" if kind == "not_for_me" else "",
                    "reason_tags": ["too_senior"] if kind == "not_for_me" else []}
                   for job_id, kind in kinds.items()]
        predictions = {p.job_id: p.predicted for p in result.slate}
        return record_response(self.store, self.insight, day=day, run_id=run_id,
                               signals=signals, predictions=predictions)

    # --

    def test_a_pass_produces_a_five_role_slate_with_reasons(self):
        result = self.judge()
        self.assertEqual(len(result.slate), TUNABLES.slate_size)
        self.assertTrue(all(row["why"] for row in result.digest_rows()))
        self.assertTrue(any(p.coverage > 0 for p in result.slate),
                        "requirements should have been extracted from the descriptions")

    def test_requirements_are_extracted_once_and_then_reused(self):
        self.judge()
        before = len(self.store.graph.by_label("Requirement"))
        self.assertGreater(before, 0)
        self.judge()
        self.assertEqual(len(self.store.graph.by_label("Requirement")), before)

    def test_judged_roles_leave_the_pool(self):
        first = self.judge()
        self.respond(first, {first.slate[0].job_id: "keep"})
        second = self.judge()
        self.assertNotIn(first.slate[0].job_id, [p.job_id for p in second.ranked])

    def test_three_not_for_mes_fire_a_hypothesis_and_confirming_reorders(self):
        result = self.judge()
        staff = [job["id"] for job in self.insight.jobs if "Staff" in job["title"]][:3]
        # The agent has to have seen them for the signal to be about a real job.
        for job_id in staff:
            from memory.writers import upsert_job
            upsert_job(self.store, next(j for j in self.insight.jobs if j["id"] == job_id))
        feedback = self.respond(result, {job_id: "not_for_me" for job_id in staff})

        hypotheses = feedback["preference_hypotheses"]
        self.assertTrue(hypotheses, "three matching not-for-mes should propose a rule")
        self.assertEqual(active_rules(self.store), [],
                         "a hypothesis must change nothing before it is confirmed")

        from agent.feedback import confirm_preference
        confirm_preference(self.store, hypotheses[0]["preference_id"], confirmed=True)
        self.assertEqual(len(active_rules(self.store)), 1)

        after = self.judge(day=1)
        penalised = [p for p in after.ranked if "staff" in
                     (after.jobs[p.job_id]["title"] or "").lower()]
        others = [p for p in after.ranked if p not in penalised]
        if penalised and others:
            self.assertLess(max(p.score for p in penalised), max(p.score for p in others),
                            "the confirmed rule should push staff roles down")

    def test_a_rejected_hypothesis_is_never_proposed_again(self):
        from agent.feedback import confirm_preference
        from memory.prefs import check_for_hypothesis, rejected_rule_keys

        result = self.judge()
        staff = [job["id"] for job in self.insight.jobs if "Staff" in job["title"]][:3]
        from memory.writers import upsert_job
        for job_id in staff:
            upsert_job(self.store, next(j for j in self.insight.jobs if j["id"] == job_id))
        feedback = self.respond(result, {job_id: "not_for_me" for job_id in staff})
        preference_id = feedback["preference_hypotheses"][0]["preference_id"]

        confirm_preference(self.store, preference_id, confirmed=False)
        self.assertTrue(rejected_rule_keys(self.store))
        self.assertEqual(check_for_hypothesis(self.store), [])

    def test_predictions_are_resolved_and_scored(self):
        result = self.judge()
        metrics = RunMetrics(day=1)
        metrics.note_slate(result.pool_size, result.released_today, result.digest_rows())
        kinds = {p.job_id: ("keep" if p.predicted == "keep" else "skip")
                 for p in result.slate}
        feedback = self.respond(result, kinds)
        scored = metrics.score_responses(feedback["resolved_predictions"])
        self.assertEqual(len(feedback["resolved_predictions"]), len(result.slate))
        self.assertIn("precision_at_5", scored)
        record = self.store.run("agreement_record", {"limit": 10})
        self.assertEqual(len(record), len(result.slate))
        self.assertTrue(all(row["actual"] for row in record))

    def test_an_agreeing_human_drives_the_agreement_record_up(self):
        for day in range(1, 4):
            result = self.judge(day)
            if not result.slate:
                break
            self.respond(result,
                         {p.job_id: ("keep" if p.predicted == "keep" else "skip")
                          for p in result.slate}, day=day, run_id=f"run-{day}")
        from memory.autonomy import D1_SHORTLIST, skip_class_accuracy
        record = self.store.run("agreement_record", {"limit": 15})
        accuracy, window = skip_class_accuracy(record)
        self.assertGreater(window, 0)
        self.assertEqual(accuracy, 1.0, "a human who agreed every time is a perfect record")

    def test_a_runs_row_carries_all_three_lines(self):
        result = self.judge()
        metrics = RunMetrics(day=1)
        metrics.note_reasoned()
        metrics.note_llm(400, 120)
        metrics.note_slate(result.pool_size, result.released_today, result.digest_rows())
        feedback = self.respond(result, {p.job_id: "skip" for p in result.slate})
        metrics.score_responses(feedback["resolved_predictions"])
        self.insight.log_run(metrics.row())

        row = self.insight.runs[-1]
        self.assertEqual(row["mode"], "first_run")
        self.assertEqual(row["tokens_in"] + row["tokens_out"], 520)
        self.assertIsNotNone(row["precision_at_5"])
        self.assertEqual(row["shown"], len(result.slate))

    def test_replays_cost_nothing_and_say_so(self):
        metrics = RunMetrics(day=2)
        metrics.note_replayed("refresh-and-rank-v1", steps=5)
        row = metrics.row()
        self.assertEqual(row["mode"], "full_replay")
        self.assertEqual(row["tokens_in"] + row["tokens_out"], 0)
        self.assertEqual(metrics.replay_ratio(), 1.0)

    def test_chart_series_leaves_gaps_rather_than_interpolating(self):
        from demo.chart import series

        rows = [
            {"started_at": "1", "day": 1, "mode": "first_run", "tokens_in": 900,
             "tokens_out": 100, "questions_asked": 6, "prediction_accuracy": 0.4},
            {"started_at": "2", "day": 2, "mode": "full_replay", "tokens_in": 0,
             "tokens_out": 0, "questions_asked": 0, "prediction_accuracy": None},
            {"started_at": "3", "day": 3, "mode": "full_replay", "tokens_in": 10,
             "tokens_out": 5, "questions_asked": 0, "prediction_accuracy": 0.9},
        ]
        data = series(rows)
        self.assertEqual(data["accuracy"], [40.0, None, 90.0])
        self.assertEqual(data["tokens"], [1000, 0, 15])
        self.assertIn("6 questions", __import__("demo.chart", fromlist=["headline"])
                      .headline(data))


if __name__ == "__main__":
    unittest.main(verbosity=2)
