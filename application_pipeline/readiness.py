"""Pre-submission readiness scoring, separate from job match quality."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .browser.models import AnswerStatus, ApplicationField, ValidationIssue


RESOLVED = {AnswerStatus.FILLED.value, AnswerStatus.UPLOADED.value}


@dataclass(frozen=True)
class ReadinessResult:
    score: int
    status: str
    required_fields: int
    required_resolved: int
    safely_resolved: int
    manual_review: int
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "status": self.status,
            "required_fields": self.required_fields,
            "required_resolved": self.required_resolved,
            "safely_resolved": self.safely_resolved,
            "manual_review": self.manual_review,
            "reasons": self.reasons,
        }


def calculate_readiness(
    fields: Iterable[ApplicationField],
    blockers: Iterable[str] = (),
    validation_issues: Iterable[ValidationIssue] = (),
    *,
    resume_required: bool = False,
    resume_uploaded: bool = False,
) -> ReadinessResult:
    fields = [item for item in fields if item.visible]
    blockers = list(blockers)
    issues = list(validation_issues)
    required = [item for item in fields if item.required]
    required_resolved = sum(item.answer_status in RESOLVED for item in required)
    safely_resolved = sum(item.answer_status in RESOLVED for item in fields)
    sensitive = sum(item.answer_status == AnswerStatus.SKIPPED_SENSITIVE.value for item in fields)
    required_ratio = required_resolved / len(required) if required else 0.0
    resolved_fields = [item for item in fields if item.answer_status in RESOLVED]
    confidence_ratio = (
        sum(max(0, min(100, item.confidence)) for item in resolved_fields)
        / (100 * len(resolved_fields))
        if resolved_fields else 0.0
    )
    required_errors = [issue for issue in issues if issue.required]
    resume_component = 1.0 if not resume_required or resume_uploaded else 0.0
    validation_component = 0.0 if required_errors else 1.0
    auth_component = 0.0 if blockers else 1.0
    score = round(100 * (
        0.60 * required_ratio
        + 0.15 * confidence_ratio
        + 0.10 * resume_component
        + 0.10 * auth_component
        + 0.05 * validation_component
    ))
    reasons = []
    if blockers:
        reasons.extend(blockers)
    if required_resolved < len(required):
        reasons.append("REQUIRED_FIELDS_UNRESOLVED")
    if resume_required and not resume_uploaded:
        reasons.append("RESUME_NOT_UPLOADED")
    if required_errors:
        reasons.append("VALIDATION_ERRORS")
    if sensitive:
        reasons.append("SENSITIVE_REVIEW_REQUIRED")
    if blockers:
        status = "NEEDS_USER_ACTION"
    elif required_errors or required_resolved < len(required) or (resume_required and not resume_uploaded):
        status = "NEEDS_INFORMATION"
    elif not fields:
        status = "BLOCKED"
        reasons.append("NO_FORM_FIELDS_DETECTED")
    else:
        status = "READY_FOR_REVIEW"
    manual_review = len(fields) - safely_resolved
    return ReadinessResult(score, status, len(required), required_resolved, safely_resolved, manual_review, list(dict.fromkeys(reasons)))
