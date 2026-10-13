"""Orchestrates safe application preparation without browser submission."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from candidate.answer_bank import AnswerBank
from candidate.profile import CandidateProfile

from .eligibility import INELIGIBLE, UNKNOWN_REVIEW, evaluate_eligibility
from .matching import MatchingProvider, match_job
from .private_db import upsert_application, upsert_assessment


DEFAULT_REQUIRED_QUESTIONS = ("FULL_NAME", "EMAIL", "PHONE")


def score_job(profile: CandidateProfile, job: Mapping[str, Any], private_db_path: str, provider: MatchingProvider | None = None):
    eligibility = evaluate_eligibility(profile, job)
    match = match_job(profile, job, eligibility, provider)
    upsert_assessment(private_db_path, int(job["id"]), eligibility, match)
    return eligibility, match


def prepare_job(
    profile: CandidateProfile,
    answer_bank: AnswerBank,
    job: Mapping[str, Any],
    private_db_path: str,
    required_questions: Iterable[str] = DEFAULT_REQUIRED_QUESTIONS,
    provider: MatchingProvider | None = None,
) -> dict[str, Any]:
    eligibility, match = score_job(profile, job, private_db_path, provider)
    prepared_answers: dict[str, Any] = {}
    unanswered: list[str] = []
    for question_type in required_questions:
        answer = answer_bank.lookup(question_type, profile=profile)
        if answer and answer.usable:
            prepared_answers[question_type] = {
                "value": answer.value,
                "source": answer.source,
                "confidence": answer.confidence,
            }
        else:
            unanswered.append(question_type)

    if eligibility.status == INELIGIBLE or match.recommendation == "SKIP":
        status = "SKIPPED"
    elif eligibility.status == UNKNOWN_REVIEW or match.recommendation == "REVIEW" or unanswered:
        status = "NEEDS_REVIEW"
    else:
        status = "READY_TO_APPLY"
    requires_review = status == "NEEDS_REVIEW"
    payload = {
        "job_id": int(job["id"]),
        "application_url": job.get("application_url") or job.get("url") or "",
        "ats_provider": job.get("ats_provider") or "Unknown",
        "match_score": match.match_score,
        "status": status,
        "prepared_answers": prepared_answers,
        "unanswered_questions": unanswered,
        "requires_manual_review": requires_review,
        "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "transition_reason": "application preparation completed; no submission attempted",
    }
    application = upsert_application(private_db_path, payload)
    return {
        "job": dict(job), "eligibility": eligibility, "match": match,
        "application": application, "unanswered_questions": unanswered,
    }
