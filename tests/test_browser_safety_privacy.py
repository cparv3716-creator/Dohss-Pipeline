import tempfile
import unittest
from pathlib import Path

from application_pipeline.browser.models import AnswerStatus, ApplicationField
from application_pipeline.browser.safety import SubmissionGuard
from application_pipeline.browser.session import BrowserArtifacts, BrowserConfig, BrowserSession, sanitize_html_snapshot


class BrowserSafetyPrivacyTests(unittest.TestCase):
    def test_manual_handoff_requires_positive_captcha_token_evidence(self):
        class Locator:
            def evaluate_all(self, _script):
                return True

        class Page:
            def is_closed(self):
                return False
            def locator(self, _selector):
                return Locator()
            def wait_for_timeout(self, _milliseconds):
                return None

        class Artifacts:
            def log(self, *_args, **_kwargs):
                return None

        session = object.__new__(BrowserSession)
        session.config = BrowserConfig(headless=False, manual_handoff=True, manual_handoff_timeout_seconds=1)
        session.page = Page()
        session.artifacts = Artifacts()
        self.assertTrue(session.manual_user_handoff(["CAPTCHA_REQUIRED"], lambda: []))

    def test_network_guard_distinguishes_support_requests(self):
        benign = (
            ("POST", "https://boards.greenhouse.io/autocomplete", "query=x"),
            ("POST", "https://uploads.example.test/resume/upload", "binary"),
            ("POST", "https://api.example.test/validate", "field=email"),
        )
        for request in benign:
            self.assertFalse(SubmissionGuard.classify_request(*request).blocked)
        self.assertTrue(SubmissionGuard.classify_request(
            "POST", "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123", "first_name=x"
        ).blocked)
        self.assertTrue(SubmissionGuard.classify_request(
            "POST", "https://api.lever.co/v0/postings/abc", "name=x"
        ).blocked)

    def test_report_and_html_redact_candidate_values(self):
        field = ApplicationField(
            "email", "Email", "Email", "email", True,
            current_value="private@example.test", answer_status=AnswerStatus.FILLED.value,
        )
        payload = field.to_dict()
        self.assertEqual(payload["current_value"], "[REDACTED]")
        self.assertNotIn("private@example.test", str(payload))
        sanitized = sanitize_html_snapshot(
            '<input value="private@example.test" checked><textarea>secret text</textarea>'
            '<option selected>India</option><div class="select__single-value">Chennai</div>'
            '<span>Candidate_Resume.pdf</span><p>private@example.test +91 9876543210</p>'
        )
        self.assertNotIn("private@example.test", sanitized)
        self.assertNotIn("secret text", sanitized)
        self.assertNotIn("checked", sanitized)
        self.assertNotIn("selected", sanitized)
        self.assertNotIn("Chennai", sanitized)
        self.assertNotIn("Candidate_Resume.pdf", sanitized)
        self.assertNotIn("9876543210", sanitized)

    def test_structured_browser_log_redacts_contact_values_and_url_queries(self):
        Path("data/private").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir="data/private") as tmp:
            artifacts = BrowserArtifacts(tmp, 42)
            artifacts.log(
                "test", value="private@example.test", phone="+91 9876543210",
                url="https://example.test/form?token=secret",
            )
            content = artifacts.log_path.read_text(encoding="utf-8")
            self.assertNotIn("private@example.test", content)
            self.assertNotIn("9876543210", content)
            self.assertNotIn("token=secret", content)


if __name__ == "__main__":
    unittest.main()
