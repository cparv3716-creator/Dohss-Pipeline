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
    NEEDS_SEMANTIC_REVIEW = "NEEDS_SEMANTIC_REVIEW"
    HIDDEN_INACTIVE = "HIDDEN_INACTIVE"
    BLOCKED_AUTHENTICATION = "BLOCKED_AUTHENTICATION"
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
    control_kind: str = "native"
    detected_pass: int = 1
    visible: bool = True
    enabled: bool = True
    required_source: str = ""
    logical_id: str = ""
    semantic_key: str = ""
    proposed_value: Any = field(default=None, repr=False)

    def to_dict(self, include_proposed_value: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_proposed_value:
            data.pop("proposed_value", None)
        if data.get("current_value") not in (None, "", False, []):
            data["current_value"] = "[REDACTED]"
        source = str(data.get("answer_source") or "")
        if source and not source.startswith("profile.") and source != "configured_resume":
            data["answer_source"] = "verified_answer_bank"
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
    required_controls_seen: int = 0


@dataclass
class DryRunReport:
    application_id: int
    company: str
    role: str
    ats_provider: str
    url: str
    fields: list[ApplicationField]
    authentication_blockers: list[str] = field(default_factory=list)
    security_observations: list[str] = field(default_factory=list)
    validation_issues: list[ValidationIssue] = field(default_factory=list)
    resume_uploaded: bool = False
    resume_identifier: str = ""
    screenshot_paths: list[str] = field(default_factory=list)
    html_snapshot_path: str = ""
    final_status: str = "FORM_INSPECTED"
    run_status: str = "DRY_RUN_COMPLETE"
    submission: str = "FINAL_SUBMIT_BLOCKED_BY_POLICY"
    submission_requests_blocked: list[str] = field(default_factory=list)
    report_path: str = ""
    inspection_passes: int = 1
    conditional_fields_detected: int = 0
    readiness: dict[str, Any] = field(default_factory=dict)
    free_text_drafts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        fields = [item.to_dict() for item in self.fields]
        fill_eligible = sum(
            item["answer_status"] in {
                AnswerStatus.FILLED.value, AnswerStatus.ERROR.value,
                AnswerStatus.NEEDS_SEMANTIC_REVIEW.value, AnswerStatus.PROPOSED.value,
            }
            for item in fields if item["field_type"] != FieldType.FILE.value and item["visible"]
        )
        correctly_filled = sum(item["answer_status"] == AnswerStatus.FILLED.value for item in fields)
        return {
            "application_id": self.application_id,
            "company": self.company,
            "role": self.role,
            "ats_provider": self.ats_provider,
            "url": self.url,
            "fields_detected": len(fields),
            "unique_logical_fields": len({item.get("logical_id") or item["field_id"] for item in fields}),
            "answers_filled": sum(item["answer_status"] == AnswerStatus.FILLED.value for item in fields),
            "answers_skipped": sum(item["answer_status"].startswith("SKIPPED") for item in fields),
            "missing_answers": [item["raw_question"] for item in fields if item["visible"] and item["required"] and item["answer_status"] not in (AnswerStatus.FILLED.value, AnswerStatus.UPLOADED.value)],
            "sensitive_questions": [item["raw_question"] for item in fields if item["visible"] and item["answer_status"] == AnswerStatus.SKIPPED_SENSITIVE.value],
            "fields": fields,
            "authentication_blockers": self.authentication_blockers,
            "security_observations": self.security_observations,
            "validation_issues": [asdict(item) for item in self.validation_issues],
            "resume_uploaded": self.resume_uploaded,
            "resume_identifier": self.resume_identifier,
            "screenshot_paths": self.screenshot_paths,
            "html_snapshot_path": self.html_snapshot_path,
            "final_status": self.final_status,
            "run_status": self.run_status,
            "submission": self.submission,
            "submission_requests_blocked": self.submission_requests_blocked,
            "report_path": self.report_path,
            "inspection_passes": self.inspection_passes,
            "conditional_fields_detected": self.conditional_fields_detected,
            "readiness": self.readiness,
            "field_metrics": {
                "eligible_safe_fill_fields": fill_eligible,
                "correctly_filled": correctly_filled,
                "fill_attempt_failed": sum(item["answer_status"] == AnswerStatus.ERROR.value for item in fields),
                "known_but_policy_blocked": sum(
                    bool(item["answer_verified"]) and item["answer_status"] in {
                        AnswerStatus.SKIPPED_POLICY.value, AnswerStatus.SKIPPED_SENSITIVE.value,
                    } for item in fields
                ),
                "unknown_answer": sum(item["answer_status"] == AnswerStatus.SKIPPED_UNKNOWN.value for item in fields),
                "needs_semantic_review": sum(item["answer_status"] == AnswerStatus.NEEDS_SEMANTIC_REVIEW.value for item in fields),
                "hidden_inactive": sum(not item["visible"] or item["answer_status"] == AnswerStatus.HIDDEN_INACTIVE.value for item in fields),
                "eligible_safe_fill_success_rate": round(100 * correctly_filled / fill_eligible, 1) if fill_eligible else None,
            },
            # Draft text remains in memory and is not persisted in the
            # privacy-safe report. Provenance and approval state are retained.
            "free_text_drafts": [
                {key: value for key, value in draft.items() if key != "text"}
                for draft in self.free_text_drafts
            ],
        }
