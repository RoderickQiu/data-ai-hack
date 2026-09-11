"""Offline tests for everything that does not need a live service.

Run with ``make test``. Nothing here touches the network: the graph runs on its
local backend, hotdata is not called, and the LLM is never constructed. The
things being checked are the ones that would be expensive to discover on stage —
the SQL literal renderer, the release schedule, the citation validator, the
preference induction rules, the slate mix, and the skip-class metric.
"""

from __future__ import annotations

import json
import unittest

from agent.clock import DayClock, assign_release_days, release_histogram
from agent.metrics import RunMetrics, skip_class_accuracy
from agent.predict import predict, requirement_coverage
from agent.digest import parse_reply, tags_from_text
from agent.rank import build_slate
from agent.schema import Job, from_greenhouse, normalize_many, sanitize_html
from insight.hotdata import HotdataError, identifier, qualify
from insight.queries import _literal, bind_catalog
from memory import claims as claims_mod
from memory.answers import resolve, set_standard_answer
from memory.autonomy import D1_SHORTLIST, evaluate, grant, note_decision
from memory.graph import Graph, GraphStore, LocalBackend, canonical_skill, nid
from memory.prefs import FIELDS as PREFERENCE_FIELDS, Rule, induce, preference_fit
from memory.sync import sync_graph
from memory.writers import record_prediction, record_signal, upsert_job, upsert_requirements
from mcp_server.rote import (PlayIndex, RoteError, fingerprint_ingest,
                             fingerprint_task, workspace_dir)


def make_store() -> GraphStore:
    """A store with no persistence and no mirror. Every test gets a clean graph."""
    store = GraphStore.__new__(GraphStore)
    from agent.config import Settings
    store.config = Settings()
    store.candidate_id = "cand-test"
    store.backend = LocalBackend(Graph(), mirror=None)
    store.backend.flush = lambda: {"local": "memory"}  # never touch the disk
    return store


def seed_candidate(store: GraphStore) -> None:
    from memory.claims import Claim, store_claims

    store.merge_node(store.candidate_node(), "Candidate", candidate_id=store.candidate_id)
    store_claims(store, [
        Claim("cl-a", "Led evaluation tooling for an LLM product used by 40 teams.",
              status="verified", source_doc="resume", skills=["LLM evals", "Python"]),
        Claim("cl-b", "Built and ran the Terraform estate for a 200-node cluster.",
              status="verified", source_doc="resume", skills=["Terraform"]),
        Claim("cl-c", "Rumoured Kubernetes expertise.", status="unverified",
              source_doc="cover-letter", skills=["Kubernetes"]),
    ])


class SchemaTests(unittest.TestCase):
    def test_sanitize_strips_scripts_and_entities(self):
        raw = "<p>Real text</p><script>alert('x')</script>&lt;img onerror=1&gt;"
        cleaned = sanitize_html(raw)
        self.assertIn("Real text", cleaned)
        self.assertNotIn("alert", cleaned)
        self.assertNotIn("<img", cleaned)

    def test_greenhouse_normalizer(self):
        job = from_greenhouse({
            "id": 123, "title": "Senior ML Engineer",
            "location": {"name": "Remote - US"},
            "content": "<p>We pay $180,000 - $220,000.</p>",
            "absolute_url": "https://boards.greenhouse.io/x/jobs/123",
            "first_published": "2026-08-01T00:00:00Z",
        }, "Stripe", "stripe")
        self.assertEqual(job.id, "greenhouse:stripe:123")
        self.assertTrue(job.remote)
        self.assertEqual((job.salary_min, job.salary_max), (180000, 220000))
        self.assertNotIn("<p>", job.description)

    def test_ids_are_stable_and_deduplicated(self):
        items = [{"id": 1, "title": "A"}, {"id": 1, "title": "A"}, {"id": 2, "title": "B"}]
        jobs = normalize_many("greenhouse", items, "Acme", "acme")
        self.assertEqual(len(jobs), 2)
        again = normalize_many("greenhouse", items, "Acme", "acme")
        self.assertEqual([j.id for j in jobs], [j.id for j in again])


