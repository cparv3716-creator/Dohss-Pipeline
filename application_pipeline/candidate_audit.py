"""Privacy-safe candidate/profile completeness reporting."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from candidate import AnswerBank, CandidateProfile

from .browser.safety import ResumePolicy, SENSITIVE_TYPES


@dataclass(frozen=True)
class CandidateAudit:
    sections: dict[str, str]
    verified_skill_count: int
    missing_review: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _has(profile: CandidateProfile, *names: str) -> bool:
    return all(profile.verified_value(name) not in (None, "", [], {}) for name in names)


def _count_values(value: Any) -> int:
    if value in (None, "", [], {}):
        return 0
    if isinstance(value, dict):
        return len([item for item in value.values() if item not in (None, "", [], {})])
    if isinstance(value, (list, tuple, set)):
        return len([item for item in value if item not in (None, "", [], {})])
    return 1


def audit_candidate(
    profile: CandidateProfile,
    bank: AnswerBank,
    resume_policy: ResumePolicy,
    enabled_sensitive_types: set[str] | None = None,
) -> CandidateAudit:
    skills = sum(
        _count_values(profile.verified_value(name))
        for name in ("skills", "programming_languages", "software_tools")
    )
    sections = {
        "identity": "complete" if _has(profile, "full_name", "email", "phone") else "incomplete",
        "education": "complete" if _has(profile, "degrees", "institutions") else "incomplete",
        "skills": f"{skills} verified",
        "employment history": "complete" if any(
            profile.verified_value(name) not in (None, "", [], {})
            for name in ("work_experience", "internships")
        ) else "incomplete",
        "work authorization": "configured" if _has(profile, "work_authorization") else "incomplete",
        "salary preferences": "configured" if _has(profile, "salary_preferences") else "unknown",
        "relocation": "configured" if _has(profile, "relocation_preferences") else "unknown",
        "notice period": "configured" if _has(profile, "notice_period") else "unknown",
    }
    missing = []
    for question_type, label in (
        ("EXPECTED_CTC", "expected CTC"),
        ("WORK_AUTHORIZATION", "work authorization"),
        ("VISA_SPONSORSHIP", "visa sponsorship"),
    ):
        if bank.lookup(question_type, profile=profile) is None:
            missing.append(label)

    warnings = []
    risky = [
        entry.question_type for entry in bank.entries
        if entry.auto_answer_allowed and (
            entry.question_type in SENSITIVE_TYPES | {"WORK_AUTHORIZATION"}
            or entry.sensitivity not in {"public", "private", "personal"}
        )
    ]
    if risky:
        warnings.append(f"{len(risky)} sensitive answer permission(s) require policy review")
    if enabled_sensitive_types:
        warnings.append(f"{len(enabled_sensitive_types)} sensitive question type(s) enabled by browser policy")
    try:
        resume = resume_policy.validate()
        sections["resume"] = "configured" if resume else "not configured"
    except ValueError:
        sections["resume"] = "configuration invalid"
        warnings.append("configured resume failed private-path validation")
    return CandidateAudit(sections, skills, missing, warnings)


def format_candidate_audit(audit: CandidateAudit) -> str:
    lines = ["Candidate profile:"]
    lines.extend(f"- {name}: {status}" for name, status in audit.sections.items())
    lines.append("")
    lines.append("Missing information requiring review:")
    lines.extend(f"- {item}" for item in audit.missing_review)
    if not audit.missing_review:
        lines.append("- none")
    if audit.warnings:
        lines.extend(("", "Safety warnings:"))
        lines.extend(f"- {item}" for item in audit.warnings)
    return "\n".join(lines)
