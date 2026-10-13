"""Normalized application-form and dry-run models."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class FieldType(str, Enum):
    TEXT = "text"
    EMAIL = "email"
    PHONE = "phone"
    TEXTAREA = "textarea"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    SELECT = "select"
    FILE = "file"
    NUMBER = "number"
    DATE = "date"
    UNKNOWN = "unknown"


class AnswerStatus(str, Enum):
    UNRESOLVED = "UNRESOLVED"
    PROPOSED = "PROPOSED"
    FILLED = "FILLED"
    SKIPPED_UNKNOWN = "SKIPPED_UNKNOWN"
    SKIPPED_SENSITIVE = "SKIPPED_SENSITIVE"
    SKIPPED_POLICY = "SKIPPED_POLICY"
    UPLOADED = "UPLOADED"
    ERROR = "ERROR"


class AuthenticationBlocker(str, Enum):
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    MFA_REQUIRED = "MFA_REQUIRED"
    OTP_REQUIRED = "OTP_REQUIRED"
    BOT_DETECTION = "BOT_DETECTION"


@dataclass
class ApplicationField:
    field_id: str
    label: str
    raw_question: str
    field_type: str
    required: bool
    options: list[dict[str, str]] = field(default_factory=list)
    current_value: Any = None
    canonical_question_type: str = "OTHER"
    skill: str = ""
    sensitivity: str = "private"
    confidence: int = 0
    answer_source: str = ""
    answer_verified: bool = False
    auto_fill_allowed: bool = False
    answer_status: str = AnswerStatus.UNRESOLVED.value
    answer_reason: str = ""
    selector: str = ""
    frame_url: str = ""
    proposed_value: Any = field(default=None, repr=False)

    def to_dict(self, include_proposed_value: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_proposed_value:
            data.pop("proposed_value", None)
        return data


@dataclass(frozen=True)
class ValidationIssue:
    field_id: str
    code: str
    message: str
    required: bool = False


@dataclass
class InspectionResult:
    ats_provider: str
    url: str
    fields: list[ApplicationField]
    blockers: list[str] = field(default_factory=list)
    final_submit_controls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class DryRunReport:
    application_id: int
    company: str
    role: str
    ats_provider: str
    url: str
    fields: list[ApplicationField]
    authentication_blockers: list[str] = field(default_factory=list)
    validation_issues: list[ValidationIssue] = field(default_factory=list)
    resume_uploaded: bool = False
    resume_path: str = ""
    screenshot_paths: list[str] = field(default_factory=list)
    html_snapshot_path: str = ""
    final_status: str = "FORM_INSPECTED"
    run_status: str = "DRY_RUN_COMPLETE"
    submission: str = "FINAL_SUBMIT_BLOCKED_BY_POLICY"
    submission_requests_blocked: list[str] = field(default_factory=list)
    report_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        fields = [item.to_dict() for item in self.fields]
        return {
            "application_id": self.application_id,
            "company": self.company,
            "role": self.role,
            "ats_provider": self.ats_provider,
            "url": self.url,
            "fields_detected": len(fields),
            "answers_filled": sum(item["answer_status"] == AnswerStatus.FILLED.value for item in fields),
            "answers_skipped": sum(item["answer_status"].startswith("SKIPPED") for item in fields),
            "missing_answers": [item["raw_question"] for item in fields if item["required"] and item["answer_status"] not in (AnswerStatus.FILLED.value, AnswerStatus.UPLOADED.value)],
            "sensitive_questions": [item["raw_question"] for item in fields if item["answer_status"] == AnswerStatus.SKIPPED_SENSITIVE.value],
            "fields": fields,
            "authentication_blockers": self.authentication_blockers,
            "validation_issues": [asdict(item) for item in self.validation_issues],
            "resume_uploaded": self.resume_uploaded,
            "resume_path": self.resume_path,
            "screenshot_paths": self.screenshot_paths,
            "html_snapshot_path": self.html_snapshot_path,
            "final_status": self.final_status,
            "run_status": self.run_status,
            "submission": self.submission,
            "submission_requests_blocked": self.submission_requests_blocked,
            "report_path": self.report_path,
        }