class ClockTests(unittest.TestCase):
    def make_jobs(self, n: int) -> list[Job]:
        return [Job(id=f"greenhouse:acme:{i}", source=["greenhouse", "lever", "ashby"][i % 3],
                    ats_type="x", company="Acme", company_slug="acme", title=f"Role {i}")
                for i in range(n)]

    def test_every_day_gets_a_non_trivial_batch(self):
        jobs = assign_release_days(self.make_jobs(1000), horizon=30)
        histogram = release_histogram(jobs)
        self.assertEqual(len(histogram), 30)
        self.assertLessEqual(max(histogram.values()) - min(histogram.values()), 1)

    def test_assignment_is_deterministic(self):
        first = {j.id: j.release_day for j in assign_release_days(self.make_jobs(200))}
        second = {j.id: j.release_day for j in assign_release_days(self.make_jobs(200))}
        self.assertEqual(first, second)

    def test_clock_wraps_rather_than_running_off_the_corpus(self):
        clock = DayClock(day=30, horizon=30)
        clock.save = lambda: clock          # no disk in tests
        self.assertEqual(clock.advance(), 1)


class QueryRendererTests(unittest.TestCase):
    def test_quotes_are_escaped(self):
        self.assertEqual(_literal("O'Brien"), "'O''Brien'")

    def test_injection_attempt_stays_a_literal(self):
        rendered = _literal("x'; DROP TABLE jobs; --")
        self.assertTrue(rendered.startswith("'") and rendered.endswith("'"))
        self.assertNotIn("';", rendered.replace("''", ""))

    def test_unsupported_types_raise(self):
        for value in ({"a": 1}, [[1]], object()):
            with self.assertRaises(ValueError):
                _literal(value)

    def test_empty_in_list_is_never_matching(self):
        self.assertEqual(_literal([]), "(NULL)")

    def test_named_query_rejects_unknown_params(self):
        queries = bind_catalog("jobs")
        with self.assertRaises(KeyError):
            queries["job"].render({"job_id": "x", "sneaky": 1})
        sql = queries["job"].render({"job_id": "greenhouse:acme:1"})
        self.assertIn("jobs.public.jobs", sql)
        self.assertIn("'greenhouse:acme:1'", sql)

    def test_catalog_must_be_an_identifier(self):
        with self.assertRaises(ValueError):
            bind_catalog("jobs; DROP TABLE x")

    def test_every_field_a_preference_can_name_is_actually_selected(self):
        """A rule over a column the ranking queries do not select is inert.

        It gets proposed, the human confirms it, and then it matches nothing —
        because Rule.matches returns False on an absent field. The shortlist
        does not move and P2 fails on stage while looking like it worked. This
        is the cheap structural check that stops that happening again.
        """
        queries = bind_catalog("jobs")
        for name in ("narrow", "pool", "released_today", "jobs_by_id", "job"):
            sql = queries[name].sql.lower()
            select = sql.split(" from ")[0]
            for rule_field in PREFERENCE_FIELDS:
                if rule_field in ("salary_min",):
                    continue        # not used by any inducer
                self.assertIn(rule_field, select,
                              f"{name} does not select {rule_field}, so a "
                              f"preference rule over it could never match")


class OperatorInputTests(unittest.TestCase):
    """The names and paths a human types on the command line.

    ``--project``, ``--source-table`` and a workspace name all end up in a SQL
    string or a mkdir, and none of them can be bound as a parameter. Each one is
    checked at the edge instead, so the failure is an error message rather than
    a rewritten query or a directory outside ROTE_HOME.
    """

    def test_bare_table_is_qualified(self):
        self.assertEqual(qualify("tech_jobs_20260911", "jobs"),
                         "jobs.public.tech_jobs_20260911")

    def test_already_qualified_name_passes_through(self):
        self.assertEqual(qualify("jobs.public.t", "jobs"), "jobs.public.t")

    def test_a_name_carrying_sql_is_rejected(self):
        for bad in ("jobs; DROP TABLE jobs --", "jobs WHERE 1=1", "j'obs",
                    "jobs.public", "a.b.c.d", "1abc", ""):
            with self.assertRaises(HotdataError, msg=bad):
                qualify(bad, "jobs")

    def test_column_names_are_checked_too(self):
        self.assertEqual(identifier("salary_max"), "salary_max")
        with self.assertRaises(HotdataError):
            identifier("salary_max, (SELECT 1)")

    def test_workspace_stays_under_rote_home(self):
        directory = workspace_dir("data-ai-hack")
        self.assertEqual(directory.name, "data-ai-hack")
        self.assertEqual(directory.parent.name, "workspaces")
        for bad in ("../../etc", "/etc", "a/b", "..", ""):
            with self.assertRaises(RoteError, msg=bad):
                workspace_dir(bad)


