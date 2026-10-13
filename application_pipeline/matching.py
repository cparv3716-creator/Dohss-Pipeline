"""Provider-independent matching contracts and deterministic fallback."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping

from candidate.profile import CandidateProfile
from .eligibility import EligibilityResult, INELIGIBLE, UNKNOWN_REVIEW


@dataclass(frozen=True)
class MatchResult:
    match_score: int
    recommendation: str
    confidence: int
    strengths: list[str]
    gaps: list[str]
    reasoning: str
    missing_information: list[str]

    def __post_init__(self) -> None:
        if not isinstance(self.match_score, int) or not 0 <= self.match_score <= 100:
            raise ValueError("match_score must be an integer from 0 to 100")
        if not isinstance(self.confidence, int) or not 0 <= self.confidence <= 100:
            raise ValueError("confidence must be an integer from 0 to 100")
        if self.recommendation not in ("APPLY", "REVIEW", "SKIP"):
            raise ValueError("recommendation must be APPLY, REVIEW, or SKIP")


class MatchingProvider(ABC):
    @abstractmethod
    def match(self, verified_candidate: Mapping[str, Any], job: Mapping[str, Any]) -> MatchResult:
        """Match only the supplied verified candidate facts to a job."""


def _tokens(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        value = " ".join(str(v) for v in value.values())
    elif isinstance(value, (list, tuple, set)):
        value = " ".join(str(v) for v in value)
    return {token for token in re.findall(r"[a-z0-9+#.]{2,}", str(value or "").lower())}


class DeterministicMatcher(MatchingProvider):
    """Offline matcher intended for deterministic tests and no-key operation."""

    def match(self, verified_candidate: Mapping[str, Any], job: Mapping[str, Any]) -> MatchResult:
        candidate_skills = set().union(
            _tokens(verified_candidate.get("skills")),
            _tokens(verified_candidate.get("programming_languages")),
            _tokens(verified_candidate.get("software_tools")),
        )
        required = _tokens(job.get("required_skills"))
        preferred = _tokens(job.get("preferred_skills"))
        title_tokens = _tokens(job.get("title"))
        target_tokens = _tokens(verified_candidate.get("target_roles"))
        required_matches = sorted(required & candidate_skills)
        required_gaps = sorted(required - candidate_skills)
        preferred_matches = sorted(preferred & candidate_skills)
        role_overlap = title_tokens & target_tokens

        components: list[tuple[float, float]] = []
        if required:
            components.append((len(required_matches) / len(required), 0.55))
        if preferred:
            components.append((len(preferred_matches) / len(preferred), 0.15))
        if title_tokens and target_tokens:
            components.append((min(len(role_overlap) / max(len(title_tokens), 1) * 2, 1.0), 0.20))
        description_tokens = _tokens(job.get("full_description") or job.get("description"))
        evidence_tokens = set().union(candidate_skills, _tokens(verified_candidate.get("projects")), _tokens(verified_candidate.get("work_experience")), _tokens(verified_candidate.get("research")))
        if description_tokens and evidence_tokens:
            components.append((min(len(description_tokens & evidence_tokens) / 12, 1.0), 0.10))

        if components:
            total_weight = sum(weight for _, weight in components)
            score = round(100 * sum(value * weight for value, weight in components) / total_weight)
        else:
            score = 0
        missing = []
        if not candidate_skills:
            missing.append("verified skills")
        if not target_tokens:
            missing.append("verified target roles")
        if not job.get("full_description") and not job.get("description"):
            missing.append("full job description")
        confidence = max(10, min(100, 90 - 18 * len(missing)))
        recommendation = "APPLY" if score >= 70 and confidence >= 55 else "REVIEW" if score >= 40 or missing else "SKIP"
        strengths = [f"Required skill match: {skill}" for skill in required_matches[:5]]
        strengths += [f"Preferred skill match: {skill}" for skill in preferred_matches[:3]]
        if role_overlap:
            strengths.append("Role overlaps verified target roles")
        gaps = [f"Required skill not verified: {skill}" for skill in required_gaps[:8]]
        return MatchResult(
            score, recommendation, confidence, strengths, gaps,
            "Deterministic score based only on verified skills, target roles, and experience evidence.",
            missing,
        )


def match_job(profile: CandidateProfile, job: Mapping[str, Any], eligibility: EligibilityResult, provider: MatchingProvider | None = None) -> MatchResult:
    matcher = provider or DeterministicMatcher()
    result = matcher.match(profile.verified_facts(), job)
    if eligibility.status == INELIGIBLE:
        return MatchResult(result.match_score, "SKIP", result.confidence, result.strengths, result.gaps, "Hard eligibility failed. " + result.reasoning, result.missing_information)
    if eligibility.status == UNKNOWN_REVIEW and result.recommendation == "APPLY":
        return MatchResult(result.match_score, "REVIEW", result.confidence, result.strengths, result.gaps, "Hard eligibility requires review. " + result.reasoning, result.missing_information)
    return result
