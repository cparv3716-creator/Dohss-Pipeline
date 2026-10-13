"""CLI integration for the safe preparation milestone."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from candidate import AnswerBank, CandidateProfile
from database import db

from .enrichment import JobDetailEnricher
from .preparation import prepare_job, score_job
from .private_db import get_application, get_assessment, get_form_runs, list_applications


def settings(config: dict[str, Any]) -> dict[str, Any]:
    app = config.get("applications", {}) or {}
    browser = app.get("browser", {}) or {}
    return {
        "profile_path": app.get("profile_path", "candidate/profile.yaml"),
        "answer_bank_path": app.get("answer_bank_path", "candidate/application_answers.yaml"),
        "private_db_path": app.get("private_db_path", "data/private/applications.db"),
        "required_questions": app.get("required_questions", ["FULL_NAME", "EMAIL", "PHONE"]),
        "notify_telegram": bool(app.get("notify_telegram", False)),
        "enrichment_timeout": int(app.get("enrichment_timeout_seconds", 15)),
        "browser": browser,
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


def _browser_service(config: dict[str, Any], args: list[str]):
    from application_pipeline.browser.safety import ResumePolicy, SafeFillPolicy
    from application_pipeline.browser.service import BrowserApplicationService
    from application_pipeline.browser.session import BrowserConfig

    cfg = settings(config)
    browser = cfg["browser"]
    headless = "--headed" not in args and bool(browser.get("headless", True))
    manual_handoff = "--manual-handoff" in args or bool(browser.get("manual_handoff", False))
    if manual_handoff and headless:
        raise ValueError("manual handoff requires --headed")
    browser_config = BrowserConfig(
        headless=headless,
        timeout_ms=int(browser.get("timeout_ms", 30_000)),
        navigation_timeout_ms=int(browser.get("navigation_timeout_ms", 45_000)),
        slow_mo_ms=int(browser.get("slow_mo_ms", 0)),
        artifacts_root=str(browser.get("artifacts_root", "data/private/browser_runs")),
        manual_handoff=manual_handoff,
        save_html="--no-html" not in args and bool(browser.get("save_html", True)),
        max_fill_passes=int(browser.get("max_fill_passes", 3)),
        dom_settle_ms=int(browser.get("dom_settle_ms", 800)),
    )
    policy = SafeFillPolicy(
        minimum_confidence=int(browser.get("minimum_confidence", 85)),
        allowed_sensitive_types=set(browser.get("allowed_sensitive_question_types", []) or []),
        allowed_sensitivities=set(browser.get("allowed_sensitivities", ["public", "private", "personal"]) or []),
    )
    resume = ResumePolicy(
        configured_path=str(browser.get("resume_path", "")),
        allowed_roots=browser.get("allowed_resume_roots", ["data/private/resumes"]),
    )
    return BrowserApplicationService(
        config.get("database", {}).get("path", "data/pipeline.db"),
        cfg["private_db_path"], browser_config, policy, resume,
    )


def inspect_application(config: dict[str, Any], application_id: int, args: list[str]):
    return _browser_service(config, args).inspect(application_id)


def dry_run_application(config: dict[str, Any], application_id: int, args: list[str]):
    profile, bank, _ = _load_private_inputs(config)
    return _browser_service(config, args).dry_run(application_id, profile, bank)


def display_browser_report(report: Any) -> str:
    payload = report.to_dict()
    return "\n".join(
        [
            f"Company: {payload['company']}",
            f"Role: {payload['role']}",
            f"ATS: {payload['ats_provider']}",
            f"Fields detected: {payload['fields_detected']}",
            f"Safely filled: {payload['answers_filled']}",
            f"Unanswered required: {len(payload['missing_answers'])}",
            f"Sensitive review: {len(payload['sensitive_questions'])}",
            f"Resume uploaded: {'yes' if payload['resume_uploaded'] else 'no'}",
            f"Authentication blockers: {', '.join(payload['authentication_blockers']) if payload['authentication_blockers'] else 'none'}",
            f"Status: {payload['final_status']}",
            f"Readiness: {payload.get('readiness', {}).get('score', 0)}% ({payload.get('readiness', {}).get('status', 'UNKNOWN')})",
            "Submission: BLOCKED BY POLICY",
            f"Private report: {payload['report_path']}",
        ]
    )


def candidate_audit(config: dict[str, Any]) -> str:
    from application_pipeline.browser.safety import ResumePolicy
    from application_pipeline.candidate_audit import audit_candidate, format_candidate_audit

    profile, bank, cfg = _load_private_inputs(config)
    browser = cfg["browser"]
    resume = ResumePolicy(
        configured_path=str(browser.get("resume_path", "")),
        allowed_roots=browser.get("allowed_resume_roots", ["data/private/resumes"]),
    )
    return format_candidate_audit(audit_candidate(
        profile, bank, resume,
        set(browser.get("allowed_sensitive_question_types", []) or []),
    ))


def review_application(config: dict[str, Any], application_id: int) -> str:
    cfg = settings(config)
    application = get_application(cfg["private_db_path"], application_id)
    if not application:
        raise KeyError(f"Application {application_id} not found")
    job = db.get_job(config.get("database", {}).get("path", "data/pipeline.db"), application["job_id"])
    runs = get_form_runs(cfg["private_db_path"], application_id)
    if not runs:
        raise ValueError("No browser inspection or dry run is available for this application")
    run = runs[0]
    payload = {}
    report_path = Path(str(run.get("report_path") or ""))
    if report_path.is_file():
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    fields = payload.get("fields") or json_list(run.get("fields_json"))
    readiness = payload.get("readiness") or {}
    resolved = {"FILLED", "UPLOADED"}
    lines = [
        "Application:",
        f"{(job or {}).get('title', 'Unknown role')} — {(job or {}).get('company', 'Unknown company')}",
        f"ATS: {application.get('ats_provider') or 'Unknown'}",
        f"Match: {application.get('match_score') if application.get('match_score') is not None else 'unknown'}/100",
        "",
        "Fields:",
    ]
    for item in fields:
        if not item.get("visible", True):
            continue
        status = str(item.get("answer_status") or "UNRESOLVED")
        label = str(item.get("label") or item.get("raw_question") or "Unlabelled field")
        source = str(item.get("answer_source") or "").replace("profile.", "verified profile: ")
        if status in resolved:
            lines.append(f"[OK] {label} — {source or 'verified/configured'}")
        elif status == "SKIPPED_SENSITIVE":
            lines.append(f"[!] {label} — sensitive/manual only")
        elif str(item.get("canonical_question_type")) in {"WHY_COMPANY", "WHY_ROLE", "PROJECT_EXAMPLE", "LEADERSHIP_EXAMPLE"}:
            lines.append(f"[!] {label} — prepared text requires review")
        else:
            lines.append(f"[!] {label} — unanswered")
    required = [item for item in fields if item.get("visible", True) and item.get("required")]
    safe = [item for item in fields if item.get("visible", True) and item.get("answer_status") in resolved]
    lines.extend([
        "",
        f"Required fields: {len(required)}",
        f"Safely resolved: {len(safe)}",
        f"Manual review: {len([item for item in fields if item.get('visible', True)]) - len(safe)}",
        f"Application readiness: {readiness.get('score', 0)}%",
        f"Submission readiness: {readiness.get('status', 'NEEDS_INFORMATION')}",
        "Submission: BLOCKED BY POLICY",
    ])
    return "\n".join(lines)
