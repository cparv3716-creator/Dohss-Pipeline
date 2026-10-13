"""CLI integration for the safe preparation milestone."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from candidate import AnswerBank, CandidateProfile
from database import db

from .enrichment import JobDetailEnricher
from .preparation import prepare_job, score_job
from .private_db import get_assessment, list_applications


def settings(config: dict[str, Any]) -> dict[str, Any]:
    app = config.get("applications", {}) or {}
    return {
        "profile_path": app.get("profile_path", "candidate/profile.yaml"),
        "answer_bank_path": app.get("answer_bank_path", "candidate/application_answers.yaml"),
        "private_db_path": app.get("private_db_path", "data/private/applications.db"),
        "required_questions": app.get("required_questions", ["FULL_NAME", "EMAIL", "PHONE"]),
        "notify_telegram": bool(app.get("notify_telegram", False)),
        "enrichment_timeout": int(app.get("enrichment_timeout_seconds", 15)),
    }


def _limit(args: list[str], default: int = 25) -> int:
    if "--limit" in args:
        try:
            return max(1, int(args[args.index("--limit") + 1]))
        except (ValueError, IndexError):
            raise ValueError("--limit requires a positive integer")
    numeric = [arg for arg in args if arg.isdigit()]
    return int(numeric[0]) if numeric else default


def discover_jobs(config: dict[str, Any]) -> tuple[int, list[str]]:
    """Run existing job sources without notifications or contact enrichment."""
    public_db = config.get("database", {}).get("path", "data/pipeline.db")
    sources = []
    errors: list[str] = []
    for label, module, function in (
        ("LinkedIn", "scrapers.linkedin_jobs", "run_all_keywords"),
        ("Naukri", "scrapers.naukri", "run_all_keywords"),
        ("Internshala", "scrapers.internshala", "run_all_keywords"),
        ("Google Alerts", "scrapers.google_alerts", "run_all_feeds"),
    ):
        try:
            imported = __import__(module, fromlist=[function])
            sources.append((label, getattr(imported, function)(config)))
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
    inserted = 0
    for _, jobs in sources:
        for job in jobs:
            try:
                inserted += int(db.insert_job(public_db, job))
            except Exception as exc:
                errors.append(f"job insert: {type(exc).__name__}: {exc}")
    return inserted, errors


def enrich_jobs(config: dict[str, Any], args: list[str]) -> list[dict[str, Any]]:
    public_db = config.get("database", {}).get("path", "data/pipeline.db")
    db.init_db(public_db)
    limit = _limit(args)
    jobs = db.get_jobs(public_db, limit=limit, enrichment_pending="--all" not in args)
    enricher = JobDetailEnricher(settings(config)["enrichment_timeout"])
    results = []
    for job in jobs:
        result = enricher.enrich(job)
        db.update_job_enrichment(public_db, job["id"], result)
        results.append({"job_id": job["id"], "company": job["company"], "title": job["title"], "status": result.status, "error": result.error})
    return results


def _load_private_inputs(config: dict[str, Any]) -> tuple[CandidateProfile, AnswerBank, dict[str, Any]]:
    cfg = settings(config)
    public_db = Path(config.get("database", {}).get("path", "data/pipeline.db")).resolve()
    private_db = Path(cfg["private_db_path"]).resolve()
    if public_db == private_db:
        raise ValueError("applications.private_db_path must be separate from the public discovery database")
    profile = CandidateProfile.load(cfg["profile_path"])
    bank = AnswerBank.load(cfg["answer_bank_path"])
    return profile, bank, cfg


def score_jobs(config: dict[str, Any], args: list[str]) -> list[dict[str, Any]]:
    public_db = config.get("database", {}).get("path", "data/pipeline.db")
    profile, _, cfg = _load_private_inputs(config)
    db.init_db(public_db)
    output = []
    for job in db.get_jobs(public_db, limit=_limit(args)):
        eligibility, match = score_job(profile, job, cfg["private_db_path"])
        output.append({"job": job, "eligibility": eligibility, "match": match})
    return output


def prepare_applications(config: dict[str, Any], args: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    public_db = config.get("database", {}).get("path", "data/pipeline.db")
    profile, bank, cfg = _load_private_inputs(config)  # Fail before network when private config is absent.
    db.init_db(public_db)
    errors: list[str] = []
    if "--no-discovery" not in args:
        inserted, discovery_errors = discover_jobs(config)
        errors.extend(discovery_errors)
        print(f"Discovery: {inserted} new job(s)")
    enrichment_results = enrich_jobs(config, args)
    failed = sum(row["status"] == "FAILED" for row in enrichment_results)
    if enrichment_results:
        print(f"Enrichment: {len(enrichment_results) - failed} completed/partial, {failed} failed")
    prepared = []
    for job in db.get_jobs(public_db, limit=_limit(args)):
        try:
            prepared.append(
                prepare_job(
                    profile, bank, job, cfg["private_db_path"],
                    required_questions=cfg["required_questions"],
                )
            )
        except Exception as exc:
            errors.append(f"job {job['id']}: {type(exc).__name__}: {exc}")
    if cfg["notify_telegram"] and prepared:
        try:
            from notifier.telegram import notify_prepared_applications
            notify_prepared_applications(config, prepared)
        except Exception as exc:
            errors.append(f"Telegram: {type(exc).__name__}: {exc}")
    return prepared, errors


def review_applications(config: dict[str, Any], args: list[str]) -> list[dict[str, Any]]:
    return list_applications(settings(config)["private_db_path"], limit=_limit(args, 100))


def job_detail(config: dict[str, Any], job_id: int) -> dict[str, Any] | None:
    public_db = config.get("database", {}).get("path", "data/pipeline.db")
    db.init_db(public_db)
    job = db.get_job(public_db, job_id)
    if not job:
        return None
    cfg = settings(config)
    assessment = get_assessment(cfg["private_db_path"], job_id) if Path(cfg["private_db_path"]).exists() else None
    applications = list_applications(cfg["private_db_path"], job_id=job_id) if Path(cfg["private_db_path"]).exists() else []
    return {"job": job, "assessment": assessment, "application": applications[0] if applications else None}


def display_result(result: dict[str, Any]) -> str:
    job = result["job"]
    eligibility = result["eligibility"]
    match = result["match"]
    application = result.get("application") or {}
    lines = [
        f"Job {job['id']}: {job.get('title') or '-'} at {job.get('company') or '-'}",
        f"Eligibility: {eligibility.status}",
    ]
    failures = [reason["message"] for reason in eligibility.reasons if reason["outcome"] != "ELIGIBLE"]
    if failures:
        lines.append(f"Reason: {failures[0]}")
    lines.extend(
        [
            f"Match: {match.match_score}/100 ({match.recommendation}, confidence {match.confidence})",
            f"ATS: {job.get('ats_provider') or 'Unknown'}",
            f"Status: {application.get('status') or '-'}",
        ]
    )
    unanswered = result.get("unanswered_questions") or []
    if unanswered:
        lines.append("Missing answers: " + ", ".join(unanswered))
    return "\n".join(lines)


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []
