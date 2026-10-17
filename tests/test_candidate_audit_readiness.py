import tempfile
import unittest
from pathlib import Path

from application_pipeline.browser.models import AnswerStatus, ApplicationField, ValidationIssue
from application_pipeline.browser.safety import ResumePolicy
from application_pipeline.candidate_audit import audit_candidate, format_candidate_audit
from application_pipeline.free_text import ConservativeGroundedDraftProvider, prepare_free_text
from application_pipeline.readiness import calculate_readiness
from candidate import AnswerBank, CandidateProfile
from tests.helpers import answer, verified, write_answers, write_profile


class CandidateAuditAndReadinessTests(unittest.TestCase):
    def test_candidate_audit_does_not_render_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = CandidateProfile.load(write_profile(
                root / "profile.yaml",
                full_name=verified("Sensitive Name"),
                email=verified("private@example.test"),
                phone=verified("9999999999"),
                skills=verified(["Python", "SQL"]),
            ))
            bank = AnswerBank.load(write_answers(root / "answers.yaml", {
                "EMAIL": answer("private@example.test"),
            }))
            rendered = format_candidate_audit(audit_candidate(profile, bank, ResumePolicy()))
            self.assertIn("identity: complete", rendered)
            self.assertIn("skills: 2 verified", rendered)
            self.assertNotIn("Sensitive Name", rendered)
            self.assertNotIn("private@example.test", rendered)
            self.assertNotIn("9999999999", rendered)

    def test_readiness_precedence_and_scoring(self):
        fields = [
            ApplicationField("email", "Email", "Email", "email", True, confidence=95, answer_status=AnswerStatus.FILLED.value),
            ApplicationField("salary", "Salary", "Expected salary", "text", True, answer_status=AnswerStatus.SKIPPED_SENSITIVE.value),
        ]
        result = calculate_readiness(fields)
        self.assertEqual(result.status, "NEEDS_INFORMATION")
        self.assertEqual(result.required_resolved, 1)
        blocked = calculate_readiness(fields, ["CAPTCHA_REQUIRED"])
        self.assertEqual(blocked.status, "NEEDS_USER_ACTION")
        self.assertLess(blocked.score, result.score)

    def test_free_text_requires_verified_provenance(self):
        class Provider:
            def generate(self, **_kwargs):
                return "Grounded draft", ["profile.projects"]

        draft = prepare_free_text(
            "Why this role?", "WHY_ROLE",
            {"projects": [{"name": "Verified project"}]},
            "A verified job description", Provider(),
        )
        self.assertEqual(draft.status, "PREPARED_REVIEW_REQUIRED")
        self.assertEqual(draft.provenance, ["profile.projects"])
        invalid = prepare_free_text(
            "Why this role?", "WHY_ROLE",
            {"projects": [{"name": "Verified project"}]},
            "A verified job description",
            type("BadProvider", (), {"generate": lambda self, **kwargs: ("Draft", ["invented.fact"])})(),
        )
        self.assertEqual(invalid.status, "NEEDS_REVIEW")

    def test_conservative_free_text_uses_only_declared_provenance(self):
        draft = prepare_free_text(
            "Describe a relevant project", "PROJECT_EXAMPLE",
            {"projects": [{"name": "Verified project", "result": "Verified result"}]},
            "The role requires analysis.", ConservativeGroundedDraftProvider(),
        )
        self.assertEqual(draft.status, "PREPARED_REVIEW_REQUIRED")
        self.assertEqual(draft.provenance, ["profile.projects"])
        self.assertIn("Verified project", draft.text)
        company = prepare_free_text(
            "Why this company?", "WHY_COMPANY", {"projects": ["Verified project"]},
            "Public job description", ConservativeGroundedDraftProvider(),
        )
        self.assertEqual(company.status, "NEEDS_REVIEW")

    def test_required_validation_error_prevents_ready(self):
        field = ApplicationField("email", "Email", "Email", "email", True, confidence=100, answer_status=AnswerStatus.FILLED.value)
        result = calculate_readiness([field], validation_issues=[ValidationIssue("email", "INVALID", "invalid", True)])
        self.assertEqual(result.status, "NEEDS_INFORMATION")


if __name__ == "__main__":
    unittest.main()
