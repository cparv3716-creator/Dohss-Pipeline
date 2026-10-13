"""Grounded free-text preparation contract with mandatory provenance."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence


FREE_TEXT_TYPES = {
    "WHY_COMPANY", "WHY_ROLE", "PROJECT_EXAMPLE", "LEADERSHIP_EXAMPLE", "OTHER",
}


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    source: str
    fact: str


@dataclass(frozen=True)
class FreeTextDraft:
    status: str
    text: str = ""
    provenance: list[str] = field(default_factory=list)
    reason: str = ""


class GroundedFreeTextProvider(Protocol):
    def generate(
        self,
        *,
        question: str,
        question_type: str,
        verified_evidence: Sequence[EvidenceRecord],
        job_description: str,
    ) -> tuple[str, Sequence[str]]:
        """Return draft text and the evidence IDs actually used."""


def prepare_free_text(
    question: str,
    question_type: str,
    verified_candidate: Mapping[str, object],
    job_description: str,
    provider: GroundedFreeTextProvider | None = None,
) -> FreeTextDraft:
    if question_type not in FREE_TEXT_TYPES:
        return FreeTextDraft("NOT_APPLICABLE", reason="QUESTION_IS_NOT_FREE_TEXT")
    evidence = [
        EvidenceRecord(f"profile.{name}", f"profile.{name}", repr(value))
        for name, value in verified_candidate.items()
        if value not in (None, "", [], {})
        and name in {"education", "work_experience", "internships", "projects", "research", "skills"}
    ]
    if not evidence or not str(job_description or "").strip():
        return FreeTextDraft("NEEDS_REVIEW", reason="INSUFFICIENT_VERIFIED_EVIDENCE")
    if provider is None:
        return FreeTextDraft("NEEDS_REVIEW", reason="NO_GROUNDED_TEXT_PROVIDER_CONFIGURED")
    text, used_ids = provider.generate(
        question=question,
        question_type=question_type,
        verified_evidence=evidence,
        job_description=job_description,
    )
    valid_ids = {item.evidence_id for item in evidence}
    provenance = list(dict.fromkeys(str(item) for item in used_ids))
    if not str(text or "").strip() or not provenance or any(item not in valid_ids for item in provenance):
        return FreeTextDraft("NEEDS_REVIEW", reason="INVALID_OR_MISSING_PROVENANCE")
    return FreeTextDraft("PREPARED_REVIEW_REQUIRED", str(text).strip(), provenance, "HUMAN_APPROVAL_REQUIRED")
