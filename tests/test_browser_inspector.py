import tempfile
import unittest
from pathlib import Path

from application_pipeline.browser.inspector import (
    detect_authentication_blockers,
    extract_fields_from_html,
    normalize_question,
    resolve_answers,
)
from application_pipeline.browser.models import AnswerStatus
from application_pipeline.browser.safety import ResumePolicy, SafeFillPolicy, SubmissionGuard
from application_pipeline.questions import classify_question
from candidate import AnswerBank, CandidateProfile
from tests.helpers import answer, verified, write_answers, write_profile


FIXTURES = Path(__file__).parent / "fixtures"


class BrowserInspectorTests(unittest.TestCase):
    def test_greenhouse_field_extraction_and_control_parsing(self):
        fields = extract_fields_from_html((FIXTURES / "greenhouse_application.html").read_text(encoding="utf-8"))
        types = {field.field_type for field in fields}
        self.assertTrue({"text", "email", "phone", "select", "radio", "checkbox", "file", "textarea"}.issubset(types))
        notice = next(field for field in fields if field.field_id == "notice")
        self.assertEqual([item["label"] for item in notice.options], ["Select", "30 days", "60 days"])
        radio = next(field for field in fields if field.field_id == "authorized")
        self.assertEqual(radio.raw_question, "Are you authorized to work in India?")
        self.assertEqual([item["value"] for item in radio.options], ["yes", "no"])
        checkbox = next(field for field in fields if field.field_id == "updates")
        self.assertEqual(checkbox.field_type, "checkbox")

    def test_lever_question_normalization_and_skill(self):
        fields = extract_fields_from_html((FIXTURES / "lever_application.html").read_text(encoding="utf-8"))
        skill = next(field for field in fields if field.field_id == "python_years")
        self.assertEqual(skill.canonical_question_type, "SKILL_EXPERIENCE")
        self.assertEqual(skill.skill, "Python")
        self.assertEqual(normalize_question("  Expected salary *  "), "Expected salary")

    def test_safe_answer_lookup_sensitive_and_unknown_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = CandidateProfile.load(write_profile(root / "profile.yaml"))
            bank = AnswerBank.load(write_answers(root / "answers.yaml", {
                "EMAIL": answer("candidate@example.test"),
                "EXPECTED_CTC": answer("100", sensitivity="highly_sensitive"),
            }))
            fields = extract_fields_from_html((FIXTURES / "lever_application.html").read_text(encoding="utf-8"))
            resolve_answers(fields, profile, bank, SafeFillPolicy(minimum_confidence=85))
            email = next(field for field in fields if field.field_id == "email")
            salary = next(field for field in fields if field.field_id == "salary")
            name = next(field for field in fields if field.field_id == "name")
            self.assertEqual(email.answer_status, AnswerStatus.PROPOSED.value)
            self.assertEqual(salary.answer_status, AnswerStatus.SKIPPED_SENSITIVE.value)
            self.assertEqual(name.answer_status, AnswerStatus.SKIPPED_UNKNOWN.value)
            self.assertEqual(email.answer_source, "test")
            self.assertTrue(email.answer_verified)

    def test_authentication_blockers(self):
        html = """
        <html><body><h1>Sign in</h1><input type=password>
        <div class=g-recaptcha data-sitekey=x></div>
        <p>Enter the one-time password for MFA verification</p><input name=otp></body></html>
        """
        blockers = detect_authentication_blockers(html)
        self.assertIn("LOGIN_REQUIRED", blockers)
        self.assertIn("CAPTCHA_REQUIRED", blockers)
        self.assertIn("MFA_REQUIRED", blockers)
        self.assertIn("OTP_REQUIRED", blockers)

    def test_submission_guard_and_no_submit_interface(self):
        from application_pipeline.ats.greenhouse import GreenhouseAdapter
        adapter = GreenhouseAdapter()
        self.assertFalse(hasattr(adapter, "submit"))
        self.assertTrue(SubmissionGuard.is_submission_request("POST", "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1/applications"))
        self.assertFalse(SubmissionGuard.is_submission_request("GET", "https://boards.greenhouse.io/acme/jobs/1"))

    def test_resume_path_restrictions(self):
        private_root = Path("data/private/test_resumes").resolve()
        private_root.mkdir(parents=True, exist_ok=True)
        resume = private_root / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 test")
        try:
            artifact = ResumePolicy(str(resume), [str(private_root)]).validate()
            self.assertEqual(artifact.path, resume)
            with tempfile.TemporaryDirectory() as tmp:
                outside = Path(tmp) / "resume.pdf"
                outside.write_bytes(b"%PDF-1.4 test")
                with self.assertRaisesRegex(ValueError, "outside allowed"):
                    ResumePolicy(str(outside), [str(private_root)]).validate()
        finally:
            resume.unlink(missing_ok=True)
            private_root.rmdir()


if __name__ == "__main__":
    unittest.main()
