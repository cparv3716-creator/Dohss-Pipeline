"""Strict candidate profile loading with explicit verified/unknown facts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import yaml


class ProfileValidationError(ValueError):
    """Raised when candidate data could be mistaken for a verified fact."""


@dataclass(frozen=True)
class Fact:
    value: Any = None
    verified: bool = False
    unknown: bool = True
    source: str = ""

    @classmethod
    def from_raw(cls, name: str, raw: Any) -> "Fact":
        if not isinstance(raw, Mapping):
            raise ProfileValidationError(
                f"{name} must be an object with value, verified, and unknown fields"
            )
        verified = raw.get("verified")
        unknown = raw.get("unknown")
        if not isinstance(verified, bool) or not isinstance(unknown, bool):
            raise ProfileValidationError(f"{name}.verified and {name}.unknown must be booleans")
        value = raw.get("value")
        if unknown and value not in (None, "", [], {}):
            raise ProfileValidationError(f"{name} cannot have a value while unknown=true")
        if verified and unknown:
            raise ProfileValidationError(f"{name} cannot be both verified and unknown")
        if verified and value in (None, ""):
            raise ProfileValidationError(f"{name} cannot be verified without a value")
        # A non-empty but unverified value may be retained as a note, but is never
        # exposed through verified_value().
        return cls(value=value, verified=verified, unknown=unknown, source=str(raw.get("source") or ""))

    def verified_value(self, default: Any = None) -> Any:
        return self.value if self.verified and not self.unknown else default


PROFILE_FIELDS = (
    "full_name", "email", "phone", "city", "linkedin", "github", "portfolio",
    "education", "degrees", "institutions", "graduation_dates", "cgpa",
    "work_experience", "internships", "projects", "research", "skills",
    "programming_languages", "software_tools", "experience_levels",
    "years_of_experience", "target_roles", "preferred_locations",
    "remote_preference", "relocation_preferences", "employment_type",
    "notice_period", "work_authorization", "visa_sponsorship_requirements",
    "salary_preferences", "application_preferences",
)


class CandidateProfile:
    def __init__(self, facts: Mapping[str, Fact], source_path: Optional[Path] = None):
        self._facts = dict(facts)
        self.source_path = source_path

    @classmethod
    def load(cls, path: str | Path) -> "CandidateProfile":
        source = Path(path)
        if not source.exists():
            raise FileNotFoundError(
                f"Candidate profile not found: {source}. Copy candidate/profile.example.yaml "
                "to candidate/profile.yaml and fill only facts you can verify."
            )
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, Mapping):
            raise ProfileValidationError("candidate profile root must be a mapping")
        version = raw.get("version", 1)
        if version != 1:
            raise ProfileValidationError(f"unsupported candidate profile version: {version}")
        facts_raw = raw.get("profile")
        if not isinstance(facts_raw, Mapping):
            raise ProfileValidationError("candidate profile must contain a 'profile' mapping")
        unknown_fields = sorted(set(facts_raw) - set(PROFILE_FIELDS))
        if unknown_fields:
            raise ProfileValidationError(f"unknown candidate profile fields: {', '.join(unknown_fields)}")
        facts = {
            name: Fact.from_raw(f"profile.{name}", facts_raw.get(name, _unknown_fact()))
            for name in PROFILE_FIELDS
        }
        return cls(facts, source)

    def fact(self, name: str) -> Fact:
        if name not in self._facts:
            raise KeyError(name)
        return self._facts[name]

    def verified_value(self, name: str, default: Any = None) -> Any:
        return self.fact(name).verified_value(default)

    def verified_facts(self, names: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        selected = names or self._facts.keys()
        return {
            name: self._facts[name].value
            for name in selected
            if name in self._facts and self._facts[name].verified and not self._facts[name].unknown
        }

    def missing(self, names: Iterable[str]) -> list[str]:
        return [name for name in names if self.verified_value(name) in (None, "", [], {})]


def _unknown_fact() -> dict[str, Any]:
    return {"value": None, "verified": False, "unknown": True, "source": ""}