class CompanyProfileTests(unittest.TestCase):
    def test_a_known_company_gets_a_size_a_size_rule_can_bite_on(self):
        from insight.companies import profile
        from memory.prefs import Rule

        stripe = profile("Stripe")
        self.assertGreaterEqual(stripe["company_size"], 2000)
        rule = Rule("company_size", "gte", 2000, "penalty", 0.5)
        self.assertTrue(rule.matches(stripe))

    def test_an_unknown_company_has_no_size_and_is_not_matched(self):
        from insight.companies import profile
        from memory.prefs import Rule

        self.assertEqual(profile("Some Startup Nobody Listed"), {})
        # Not knowing a size is not the same as knowing it is small.
        self.assertFalse(Rule("company_size", "lt", 2000).matches({"company": "x"}))

    def test_coverage_names_the_companies_with_no_headcount(self):
        from insight.companies import coverage
        report = coverage(["Stripe", "Stripe", "Nobody Ltd"])
        self.assertEqual(report["missing"], ["Nobody Ltd"])


class GraphQueryTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        seed_candidate(self.store)
        upsert_job(self.store, {"id": "j1", "company": "Acme", "company_slug": "acme",
                                "title": "Senior ML Engineer", "source": "greenhouse"})
        upsert_requirements(self.store, "j1", [
            {"text": "5 years of LLM evals", "kind": "required", "skill": "LLM evals"},
            {"text": "Kubernetes depth", "kind": "required", "skill": "Kubernetes"},
        ])

    def test_claim_coverage_finds_the_gap(self):
        rows = self.store.run("claim_coverage", {"job_id": "j1"})
        self.assertEqual(len(rows), 2)
        gaps = [row for row in rows if row["is_gap"]]
        self.assertEqual(len(gaps), 1)
        self.assertIn("Kubernetes", gaps[0]["skill"])

    def test_unverified_claims_do_not_count_as_coverage(self):
        # cl-c claims Kubernetes but is unverified, so it must not close the gap.
        rows = {row["skill"]: row for row in self.store.run("claim_coverage", {"job_id": "j1"})}
        self.assertEqual(rows["Kubernetes"]["supporting_claims"], [])

    def test_skill_canonicalization_merges_aliases(self):
        self.assertEqual(canonical_skill("AI evals"), canonical_skill("LLM Evals"))

    def test_warm_path_runs_through_a_reply(self):
        from memory.writers import record_application, record_outcome
        upsert_job(self.store, {"id": "j0", "company": "Stripe", "company_slug": "stripe",
                                "title": "ML Engineer", "source": "greenhouse"})
        upsert_requirements(self.store, "j0", [
            {"text": "LLM evals", "kind": "required", "skill": "LLM evals"}])
        record_application(self.store, "j0", day=3)
        record_outcome(self.store, "j0", "replied", day=5)
        rows = self.store.run("warm_path", {"limit": 5})
        self.assertTrue(any(row["job_id"] == "j1" for row in rows))

    def test_unknown_query_name_raises(self):
        with self.assertRaises(KeyError):
            self.store.run("drop_everything")


class CitationTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        seed_candidate(self.store)

    def test_valid_pack_passes(self):
        text = ("I led evaluation tooling for an LLM product [claim:cl-a]. "
                "I would be glad to bring that to your team.")
        result = claims_mod.validate_citations(self.store, text)
        self.assertTrue(result.ok, result.reason())

    def test_uncited_fact_fails(self):
        text = "I led a team of 40 engineers at Acme."
        result = claims_mod.validate_citations(self.store, text)
        self.assertFalse(result.ok)
        self.assertTrue(result.uncited_facts)

    def test_citation_to_an_unverified_claim_fails(self):
        result = claims_mod.validate_citations(
            self.store, "I have deep Kubernetes experience [claim:cl-c].")
        self.assertFalse(result.ok)
        self.assertEqual(result.unverified_claims, ["cl-c"])

    def test_citation_to_a_made_up_claim_fails(self):
        result = claims_mod.validate_citations(
            self.store, "I shipped a payments platform [claim:cl-nope].")
        self.assertFalse(result.ok)
        self.assertEqual(result.unknown_claims, ["cl-nope"])

    def test_render_splits_copy_from_provenance(self):
        rendered = claims_mod.render_pack_text(
            "I led evals [claim:cl-a].", {"cl-a": "Led evaluation tooling."})
        self.assertNotIn("[claim:", rendered["copy"])
        self.assertEqual(rendered["provenance"][0]["claim_id"], "cl-a")


class AnswerLadderTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        seed_candidate(self.store)

    def test_unknown_fact_is_asked(self):
        self.assertEqual(resolve(self.store, "What is your notice period?").source, "ask")

    def test_stored_answer_is_never_asked_again(self):
        set_standard_answer(self.store, "notice_period", "Four weeks.")
        resolution = resolve(self.store, "What is your notice period?")
        self.assertEqual(resolution.source, "standard_answer")
        self.assertEqual(resolution.text, "Four weeks.")

    def test_differently_worded_question_hits_the_same_key(self):
        set_standard_answer(self.store, "work_authorization", "Yes, no sponsorship needed.")
        for wording in ("Are you authorized to work in the US?",
                        "Do you require visa sponsorship?"):
            self.assertEqual(resolve(self.store, wording).source, "standard_answer")

    def test_derivation_needs_approval_and_names_its_claims(self):
        resolution = resolve(self.store, "Do you have experience with Terraform?")
        self.assertEqual(resolution.source, "generated")
        self.assertTrue(resolution.needs_approval)
        self.assertIn("cl-b", resolution.from_claims)


class PreferenceTests(unittest.TestCase):
    def signals(self, n: int, **extra):
        return [{"kind": "not_for_me", "reason_tags": ["too_senior"], "day": i,
                 "title": "Staff Engineer", "company": f"Co{i}", **extra}
                for i in range(n)]

    def test_two_signals_do_not_make_a_rule(self):
        self.assertEqual(induce(self.signals(2)), [])

    def test_three_signals_propose_a_hypothesis(self):
        proposals = induce(self.signals(3))
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].status, "hypothesis")
        self.assertIn("staff", str(proposals[0].rule.value))

    def test_skip_never_teaches(self):
        skips = [dict(s, kind="skip") for s in self.signals(5)]
        self.assertEqual(induce(skips), [])

    def test_a_rejected_rule_is_never_proposed_again(self):
        rule = induce(self.signals(3))[0].rule
        self.assertEqual(induce(self.signals(3), rejected_keys=[rule.key()]), [])

    def test_rules_are_structured_and_validated(self):
        with self.assertRaises(ValueError):
            Rule("vibes", "lt", 3)
        with self.assertRaises(ValueError):
            Rule("company_size", "approximately", 3)

    def test_preference_fit_is_neutral_without_a_match(self):
        rules = [Rule("company_size", "gte", 2000, "penalty", 0.5)]
        score, reasons = preference_fit(rules, {"company_size": 50})
        self.assertEqual(score, 0.5)
        self.assertEqual(reasons, [])
        score, reasons = preference_fit(rules, {"company_size": 9000})
        self.assertLess(score, 0.5)
        self.assertTrue(reasons)

    def test_hypothesis_changes_nothing_until_confirmed(self):
        from memory.prefs import active_rules, confirm, propose
        store = make_store()
        seed_candidate(store)
        proposal = induce(self.signals(3))[0]
        propose(store, proposal)
        self.assertEqual(active_rules(store), [])
        confirm(store, proposal.preference_id)
        self.assertEqual(len(active_rules(store)), 1)


