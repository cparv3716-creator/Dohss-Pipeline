import importlib.util
import tempfile
import unittest
from pathlib import Path

from application_pipeline.ats.greenhouse import GreenhouseAdapter
from application_pipeline.browser.inspector import resolve_answers
from application_pipeline.browser.models import AnswerStatus
from application_pipeline.browser.safety import ResumeArtifact, SafeFillPolicy
from application_pipeline.browser.session import BrowserConfig, BrowserSession
from candidate import AnswerBank, CandidateProfile
from tests.helpers import answer, write_answers, write_profile


FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_application.html"


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright package is not installed")
class LocalPlaywrightTests(unittest.TestCase):
    def test_real_chromium_safe_fill_upload_and_submit_guard(self):
        private_parent = Path("data/private")
        private_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private_parent) as tmp:
            root = Path(tmp).resolve()
            resume = root / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4 synthetic test artifact")
            profile = CandidateProfile.load(write_profile(root / "profile.yaml"))
            bank = AnswerBank.load(write_answers(root / "answers.yaml", {
                "EMAIL": answer("candidate@example.test"),
                "NOTICE_PERIOD": answer("30 days"),
                "WORK_AUTHORIZATION": answer(True, sensitivity="highly_sensitive"),
            }))
            config = BrowserConfig(headless=True, artifacts_root=str(root), save_html=False)
            try:
                session_context = BrowserSession(config, 999)
                session = session_context.__enter__()
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    self.skipTest("Playwright Chromium is not installed")
                raise
            try:
                adapter = GreenhouseAdapter()
                adapter.open_application(session, FIXTURE.resolve().as_uri())
                inspection = adapter.inspect_form(session)
                policy = SafeFillPolicy(
                    minimum_confidence=85,
                    allowed_sensitive_types={"WORK_AUTHORIZATION"},
                    allowed_sensitivities={"private", "personal", "highly_sensitive"},
                )
                resolve_answers(inspection.fields, profile, bank, policy)
                uploaded = adapter.fill_safe_fields(session, inspection.fields, ResumeArtifact(resume))
                self.assertTrue(uploaded)
                self.assertEqual(session.page.locator("#email").input_value(), "candidate@example.test")
                self.assertEqual(session.page.locator("#notice").input_value(), "30")
                self.assertTrue(session.page.locator('input[name="authorized"][value="yes"]').is_checked())
                resume_field = next(item for item in inspection.fields if item.field_type == "file")
                self.assertEqual(resume_field.answer_status, AnswerStatus.UPLOADED.value)

                # Local fixture only: prove even an accidental click is cancelled.
                session.page.get_by_role("button", name="Submit Application").click()
                blocked = session.page.evaluate("window.__dohssBlockedSubmissions")
                self.assertTrue(blocked)
                self.assertEqual(session.page.url, FIXTURE.resolve().as_uri())
            finally:
                session_context.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
