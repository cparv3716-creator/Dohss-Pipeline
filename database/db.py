"""
database/db.py - SQLite layer for the DoHSS placement pipeline.
"""
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path


# These columns are added idempotently so historical and CI-committed discovery
# databases remain usable. Candidate-derived columns exist for model/API
# compatibility but are deliberately left NULL in the public database; private
# values belong in data/private/applications.db.
JOB_MIGRATIONS = (
    ("full_description", "TEXT"),
    ("responsibilities", "TEXT"),
    ("requirements", "TEXT"),
    ("required_skills", "TEXT"),
    ("preferred_skills", "TEXT"),
    ("required_experience_min", "REAL"),
    ("required_experience_max", "REAL"),
    ("education_requirement", "TEXT"),
    ("employment_type", "TEXT"),
    ("remote_type", "TEXT"),
    ("application_url", "TEXT"),
    ("ats_provider", "TEXT"),
    ("company_career_url", "TEXT"),
    ("eligibility_status", "TEXT"),
    ("eligibility_reasons", "TEXT"),
    ("match_score", "INTEGER"),
    ("match_confidence", "INTEGER"),
    ("match_strengths", "TEXT"),
    ("match_gaps", "TEXT"),
    ("application_recommendation", "TEXT"),
    ("application_status", "TEXT"),
    ("needs_review", "INTEGER"),
    ("last_enriched_at", "TEXT"),
    ("enrichment_status", "TEXT DEFAULT 'PENDING'"),
    ("enrichment_error", "TEXT"),
)


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
            evidence_text      TEXT,
            email_category     TEXT,
            allowed_for_auto_outreach INTEGER DEFAULT 0,
            confidence_score   INTEGER DEFAULT 0,
            last_seen_at       TEXT DEFAULT (datetime('now')),
            created_at         TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_dedupe
            ON contacts (email, company_domain, source_url);
        CREATE INDEX IF NOT EXISTS idx_contacts_opportunity
            ON contacts (opportunity_id, confidence_score);
        CREATE TABLE IF NOT EXISTS companies (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            name              TEXT NOT NULL,
            normalized_name   TEXT UNIQUE NOT NULL,
            official_domain   TEXT,
            domain_confidence INTEGER DEFAULT 0,
            careers_url       TEXT,
            contact_url       TEXT,
            ats_platform      TEXT,
            source_url        TEXT,
            source_type       TEXT,
            evidence_text     TEXT,
            created_at        TEXT DEFAULT (datetime('now')),
            updated_at        TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS target_companies (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            company                TEXT NOT NULL,
            normalized_company     TEXT UNIQUE NOT NULL,
            domain                 TEXT,
            company_category       TEXT,
            size_tier              TEXT,
            source_institutes      TEXT,
            likely_role_families   TEXT,
            specific_roles_offered TEXT,
            evidence_url           TEXT,
            evidence_text          TEXT,
            priority_score         INTEGER DEFAULT 70,
            confidence_score       INTEGER DEFAULT 70,
            notes                  TEXT,
            created_at             TEXT DEFAULT (datetime('now')),
            updated_at             TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_target_companies_priority
            ON target_companies (priority_score DESC);
        CREATE INDEX IF NOT EXISTS idx_target_companies_category
            ON target_companies (company_category);
        CREATE TABLE IF NOT EXISTS company_roles (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id       INTEGER REFERENCES target_companies(id),
            company          TEXT NOT NULL,
            role_title       TEXT NOT NULL,
            role_family      TEXT,
            source_institute TEXT,
            evidence_url     TEXT,
            confidence_score INTEGER DEFAULT 70,
            notes            TEXT,
            created_at       TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_company_roles_company
            ON company_roles (company);
    """)
    existing_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(contacts)").fetchall()
    }
    if "evidence_text" not in existing_cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN evidence_text TEXT")
    if "allowed_for_auto_outreach" not in existing_cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN allowed_for_auto_outreach INTEGER DEFAULT 0")
    if "email_category" not in existing_cols:
        conn.execute("ALTER TABLE contacts ADD COLUMN email_category TEXT")

    job_cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for col, definition in JOB_MIGRATIONS:
        if col not in job_cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_enrichment_status ON jobs(enrichment_status, found_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ats_provider ON jobs(ats_provider)")

    # Migrate hr_contacts with DoHSS-specific columns
    hr_cols = {row["name"] for row in conn.execute("PRAGMA table_info(hr_contacts)").fetchall()}
    _hr_migrations = [
        ("company_role_fit",         "TEXT"),
        ("relevant_role_families",   "TEXT"),
        ("source_institutes",        "TEXT"),
        ("verified_current_company", "INTEGER DEFAULT 0"),
        ("discovered_for_role_family", "TEXT"),
        ("evidence_url",             "TEXT"),
        ("confidence_score",         "INTEGER DEFAULT 70"),
        ("manual_review_required",   "INTEGER DEFAULT 1"),
        ("allowed_for_auto_outreach","INTEGER DEFAULT 0"),
    ]
    for col, defn in _hr_migrations:
        if col not in hr_cols:
            conn.execute(f"ALTER TABLE hr_contacts ADD COLUMN {col} {defn}")
    conn.execute("UPDATE contacts SET allowed_for_auto_outreach=0 WHERE source_type='guessed_role_email'")
    conn.execute(
        """
        DELETE FROM contacts
        WHERE source_type='guessed_role_email'
          AND (
            lower(company_name) IN ('hire feed','linkedin','naukri','internshala','google alerts')
            OR lower(company_name) LIKE '%jobs feed%'
            OR lower(company_name) LIKE '%hire feed%'
            OR lower(company_name) LIKE '%alert feed%'
            OR lower(company_domain) LIKE '%linkedin.com%'
            OR lower(company_domain) LIKE '%naukri.com%'
            OR lower(company_domain) LIKE '%internshala.com%'
            OR lower(company_domain) LIKE '%google.com%'
            OR lower(company_domain) LIKE '%feed%'
            OR lower(company_domain) LIKE '%alert%'
          )
        """
    )
    conn.commit()
    conn.close()


def normalize_company_name(name):
    return " ".join(str(name or "").lower().strip().split())


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
    return decoded_job(row) if row else None


def get_job(db_path, job_id):
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (int(job_id),)).fetchone()
    conn.close()
    return decoded_job(row) if row else None


def get_jobs(db_path, limit=100, *, enrichment_pending=False):
    conn = get_connection(db_path)
    if enrichment_pending:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE COALESCE(enrichment_status, 'PENDING') IN ('PENDING','FAILED','PARTIAL')
            ORDER BY found_at DESC LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs ORDER BY found_at DESC LIMIT ?", (int(limit),)).fetchall()
    conn.close()
    return [decoded_job(row) for row in rows]


def update_job_enrichment(db_path, job_id, result):
    """Store only non-sensitive facts obtained from the public job page."""
    allowed = {
        "full_description", "responsibilities", "requirements", "required_skills",
        "preferred_skills", "required_experience_min", "required_experience_max",
        "education_requirement", "employment_type", "remote_type", "location",
        "application_url", "ats_provider", "company_career_url",
    }
    fields = {
        key: value for key, value in (result.fields or {}).items()
        if key in allowed and value not in (None, "", [], {})
    }
    for key in ("requirements", "required_skills", "preferred_skills"):
        if key in fields and isinstance(fields[key], (list, dict)):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    fields.update(
        {
            "enrichment_status": result.status,
            "enrichment_error": result.error or "",
            "last_enriched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    assignments = ", ".join(f"{name}=?" for name in fields)
    conn = get_connection(db_path)
    try:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), int(job_id)))
        conn.commit()
    finally:
        conn.close()


def decoded_job(row):
    """Decode JSON collection columns without changing legacy row contracts."""
    job = dict(row)
    for key in ("requirements", "required_skills", "preferred_skills"):
        value = job.get(key)
        if isinstance(value, str) and value.strip().startswith(("[", "{")):
            try:
                job[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return job


def get_recent_jobs_for_enrichment(db_path, limit=50):
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT j.*
        FROM jobs j
        LEFT JOIN contacts c ON c.opportunity_id = j.id AND c.confidence_score >= 80
        WHERE c.id IS NULL
        ORDER BY j.found_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_companies_from_jobs(db_path, limit=50):
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT company AS name, MAX(url) AS source_url, MAX(found_at) AS last_found
        FROM jobs
        WHERE company IS NOT NULL AND trim(company) != ''
        GROUP BY lower(trim(company))
        ORDER BY MAX(found_at) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_company(db_path, name):
    conn = get_connection(db_path)
    normalized = normalize_company_name(name)
    row = conn.execute("SELECT * FROM companies WHERE normalized_name=?", (normalized,)).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_company(db_path, company):
    conn = get_connection(db_path)
    now = datetime.now().isoformat()
    payload = {
        "name": company.get("name", ""),
        "normalized_name": company.get("normalized_name") or normalize_company_name(company.get("name", "")),
        "official_domain": company.get("official_domain", ""),
        "domain_confidence": int(company.get("domain_confidence") or 0),
        "careers_url": company.get("careers_url", ""),
        "contact_url": company.get("contact_url", ""),
        "ats_platform": company.get("ats_platform", ""),
        "source_url": company.get("source_url", ""),
        "source_type": company.get("source_type", ""),
        "evidence_text": company.get("evidence_text", ""),
        "updated_at": now,
    }
    try:
        conn.execute(
            """
            INSERT INTO companies
            (name,normalized_name,official_domain,domain_confidence,careers_url,
             contact_url,ats_platform,source_url,source_type,evidence_text,updated_at)
            VALUES
            (:name,:normalized_name,:official_domain,:domain_confidence,:careers_url,
             :contact_url,:ats_platform,:source_url,:source_type,:evidence_text,:updated_at)
            ON CONFLICT(normalized_name) DO UPDATE SET
                name=excluded.name,
                official_domain=excluded.official_domain,
                domain_confidence=excluded.domain_confidence,
                careers_url=excluded.careers_url,
                contact_url=excluded.contact_url,
                ats_platform=excluded.ats_platform,
                source_url=excluded.source_url,
                source_type=excluded.source_type,
                evidence_text=excluded.evidence_text,
                updated_at=excluded.updated_at
            """,
            payload,
        )
        conn.commit()
    finally:
        conn.close()


def get_companies(db_path, limit=100000):
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT *
        FROM companies
        ORDER BY domain_confidence DESC, updated_at DESC
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


def get_contacts(db_path, min_confidence=0, limit=50, include_guesses=False):
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT c.*, j.title AS opportunity_title, j.url AS opportunity_url
        FROM contacts c
        LEFT JOIN jobs j ON j.id = c.opportunity_id
        WHERE c.confidence_score >= ?
          AND c.confidence_score > 0
          AND COALESCE(c.email_category,'') NOT IN ('excluded_non_hiring_email','invalid_email','other_public_email')
          AND (? OR c.source_type != 'guessed_role_email')
        ORDER BY c.confidence_score DESC, c.last_seen_at DESC
        LIMIT ?
        """,
        (min_confidence, int(bool(include_guesses)), limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_contact_guesses(db_path, limit=100000):
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT c.*, j.title AS opportunity_title, j.url AS opportunity_url
        FROM contacts c
        LEFT JOIN jobs j ON j.id = c.opportunity_id
        WHERE c.source_type='guessed_role_email'
        ORDER BY c.company_name, c.email
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def insert_contact(db_path, contact):
    from enrichment.contact_classifier import classify_contact

    classified = classify_contact(contact) if contact.get("email") else None
    if classified:
        if not classified["is_valid"] or classified["excluded"] or classified["confidence_score"] <= 0:
            return False
        contact = {
            **contact,
            "email": classified["email"],
            "email_category": classified["email_category"],
            "confidence_score": classified["confidence_score"],
            "role_title": classified["role_title"] or contact.get("role_title", ""),
            "contact_name": classified["contact_name"] or contact.get("contact_name", ""),
            "source_type": classified.get("source_type") or contact.get("source_type", ""),
            "allowed_for_auto_outreach": classified["allowed_for_auto_outreach"],
        }
    elif int(contact.get("confidence_score") or 0) <= 0:
        return False

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
        "evidence_text": contact.get("evidence_text", ""),
        "email_category": contact.get("email_category", ""),
        "allowed_for_auto_outreach": int(bool(contact.get("allowed_for_auto_outreach", False))),
        "confidence_score": int(contact.get("confidence_score") or 0),
        "last_seen_at": now,
    }
    try:
        conn.execute(
            """
            INSERT INTO contacts
            (opportunity_id,company_name,company_domain,contact_name,role_title,email,
             linkedin_url,source_url,source_type,evidence_text,email_category,allowed_for_auto_outreach,
             confidence_score,last_seen_at)
            VALUES
            (:opportunity_id,:company_name,:company_domain,:contact_name,:role_title,:email,
             :linkedin_url,:source_url,:source_type,:evidence_text,:email_category,:allowed_for_auto_outreach,
             :confidence_score,:last_seen_at)
            ON CONFLICT(email, company_domain, source_url) DO UPDATE SET
                opportunity_id=excluded.opportunity_id,
                company_name=excluded.company_name,
                contact_name=excluded.contact_name,
                role_title=excluded.role_title,
                linkedin_url=excluded.linkedin_url,
                source_type=excluded.source_type,
                evidence_text=excluded.evidence_text,
                email_category=excluded.email_category,
                allowed_for_auto_outreach=excluded.allowed_for_auto_outreach,
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
    total = conn.execute("SELECT COUNT(*) FROM contacts WHERE source_type != 'guessed_role_email'").fetchone()[0]
    guesses = conn.execute("SELECT COUNT(*) FROM contacts WHERE source_type='guessed_role_email'").fetchone()[0]
    excluded = conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE confidence_score <= 0 OR COALESCE(email_category,'')='excluded_non_hiring_email'"
    ).fetchone()[0]
    high_confidence = conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE confidence_score >= 80 AND source_type != 'guessed_role_email'"
    ).fetchone()[0]
    conn.close()
    return {"total": total, "high_confidence": high_confidence, "guesses": guesses, "excluded": excluded}


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


# ---------------------------------------------------------------------------
# target_companies
# ---------------------------------------------------------------------------

def _norm_company(name):
    return " ".join(str(name or "").lower().strip().split())


def upsert_target_company(db_path, company):
    conn = get_connection(db_path)
    import json
    now = datetime.now().isoformat()

    def _j(v):
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v)

    payload = {
        "company": company.get("company", ""),
        "normalized_company": _norm_company(company.get("company", "")),
        "domain": company.get("domain", ""),
        "company_category": company.get("company_category", ""),
        "size_tier": company.get("size_tier", ""),
        "source_institutes": _j(company.get("source_institutes")),
        "likely_role_families": _j(company.get("likely_role_families")),
        "specific_roles_offered": _j(company.get("specific_roles_offered")),
        "evidence_url": company.get("evidence_url", ""),
        "evidence_text": company.get("evidence_text", ""),
        "priority_score": int(company.get("priority_score") or 70),
        "confidence_score": int(company.get("confidence_score") or 70),
        "notes": company.get("notes", ""),
        "updated_at": now,
    }
    try:
        conn.execute(
            """
            INSERT INTO target_companies
            (company,normalized_company,domain,company_category,size_tier,
             source_institutes,likely_role_families,specific_roles_offered,
             evidence_url,evidence_text,priority_score,confidence_score,notes,updated_at)
            VALUES
            (:company,:normalized_company,:domain,:company_category,:size_tier,
             :source_institutes,:likely_role_families,:specific_roles_offered,
             :evidence_url,:evidence_text,:priority_score,:confidence_score,:notes,:updated_at)
            ON CONFLICT(normalized_company) DO UPDATE SET
                company=excluded.company,
                domain=COALESCE(NULLIF(excluded.domain,''), target_companies.domain),
                company_category=COALESCE(NULLIF(excluded.company_category,''), target_companies.company_category),
                size_tier=COALESCE(NULLIF(excluded.size_tier,''), target_companies.size_tier),
                source_institutes=COALESCE(NULLIF(excluded.source_institutes,''), target_companies.source_institutes),
                likely_role_families=COALESCE(NULLIF(excluded.likely_role_families,''), target_companies.likely_role_families),
                specific_roles_offered=COALESCE(NULLIF(excluded.specific_roles_offered,''), target_companies.specific_roles_offered),
                evidence_url=COALESCE(NULLIF(excluded.evidence_url,''), target_companies.evidence_url),
                evidence_text=COALESCE(NULLIF(excluded.evidence_text,''), target_companies.evidence_text),
                priority_score=MAX(target_companies.priority_score, excluded.priority_score),
                confidence_score=MAX(target_companies.confidence_score, excluded.confidence_score),
                notes=COALESCE(NULLIF(excluded.notes,''), target_companies.notes),
                updated_at=excluded.updated_at
            """,
            payload,
        )
        inserted = conn.total_changes > 0
        conn.commit()
        return inserted
    finally:
        conn.close()


def get_target_companies(db_path, min_priority=0, category=None, limit=2000):
    conn = get_connection(db_path)
    if category:
        rows = conn.execute(
            "SELECT * FROM target_companies WHERE priority_score >= ? AND company_category = ? "
            "ORDER BY priority_score DESC, company LIMIT ?",
            (min_priority, category, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM target_companies WHERE priority_score >= ? "
            "ORDER BY priority_score DESC, company LIMIT ?",
            (min_priority, limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_target_company_stats(db_path):
    conn = get_connection(db_path)
    total = conn.execute("SELECT COUNT(*) FROM target_companies").fetchone()[0]
    by_category = [
        dict(r) for r in conn.execute(
            "SELECT company_category, COUNT(*) as n FROM target_companies "
            "GROUP BY company_category ORDER BY n DESC"
        ).fetchall()
    ]
    by_priority = {
        "high": conn.execute("SELECT COUNT(*) FROM target_companies WHERE priority_score >= 90").fetchone()[0],
        "mid": conn.execute("SELECT COUNT(*) FROM target_companies WHERE priority_score >= 70 AND priority_score < 90").fetchone()[0],
        "low": conn.execute("SELECT COUNT(*) FROM target_companies WHERE priority_score < 70").fetchone()[0],
    }
    total_roles = conn.execute("SELECT COUNT(*) FROM company_roles").fetchone()[0]
    conn.close()
    return {"total": total, "by_category": by_category, "by_priority": by_priority, "total_roles": total_roles}


# ---------------------------------------------------------------------------
# company_roles
# ---------------------------------------------------------------------------

def insert_company_role(db_path, role):
    conn = get_connection(db_path)
    now = datetime.now().isoformat()
    try:
        conn.execute(
            """
            INSERT INTO company_roles
            (company_id,company,role_title,role_family,source_institute,
             evidence_url,confidence_score,notes,created_at)
            VALUES
            (:company_id,:company,:role_title,:role_family,:source_institute,
             :evidence_url,:confidence_score,:notes,:created_at)
            """,
            {
                "company_id": role.get("company_id"),
                "company": role.get("company", ""),
                "role_title": role.get("role_title", ""),
                "role_family": role.get("role_family", ""),
                "source_institute": role.get("source_institute", ""),
                "evidence_url": role.get("evidence_url", ""),
                "confidence_score": int(role.get("confidence_score") or 70),
                "notes": role.get("notes", ""),
                "created_at": now,
            },
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def get_company_roles(db_path, company=None, limit=5000):
    conn = get_connection(db_path)
    if company:
        rows = conn.execute(
            "SELECT * FROM company_roles WHERE lower(company)=lower(?) ORDER BY confidence_score DESC LIMIT ?",
            (company, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM company_roles ORDER BY confidence_score DESC, company LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Extended HR contacts (DoHSS-aware)
# ---------------------------------------------------------------------------

def insert_hr_extended(db_path, hr):
    import json
    conn = get_connection(db_path)

    def _j(v):
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v)

    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO hr_contacts
            (name,company,designation,email,linkedin_url,source,notes,
             company_role_fit,relevant_role_families,source_institutes,
             verified_current_company,discovered_for_role_family,evidence_url,
             confidence_score,manual_review_required,allowed_for_auto_outreach)
            VALUES
            (:name,:company,:designation,:email,:linkedin_url,:source,:notes,
             :company_role_fit,:relevant_role_families,:source_institutes,
             :verified_current_company,:discovered_for_role_family,:evidence_url,
             :confidence_score,:manual_review_required,:allowed_for_auto_outreach)
            """,
            {
                "name": hr.get("name", ""),
                "company": hr.get("company", ""),
                "designation": hr.get("designation", ""),
                "email": hr.get("email", ""),
                "linkedin_url": hr.get("linkedin_url", ""),
                "source": hr.get("source", "linkedin_web_search"),
                "notes": hr.get("notes", ""),
                "company_role_fit": hr.get("company_role_fit", ""),
                "relevant_role_families": _j(hr.get("relevant_role_families")),
                "source_institutes": _j(hr.get("source_institutes")),
                "verified_current_company": int(bool(hr.get("verified_current_company", False))),
                "discovered_for_role_family": hr.get("discovered_for_role_family", ""),
                "evidence_url": hr.get("evidence_url", ""),
                "confidence_score": int(hr.get("confidence_score") or 70),
                "manual_review_required": int(bool(hr.get("manual_review_required", True))),
                "allowed_for_auto_outreach": int(bool(hr.get("allowed_for_auto_outreach", False))),
            },
        )
        inserted = conn.total_changes > 0
        conn.commit()
        return inserted
    finally:
        conn.close()


def get_hr_contacts_dohss(db_path, min_confidence=70, limit=1000):
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT h.*,
               tc.company_category,
               tc.priority_score AS company_priority
        FROM hr_contacts h
        LEFT JOIN target_companies tc ON lower(trim(h.company)) = tc.normalized_company
        WHERE h.confidence_score >= ?
        ORDER BY h.confidence_score DESC, tc.priority_score DESC, h.added_at DESC
        LIMIT ?
        """,
        (min_confidence, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_hr_stats_dohss(db_path):
    conn = get_connection(db_path)
    total = conn.execute("SELECT COUNT(*) FROM hr_contacts").fetchone()[0]
    high = conn.execute("SELECT COUNT(*) FROM hr_contacts WHERE confidence_score >= 90").fetchone()[0]
    mid = conn.execute("SELECT COUNT(*) FROM hr_contacts WHERE confidence_score >= 70 AND confidence_score < 90").fetchone()[0]
    needs_review = conn.execute("SELECT COUNT(*) FROM hr_contacts WHERE manual_review_required=1").fetchone()[0]
    auto_ready = conn.execute("SELECT COUNT(*) FROM hr_contacts WHERE allowed_for_auto_outreach=1").fetchone()[0]
    by_company = [
        dict(r) for r in conn.execute(
            "SELECT company, COUNT(*) as n FROM hr_contacts GROUP BY lower(trim(company)) ORDER BY n DESC LIMIT 20"
        ).fetchall()
    ]
    conn.close()
    return {
        "total": total, "high_confidence": high, "mid_confidence": mid,
        "needs_review": needs_review, "auto_ready": auto_ready,
        "by_company": by_company,
    }
