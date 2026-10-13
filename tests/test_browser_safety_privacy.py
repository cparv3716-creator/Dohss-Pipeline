import unittest

from application_pipeline.browser.models import AnswerStatus, ApplicationField
from application_pipeline.browser.safety import SubmissionGuard
from application_pipeline.browser.session import sanitize_html_snapshot


class BrowserSafetyPrivacyTests(unittest.TestCase):
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
            '<input value="private@example.test" checked><textarea>secret text</textarea><option selected>India</option>'
        )
        self.assertNotIn("private@example.test", sanitized)
        self.assertNotIn("secret text", sanitized)
        self.assertNotIn("checked", sanitized)
        self.assertNotIn("selected", sanitized)


if __name__ == "__main__":
    unittest.main()
