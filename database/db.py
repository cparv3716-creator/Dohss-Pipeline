"""
database/db.py - SQLite layer for the DoHSS placement pipeline.
"""
import sqlite3
from datetime import datetime
from pathlib import Path


def get_connection(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path):
    conn = get_connection(db_path)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT NOT NULL,
            company         TEXT NOT NULL,
            location        TEXT,
            description     TEXT,
            url             TEXT UNIQUE,
            source          TEXT,
            keyword_matched TEXT,
            salary          TEXT,
            posted_at       TEXT,
            found_at        TEXT DEFAULT (datetime('now')),
            is_notified     INTEGER DEFAULT 0,
            is_relevant     INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS hr_contacts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            company      TEXT,
            designation  TEXT,
            email        TEXT,
            linkedin_url TEXT UNIQUE,
            source       TEXT,
            notes        TEXT,
            added_at     TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS institutes (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT UNIQUE NOT NULL,
            full_name      TEXT,
            placement_page TEXT,
            last_hash      TEXT,
            last_checked   TEXT,
            last_changed   TEXT,
            pdf_count      INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS placement_pdfs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            institute     TEXT,
            filename      TEXT,
            url           TEXT UNIQUE,
            local_path    TEXT,
            parsed        INTEGER DEFAULT 0,
            downloaded_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS placement_data (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            institute    TEXT,
            year         TEXT,
            company      TEXT,
            role         TEXT,
            profile_type TEXT,
            ctc_lpa      REAL,
            count        INTEGER,
            pdf_id       INTEGER REFERENCES placement_pdfs(id),
            added_at     TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS outreach (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            hr_id          INTEGER REFERENCES hr_contacts(id),
            company        TEXT,
            contact_email  TEXT,
            status         TEXT DEFAULT 'pending',
            emailed_at     TEXT,
            follow_up_1_at TEXT,
            follow_up_2_at TEXT,
            responded_at   TEXT,
            notes          TEXT,
            updated_at     TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS contacts (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id     INTEGER REFERENCES jobs(id),
            company_name       TEXT,
            company_domain     TEXT,
            contact_name       TEXT,
            role_title         TEXT,
            email              TEXT,
            linkedin_url       TEXT,
            source_url         TEXT NOT NULL,
            source_type        TEXT,
            confidence_score   INTEGER DEFAULT 0,
            last_seen_at       TEXT DEFAULT (datetime('now')),
            created_at         TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_dedupe
            ON contacts (email, company_domain, source_url);
        CREATE INDEX IF NOT EXISTS idx_contacts_opportunity
            ON contacts (opportunity_id, confidence_score);
    """)
    conn.commit()
    conn.close()


def insert_job(db_path, job):
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO jobs "
            "(title,company,location,description,url,source,keyword_matched,salary,posted_at) "
            "VALUES (:title,:company,:location,:description,:url,:source,:keyword_matched,:salary,:posted_at)",
            job,
        )
        inserted = conn.total_changes > 0
        conn.commit()
        return inserted
    finally:
        conn.close()


def get_job_by_url(db_path, url):
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM jobs WHERE url=?", (url,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_recent_jobs_for_enrichment(db_path, limit=50):
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT j.*
        FROM jobs j
        LEFT JOIN contacts c ON c.opportunity_id = j.id
        WHERE c.id IS NULL
        ORDER BY j.found_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_unnotified_jobs(db_path):
    conn = get_connection(db_path)
    rows = conn.execute("SELECT * FROM jobs WHERE is_notified=0 ORDER BY found_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_contacts_for_opportunity(db_path, opportunity_id, min_confidence=0, limit=3):
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT *
        FROM contacts
        WHERE opportunity_id=? AND confidence_score >= ?
        ORDER BY confidence_score DESC, last_seen_at DESC
        LIMIT ?
        """,
        (opportunity_id, min_confidence, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_contacts(db_path, min_confidence=0, limit=50):
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT c.*, j.title AS opportunity_title, j.url AS opportunity_url
        FROM contacts c
        LEFT JOIN jobs j ON j.id = c.opportunity_id
        WHERE c.confidence_score >= ?
        ORDER BY c.confidence_score DESC, c.last_seen_at DESC
        LIMIT ?
        """,
        (min_confidence, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def insert_contact(db_path, contact):
    conn = get_connection(db_path)
    now = datetime.now().isoformat()
    payload = {
        "opportunity_id": contact.get("opportunity_id"),
        "company_name": contact.get("company_name", ""),
        "company_domain": contact.get("company_domain", ""),
        "contact_name": contact.get("contact_name", ""),
        "role_title": contact.get("role_title", ""),
        "email": contact.get("email", ""),
        "linkedin_url": contact.get("linkedin_url", ""),
        "source_url": contact.get("source_url", ""),
        "source_type": contact.get("source_type", "public_page"),
        "confidence_score": int(contact.get("confidence_score") or 0),
        "last_seen_at": now,
    }
    try:
        conn.execute(
            """
            INSERT INTO contacts
            (opportunity_id,company_name,company_domain,contact_name,role_title,email,
             linkedin_url,source_url,source_type,confidence_score,last_seen_at)
            VALUES
            (:opportunity_id,:company_name,:company_domain,:contact_name,:role_title,:email,
             :linkedin_url,:source_url,:source_type,:confidence_score,:last_seen_at)
            ON CONFLICT(email, company_domain, source_url) DO UPDATE SET
                opportunity_id=excluded.opportunity_id,
                company_name=excluded.company_name,
                contact_name=excluded.contact_name,
                role_title=excluded.role_title,
                linkedin_url=excluded.linkedin_url,
                source_type=excluded.source_type,
                confidence_score=MAX(contacts.confidence_score, excluded.confidence_score),
                last_seen_at=excluded.last_seen_at
            """,
            payload,
        )
        inserted = conn.total_changes > 0
        conn.commit()
        return inserted
    finally:
        conn.close()


def get_contact_stats(db_path):
    conn = get_connection(db_path)
    total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    high_confidence = conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE confidence_score >= 70"
    ).fetchone()[0]
    conn.close()
    return {"total": total, "high_confidence": high_confidence}


def mark_jobs_notified(db_path, job_ids):
    conn = get_connection(db_path)
    conn.executemany("UPDATE jobs SET is_notified=1 WHERE id=?", [(i,) for i in job_ids])
    conn.commit()
    conn.close()


def get_job_stats(db_path):
    conn = get_connection(db_path)
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    today = conn.execute("SELECT COUNT(*) FROM jobs WHERE date(found_at)=date('now')").fetchone()[0]
    sources = conn.execute("SELECT source, COUNT(*) as n FROM jobs GROUP BY source").fetchall()
    conn.close()
    return {"total": total, "today": today, "by_source": [dict(r) for r in sources]}


def insert_hr(db_path, hr):
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO hr_contacts "
            "(name,company,designation,email,linkedin_url,source,notes) "
            "VALUES (:name,:company,:designation,:email,:linkedin_url,:source,:notes)",
            hr,
        )
        inserted = conn.total_changes > 0
        conn.commit()
        return inserted
    finally:
        conn.close()


def get_all_hrs(db_path):
    conn = get_connection(db_path)
    rows = conn.execute("SELECT * FROM hr_contacts ORDER BY added_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_institute(db_path, inst):
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO institutes (name,full_name,placement_page) "
        "VALUES (:name,:full_name,:placement_page) "
        "ON CONFLICT(name) DO UPDATE SET placement_page=excluded.placement_page",
        inst,
    )
    conn.commit()
    conn.close()


def get_institute_hash(db_path, name):
    conn = get_connection(db_path)
    row = conn.execute("SELECT last_hash FROM institutes WHERE name=?", (name,)).fetchone()
    conn.close()
    return row["last_hash"] if row else None


def update_institute_hash(db_path, name, new_hash, changed):
    conn = get_connection(db_path)
    now = datetime.now().isoformat()
    if changed:
        conn.execute(
            "UPDATE institutes SET last_hash=?,last_checked=?,last_changed=? WHERE name=?",
            (new_hash, now, now, name),
        )
    else:
        conn.execute(
            "UPDATE institutes SET last_hash=?,last_checked=? WHERE name=?",
            (new_hash, now, name),
        )
    conn.commit()
    conn.close()


def insert_pdf(db_path, pdf):
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO placement_pdfs (institute,filename,url,local_path) "
            "VALUES (:institute,:filename,:url,:local_path)",
            pdf,
        )
        inserted = conn.total_changes > 0
        conn.commit()
        return inserted
    finally:
        conn.close()


def insert_placement_data(db_path, rows):
    conn = get_connection(db_path)
    conn.executemany(
        "INSERT INTO placement_data "
        "(institute,year,company,role,profile_type,ctc_lpa,count,pdf_id) "
        "VALUES (:institute,:year,:company,:role,:profile_type,:ctc_lpa,:count,:pdf_id)",
        rows,
    )
    conn.commit()
    conn.close()


def insert_outreach(db_path, record):
    conn = get_connection(db_path)
    cur = conn.execute(
        "INSERT INTO outreach (hr_id,company,contact_email,status,notes) "
        "VALUES (:hr_id,:company,:contact_email,:status,:notes)",
        record,
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_outreach_status(db_path, outreach_id, status, notes=""):
    conn = get_connection(db_path)
    now = datetime.now().isoformat()
    field_map = {"emailed": "emailed_at", "follow_up_1": "follow_up_1_at",
                 "follow_up_2": "follow_up_2_at", "responded": "responded_at"}
    set_parts = ["status=?", "updated_at=?"]
    args = [status, now]
    if status in field_map:
        set_parts.append(field_map[status] + "=?")
        args.append(now)
    if notes:
        set_parts.append("notes=?")
        args.append(notes)
    args.append(outreach_id)
    conn.execute("UPDATE outreach SET " + ", ".join(set_parts) + " WHERE id=?", args)
    conn.commit()
    conn.close()


def get_outreach_stats(db_path):
    conn = get_connection(db_path)
    rows = conn.execute("SELECT status, COUNT(*) as n FROM outreach GROUP BY status").fetchall()
    conn.close()
    return [dict(r) for r in rows]
