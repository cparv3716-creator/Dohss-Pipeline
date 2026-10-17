import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from application_pipeline.browser.models import AnswerStatus, ApplicationField, InspectionResult, ValidationIssue
from application_pipeline.browser.service import BrowserApplicationService
from application_pipeline.browser.session import BrowserConfig
from application_pipeline.cli import review_application
from application_pipeline.private_db import (
    get_application, get_form_runs, list_user_action_queue, upsert_application,
)
from candidate import AnswerBank, CandidateProfile
from database import db
from tests.helpers import answer, write_answers, write_profile


class FakeArtifacts:
    def __init__(self, root):
        self.root = root
    def path(self, name):
        return self.root / name
    def log(self, *_args, **_kwargs):
        return None


class FakeSession:
    root = None
    def __init__(self, config, application_id):
        self.config = config
        self.application_id = application_id
        self.page = SimpleNamespace(url="https://boards.greenhouse.io/acme/jobs/1")
        self.guard = SimpleNamespace(blocked_requests=[], submit_control_texts=lambda _page: ["Submit Application"])
        self.artifacts = FakeArtifacts(self.root)
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return None
    def screenshot(self, name):
        path = self.artifacts.path(name)
        path.write_bytes(b"png")
        return str(path)
    def html_snapshot(self, name):
        path = self.artifacts.path(name)
        path.write_text("<html></html>", encoding="utf-8")
        return str(path)
    def manual_user_handoff(self, _reasons):
        return False
    def form_signature(self):
        return "stable"
    def wait_for_form_change(self, _signature):
        return False


class FakeAdapter:
    provider = "Greenhouse"
    blockers = []
    def open_application(self, session, url):
        session.page.url = url
    def inspect_form(self, session, pass_number=1):
        return InspectionResult(
            "Greenhouse", session.page.url,
            [ApplicationField("email", "Email", "Email", "email", True, canonical_question_type="EMAIL", confidence=90, selector="#email")],
            blockers=list(self.blockers), final_submit_controls=["Submit Application"],
        )
    def fill_safe_fields(self, _session, fields, _resume):
        for field in fields:
            if field.answer_status == AnswerStatus.PROPOSED.value:
                field.answer_status = AnswerStatus.FILLED.value
                field.current_value = field.proposed_value
        return False
    def validate(self, _session, _fields):
        return [ValidationIssue("final_submit", "FINAL_SUBMIT_BLOCKED_BY_POLICY", "blocked", False)]
    def capture_review(self, session):
        return [session.screenshot("review.png")], session.html_snapshot("review.html")


class FailingAdapter(FakeAdapter):
    def open_application(self, _session, _url):
        raise RuntimeError("synthetic navigation failure")


class HandoffSession(FakeSession):
    handoff_calls = 0
    def manual_user_handoff(self, _reasons):
        type(self).handoff_calls += 1
        return True


class TimeoutHandoffSession(FakeSession):
    def manual_user_handoff(self, _reasons, *_args):
        return False


class HandoffAdapter(FakeAdapter):
    inspections = 0
    def inspect_form(self, session, pass_number=1):
        type(self).inspections += 1
        result = super().inspect_form(session, pass_number)
        result.blockers = ["CAPTCHA_REQUIRED"] if type(self).inspections == 1 else []
        return result


