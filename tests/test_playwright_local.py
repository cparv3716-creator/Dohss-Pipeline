import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

from application_pipeline.ats.base import _unique_option
from application_pipeline.ats.greenhouse import GreenhouseAdapter
from application_pipeline.browser.inspector import (
    detect_page_security_state,
    inspect_page_fields,
    resolve_answers,
)
from application_pipeline.browser.metrics import required_detection_metrics
from application_pipeline.browser.models import AnswerStatus
from application_pipeline.browser.safety import ResumeArtifact, SafeFillPolicy
from application_pipeline.browser.session import BrowserConfig, BrowserSession
from application_pipeline.browser.service import BrowserApplicationService
from application_pipeline.browser.safety import ResumePolicy
from application_pipeline.private_db import upsert_application
from candidate import AnswerBank, CandidateProfile
from database import db
from tests.helpers import answer, write_answers, write_profile


FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_application.html"
DYNAMIC_FIXTURE = Path(__file__).parent / "fixtures" / "dynamic_application.html"
SEMANTIC_FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_semantic_reference.html"
SEMANTIC_LABELS = Path(__file__).parent / "fixtures" / "greenhouse_semantic_reference.json"
DEGREE_FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_degree_combobox.html"
INDIA_REFERENCE_FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_india_reference.html"
INDIA_REFERENCE_LABELS = Path(__file__).parent / "fixtures" / "greenhouse_india_reference.json"


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright package is not installed")
class LocalPlaywrightTests(unittest.TestCase):
    def test_invisible_recaptcha_registration_is_observed_without_blocking(self):
        private_parent = Path("data/private")
        private_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private_parent) as tmp:
            context = BrowserSession(
                BrowserConfig(headless=True, artifacts_root=tmp, save_html=False), 995
            )
            try:
                session = context.__enter__()
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    self.skipTest("Playwright Chromium is not installed")
                raise
            try:
                session.page.set_content(
                    '<div class="grecaptcha-badge"><iframe title="reCAPTCHA" '
                    'src="https://www.recaptcha.net/recaptcha/enterprise/anchor?size=invisible" '
                    'style="width:256px;height:60px"></iframe>'
                    '<textarea name="g-recaptcha-response" style="display:none"></textarea></div>'
                )
                blockers, observations = detect_page_security_state(session.page)
                self.assertNotIn("CAPTCHA_REQUIRED", blockers)
                self.assertEqual(observations, ["INVISIBLE_RECAPTCHA_REGISTERED_NOT_CHALLENGED"])
            finally:
                context.__exit__(None, None, None)

    def test_visible_recaptcha_widget_remains_actionable_blocker(self):
        private_parent = Path("data/private")
        private_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private_parent) as tmp:
            context = BrowserSession(
                BrowserConfig(headless=True, artifacts_root=tmp, save_html=False), 994
            )
            try:
                session = context.__enter__()
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    self.skipTest("Playwright Chromium is not installed")
                raise
            try:
                session.page.set_content(
                    '<div class="g-recaptcha" data-sitekey="test" '
                    'style="width:304px;height:78px">Confirm you are not a robot</div>'
                )
                blockers, observations = detect_page_security_state(session.page)
                self.assertIn("CAPTCHA_REQUIRED", blockers)
                self.assertEqual(observations, [])
            finally:
                context.__exit__(None, None, None)

    def test_city_option_requires_one_unambiguous_geographic_match(self):
        options = [{"value": "chennai-in", "label": "Chennai, Tamil Nadu, India"}]
        self.assertEqual(_unique_option("Chennai", options, "CURRENT_CITY"), options[0])
        ambiguous = options + [{"value": "chennai-us", "label": "Chennai, New York, United States"}]
        self.assertIsNone(_unique_option("Chennai", ambiguous, "CURRENT_CITY"))

    def test_immediate_notice_period_matches_explicit_immediately_available(self):
        options = [{"value": "immediate", "label": "Immediately Available"}]
        self.assertEqual(_unique_option("immediate", options, "NOTICE_PERIOD"), options[0])

    def test_degree_prefers_generic_level_over_specialized_or_accelerated_options(self):
        options = [
            {"value": "accelerated", "label": "Accelerated Master's"},
            {"value": "mba", "label": "Master of Business Administration (M.B.A.)"},
            {"value": "masters", "label": "Masters"},
            {"value": "masters-degree", "label": "Master's Degree"},
        ]
        self.assertEqual(
            _unique_option("M.A. Economics", options, "DEGREE"),
            {"value": "masters-degree", "label": "Master's Degree"},
        )

    def test_delayed_greenhouse_degree_combobox_exact_semantic_fill(self):
        private_parent = Path("data/private")
        private_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private_parent) as tmp:
            root = Path(tmp).resolve()
            profile = CandidateProfile.load(write_profile(root / "profile.yaml"))
            bank = AnswerBank.load(write_answers(root / "answers.yaml", {"DEGREE": answer("M.A. Economics")}))
            context = BrowserSession(BrowserConfig(headless=True, artifacts_root=str(root), save_html=False), 997)
            try:
                session = context.__enter__()
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    self.skipTest("Playwright Chromium is not installed")
                raise
            try:
                adapter = GreenhouseAdapter()
                adapter.open_application(session, DEGREE_FIXTURE.resolve().as_uri())
                inspection = adapter.inspect_form(session)
                self.assertEqual(sum(item.label == "Degree" for item in inspection.fields), 1)
                resolve_answers(inspection.fields, profile, bank, SafeFillPolicy())
                adapter.fill_safe_fields(session, inspection.fields)
                degree = next(item for item in inspection.fields if item.label == "Degree")
                self.assertEqual(degree.answer_status, AnswerStatus.FILLED.value)
                self.assertEqual(session.page.locator("#degree-input").input_value(), "Master's degree")
            finally:
                context.__exit__(None, None, None)

    def test_live_dom_semantic_reference_meets_measured_detection_target(self):
        private_parent = Path("data/private")
        private_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private_parent) as tmp:
            root = Path(tmp).resolve()
            context = BrowserSession(BrowserConfig(headless=True, artifacts_root=str(root), save_html=False), 998)
            try:
                session = context.__enter__()
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    self.skipTest("Playwright Chromium is not installed")
                raise
            try:
                session.open(SEMANTIC_FIXTURE.resolve().as_uri())
                fields = inspect_page_fields(session.page)
                expected = json.loads(SEMANTIC_LABELS.read_text(encoding="utf-8"))["expected"]
                metrics = required_detection_metrics(expected, fields)
                self.assertGreaterEqual(metrics.precision, 0.95)
                self.assertGreaterEqual(metrics.recall, 0.95)
                self.assertEqual(sum(item.label == "Degree" for item in fields), 1)
                self.assertEqual(sum(item.label == "City" for item in fields), 2)
            finally:
                context.__exit__(None, None, None)

    def test_india_live_structure_reference_meets_detection_target(self):
        private_parent = Path("data/private")
        private_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private_parent) as tmp:
            root = Path(tmp).resolve()
            context = BrowserSession(BrowserConfig(headless=True, artifacts_root=str(root), save_html=False), 996)
            try:
                session = context.__enter__()
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    self.skipTest("Playwright Chromium is not installed")
                raise
            try:
                session.open(INDIA_REFERENCE_FIXTURE.resolve().as_uri())
                fields = inspect_page_fields(session.page)
                expected = json.loads(INDIA_REFERENCE_LABELS.read_text(encoding="utf-8"))["expected"]
                metrics = required_detection_metrics(expected, fields)
                self.assertEqual(metrics.true_positive, 9)
                self.assertGreaterEqual(metrics.precision, 0.95)
                self.assertGreaterEqual(metrics.recall, 0.95)
                self.assertEqual(len({item.logical_id for item in fields}), len(fields))
            finally:
                context.__exit__(None, None, None)

    def test_combined_independently_labelled_greenhouse_dataset_meets_target(self):
        private_parent = Path("data/private")
        private_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private_parent) as tmp:
            context = BrowserSession(
                BrowserConfig(headless=True, artifacts_root=tmp, save_html=False), 993
            )
            try:
                session = context.__enter__()
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    self.skipTest("Playwright Chromium is not installed")
                raise
            try:
                expected = []
                detected = []
                for fixture, labels in (
                    (SEMANTIC_FIXTURE, SEMANTIC_LABELS),
                    (INDIA_REFERENCE_FIXTURE, INDIA_REFERENCE_LABELS),
                ):
                    session.open(fixture.resolve().as_uri())
                    expected.extend(json.loads(labels.read_text(encoding="utf-8"))["expected"])
                    detected.extend(inspect_page_fields(session.page))
                metrics = required_detection_metrics(expected, detected)
                self.assertEqual(metrics.true_positive, 27)
                self.assertEqual(metrics.false_positive, 0)
                self.assertEqual(metrics.false_negative, 0)
                self.assertEqual(metrics.precision, 1.0)
                self.assertEqual(metrics.recall, 1.0)
            finally:
                context.__exit__(None, None, None)

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
                uploaded = adapter.fill_safe_fields(session, inspection.fields, ResumeArtifact(resume, "resume:test"))
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

    def test_resume_upload_acknowledges_react_replacement_ui(self):
        private_parent = Path("data/private")
        private_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private_parent) as tmp:
            root = Path(tmp).resolve()
            resume = root / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4 synthetic test artifact")
            context = BrowserSession(
                BrowserConfig(headless=True, artifacts_root=str(root), save_html=False), 991
            )
            try:
                session = context.__enter__()
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    self.skipTest("Playwright Chromium is not installed")
                raise
            try:
                session.page.set_content(
                    """
                    <form><div class="file-upload" role="group" aria-labelledby="upload-label-resume">
                      <div id="upload-label-resume">Resume/CV *</div>
                      <input id="resume" type="file" required>
                    </div><button type="submit">Submit Application</button></form>
                    <script>
                    document.querySelector('#resume').addEventListener('change', (event) => {
                      const group = event.target.closest('[role=group]');
                      event.target.remove();
                      group.insertAdjacentHTML('beforeend',
                        '<div class="file-upload__filename">Uploaded file'
                        + '<button type="button" aria-label="Remove file">Remove</button></div>');
                    });
                    </script>
                    """
                )
                adapter = GreenhouseAdapter()
                inspection = adapter.inspect_form(session)
                uploaded = adapter.fill_safe_fields(
                    session, inspection.fields, ResumeArtifact(resume, "resume:test")
                )
                self.assertTrue(uploaded)
                field = next(item for item in inspection.fields if item.field_type == "file")
                self.assertEqual(field.answer_status, AnswerStatus.UPLOADED.value)
            finally:
                context.__exit__(None, None, None)

    def test_iterative_combobox_conditional_field_and_resume_acknowledgement(self):
        private_parent = Path("data/private")
        private_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private_parent) as tmp:
            root = Path(tmp).resolve()
            public_db = root / "public.db"
            private_db = root / "private.db"
            resume = root / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4 synthetic test artifact")
            db.init_db(public_db)
            url = DYNAMIC_FIXTURE.resolve().as_uri()
            db.insert_job(public_db, {
                "title": "Analyst", "company": "Fixture", "location": "Remote",
                "description": "Fixture", "url": url, "source": "test",
                "keyword_matched": "analyst", "salary": "", "posted_at": "2026-01-01",
            })
            job = db.get_job_by_url(public_db, url)
            application = upsert_application(private_db, {
                "job_id": job["id"], "application_url": url,
                "ats_provider": "Greenhouse", "status": "READY_TO_APPLY",
            })
            profile = CandidateProfile.load(write_profile(root / "profile.yaml"))
            bank = AnswerBank.load(write_answers(root / "answers.yaml", {
                "EMAIL": answer("candidate@example.test"),
                "COUNTRY": answer("India"),
                "STATE_PROVINCE": answer("Tamil Nadu"),
            }))
            service = BrowserApplicationService(
                str(public_db), str(private_db),
                BrowserConfig(headless=True, artifacts_root=str(root), save_html=False, max_fill_passes=3, dom_settle_ms=100),
                resume_policy=ResumePolicy(str(resume), [str(root)]),
            )
            try:
                report = service.dry_run(application["id"], profile, bank)
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    self.skipTest("Playwright Chromium is not installed")
                raise
            self.assertGreaterEqual(report.inspection_passes, 2)
            self.assertEqual(report.conditional_fields_detected, 1)
            self.assertTrue(report.resume_uploaded)
            self.assertTrue(report.resume_identifier.startswith("resume:sha256:"))
            state = next(item for item in report.fields if item.field_id == "state")
            self.assertEqual(state.answer_status, AnswerStatus.FILLED.value)
            self.assertEqual(report.submission, "FINAL_SUBMIT_BLOCKED_BY_POLICY")


if __name__ == "__main__":
    unittest.main()
