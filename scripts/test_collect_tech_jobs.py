"""Checks for filtering, provenance and date semantics before a live load."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("collector", Path(__file__).with_name("collect-tech-jobs.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CollectorTests(unittest.TestCase):
    fetched = "2026-09-11T19:00:00+00:00"
    source = {"ats": "ashby", "slug": "example", "company": "Example", "url": "https://api.ashbyhq.com/posting-api/job-board/example"}

    def job(self, **changes):
        return dict({"id": "a", "title": "Software Engineer", "location": "San Francisco",
            "descriptionPlain": "Build services", "jobUrl": "https://jobs.ashbyhq.com/example/a",
            "publishedAt": "2026-09-11T01:00:00Z", "isListed": True}, **changes)

    def test_local_day_and_republication_are_not_first_publication(self):
        row = c.normalize(self.job(), self.source, self.fetched)
        self.assertFalse(row["published_today"])
        self.assertIsNone(row["first_published_today"])
        self.assertEqual(row["posted_at_kind"], "last_published")

    def test_unlisted_and_non_technical_roles_are_excluded(self):
        self.assertIsNone(c.normalize(self.job(isListed=False), self.source, self.fetched))
        self.assertIsNone(c.normalize(self.job(title="Technical Recruiter"), self.source, self.fetched))
        self.assertIsNone(c.normalize(self.job(title="Account Executive, AI"), self.source, self.fetched))

    def test_remote_does_not_imply_worldwide_eligibility(self):
        self.assertIsNone(c.normalize(self.job(location="London"), self.source, self.fetched))
        row = c.normalize(self.job(location="London", workplaceType="Remote"), self.source, self.fetched)
        self.assertTrue(row["remote"])
        self.assertEqual(row["region_match"], "remote_location_restrictions_apply")

    def test_lever_created_date_is_not_publication_date(self):
        source = dict(self.source, ats="lever")
        row = c.normalize(self.job(createdAt=1789146000000), source, self.fetched)
        self.assertIsNone(row["posted_at"])
        self.assertIsNotNone(row["source_created_at"])
        self.assertIsNone(row["published_today"])

    def test_greenhouse_first_publication_ignores_update_date(self):
        row = c.normalize(self.job(first_published="2026-09-10T18:00:00Z", updated_at=self.fetched),
                          dict(self.source, ats="greenhouse"), self.fetched)
        self.assertFalse(row["first_published_today"])

    def test_description_removes_active_html(self):
        self.assertEqual(c.text("&lt;p&gt;Build &amp;amp; test&lt;/p&gt;<script>evil()</script>"), "Build & test")

    def test_office_metadata_recovers_generic_location(self):
        row = c.normalize(self.job(location={"name": "Hybrid"}, offices=[{"name": "Austin, TX"}]),
                          dict(self.source, ats="greenhouse"), self.fetched)
        self.assertIn("Austin", row["location"])

    def test_business_roles_do_not_match_technical_department_names(self):
        for title in ["People Partner, Engineering", "Senior Manager, Infrastructure Asset Accounting", "Strategic Finance Lead - AI"]:
            self.assertIsNone(c.role(title))
        self.assertEqual(c.role("Finance Systems Engineer"), "infrastructure")
        self.assertEqual(c.role("Data Scientist, Finance"), "data_analytics")


if __name__ == "__main__":
    unittest.main()