class PredictionTests(unittest.TestCase):
    def test_coverage_weights_required_above_preferred(self):
        rows = [{"kind": "required", "supporting_claims": []},
                {"kind": "preferred", "supporting_claims": ["cl-a"]}]
        coverage, supported, total, gaps = requirement_coverage(rows)
        self.assertAlmostEqual(coverage, 1 / 3)
        self.assertEqual((supported, total), (1, 2))

    def test_score_is_the_documented_weighted_sum(self):
        rows = [{"kind": "required", "supporting_claims": ["cl-a"], "skill": "Python"}]
        prediction = predict({"id": "j1"}, rows, [], similarity=1.0)
        # coverage 1.0, similarity 1.0, preference 0.5 -> .45 + .30 + .125
        self.assertAlmostEqual(prediction.score, 0.875, places=3)
        self.assertEqual(prediction.predicted, "keep")

    def test_gaps_are_named_not_hidden(self):
        rows = [{"kind": "required", "supporting_claims": [], "skill": "Kubernetes"}]
        prediction = predict({"id": "j1"}, rows, [])
        self.assertEqual(prediction.gaps, ["Kubernetes"])
        self.assertIn("Kubernetes", " ".join(prediction.reasons))


class SlateTests(unittest.TestCase):
    def ranked(self, n: int):
        return [predict({"id": f"j{i}"}, [], [], similarity=1 - i / n) for i in range(n)]

    def test_two_of_five_come_from_outside_the_top(self):
        ranked = self.ranked(20)
        slate = build_slate(ranked)
        self.assertEqual(len(slate), 5)
        top_ids = {p.job_id for p in ranked[:3]}
        self.assertEqual(len({p.job_id for p in slate} & top_ids), 3)
        self.assertEqual(len([p for p in slate if p.job_id not in top_ids]), 2)

    def test_slate_is_deterministic(self):
        first = [p.job_id for p in build_slate(self.ranked(20))]
        second = [p.job_id for p in build_slate(self.ranked(20))]
        self.assertEqual(first, second)

    def test_short_pool_does_not_crash(self):
        self.assertEqual(len(build_slate(self.ranked(2))), 2)


class MetricsTests(unittest.TestCase):
    def test_skip_class_accuracy_ignores_easy_keeps(self):
        responses = [{"predicted": "keep", "actual": "keep"}] * 4 + [
            {"predicted": "keep", "actual": "skip"}]
        self.assertEqual(skip_class_accuracy(responses), 0.0)

    def test_no_skips_reports_none_not_zero(self):
        self.assertIsNone(skip_class_accuracy([{"predicted": "keep", "actual": "keep"}]))

    def test_mode_is_derived_from_what_happened(self):
        metrics = RunMetrics(day=1)
        metrics.note_replayed("p1")
        self.assertEqual(metrics.settle_mode(), "full_replay")
        metrics.note_reasoned()
        self.assertEqual(metrics.settle_mode(), "partial_replay")

    def test_row_has_exactly_the_runs_columns(self):
        from agent.schema import RUNS_COLUMNS
        self.assertEqual(tuple(RunMetrics(day=1).row()), RUNS_COLUMNS)


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        seed_candidate(self.store)

    def seed_record(self, correct: int, wrong: int) -> None:
        day = 0
        for index in range(correct + wrong):
            job_id = f"j{index}"
            upsert_job(self.store, {"id": job_id, "company": "Acme",
                                    "company_slug": "acme", "title": "Role"})
            actual = "skip"
            predicted = "skip" if index < correct else "keep"
            day += 1
            record_prediction(self.store, job_id, predicted, day=day, score=0.4)
            self.store.merge_edge(self.store.candidate_node(), "PREDICTED_KEEP",
                                  nid("job", job_id), actual=actual)

    def test_not_ready_without_a_record(self):
        readiness = evaluate(self.store, D1_SHORTLIST)
        self.assertFalse(readiness.ready)
        self.assertIsNone(readiness.prompt())

    def test_ready_cites_a_real_count(self):
        self.seed_record(correct=14, wrong=1)
        readiness = evaluate(self.store, D1_SHORTLIST)
        self.assertTrue(readiness.ready)
        self.assertIn("15", readiness.prompt())

    def test_a_poor_record_never_becomes_ready(self):
        self.seed_record(correct=7, wrong=8)
        self.assertFalse(evaluate(self.store, D1_SHORTLIST).ready)

    def test_two_overrides_hand_the_domain_back(self):
        grant(self.store, D1_SHORTLIST)
        note_decision(self.store, D1_SHORTLIST, overridden=True)
        result = note_decision(self.store, D1_SHORTLIST, overridden=True)
        self.assertTrue(result["downgraded"])
        self.assertEqual(result["state"], "Supervised")

    def test_facts_are_never_autonomous(self):
        from memory.autonomy import NEVER_AUTONOMOUS
        joined = " ".join(NEVER_AUTONOMOUS).lower()
        self.assertIn("submit", joined)
        self.assertIn("standardanswer", joined.replace(" ", ""))


