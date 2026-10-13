import unittest

from application_pipeline.ats import detect_ats
from application_pipeline.enrichment import JobDetailEnricher, parse_job_detail
from application_pipeline.questions import classify_question


class AtsEnrichmentQuestionTests(unittest.TestCase):
    def test_common_ats_detection(self):
        cases = {
            "https://boards.greenhouse.io/acme/jobs/1": "Greenhouse",
            "https://jobs.lever.co/acme/1": "Lever",
            "https://jobs.ashbyhq.com/acme/1": "Ashby",
            "https://acme.wd5.myworkdayjobs.com/job/1": "Workday",
            "https://jobs.smartrecruiters.com/Acme/1": "SmartRecruiters",
            "https://linkedin.com/jobs/view/1": "LinkedIn",
            "https://internshala.com/job/detail/1": "Internshala",
            "https://naukri.com/job-listings-1": "Naukri",
            "https://acme.example/careers/analyst": "Custom/Company ATS",
            "https://example.org/page": "Unknown",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_ats(url), expected)

    def test_structured_job_parsing(self):
        page = """
        <html><head><script type="application/ld+json">
        {"@type":"JobPosting","url":"https://boards.greenhouse.io/acme/jobs/1",
         "employmentType":"FULL_TIME","description":"<h2>Responsibilities</h2><p>Analyze data.</p><h2>Requirements</h2><p>3-5 years experience. Python and SQL. Bachelor's degree.</p><h2>Preferred Qualifications</h2><p>Tableau.</p>"}
        </script></head><body></body></html>
        """
        parsed = parse_job_detail(page, "https://acme.example/job/1")
        self.assertEqual(parsed["ats_provider"], "Greenhouse")
        self.assertEqual(parsed["required_experience_min"], 3)
        self.assertEqual(parsed["required_experience_max"], 5)
        self.assertIn("Python", parsed["required_skills"])
        self.assertIn("Bachelor", parsed["education_requirement"])
        self.assertEqual(parsed["employment_type"], "Full-time")

    def test_enrichment_failure_is_returned_not_raised(self):
        def fail(_url):
            raise TimeoutError("offline")
        result = JobDetailEnricher(fetcher=fail).enrich({"url": "https://example.test/job"})
        self.assertEqual(result.status, "FAILED")
        self.assertIn("TimeoutError", result.error)

    def test_question_classification(self):
        skill = classify_question("How many years have you worked with Python?")
        self.assertEqual(skill.question_type, "SKILL_EXPERIENCE")
        self.assertEqual(skill.skill, "Python")
        joining = classify_question("When can you join?")
        self.assertIn(joining.question_type, ("NOTICE_PERIOD", "AVAILABILITY"))
        self.assertEqual(classify_question("What is your expected salary?").question_type, "EXPECTED_CTC")
        self.assertEqual(classify_question("What technical skills have you used?").question_type, "VERIFIED_SKILLS")


if __name__ == "__main__":
    unittest.main()
