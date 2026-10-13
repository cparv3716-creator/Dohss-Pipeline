import tempfile
import unittest
from pathlib import Path

from application_pipeline.eligibility import ELIGIBLE, INELIGIBLE, UNKNOWN_REVIEW, evaluate_eligibility
from application_pipeline.matching import MatchResult, match_job
from candidate import CandidateProfile
from tests.helpers import verified, write_profile


class EligibilityAndMatchingTests(unittest.TestCase):
    def _profile(self, tmp, **facts):
        return CandidateProfile.load(write_profile(Path(tmp) / "profile.yaml", **facts))

    def test_experience_requirement_is_hard_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(tmp, years_of_experience=verified(2))
            result = evaluate_eligibility(profile, {"required_experience_min": 5})
            self.assertEqual(result.status, INELIGIBLE)
            self.assertEqual(result.reasons[0]["code"], "EXPERIENCE_TOO_LOW")

    def test_unknown_experience_requires_review_not_invention(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(tmp)
            result = evaluate_eligibility(profile, {"required_experience_min": 2})
            self.assertEqual(result.status, UNKNOWN_REVIEW)
            self.assertIn("EXPERIENCE_UNKNOWN", [reason["code"] for reason in result.reasons])

    def test_mandatory_missing_skill_is_ineligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(tmp, skills=verified(["Python"]))
            result = evaluate_eligibility(profile, {"required_skills": ["Python", "SQL"]})
            self.assertEqual(result.status, INELIGIBLE)

    def test_candidate_exclusion_has_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(tmp, application_preferences=verified({"excluded_companies": ["Example Corp"]}))
            result = evaluate_eligibility(profile, {"company": "Example Corp"})
            self.assertEqual(result.status, INELIGIBLE)

    def test_work_authorization_by_country(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(tmp, work_authorization=verified({"india": False}))
            job = {
                "location": "Mumbai, India",
                "full_description": "Applicants must be authorized to work in India.",
            }
            result = evaluate_eligibility(profile, job)
            self.assertEqual(result.status, INELIGIBLE)
            self.assertIn("WORK_AUTHORIZATION_MISSING", [reason["code"] for reason in result.reasons])

    def test_deterministic_match_uses_verified_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(
                tmp, skills=verified(["Python", "SQL"]),
                target_roles=verified(["Data Analyst"]),
            )
            job = {"title": "Data Analyst", "required_skills": ["Python", "SQL"], "full_description": "Analyze data with Python SQL"}
            eligibility = evaluate_eligibility(profile, job)
            result = match_job(profile, job, eligibility)
            self.assertEqual(eligibility.status, ELIGIBLE)
            self.assertGreaterEqual(result.match_score, 70)
            self.assertEqual(result.recommendation, "APPLY")

    def test_match_result_validation(self):
        with self.assertRaises(ValueError):
            MatchResult(101, "APPLY", 90, [], [], "", [])
        with self.assertRaises(ValueError):
            MatchResult(50, "MAYBE", 90, [], [], "", [])

    def test_hard_failure_forces_skip_regardless_of_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(tmp, skills=verified(["Python"]), years_of_experience=verified(0))
            job = {"required_skills": ["Python"], "required_experience_min": 5}
            eligibility = evaluate_eligibility(profile, job)
            result = match_job(profile, job, eligibility)
            self.assertEqual(result.recommendation, "SKIP")


if __name__ == "__main__":
    unittest.main()
