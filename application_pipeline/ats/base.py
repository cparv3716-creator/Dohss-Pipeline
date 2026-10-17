"""Provider-independent ATS interface with no submission capability."""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Optional

from application_pipeline.browser.inspector import detect_page_security_state, inspect_page_fields
from application_pipeline.browser.models import AnswerStatus, ApplicationField, InspectionResult, ValidationIssue
from application_pipeline.browser.safety import ResumeArtifact


class SemanticMatchError(ValueError):
    """A verified value could not be mapped to exactly one control option."""


def _normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _string_value(value).casefold()).strip()


def _degree_level(value: Any) -> str:
    text = _normal(value)
    if re.search(r"\b(phd|doctor(?:ate|al))\b", text):
        return "doctorate"
    if re.search(r"\b(m\s*a|m\s*sc|m\s*s|m\s*b\s*a|master(?:s)?)\b", text):
        return "masters"
    if re.search(r"\b(b\s*a|b\s*sc|b\s*s|b\s*tech|bachelor(?:s)?)\b", text):
        return "bachelors"
    return ""


def _degree_kind(value: Any) -> str:
    text = _normal(value)
    kinds = (
        ("masters_arts", r"\b(m\s*a\b|master(?:s)? of arts\b)"),
        ("masters_science", r"\b(m\s*s\b|m\s*sc\b|master(?:s)? of science\b)"),
        ("masters_business", r"\b(m\s*b\s*a\b|master(?:s)? of business administration\b)"),
        ("bachelors_arts", r"\b(b\s*a\b|bachelor(?:s)? of arts\b)"),
        ("bachelors_science", r"\b(b\s*s\b|b\s*sc\b|bachelor(?:s)? of science\b)"),
        ("bachelors_technology", r"\b(b\s*tech\b|bachelor(?:s)? of technology\b)"),
    )
    return next((name for name, pattern in kinds if re.search(pattern, text)), "")


def _search_query(value: Any, question_type: str) -> str:
    if question_type in {"DEGREE", "HIGHEST_EDUCATION"}:
        return {
            "masters_arts": "Master of Arts",
            "masters_science": "Master of Science",
            "masters_business": "Master of Business Administration",
            "bachelors_arts": "Bachelor of Arts",
            "bachelors_science": "Bachelor of Science",
            "bachelors_technology": "Bachelor of Technology",
        }.get(_degree_kind(value), _string_value(value))
    return _string_value(value)


def _fallback_search_query(value: Any, question_type: str) -> str:
    if question_type in {"DEGREE", "HIGHEST_EDUCATION"}:
        return {
            "masters": "Master",
            "bachelors": "Bachelor",
            "doctorate": "Doctor",
        }.get(_degree_level(value), "")
    return ""


def _string_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{key}: {item}" for key, item in value.items())
    return str(value)


def _matches(value: Any, option: dict[str, str], question_type: str = "") -> bool:
    desired = _normal(value)
    aliases = {desired}
    if desired in ("true", "yes", "authorized", "willing"):
        aliases.update(("yes", "true"))
    if desired in ("false", "no", "not authorized", "not willing"):
        aliases.update(("no", "false"))
    if question_type in {"NOTICE_PERIOD", "AVAILABILITY"} and desired == "immediate":
        aliases.update(("immediately", "immediately available", "available immediately", "0 days", "zero days"))
    option_values = {_normal(option.get("value") or ""), _normal(option.get("label") or "")}
    if option_values & aliases:
        return True
    if question_type == "UNIVERSITY":
        return any(
            item == desired
            or (item.startswith(desired + " ") and bool(re.fullmatch(r"[a-z0-9]{2,8}", item[len(desired):].strip())))
            for item in option_values
        )
    if question_type == "CURRENT_CITY":
        # Location widgets commonly render a verified city as
        # "City, State, Country". Prefix matching is safe only because the
        # caller still requires exactly one matching option.
        return any(item == desired or item.startswith(desired + " ") for item in option_values)
    if question_type in {"DEGREE", "HIGHEST_EDUCATION"}:
        wanted_kind = _degree_kind(value)
        option_kinds = {kind for item in option_values if (kind := _degree_kind(item))}
        if wanted_kind and option_kinds:
            return option_kinds == {wanted_kind}
        wanted_level = _degree_level(value)
        levels = {level for item in option_values if (level := _degree_level(item))}
        return bool(wanted_level and levels == {wanted_level})
    return False


