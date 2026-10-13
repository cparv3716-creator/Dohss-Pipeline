"""Job-application preparation services (never submits applications)."""

from .ats import detect_ats
from .eligibility import EligibilityResult, evaluate_eligibility
from .matching import DeterministicMatcher, MatchResult

__all__ = [
    "DeterministicMatcher", "EligibilityResult", "MatchResult",
    "detect_ats", "evaluate_eligibility",
]
