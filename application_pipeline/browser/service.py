"""Application inspection and safe dry-run orchestration."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from candidate import AnswerBank, CandidateProfile
from database import db

from application_pipeline.ats import adapter_for, detect_ats
from application_pipeline.private_db import (
    create_form_run,
    finish_form_run,
    get_application,
    update_browser_status,
)

from .inspector import resolve_answers
from .models import AnswerStatus, DryRunReport, ValidationIssue
from .safety import ResumePolicy, SafeFillPolicy
from .session import BrowserConfig, BrowserSession


BROWSER_ALLOWED_START_STATES = {
    "READY_TO_APPLY", "NEEDS_REVIEW", "FORM_INSPECTED", "FORM_PARTIALLY_FILLED",
    "FORM_READY_FOR_REVIEW", "NEEDS_USER_ACTION", "BLOCKED", "DRY_RUN_COMPLETE",
}


class BrowserApplicationService:
    def __init__(
        self,
        public_db_path: str,
        private_db_path: str,
        browser_config: BrowserConfig,
        safe_fill_policy: Optional[SafeFillPolicy] = None,
        resume_policy: Optional[ResumePolicy] = None,
        session_factory: Callable[[BrowserConfig, int], Any] = BrowserSession,
        adapter_factory: Callable[[str, str], Any] = adapter_for,
    ):
        if Path(public_db_path).resolve() == Path(private_db_path).resolve():
            raise ValueError("Browser application data must use the private database")
        self.public_db_path = public_db_path
        self.private_db_path = private_db_path
        self.browser_config = browser_config
        self.safe_fill_policy = safe_fill_policy or SafeFillPolicy()
        self.resume_policy = resume_policy or ResumePolicy()
        self.session_factory = session_factory
        self.adapter_factory = adapter_factory

    def _load(self, application_id: int) -> tuple[dict[str, Any], dict[str, Any], str, Any]:
        application = get_application(self.private_db_path, application_id)
        if not application:
            raise KeyError(f"Application {application_id} not found")
        if application["status"] not in BROWSER_ALLOWED_START_STATES:
            raise ValueError(f"Application {application_id} is not ready for browser inspection: {application['status']}")
        job = db.get_job(self.public_db_path, application["job_id"])
        if not job:
            raise KeyError(f"Public job {application['job_id']} not found")
        url = str(application.get("application_url") or job.get("application_url") or job.get("url") or "").strip()
        if not url:
            raise ValueError("Application has no URL")
        provider = detect_ats(url)
        adapter = self.adapter_factory(url, application.get("ats_provider") or provider)
        return application, job, url, adapter

    @staticmethod
    def _write_report(session: Any, report: DryRunReport) -> dict[str, Any]:
        path = session.artifacts.path("report.json")
        report.report_path = str(path)
        payload = report.to_dict()
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        session.artifacts.log("report_saved", path=str(path), final_status=report.final_status)
        return payload

    def _record_failure(self, application_id: int, run_id: int, operation: str, exc: Exception) -> None:
        """Close the audit record without hiding the original browser exception."""
        error = f"{type(exc).__name__}: {exc}"
        payload = {
            "run_status": "FAILED",
            "fields": [],
            "authentication_blockers": [],
            "validation_issues": [{
                "field_id": "browser",
                "code": "BROWSER_OPERATION_FAILED",
                "message": error,
                "required": False,
            }],
            "screenshot_paths": [],
            "html_snapshot_path": "",
            "report_path": "",
            "resume_path": "",
            "submission_requests_blocked": [],
        }
        try:
            finish_form_run(self.private_db_path, run_id, payload)
            update_browser_status(
                self.private_db_path,
                application_id,
                "BLOCKED",
                f"{operation} failed safely before submission: {error}",
            )
        except Exception:
            # Audit cleanup must never replace the actionable root exception.
            pass

    def inspect(self, application_id: int) -> DryRunReport:
        application, job, url, adapter = self._load(application_id)
        run_id = create_form_run(self.private_db_path, application_id, "INSPECT", adapter.provider, url)
        try:
            with self.session_factory(self.browser_config, application_id) as session:
                adapter.open_application(session, url)
                inspection = adapter.inspect_form(session)
                if inspection.blockers and session.manual_user_handoff(inspection.blockers):
                    inspection = adapter.inspect_form(session)
                screenshots = [session.screenshot("inspection.png")]
                html_path = session.html_snapshot("inspection.html")
                status = "NEEDS_USER_ACTION" if inspection.blockers else "FORM_INSPECTED"
                report = DryRunReport(
                    application_id=application_id, company=job.get("company") or "",
                    role=job.get("title") or "", ats_provider=adapter.provider,
                    url=inspection.url, fields=inspection.fields,
                    authentication_blockers=inspection.blockers,
                    screenshot_paths=screenshots, html_snapshot_path=html_path,
                    final_status=status, run_status="INSPECTION_COMPLETE",
                    submission_requests_blocked=list(session.guard.blocked_requests),
                )
                payload = self._write_report(session, report)
        except Exception as exc:
            self._record_failure(application_id, run_id, "application inspection", exc)
            raise
        update_browser_status(self.private_db_path, application_id, status, "application form inspected; submission remained blocked")
        finish_form_run(self.private_db_path, run_id, payload)
        return report

    def dry_run(self, application_id: int, profile: CandidateProfile, bank: AnswerBank) -> DryRunReport:
        application, job, url, adapter = self._load(application_id)
        run_id = create_form_run(self.private_db_path, application_id, "DRY_RUN", adapter.provider, url)
        try:
            with self.session_factory(self.browser_config, application_id) as session:
                adapter.open_application(session, url)
                inspection = adapter.inspect_form(session)
                if inspection.blockers and session.manual_user_handoff(inspection.blockers):
                    inspection = adapter.inspect_form(session)
                screenshots = [session.screenshot("before_fill.png")]
                validation: list[ValidationIssue] = []
                resume = None
                resume_uploaded = False
                if inspection.blockers:
                    status = "NEEDS_USER_ACTION"
                else:
                    resolve_answers(inspection.fields, profile, bank, self.safe_fill_policy)
                    try:
                        resume = self.resume_policy.validate()
                    except ValueError as exc:
                        validation.append(ValidationIssue("resume", "RESUME_POLICY_BLOCKED", str(exc), False))
                    resume_uploaded = adapter.fill_safe_fields(session, inspection.fields, resume)
                    validation.extend(adapter.validate(session, inspection.fields))
                    unresolved_required = any(issue.required for issue in validation)
                    status = "FORM_PARTIALLY_FILLED" if unresolved_required else "FORM_READY_FOR_REVIEW"
                review_screenshots, html_path = adapter.capture_review(session)
                screenshots.extend(review_screenshots)
                report = DryRunReport(
                    application_id=application_id, company=job.get("company") or "",
                    role=job.get("title") or "", ats_provider=adapter.provider,
                    url=inspection.url, fields=inspection.fields,
                    authentication_blockers=inspection.blockers,
                    validation_issues=validation,
                    resume_uploaded=resume_uploaded,
                    resume_path=str(resume.path) if resume_uploaded and resume else "",
                    screenshot_paths=screenshots,
                    html_snapshot_path=html_path,
                    final_status=status, run_status="DRY_RUN_COMPLETE",
                    submission_requests_blocked=list(session.guard.blocked_requests),
                )
                payload = self._write_report(session, report)
        except Exception as exc:
            self._record_failure(application_id, run_id, "application dry run", exc)
            raise
        update_browser_status(
            self.private_db_path, application_id, status,
            "safe browser dry run completed; final submission blocked by policy",
            resume_path=report.resume_path,
        )
        finish_form_run(self.private_db_path, run_id, payload)
        return report
