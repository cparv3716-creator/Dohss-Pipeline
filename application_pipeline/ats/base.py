"""Provider-independent ATS interface with no submission capability."""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Optional

from application_pipeline.browser.inspector import detect_page_blockers, inspect_page_fields
from application_pipeline.browser.models import AnswerStatus, ApplicationField, InspectionResult, ValidationIssue
from application_pipeline.browser.safety import ResumeArtifact


def _string_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{key}: {item}" for key, item in value.items())
    return str(value)


def _matches(value: Any, option: dict[str, str]) -> bool:
    desired = _string_value(value).strip().lower()
    aliases = {desired}
    if desired in ("true", "yes", "authorized", "willing"):
        aliases.update(("yes", "true"))
    if desired in ("false", "no", "not authorized", "not willing"):
        aliases.update(("no", "false"))
    return str(option.get("value") or "").strip().lower() in aliases or str(option.get("label") or "").strip().lower() in aliases


class ApplicationATSAdapter(ABC):
    provider = "Unknown"
    auto_fill_enabled = True

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        raise NotImplementedError

    def open_application(self, session: Any, url: str) -> Any:
        response = session.open(url)
        try:
            session.page.locator("form, input, select, textarea").first.wait_for(state="attached", timeout=10_000)
        except Exception:
            pass
        return response

    def inspect_form(self, session: Any, pass_number: int = 1) -> InspectionResult:
        fields = inspect_page_fields(session.page, pass_number)
        return InspectionResult(
            ats_provider=self.provider,
            url=session.page.url,
            fields=fields,
            blockers=detect_page_blockers(session.page),
            final_submit_controls=session.guard.submit_control_texts(session.page),
        )

    def extract_questions(self, inspection: InspectionResult) -> list[ApplicationField]:
        return inspection.fields

    def _frame(self, page: Any, field: ApplicationField) -> Any:
        for frame in page.frames:
            if field.frame_url and frame.url == field.frame_url:
                return frame
        return page.main_frame

    def _fill_field(self, page: Any, field: ApplicationField) -> None:
        frame = self._frame(page, field)
        locator = frame.locator(field.selector)
        value = field.proposed_value
        if field.field_type in ("text", "email", "phone", "textarea", "number", "date"):
            locator.first.fill(_string_value(value))
        elif field.field_type == "select" and field.control_kind == "combobox":
            locator.first.click()
            page.wait_for_timeout(150)
            options = frame.locator('[role="option"]:visible')
            available = options.evaluate_all(
                "els => els.map(e => ({value: e.getAttribute('data-value') || e.innerText.trim(), label: e.innerText.trim()}))"
            )
            field.options = list(available)
            option = next((item for item in field.options if _matches(value, item)), None)
            if not option:
                page.keyboard.press("Escape")
                raise ValueError("No custom dropdown option exactly matches the verified answer")
            choices = options.filter(has_text=re.compile(rf"^\s*{re.escape(str(option['label']))}\s*$", re.I))
            if choices.count() != 1:
                page.keyboard.press("Escape")
                raise ValueError("Custom dropdown answer is ambiguous")
            choices.first.click()
        elif field.field_type == "select":
            option = next((item for item in field.options if _matches(value, item)), None)
            if not option:
                raise ValueError("No dropdown option matches the verified answer")
            locator.first.select_option(value=option["value"])
        elif field.field_type == "radio":
            option = next((item for item in field.options if _matches(value, item)), None)
            if not option:
                raise ValueError("No radio option matches the verified answer")
            frame.locator(f'{field.selector}[value={json.dumps(option["value"])}]').first.check()
        elif field.field_type == "checkbox":
            truthy = value is True or str(value).strip().lower() in ("yes", "true", "1", "checked")
            if truthy:
                locator.first.check()
            else:
                locator.first.uncheck()
        else:
            raise ValueError(f"Unsupported field type: {field.field_type}")
        field.current_value = value
        field.answer_status = AnswerStatus.FILLED.value

    def fill_safe_fields(self, session: Any, fields: list[ApplicationField], resume: Optional[ResumeArtifact] = None) -> bool:
        if not self.auto_fill_enabled:
            for field in fields:
                if field.answer_status == AnswerStatus.PROPOSED.value:
                    field.answer_status = AnswerStatus.SKIPPED_POLICY.value
                    field.answer_reason = "GENERIC_ATS_INSPECTION_ONLY"
            return False

        # Upload first. Modern ATS forms frequently re-render their React tree
        # after select/text changes, which can replace the file input and make
        # an otherwise valid resume selector stale.
        resume_uploaded = False
        if resume:
            resume_fields = [
                field for field in fields
                if field.field_type == "file" and re.search(r"\b(resume|cv)\b", field.raw_question, re.I)
            ]
            if resume_fields:
                field = resume_fields[0]
                try:
                    frame = self._frame(session.page, field)
                    frame.locator(field.selector).first.set_input_files(str(resume.path))
                    try:
                        acknowledgement = frame.locator(field.selector).first.evaluate(
                            "el => el.files && el.files.length ? {count: el.files.length, name: el.files[0].name} : {count: 0, name: ''}",
                            timeout=2_000,
                        )
                    except Exception:
                        # Greenhouse replaces the native input after a
                        # successful upload. The rendered filename is its
                        # acknowledgement in that state.
                        acknowledgement = {"count": 0, "name": ""}
                    native_ack = acknowledgement.get("count") == 1 and acknowledgement.get("name") == resume.path.name
                    if not native_ack:
                        try:
                            frame.get_by_text(resume.path.name, exact=False).first.wait_for(
                                state="attached", timeout=5_000,
                            )
                        except Exception:
                            pass
                    rendered_name = resume.path.name in frame.content()
                    if not native_ack and not rendered_name:
                        raise ValueError("The application page did not acknowledge the configured resume")
                    field.current_value = resume.identifier
                    field.answer_source = "configured_resume"
                    field.answer_verified = True
                    field.auto_fill_allowed = True
                    field.answer_status = AnswerStatus.UPLOADED.value
                    field.answer_reason = "CONFIGURED_PRIVATE_RESUME"
                    resume_uploaded = True
                    session.artifacts.log("resume_uploaded", field_id=field.field_id, resume_identifier=resume.identifier)
                except Exception as exc:
                    field.answer_status = AnswerStatus.ERROR.value
                    field.answer_reason = f"RESUME_UPLOAD_FAILED:{type(exc).__name__}"

        for field in fields:
            if not field.visible or field.answer_status != AnswerStatus.PROPOSED.value:
                continue
            try:
                self._fill_field(session.page, field)
                session.artifacts.log("field_filled", field_id=field.field_id, question_type=field.canonical_question_type)
            except Exception as exc:
                field.answer_status = AnswerStatus.ERROR.value
                field.answer_reason = f"FIELD_FILL_FAILED:{type(exc).__name__}"
                session.artifacts.log("field_fill_failed", field_id=field.field_id, error=field.answer_reason)

        return resume_uploaded

    def validate(self, session: Any, fields: list[ApplicationField]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for field in fields:
            if not field.visible or not field.required:
                continue
            is_present = field.answer_status in (AnswerStatus.FILLED.value, AnswerStatus.UPLOADED.value)
            if not is_present and field.current_value not in (None, "", False, []):
                is_present = True
            if not is_present:
                issues.append(ValidationIssue(field.field_id, "REQUIRED_FIELD_UNRESOLVED", f"Required field is unresolved: {field.raw_question}", True))
        if session.guard.submit_control_texts(session.page):
            issues.append(ValidationIssue("final_submit", "FINAL_SUBMIT_BLOCKED_BY_POLICY", "Final submission control was detected and deliberately left untouched", False))
        return issues

    def capture_review(self, session: Any) -> tuple[list[str], str]:
        screenshot = session.screenshot("review.png", full_page=True)
        html = session.html_snapshot("review.html")
        return [screenshot], html

    def close(self, session: Any) -> None:
        session.close()
