"""
enrichment/placement_contacts.py
================================
Mine NAMED placement contacts (company SPOCs / recruiters / placement-cell
coordinators) out of institute placement-report PDFs.

This is a fully legitimate name source: the institute's own published report,
which routinely lists a named company SPOC and often their email/phone. Those
named contacts are exactly the input the verified-email engine needs later
(name + company -> verified email).

The pure functions (extract_contacts_from_table / extract_contacts_from_text)
take plain Python and can be unit-tested with no PDF or network. extract_from_pdf
wraps them with pdfplumber; save_placement_contacts writes to hr_contacts.
"""
import logging
import re

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\-\s]?|0)?([6-9]\d{4}[\s\-]?\d{5})(?!\d)")
# A person name: optional honorific, then 2-4 capitalised words.
NAME_RE = re.compile(
    r"(?:(?:Mr|Mrs|Ms|Dr|Prof)\.?\s+)?"
    r"([A-Z][a-z]+(?:\s+[A-Z](?:[a-z]+|\.)){1,3})"
)

# Column headers that identify a person to contact (NOT a student name).
CONTACT_COL_HINTS = (
    "spoc", "coordinator", "co-ordinator", "recruiter", "poc",
    "point of contact", "contact person", "contact name", "hr contact",
    "hr name", "representative", "signatory", "contact",
)
COMPANY_COL_HINTS = ("company", "organisation", "organization", "employer", "recruiter org")
ROLE_COL_HINTS = ("designation", "role", "title", "position")

# Free-text cues that a named contact follows.
TEXT_ROLE_CUES = (
    "spoc", "single point of contact", "placement coordinator", "placement officer",
    "placement cell", "training & placement", "training and placement",
    "recruiter", "hr contact", "contact person", "point of contact",
    "for queries", "for any queries", "for further details", "reach out to",
    "please contact", "kindly contact",
)

# Reject obvious non-person / boilerplate matches.
NAME_STOPWORDS = {
    "placement report", "annual report", "delhi school", "indian statistical",
    "tata institute", "training placement", "placement cell", "private limited",
    "research analyst", "data analyst", "policy analyst",
}

# If a candidate "name" contains any of these tokens it is structure/boilerplate,
# not a person (e.g. "Final Placement Statistics", "Company Role", "Placement Coordinator Mr").
BAD_NAME_TOKENS = {
    "placement", "company", "companies", "role", "roles", "designation", "statistics",
    "final", "report", "recruiter", "recruiters", "coordinator", "officer", "contact",
    "contacts", "analyst", "consultant", "manager", "engineer", "associate", "details",
    "queries", "campus", "recruitment", "school", "institute", "university", "college",
    "limited", "ltd", "pvt", "private", "ctc", "email", "phone", "salary", "package",
    "summary", "overview", "table", "annexure", "profile", "domain", "sector",
}


def _clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _looks_like_person(name):
    n = _clean(name)
    if not n or len(n) < 4:
        return False
    low = n.lower()
    if low in NAME_STOPWORDS:
        return False
    # at least two words, each starting uppercase
    parts = n.split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    if any(re.sub(r"[^a-z]", "", p.lower()) in BAD_NAME_TOKENS for p in parts):
        return False
    return all(p[:1].isupper() for p in parts if p)


def _first_email(text):
    m = EMAIL_RE.search(text or "")
    return m.group(0).strip().strip(".").lower() if m else ""


def _first_phone(text):
    for m in PHONE_RE.findall(text or ""):
        d = re.sub(r"[\s\-]", "", m)
        if len(d) == 10:
            return d
    return ""


def extract_contacts_from_table(header, rows):
    """header: list[str]; rows: list[list]. Returns list of contact dicts."""
    norm = [_clean(h).lower() for h in (header or [])]

    def find_col(hints):
        for hint in hints:
            for i, h in enumerate(norm):
                if hint in h:
                    return i
        return None

    contact_col = find_col(CONTACT_COL_HINTS)
    company_col = find_col(COMPANY_COL_HINTS)
    role_col = find_col(ROLE_COL_HINTS)
    # also allow a dedicated email column
    email_col = next((i for i, h in enumerate(norm) if "email" in h or "e-mail" in h), None)

    if contact_col is None and email_col is None:
        return []  # no person/email column -> not a contact table

    out = []
    for row in rows or []:
        def cell(i):
            return _clean(row[i]) if (i is not None and i < len(row)) else ""

        raw_contact = cell(contact_col)
        company = cell(company_col)
        role = cell(role_col)
        email = _first_email(cell(email_col)) or _first_email(raw_contact)
        phone = _first_phone(raw_contact) or _first_phone(cell(email_col))

        name = ""
        m = NAME_RE.search(raw_contact)
        if m and _looks_like_person(m.group(1)):
            name = _clean(m.group(1))

        if not name and not email:
            continue
        out.append({
            "company": company,
            "contact_name": name,
            "role_title": role or "Placement SPOC",
            "email": email,
            "phone": phone,
            "evidence_text": _clean(" | ".join(c for c in (company, raw_contact, role) if c))[:400],
        })
    return out


