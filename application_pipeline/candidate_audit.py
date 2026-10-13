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


def _flatten_values(value: Any) -> set[str]:
    if value in (None, "", [], {}):
        return set()
    if isinstance(value, dict):
        flattened: set[str] = set()
        for item in value.values():
            flattened.update(_flatten_values(item))
        return flattened
    if isinstance(value, (list, tuple, set)):
        flattened = set()
        for item in value:
            flattened.update(_flatten_values(item))
        return flattened
    return {str(value).strip().casefold()}


def audit_candidate(
    profile: CandidateProfile,
    bank: AnswerBank,
    resume_policy: ResumePolicy,
    enabled_sensitive_types: set[str] | None = None,
) -> CandidateAudit:
    verified_skills: set[str] = set()
    for name in ("skills", "programming_languages", "software_tools"):
        verified_skills.update(
            _flatten_values(profile.verified_value(name))
        )
    skills = len(verified_skills)
    sections = {
        "identity": "complete" if _has(profile, "full_name", "email", "phone") else "incomplete",
        "education": "complete" if _has(profile, "degrees", "institutions") else "incomplete",
        "projects": "complete" if _has(profile, "projects") else "incomplete",
        "research": "complete" if _has(profile, "research") else "incomplete",
        "competitions": "complete" if _has(profile, "competitions") else "incomplete",
        "positions of responsibility": "complete" if _has(profile, "positions_of_responsibility") else "incomplete",
        "coursework": "complete" if _has(profile, "coursework") else "incomplete",
        "skills": f"{skills} verified",
        "GitHub": "configured" if _has(profile, "github") else "unknown",
        "employment history": "complete" if any(
            profile.verified_value(name) not in (None, "", [], {})
            for name in ("work_experience", "internships")
        ) else "incomplete",
        "work authorization": "configured" if _has(profile, "work_authorization") else "unknown",
        "visa sponsorship": "configured" if _has(profile, "visa_sponsorship_requirements") else "unknown",
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