class BrowserServiceTests(unittest.TestCase):
    def _setup(self, root):
        public_db = root / "public.db"
        private_db = root / "private.db"
        db.init_db(public_db)
        db.insert_job(public_db, {
            "title": "Analyst", "company": "Example", "location": "Remote", "description": "",
            "url": "https://boards.greenhouse.io/acme/jobs/1", "source": "test",
            "keyword_matched": "analyst", "salary": "", "posted_at": "2026-01-01",
        })
        job = db.get_job_by_url(public_db, "https://boards.greenhouse.io/acme/jobs/1")
        app = upsert_application(private_db, {
            "job_id": job["id"], "application_url": job["url"], "ats_provider": "Greenhouse",
            "match_score": 90, "status": "READY_TO_APPLY",
        })
        return public_db, private_db, app

    def test_dry_run_transition_and_report_never_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_db, private_db, app = self._setup(root)
            profile = CandidateProfile.load(write_profile(root / "profile.yaml"))
            bank = AnswerBank.load(write_answers(root / "answers.yaml", {"EMAIL": answer("candidate@example.test")}))
            FakeSession.root = root
            service = BrowserApplicationService(
                str(public_db), str(private_db), BrowserConfig(artifacts_root=str(root)),
                session_factory=FakeSession, adapter_factory=lambda *_args: FakeAdapter(),
            )
            report = service.dry_run(app["id"], profile, bank)
            self.assertEqual(report.final_status, "FORM_READY_FOR_REVIEW")
            self.assertEqual(report.submission, "FINAL_SUBMIT_BLOCKED_BY_POLICY")
            self.assertEqual(get_application(private_db, app["id"])["status"], "FORM_READY_FOR_REVIEW")
            self.assertEqual(get_form_runs(private_db, app["id"])[0]["status"], "DRY_RUN_COMPLETE")
            rendered = review_application({
                "database": {"path": str(public_db)},
                "applications": {"private_db_path": str(private_db)},
            }, app["id"])
            self.assertIn("Application readiness:", rendered)
            self.assertIn("Submission: BLOCKED BY POLICY", rendered)
            self.assertNotIn("candidate@example.test", rendered)

    def test_captcha_transitions_to_needs_user_action_after_safe_prefill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_db, private_db, app = self._setup(root)
            profile = CandidateProfile.load(write_profile(root / "profile.yaml"))
            bank = AnswerBank.empty()
            FakeSession.root = root
            adapter = FakeAdapter()
            adapter.blockers = ["CAPTCHA_REQUIRED"]
            service = BrowserApplicationService(
                str(public_db), str(private_db), BrowserConfig(artifacts_root=str(root)),
                session_factory=FakeSession, adapter_factory=lambda *_args: adapter,
            )
            report = service.dry_run(app["id"], profile, bank)
            self.assertEqual(report.final_status, "NEEDS_USER_ACTION")
            self.assertEqual(report.authentication_blockers, ["CAPTCHA_REQUIRED"])
            self.assertEqual(report.fields[0].answer_status, AnswerStatus.SKIPPED_UNKNOWN.value)

    def test_browser_failure_closes_run_and_blocks_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_db, private_db, app = self._setup(root)
            profile = CandidateProfile.load(write_profile(root / "profile.yaml"))
            FakeSession.root = root
            service = BrowserApplicationService(
                str(public_db), str(private_db), BrowserConfig(artifacts_root=str(root)),
                session_factory=FakeSession, adapter_factory=lambda *_args: FailingAdapter(),
            )
            with self.assertRaisesRegex(RuntimeError, "synthetic navigation failure"):
                service.dry_run(app["id"], profile, AnswerBank.empty())
            self.assertEqual(get_application(private_db, app["id"])["status"], "BLOCKED")
            run = get_form_runs(private_db, app["id"])[0]
            self.assertEqual(run["status"], "FAILED")
            self.assertIn("BROWSER_OPERATION_FAILED", run["validation_json"])

    def test_manual_captcha_handoff_reuses_session_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_db, private_db, app = self._setup(root)
            profile = CandidateProfile.load(write_profile(root / "profile.yaml"))
            bank = AnswerBank.load(write_answers(root / "answers.yaml", {"EMAIL": answer("candidate@example.test")}))
            HandoffSession.root = root
            HandoffSession.handoff_calls = 0
            HandoffAdapter.inspections = 0
            service = BrowserApplicationService(
                str(public_db), str(private_db), BrowserConfig(artifacts_root=str(root), headless=False, manual_handoff=True),
                session_factory=HandoffSession, adapter_factory=lambda *_args: HandoffAdapter(),
            )
            report = service.dry_run(app["id"], profile, bank)
            self.assertEqual(HandoffSession.handoff_calls, 1)
            self.assertEqual(HandoffAdapter.inspections, 2)
            self.assertEqual(report.authentication_blockers, [])
            self.assertEqual(report.final_status, "FORM_READY_FOR_REVIEW")
            queue = list_user_action_queue(private_db, "ALL")
            self.assertEqual(queue[0]["status"], "COMPLETED")

    def test_manual_captcha_timeout_is_queued_without_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_db, private_db, app = self._setup(root)
            profile = CandidateProfile.load(write_profile(root / "profile.yaml"))
            TimeoutHandoffSession.root = root
            adapter = FakeAdapter()
            adapter.blockers = ["CAPTCHA_REQUIRED"]
            service = BrowserApplicationService(
                str(public_db), str(private_db),
                BrowserConfig(artifacts_root=str(root), headless=False, manual_handoff=True),
                session_factory=TimeoutHandoffSession, adapter_factory=lambda *_args: adapter,
            )
            report = service.dry_run(app["id"], profile, AnswerBank.empty())
            self.assertEqual(report.final_status, "NEEDS_USER_ACTION")
            queue = list_user_action_queue(private_db, "ALL")
            self.assertEqual(queue[0]["status"], "TIMED_OUT")
            self.assertEqual(report.submission, "FINAL_SUBMIT_BLOCKED_BY_POLICY")


if __name__ == "__main__":
    unittest.main()