def extract_contacts_from_text(text):
    """Scan free text for 'role-cue: Name (email/phone)' style contacts."""
    text = text or ""
    out = []
    seen = set()

    # 1) Anchor on each email, look back a window for a name + role cue.
    for em in EMAIL_RE.finditer(text):
        email = em.group(0).strip().strip(".").lower()
        start = max(0, em.start() - 120)
        window = text[start:em.start()]
        nm = None
        for m in NAME_RE.finditer(window):
            if _looks_like_person(m.group(1)):
                nm = _clean(m.group(1))  # keep the last (closest) name
        role = ""
        low = window.lower()
        for cue in TEXT_ROLE_CUES:
            if cue in low:
                role = "Placement SPOC"
                break
        key = email
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "company": "",
            "contact_name": nm or "",
            "role_title": role or "Placement contact",
            "email": email,
            "phone": _first_phone(text[start:em.end() + 40]),
            "evidence_text": _clean(text[start:em.end() + 40])[:400],
        })

    # 2) Role-cue lines that name a person even without an email.
    for line in re.split(r"[\n\r]+", text):
        low = line.lower()
        if not any(cue in low for cue in TEXT_ROLE_CUES):
            continue
        if _first_email(line):
            continue  # already captured above
        m = NAME_RE.search(line)
        if m and _looks_like_person(m.group(1)):
            nm = _clean(m.group(1))
            if nm.lower() in seen:
                continue
            seen.add(nm.lower())
            out.append({
                "company": "",
                "contact_name": nm,
                "role_title": "Placement SPOC",
                "email": "",
                "phone": _first_phone(line),
                "evidence_text": _clean(line)[:400],
            })
    return out


_GENERIC_ROLES = {"placement spoc", "placement contact", ""}


def _merge(into, other):
    """Fold `other` into `into`, keeping the richer field values."""
    if not into.get("company") and other.get("company"):
        into["company"] = other["company"]
    if not into.get("contact_name") and other.get("contact_name"):
        into["contact_name"] = other["contact_name"]
    if not into.get("phone") and other.get("phone"):
        into["phone"] = other["phone"]
    if into.get("role_title", "").lower() in _GENERIC_ROLES and other.get("role_title", "").lower() not in _GENERIC_ROLES:
        into["role_title"] = other["role_title"]
    if len(other.get("evidence_text", "")) > len(into.get("evidence_text", "")):
        into["evidence_text"] = other["evidence_text"]
    return into


def _dedupe(contacts):
    by_key = {}
    order = []
    for c in contacts:
        if not c.get("contact_name") and not c.get("email"):
            continue
        key = ("email:" + c["email"]) if c.get("email") else ("name:" + (c.get("contact_name") or "").lower())
        if key in by_key:
            _merge(by_key[key], c)
        else:
            by_key[key] = dict(c)
            order.append(key)
    return [by_key[k] for k in order]


def extract_from_pdf(local_path, institute="", source_url=""):
    """Open a placement-report PDF and return named contact dicts."""
    import pdfplumber
    contacts = []
    try:
        with pdfplumber.open(local_path) as pdf:
            for page in pdf.pages:
                for table in (page.extract_tables() or []):
                    if table and len(table) >= 2:
                        contacts.extend(extract_contacts_from_table(table[0], table[1:]))
                contacts.extend(extract_contacts_from_text(page.extract_text() or ""))
    except Exception as exc:
        logger.error("placement contact parse error %s: %s", local_path, exc)
    for c in contacts:
        c["institute"] = institute
        c["source_url"] = source_url
    return _dedupe(contacts)


def save_placement_contacts(db_path, institute, contacts):
    """Persist named contacts into hr_contacts. Returns #inserted."""
    from database import db
    inserted = 0
    for c in contacts:
        if not c.get("contact_name") and not c.get("email"):
            continue
        notes_parts = [c.get("evidence_text", "")]
        if c.get("phone"):
            notes_parts.append(f"phone: {c['phone']}")
        if c.get("source_url"):
            notes_parts.append(f"src: {c['source_url']}")
        notes = " | ".join(p for p in notes_parts if p)
        # linkedin_url has a UNIQUE constraint; use NULL (multiple NULLs are
        # allowed) so SPOCs from the same report don't collide and get dropped.
        ok = db.insert_hr(db_path, {
            "name": c.get("contact_name") or "(name unknown)",
            "company": c.get("company") or "",
            "designation": c.get("role_title") or "Placement SPOC",
            "email": c.get("email") or "",
            "linkedin_url": c.get("linkedin_url") or None,
            "source": f"placement_report:{institute}",
            "notes": notes,
        })
        if ok:
            inserted += 1
    return inserted
