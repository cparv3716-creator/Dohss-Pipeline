import json
import tempfile
import unittest
from pathlib import Path

from application_pipeline.browser.inspector import (
    detect_authentication_blockers,
    extract_fields_from_html,
    normalize_fields,
    normalize_question,
    resolve_answers,
)
from application_pipeline.browser.models import AnswerStatus
from application_pipeline.browser.metrics import required_detection_metrics
from application_pipeline.browser.safety import ResumePolicy, SafeFillPolicy, SubmissionGuard
from application_pipeline.ats.base import _matches, _unique_option
from application_pipeline.questions import classify_question
from candidate import AnswerBank, CandidateProfile
from tests.helpers import answer, verified, write_answers, write_profile


FIXTURES = Path(__file__).parent / "fixtures"


class BrowserInspectorTests(unittest.TestCase):
    def test_logical_id_survives_react_rerender_ids(self):
        common = {
            "name": "", "tag": "input", "type": "text", "label": "Degree",
            "selector": "#dynamic", "required": True, "required_source": "aria",
            "visible": True, "enabled": True, "group_key": "education-question",
        }
        first = normalize_fields([{**common, "id": "react-select-12", "custom_select": True}])[0]
        second = normalize_fields([{**common, "id": "react-select-47", "custom_select": True}])[0]
        self.assertEqual(first.logical_id, second.logical_id)

    def test_semantic_option_matching_is_conservative(self):
        self.assertTrue(_matches("MA Economics", {"value": "masters", "label": "Master's degree"}, "DEGREE"))
        self.assertTrue(_matches("M.A. Economics", {"value": "ma", "label": "Master of Arts (M.A.)"}, "DEGREE"))
        self.assertTrue(_matches(
            "Indian Institute of Technology Madras",
            {"value": "iitm", "label": "Indian Institute of Technology Madras (IITM)"},
            "UNIVERSITY",
        ))
        self.assertTrue(_matches("immediate", {"value": "0", "label": "0 days"}, "NOTICE_PERIOD"))
        self.assertFalse(_matches("15-30 LPA", {"value": "10-20", "label": "10–20 LPA"}, "EXPECTED_CTC"))
        self.assertFalse(_matches("Chennai", {"value": "Tamil Nadu", "label": "Tamil Nadu"}, "CURRENT_CITY"))
        ambiguous = [{"value": "yes", "label": "Yes"}, {"value": "true", "label": "Yes"}]
        self.assertIsNone(_unique_option(True, ambiguous, "WORK_AUTHORIZATION"))

    def test_sanitized_greenhouse_reference_precision_recall_and_deduplication(self):
        fixture = FIXTURES / "greenhouse_semantic_reference.html"
        labels = json.loads((FIXTURES / "greenhouse_semantic_reference.json").read_text(encoding="utf-8"))
        fields = extract_fields_from_html(fixture.read_text(encoding="utf-8"), fixture.as_uri())
        metrics = required_detection_metrics(labels["expected"], fields)
        self.assertGreaterEqual(metrics.precision, 0.95)
        self.assertGreaterEqual(metrics.recall, 0.95)
        self.assertEqual(len({field.logical_id for field in fields}), len(fields))
        self.assertEqual(sum(field.label == "Degree" for field in fields), 1)
        self.assertEqual(sum(field.label == "City" for field in fields), 2)
        state = next(field for field in fields if field.label == "State")
        self.assertFalse(state.visible)
        self.assertEqual(state.answer_status, AnswerStatus.HIDDEN_INACTIVE.value)

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
