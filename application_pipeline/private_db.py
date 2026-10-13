"""Private SQLite store for candidate-derived assessments and applications."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


MILESTONE_STATUSES = (
    "DISCOVERED", "ENRICHED", "ELIGIBLE", "SKIPPED", "PREPARING",
    "READY_TO_APPLY", "NEEDS_REVIEW", "FAILED", "FORM_INSPECTED",
    "FORM_PARTIALLY_FILLED", "FORM_READY_FOR_REVIEW", "NEEDS_USER_ACTION",
    "BLOCKED", "DRY_RUN_COMPLETE",
)
ALL_DOCUMENTED_STATUSES = MILESTONE_STATUSES + ("SUBMITTED",)
TRANSITIONS = {
    "DISCOVERED": {"ENRICHED", "SKIPPED", "FAILED", "NEEDS_REVIEW"},
    "ENRICHED": {"ELIGIBLE", "SKIPPED", "NEEDS_REVIEW", "FAILED"},
    "ELIGIBLE": {"PREPARING", "SKIPPED", "NEEDS_REVIEW", "FAILED"},
    "PREPARING": {"READY_TO_APPLY", "NEEDS_REVIEW", "FAILED"},
    "READY_TO_APPLY": {"PREPARING", "NEEDS_REVIEW", "FORM_INSPECTED", "FORM_PARTIALLY_FILLED", "FORM_READY_FOR_REVIEW", "NEEDS_USER_ACTION", "BLOCKED", "DRY_RUN_COMPLETE"},
    "NEEDS_REVIEW": {"PREPARING", "READY_TO_APPLY", "SKIPPED", "FAILED", "FORM_INSPECTED", "FORM_PARTIALLY_FILLED", "FORM_READY_FOR_REVIEW", "NEEDS_USER_ACTION", "BLOCKED", "DRY_RUN_COMPLETE"},
    "SKIPPED": set(),
    "FAILED": {"PREPARING", "NEEDS_REVIEW"},
    "FORM_INSPECTED": {"FORM_PARTIALLY_FILLED", "FORM_READY_FOR_REVIEW", "NEEDS_USER_ACTION", "BLOCKED", "DRY_RUN_COMPLETE"},
    "FORM_PARTIALLY_FILLED": {"FORM_INSPECTED", "FORM_READY_FOR_REVIEW", "NEEDS_USER_ACTION", "BLOCKED", "DRY_RUN_COMPLETE"},
    "FORM_READY_FOR_REVIEW": {"FORM_INSPECTED", "FORM_PARTIALLY_FILLED", "NEEDS_USER_ACTION", "BLOCKED", "DRY_RUN_COMPLETE"},
    "NEEDS_USER_ACTION": {"FORM_INSPECTED", "FORM_PARTIALLY_FILLED", "FORM_READY_FOR_REVIEW", "BLOCKED", "DRY_RUN_COMPLETE"},
    "BLOCKED": {"FORM_INSPECTED", "FORM_PARTIALLY_FILLED", "FORM_READY_FOR_REVIEW", "NEEDS_USER_ACTION", "DRY_RUN_COMPLETE"},
    "DRY_RUN_COMPLETE": {"FORM_INSPECTED", "FORM_PARTIALLY_FILLED", "FORM_READY_FOR_REVIEW", "NEEDS_USER_ACTION", "BLOCKED"},
}


def _connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_private_db(path: str | Path) -> None:
    conn = _connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS job_assessments (
                job_id INTEGER PRIMARY KEY,
                eligibility_status TEXT NOT NULL,
                eligibility_reasons TEXT NOT NULL,
                match_score INTEGER,
                match_confidence INTEGER,
                match_strengths TEXT,
                match_gaps TEXT,
                match_reasoning TEXT,
                missing_information TEXT,
                application_recommendation TEXT,
                needs_review INTEGER NOT NULL DEFAULT 1,
                assessed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL UNIQUE,
                application_url TEXT,
                ats_provider TEXT,
                match_score INTEGER,
                status TEXT NOT NULL,
                resume_path TEXT,
                cover_letter_path TEXT,
                prepared_answers TEXT NOT NULL DEFAULT '{}',
                unanswered_questions TEXT NOT NULL DEFAULT '[]',
                requires_manual_review INTEGER NOT NULL DEFAULT 1,
                prepared_at TEXT,
                submitted_at TEXT,
                submission_reference TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS application_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL REFERENCES applications(id),
                from_status TEXT,
                to_status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS application_form_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL REFERENCES applications(id),
                run_type TEXT NOT NULL,
                ats_provider TEXT,
                url TEXT,
                status TEXT NOT NULL,
                fields_json TEXT NOT NULL DEFAULT '[]',
                blockers_json TEXT NOT NULL DEFAULT '[]',
                validation_json TEXT NOT NULL DEFAULT '[]',
                screenshot_paths TEXT NOT NULL DEFAULT '[]',
                html_snapshot_path TEXT,
                report_path TEXT,
                resume_path TEXT,
                submission_guard_events TEXT NOT NULL DEFAULT '[]',
                readiness_score INTEGER,
                readiness_status TEXT,
                inspection_passes INTEGER NOT NULL DEFAULT 1,
                conditional_fields_detected INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_application_form_runs_application
                ON application_form_runs(application_id, created_at DESC);
            CREATE TRIGGER IF NOT EXISTS prohibit_submission_insert
            BEFORE INSERT ON applications WHEN NEW.status = 'SUBMITTED'
            BEGIN SELECT RAISE(ABORT, 'SUBMITTED is disabled in the preparation milestone'); END;
            CREATE TRIGGER IF NOT EXISTS prohibit_submission_update
            BEFORE UPDATE ON applications WHEN NEW.status = 'SUBMITTED' OR NEW.submitted_at IS NOT NULL OR NEW.submission_reference IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'submission fields are disabled in the preparation milestone'); END;
            """
        )
        form_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(application_form_runs)")
        }
        for name, definition in (
            ("readiness_score", "INTEGER"),
            ("readiness_status", "TEXT"),
            ("inspection_passes", "INTEGER NOT NULL DEFAULT 1"),
            ("conditional_fields_detected", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in form_columns:
                conn.execute(f"ALTER TABLE application_form_runs ADD COLUMN {name} {definition}")
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def upsert_assessment(path: str | Path, job_id: int, eligibility: Any, match: Any) -> None:
    init_private_db(path)
    conn = _connect(path)
    try:
        conn.execute(
            """
            INSERT INTO job_assessments
            (job_id,eligibility_status,eligibility_reasons,match_score,match_confidence,
             match_strengths,match_gaps,match_reasoning,missing_information,
             application_recommendation,needs_review,assessed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
              eligibility_status=excluded.eligibility_status,
              eligibility_reasons=excluded.eligibility_reasons,
              match_score=excluded.match_score, match_confidence=excluded.match_confidence,
              match_strengths=excluded.match_strengths, match_gaps=excluded.match_gaps,
              match_reasoning=excluded.match_reasoning,
              missing_information=excluded.missing_information,
              application_recommendation=excluded.application_recommendation,
              needs_review=excluded.needs_review, assessed_at=excluded.assessed_at
            """,
            (job_id, eligibility.status, _json(eligibility.reasons), match.match_score,
             match.confidence, _json(match.strengths), _json(match.gaps), match.reasoning,
             _json(match.missing_information), match.recommendation,
             int(eligibility.status == "UNKNOWN_REVIEW" or match.recommendation == "REVIEW"), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_assessment(path: str | Path, job_id: int) -> Optional[dict[str, Any]]:
    init_private_db(path)
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM job_assessments WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_application(path: str | Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    init_private_db(path)
    status = str(payload.get("status") or "DISCOVERED")
    if status not in MILESTONE_STATUSES:
        raise ValueError(f"status {status} is not allowed in the preparation milestone")
    conn = _connect(path)
    try:
        existing = conn.execute("SELECT * FROM applications WHERE job_id=?", (payload["job_id"],)).fetchone()
        old_status = existing["status"] if existing else None
        if old_status and old_status != status and status not in TRANSITIONS.get(old_status, set()):
            raise ValueError(f"invalid application transition: {old_status} -> {status}")
        now = _now()
        conn.execute(
            """
            INSERT INTO applications
            (job_id,application_url,ats_provider,match_score,status,resume_path,
             cover_letter_path,prepared_answers,unanswered_questions,
             requires_manual_review,prepared_at,submitted_at,submission_reference,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
              application_url=excluded.application_url, ats_provider=excluded.ats_provider,
              match_score=excluded.match_score, status=excluded.status,
              resume_path=excluded.resume_path, cover_letter_path=excluded.cover_letter_path,
              prepared_answers=excluded.prepared_answers,
              unanswered_questions=excluded.unanswered_questions,
              requires_manual_review=excluded.requires_manual_review,
              prepared_at=excluded.prepared_at, updated_at=excluded.updated_at
            """,
            (payload["job_id"], payload.get("application_url", ""), payload.get("ats_provider", "Unknown"),
             payload.get("match_score"), status, payload.get("resume_path", ""),
             payload.get("cover_letter_path", ""), _json(payload.get("prepared_answers", {})),
             _json(payload.get("unanswered_questions", [])), int(bool(payload.get("requires_manual_review", True))),
             payload.get("prepared_at") or now, None, None, now),
        )
        row = conn.execute("SELECT * FROM applications WHERE job_id=?", (payload["job_id"],)).fetchone()
        if old_status != status:
            conn.execute(
                "INSERT INTO application_events (application_id,from_status,to_status,reason,created_at) VALUES (?,?,?,?,?)",
                (row["id"], old_status, status, str(payload.get("transition_reason") or ""), now),
            )
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def transition_application(path: str | Path, application_id: int, status: str, reason: str = "") -> None:
    if status == "SUBMITTED":
        raise ValueError("SUBMITTED is disabled in the preparation milestone")
    init_private_db(path)
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
        if not row:
            raise KeyError(application_id)
        if status not in TRANSITIONS.get(row["status"], set()):
            raise ValueError(f"invalid application transition: {row['status']} -> {status}")
        now = _now()
        conn.execute("UPDATE applications SET status=?, updated_at=? WHERE id=?", (status, now, application_id))
        conn.execute(
            "INSERT INTO application_events (application_id,from_status,to_status,reason,created_at) VALUES (?,?,?,?,?)",
            (application_id, row["status"], status, reason, now),
        )
        conn.commit()
    finally:
        conn.close()


def list_applications(path: str | Path, limit: int = 100, job_id: Optional[int] = None) -> list[dict[str, Any]]:
    init_private_db(path)
    conn = _connect(path)
    try:
        if job_id is None:
            rows = conn.execute("SELECT * FROM applications ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_application(path: str | Path, application_id: int) -> Optional[dict[str, Any]]:
    init_private_db(path)
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (int(application_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_form_run(path: str | Path, application_id: int, run_type: str, ats_provider: str, url: str) -> int:
    init_private_db(path)
    if run_type not in ("INSPECT", "DRY_RUN"):
        raise ValueError(f"invalid browser run type: {run_type}")
    conn = _connect(path)
    try:
        now = _now()
        cursor = conn.execute(
            """
            INSERT INTO application_form_runs
            (application_id,run_type,ats_provider,url,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (int(application_id), run_type, ats_provider, url, "RUNNING", now, now),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def finish_form_run(path: str | Path, run_id: int, report: Mapping[str, Any]) -> None:
    init_private_db(path)
    conn = _connect(path)
    try:
        conn.execute(
            """
            UPDATE application_form_runs SET
              status=?, fields_json=?, blockers_json=?, validation_json=?,
              screenshot_paths=?, html_snapshot_path=?, report_path=?, resume_path=?,
              submission_guard_events=?, readiness_score=?, readiness_status=?,
              inspection_passes=?, conditional_fields_detected=?, updated_at=?
            WHERE id=?
            """,
            (
                report.get("run_status", "DRY_RUN_COMPLETE"),
                _json(report.get("fields", [])),
                _json(report.get("authentication_blockers", [])),
                _json(report.get("validation_issues", [])),
                _json(report.get("screenshot_paths", [])),
                report.get("html_snapshot_path", ""),
                report.get("report_path", ""),
                report.get("resume_identifier", report.get("resume_path", "")),
                _json(report.get("submission_requests_blocked", [])),
                (report.get("readiness") or {}).get("score"),
                (report.get("readiness") or {}).get("status"),
                int(report.get("inspection_passes", 1)),
                int(report.get("conditional_fields_detected", 0)),
                _now(), int(run_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_browser_status(path: str | Path, application_id: int, status: str, reason: str, resume_path: str = "") -> None:
    init_private_db(path)
    conn = _connect(path)
    try:
        row = conn.execute("SELECT status FROM applications WHERE id=?", (int(application_id),)).fetchone()
    finally:
        conn.close()
    if not row:
        raise KeyError(application_id)
    if row["status"] != status:
        transition_application(path, application_id, status, reason)
    if resume_path:
        conn = _connect(path)
        try:
            conn.execute("UPDATE applications SET resume_path=?, updated_at=? WHERE id=?", (resume_path, _now(), int(application_id)))
            conn.commit()
        finally:
            conn.close()


def get_form_runs(path: str | Path, application_id: int) -> list[dict[str, Any]]:
    init_private_db(path)
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM application_form_runs WHERE application_id=? ORDER BY created_at DESC",
            (int(application_id),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
