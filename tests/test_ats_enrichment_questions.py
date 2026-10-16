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

    def test_unicode_entry_level_range_keeps_zero_minimum(self):
        page = """
        <html><body><main>
        <h2>What we're looking for</h2>
        <p>0–2 years of experience. Recent graduates are encouraged to apply. SQL.</p>
        </main></body></html>
        """
        parsed = parse_job_detail(page, "https://jobs.lever.co/acme/1")
        self.assertEqual(parsed["required_experience_min"], 0)
        self.assertEqual(parsed["required_experience_max"], 2)
        self.assertIn("SQL", parsed["required_skills"])

    def test_description_tools_are_not_promoted_to_mandatory_without_requirements(self):
        page = "<html><body><main>Work with adjacent Python and SQL teams.</main></body></html>"
        parsed = parse_job_detail(page, "https://example.test/job")
        self.assertEqual(parsed["required_skills"], [])

    def test_question_classification(self):
        skill = classify_question("How many years have you worked with Python?")
        self.assertEqual(skill.question_type, "SKILL_EXPERIENCE")
        self.assertEqual(skill.skill, "Python")
        joining = classify_question("When can you join?")
        self.assertIn(joining.question_type, ("NOTICE_PERIOD", "AVAILABILITY"))
        self.assertEqual(classify_question("What is your expected salary?").question_type, "EXPECTED_CTC")
        self.assertEqual(classify_question("What technical skills have you used?").question_type, "VERIFIED_SKILLS")
        self.assertEqual(classify_question("Preferred First Name").question_type, "PREFERRED_NAME")
        self.assertEqual(
            classify_question("Are you lawfully authorized to work in the country where this job is located?").question_type,
            "WORK_AUTHORIZATION",
        )
        self.assertEqual(
            classify_question("Will you require sponsorship in this country?").question_type,
            "VISA_SPONSORSHIP",
        )
        self.assertEqual(classify_question("What are your salary expectations?").question_type, "EXPECTED_CTC")
        self.assertEqual(classify_question("Location (City)").question_type, "CURRENT_CITY")
        self.assertEqual(classify_question("Have you worked here in the past 10 years?").question_type, "OTHER")
        self.assertEqual(classify_question("School").question_type, "UNIVERSITY")
        self.assertEqual(classify_question("End date year").question_type, "GRADUATION_YEAR")
        self.assertEqual(classify_question("Start date year").question_type, "OTHER")


if __name__ == "__main__":
    unittest.main()
