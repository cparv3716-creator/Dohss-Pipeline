"""Verified application answer bank; unknown answers are never fabricated."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from .profile import CandidateProfile, ProfileValidationError


CANONICAL_QUESTION_TYPES = (
    "FULL_NAME", "EMAIL", "PHONE", "CURRENT_CITY", "LINKEDIN", "GITHUB",
    "PORTFOLIO", "HIGHEST_EDUCATION", "DEGREE", "UNIVERSITY",
    "GRADUATION_YEAR", "CGPA", "NOTICE_PERIOD", "CURRENT_CTC", "EXPECTED_CTC",
    "WORK_AUTHORIZATION", "VISA_SPONSORSHIP", "RELOCATION",
    "YEARS_OF_EXPERIENCE", "SKILL_EXPERIENCE", "AVAILABILITY", "WHY_COMPANY",
    "WHY_ROLE", "PROJECT_EXAMPLE", "LEADERSHIP_EXAMPLE", "OTHER",
)

PROFILE_FALLBACKS = {
    "FULL_NAME": "full_name", "EMAIL": "email", "PHONE": "phone",
    "CURRENT_CITY": "city", "LINKEDIN": "linkedin", "GITHUB": "github",
    "PORTFOLIO": "portfolio", "DEGREE": "degrees", "UNIVERSITY": "institutions",
    "GRADUATION_YEAR": "graduation_dates", "CGPA": "cgpa",
    "NOTICE_PERIOD": "notice_period", "WORK_AUTHORIZATION": "work_authorization",
    "VISA_SPONSORSHIP": "visa_sponsorship_requirements",
    "RELOCATION": "relocation_preferences", "YEARS_OF_EXPERIENCE": "years_of_experience",
}


@dataclass(frozen=True)
class AnswerEntry:
    question_type: str
    value: Any
    verified: bool
    auto_answer_allowed: bool
    sensitivity: str
    source: str
    confidence: int
    skill: str = ""

    @classmethod
    def from_raw(cls, question_type: str, raw: Mapping[str, Any]) -> "AnswerEntry":
        if question_type not in CANONICAL_QUESTION_TYPES:
            raise ProfileValidationError(f"unknown canonical question type: {question_type}")
        if not isinstance(raw.get("verified"), bool) or not isinstance(raw.get("auto_answer_allowed"), bool):
            raise ProfileValidationError(f"{question_type} verification flags must be booleans")
        confidence = raw.get("confidence", 0)
        if not isinstance(confidence, int) or not 0 <= confidence <= 100:
            raise ProfileValidationError(f"{question_type}.confidence must be an integer from 0 to 100")
        value = raw.get("value")
        verified = raw["verified"]
        if verified and value in (None, ""):
            raise ProfileValidationError(f"{question_type} cannot be verified without a value")
        return cls(
            question_type=question_type, value=value, verified=verified,
            auto_answer_allowed=raw["auto_answer_allowed"],
            sensitivity=str(raw.get("sensitivity") or "private"),
            source=str(raw.get("source") or ""), confidence=confidence,
            skill=str(raw.get("skill") or ""),
        )

    @property
    def usable(self) -> bool:
        return self.verified and self.auto_answer_allowed and self.value not in (None, "")


class AnswerBank:
    def __init__(self, entries: list[AnswerEntry], source_path: Optional[Path] = None):
        self.entries = entries
        self.source_path = source_path

    @classmethod
    def empty(cls) -> "AnswerBank":
        return cls([])

    @classmethod
    def load(cls, path: str | Path, required: bool = False) -> "AnswerBank":
        source = Path(path)
        if not source.exists():
            if required:
                raise FileNotFoundError(f"Application answer bank not found: {source}")
            return cls.empty()
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        answers = raw.get("answers", {})
        if not isinstance(answers, Mapping):
            raise ProfileValidationError("answer bank must contain an 'answers' mapping")
        entries: list[AnswerEntry] = []
        for question_type, values in answers.items():
            rows = values if isinstance(values, list) else [values]
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ProfileValidationError(f"{question_type} answer must be an object")
                entries.append(AnswerEntry.from_raw(question_type, row))
        return cls(entries, source)

    def lookup(self, question_type: str, *, skill: str = "", profile: Optional[CandidateProfile] = None) -> Optional[AnswerEntry]:
        for entry in self.entries:
            if entry.question_type != question_type:
                continue
            if question_type == "SKILL_EXPERIENCE" and skill and entry.skill.lower() != skill.lower():
                continue
            if entry.verified and entry.value not in (None, ""):
                return entry
        field = PROFILE_FALLBACKS.get(question_type)
        if profile and field:
            value = profile.verified_value(field)
            if value not in (None, "", [], {}):
                return AnswerEntry(question_type, value, True, False, "private", f"profile.{field}", 100, skill)
        return None