def _unique_option(value: Any, options: list[dict[str, str]], question_type: str) -> Optional[dict[str, str]]:
    matches = [item for item in options if _matches(value, item, question_type)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 and question_type in {"DEGREE", "HIGHEST_EDUCATION"}:
        # Greenhouse often includes both a generic canonical degree and
        # specialized degrees at the same level. Prefer only a canonical
        # generic label when the verified specific degree is unavailable.
        preferred = {
            "masters": ("master s degree", "masters degree", "masters", "master degree", "master"),
            "bachelors": ("bachelor s degree", "bachelors degree", "bachelors", "bachelor degree", "bachelor"),
            "doctorate": ("doctorate", "doctoral degree", "ph d", "phd"),
        }.get(_degree_level(value), ())
        for label in preferred:
            ranked = [
                item for item in matches
                if label in {_normal(item.get("label") or ""), _normal(item.get("value") or "")}
            ]
            if len(ranked) == 1:
                return ranked[0]
    return None


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
        blockers, warnings = detect_page_security_state(session.page)
        return InspectionResult(
            ats_provider=self.provider,
            url=session.page.url,
            fields=fields,
            blockers=blockers,
            warnings=warnings,
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
            expected = _string_value(value)
            locator.first.fill(expected)
            actual = locator.first.input_value()
            if field.field_type == "phone":
                expected_digits = re.sub(r"\D", "", expected)
                actual_digits = re.sub(r"\D", "", actual)
                acknowledged = bool(expected_digits) and actual_digits.endswith(expected_digits)
            else:
                acknowledged = actual == expected
            if not acknowledged:
                raise ValueError("The control did not acknowledge the filled value")
        elif field.field_type == "select" and field.control_kind == "combobox":
            locator.first.click()
            page.wait_for_timeout(250)
            options = frame.locator('[role="option"]:visible')
            try:
                options.first.wait_for(state="visible", timeout=2_000)
            except Exception:
                pass
            available = options.evaluate_all(
                "els => els.map(e => ({value: e.getAttribute('data-value') || e.innerText.trim(), label: e.innerText.trim()}))"
            )
            if not available:
                # Current Greenhouse React Select controls sometimes reserve
                # input clicks for search focus and open only via their
                # adjacent flyout button.
                try:
                    toggle = locator.first.locator(
                        "xpath=ancestor::div[contains(@class,'select-shell')][1]"
                        "//button[contains(@aria-label,'Toggle')]"
                    )
                    if toggle.count() == 1:
                        toggle.click()
                        try:
                            options.first.wait_for(state="visible", timeout=2_000)
                        except Exception:
                            page.wait_for_timeout(300)
                        available = options.evaluate_all(
                            "els => els.map(e => ({value: e.getAttribute('data-value') || e.innerText.trim(), label: e.innerText.trim()}))"
                        )
                except Exception:
                    pass
            if not _unique_option(value, list(available), field.canonical_question_type):
                # Searchable React controls frequently render options only
                # after receiving the exact verified value. Typing does not
                # select or submit anything.
                try:
                    locator.first.fill(_search_query(value, field.canonical_question_type))
                    try:
                        options.first.wait_for(state="visible", timeout=2_000)
                    except Exception:
                        page.wait_for_timeout(300)
                    available = options.evaluate_all(
                        "els => els.map(e => ({value: e.getAttribute('data-value') || e.innerText.trim(), label: e.innerText.trim()}))"
                    )
                except Exception:
                    pass
            if not available:
                fallback_query = _fallback_search_query(value, field.canonical_question_type)
                if fallback_query:
                    try:
                        locator.first.fill(fallback_query)
                        try:
                            options.first.wait_for(state="visible", timeout=2_000)
                        except Exception:
                            page.wait_for_timeout(300)
                        available = options.evaluate_all(
                            "els => els.map(e => ({value: e.getAttribute('data-value') || e.innerText.trim(), label: e.innerText.trim()}))"
                        )
                    except Exception:
                        pass
            field.options = list(available)
            option = _unique_option(value, field.options, field.canonical_question_type)
            if not option:
                page.keyboard.press("Escape")
                raise SemanticMatchError("No unique custom dropdown option exactly matches the verified answer")
            choices = options.filter(has_text=re.compile(rf"^\s*{re.escape(str(option['label']))}\s*$", re.I))
            if choices.count() != 1:
                page.keyboard.press("Escape")
                raise SemanticMatchError("Custom dropdown answer is ambiguous")
            choices.first.click()
            selected_input = ""
            selected_shell = ""
            try:
                selected_input = locator.first.input_value()
            except Exception:
                pass
            try:
                selected_shell = locator.first.locator(
                    "xpath=ancestor::div[contains(@class,'select-shell')][1]"
                ).inner_text()
            except Exception:
                pass
            wanted_option = _normal(option.get("label") or option.get("value") or "")
            if wanted_option not in {_normal(selected_input), _normal(selected_shell)} and wanted_option not in _normal(selected_shell):
                raise ValueError("The custom dropdown did not acknowledge the selected option")
        elif field.field_type == "select":
            option = _unique_option(value, field.options, field.canonical_question_type)
            if not option:
                raise SemanticMatchError("No unique dropdown option matches the verified answer")
            locator.first.select_option(value=option["value"])
            selected = locator.first.evaluate(
                "el => el.selectedOptions && el.selectedOptions.length ? "
                "{value: el.selectedOptions[0].value, label: el.selectedOptions[0].text} : null"
            )
            if not selected or not _matches(value, selected, field.canonical_question_type):
                raise ValueError("The native dropdown did not acknowledge the selected option")
        elif field.field_type == "radio":
            option = _unique_option(value, field.options, field.canonical_question_type)
            if not option:
                raise SemanticMatchError("No unique radio option matches the verified answer")
            frame.locator(f'{field.selector}[value={json.dumps(option["value"])}]').first.check()
            if not frame.locator(f'{field.selector}[value={json.dumps(option["value"])}]').first.is_checked():
                raise ValueError("The radio group did not acknowledge the selected option")
        elif field.field_type == "checkbox":
            if field.control_kind == "checkbox_group" and len(field.options) > 1:
                wanted = list(value) if isinstance(value, (list, tuple, set)) else [value]
                chosen = [_unique_option(item, field.options, field.canonical_question_type) for item in wanted]
                if any(item is None for item in chosen) or len({item["value"] for item in chosen if item}) != len(wanted):
                    raise SemanticMatchError("Checkbox group has no unambiguous option mapping")
                for item in chosen:
                    frame.locator(f'{field.selector}[value={json.dumps(item["value"])}]').first.check()
                    if not frame.locator(f'{field.selector}[value={json.dumps(item["value"])}]').first.is_checked():
                        raise ValueError("The checkbox group did not acknowledge the selected option")
            else:
                truthy = value is True or str(value).strip().lower() in ("yes", "true", "1", "checked")
                if truthy:
                    locator.first.check()
                else:
                    locator.first.uncheck()
                if locator.first.is_checked() != truthy:
                    raise ValueError("The checkbox did not acknowledge the selected state")
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
                    structural_ack = False
                    if not native_ack and not rendered_name:
                        try:
                            upload_group = frame.locator(
                                '[role="group"], .file-upload'
                            ).filter(has_text=re.compile(r"\b(resume|cv)\b", re.I))
                            acknowledgement_ui = upload_group.locator(
                                '.file-upload__filename, button[aria-label*="Remove file" i], '
                                'button[aria-label*="Remove attachment" i]'
                            )
                            acknowledgement_ui.first.wait_for(state="attached", timeout=2_000)
                            structural_ack = acknowledgement_ui.count() > 0
                        except Exception:
                            structural_ack = False
                    if not native_ack and not rendered_name and not structural_ack:
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
            except SemanticMatchError:
                field.answer_status = AnswerStatus.NEEDS_SEMANTIC_REVIEW.value
                field.answer_reason = "VERIFIED_VALUE_HAS_NO_UNAMBIGUOUS_CONTROL_MATCH"
                session.artifacts.log("field_needs_semantic_review", field_id=field.field_id, question_type=field.canonical_question_type)
            except Exception as exc:
                field.answer_status = AnswerStatus.ERROR.value
                field.answer_reason = f"FILL_ATTEMPT_FAILED:{type(exc).__name__}"
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