class DigestTests(unittest.TestCase):
    rows = [{"job_id": "j1"}, {"job_id": "j2"}, {"job_id": "j3"}, {"job_id": "j4"}]

    def test_numbered_reply_produces_the_same_signals_as_buttons(self):
        signals = parse_reply("keep 1,3; not-for-me 2 too senior; skip 4", self.rows)
        by_id = {s["job_id"]: s for s in signals}
        self.assertEqual(by_id["j1"]["kind"], "keep")
        self.assertEqual(by_id["j2"]["kind"], "not_for_me")
        self.assertEqual(by_id["j2"]["reason_tags"], ["too_senior"])
        self.assertEqual(by_id["j4"]["kind"], "skip")

    def test_out_of_range_positions_are_ignored(self):
        self.assertEqual(parse_reply("keep 99", self.rows), [])

    def test_free_text_maps_onto_chips(self):
        self.assertIn("comp", tags_from_text("the salary is too low"))
        self.assertEqual(tags_from_text("just because"), ["other"])


class SyncTests(unittest.TestCase):
    # The payload shapes below are what the live tenant actually returns after a
    # cognify with our graphModel — anonymous nodes labelled `<Type>_<uuid>` with
    # the real text in properties, and edges labelled with the schema's own
    # property names.
    COGNEE_PAYLOAD = {
        "nodes": [
            {"id": "cand-1", "type": "Candidate", "label": "Alex Rivera",
             "properties": {"base_location": "Berlin"}},
            {"id": "claim-1", "type": "Claim", "label": "Claim_claim-1",
             "properties": {"text": "Led the evaluation tooling for an LLM assistant."}},
            {"id": "skill-1", "type": "Skill", "label": "AI evals", "properties": {}},
            {"id": "root", "type": "CandidateGraph", "label": "CandidateGraph_root",
             "properties": {}},
        ],
        "edges": [
            {"source": "root", "target": "cand-1", "label": "candidates"},
            {"source": "cand-1", "target": "claim-1", "label": "claims"},
            {"source": "claim-1", "target": "skill-1", "label": "skills"},
            {"source": "cand-1", "target": "skill-1", "label": "skills"},
        ],
    }

    def test_the_same_property_name_means_two_different_edges(self):
        store = make_store()
        sync_graph(store, self.COGNEE_PAYLOAD)
        types = store.graph.stats()["edge_types"]
        self.assertEqual(types.get("HAS_CLAIM"), 1)
        self.assertEqual(types.get("DEMONSTRATES"), 1, "a claim's skills are demonstrated")
        self.assertEqual(types.get("HAS_SKILL"), 1, "a candidate's skills are held")

    def test_an_anonymous_node_takes_its_name_from_its_text(self):
        store = make_store()
        sync_graph(store, self.COGNEE_PAYLOAD)
        claim = store.graph.by_label("Claim")[0]
        self.assertIn("evaluation tooling", claim.props["text"])
        self.assertNotIn("Claim_", claim.props["text"])

    def test_the_extracted_candidate_is_the_candidate(self):
        store = make_store()
        sync_graph(store, self.COGNEE_PAYLOAD)
        self.assertEqual(len(store.graph.by_label("Candidate")), 1)
        self.assertIsNotNone(store.graph.node(store.candidate_node()))

    def test_extraction_does_not_fork_or_downgrade_a_verified_claim(self):
        from memory.claims import Claim as ClaimRow, store_claims as store_rows

        store = make_store()
        text = "Led the evaluation tooling for an LLM assistant."
        store_rows(store, [ClaimRow(claims_mod.claim_id_for(text), text,
                                    status="verified", source_doc="resume")])
        sync_graph(store, self.COGNEE_PAYLOAD)
        claims = store.graph.by_label("Claim")
        self.assertEqual(len(claims), 1, "the same sentence is one claim, not two")
        self.assertEqual(claims[0].props["status"], "verified",
                         "an extraction must never downgrade a human's word")

    def test_cognee_plumbing_is_dropped_and_typed_nodes_are_kept(self):
        store = make_store()
        payload = {
            "nodes": [
                {"id": "n1", "type": "Skill", "name": "AI evals"},
                {"id": "n2", "type": "Claim", "name": "Led evals", "properties": {}},
                {"id": "n3", "type": "DocumentChunk", "name": "chunk 1"},
            ],
            "edges": [{"source": "n2", "target": "n1", "relationship_name": "demonstrates"},
                      {"source": "n3", "target": "n1", "relationship_name": "mentions"}],
        }
        result = sync_graph(store, payload)
        self.assertEqual(result["nodes_merged"], 2)
        self.assertEqual(result["nodes_skipped"], 1)
        self.assertEqual(result["edges_merged"], 1)
        # The skill lands on the id our own writers would have used.
        self.assertIsNotNone(store.graph.node(nid("skill", canonical_skill("AI evals"))))


class PlayTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.index = PlayIndex(self.store, rote=_NoRote())

    def test_first_encounter_is_a_miss(self):
        fingerprint = fingerprint_ingest("greenhouse", {"id": 1, "title": "x"})
        self.assertEqual(self.index.find("ingest-ats", fingerprint).mode, "none")

    def test_same_shape_replays_exactly(self):
        fingerprint = fingerprint_ingest("greenhouse", {"id": 1, "title": "x"})
        self.index.register("ingest-greenhouse-v1", "ingest-ats", fingerprint)
        self.assertEqual(self.index.find("ingest-ats", fingerprint).mode, "exact")

    def test_one_novel_field_is_a_partial_match(self):
        known = fingerprint_task("apply-pack", ["job_id", "candidate_id", "q:visa"])
        self.index.register("apply-pack-v1", "apply-pack", known)
        wider = fingerprint_task("apply-pack",
                                 ["job_id", "candidate_id", "q:visa", "q:relocation"])
        match = self.index.find("apply-pack", wider)
        self.assertEqual(match.mode, "partial")
        self.assertEqual(match.novel_fields, ("q:relocation",))

    def test_a_stale_play_is_not_replayed(self):
        fingerprint = fingerprint_ingest("greenhouse", {"id": 1})
        self.index.register("ingest-greenhouse-v1", "ingest-ats", fingerprint)
        self.index.note_run("ingest-greenhouse-v1", success=False)
        self.assertEqual(self.index.find("ingest-ats", fingerprint).mode, "none")

    def test_record_step_refuses_a_command_string(self):
        from mcp_server.rote import Rote
        rote = Rote()
        with self.assertRaises(ValueError):
            rote.record_step([])
        with self.assertRaises(ValueError):
            rote.record_step(["curl", 1])  # type: ignore[list-item]


class _NoRote:
    """Stand-in for the CLI: the registry lookup is not what these tests check."""

    def search(self, text, registry=True):
        return []


class SignalTests(unittest.TestCase):
    def test_only_three_signal_kinds_exist(self):
        store = make_store()
        seed_candidate(store)
        upsert_job(store, {"id": "j1", "company": "Acme", "company_slug": "acme",
                           "title": "Role"})
        with self.assertRaises(ValueError):
            record_signal(store, "j1", "maybe", day=1)

    def test_not_for_me_writes_a_rejection_edge(self):
        store = make_store()
        seed_candidate(store)
        upsert_job(store, {"id": "j1", "company": "Acme", "company_slug": "acme",
                           "title": "Staff Engineer"})
        record_signal(store, "j1", "not_for_me", day=2, reason_tags=["too_senior"],
                      reason_text="too senior")
        rows = store.run("rejection_reasons", {"limit": 5})
        self.assertEqual(rows[0]["job_id"], "j1")
        self.assertEqual(rows[0]["tags"], ["too_senior"])


class GraphSerializationTests(unittest.TestCase):
    def test_round_trip_preserves_nodes_and_edges(self):
        store = make_store()
        seed_candidate(store)
        upsert_job(store, {"id": "j1", "company": "Acme", "company_slug": "acme",
                           "title": "Role"})
        payload = json.loads(json.dumps(store.graph.to_dict(), default=str))
        rebuilt = Graph.from_dict(payload)
        self.assertEqual(rebuilt.stats()["nodes"], store.graph.stats()["nodes"])
        self.assertEqual(rebuilt.stats()["edges"], store.graph.stats()["edges"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
