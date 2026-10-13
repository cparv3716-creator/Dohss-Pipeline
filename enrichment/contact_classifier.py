"""Contact email classification and scoring."""
import re
from urllib.parse import urlparse

EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)

HIRING_KEYWORDS = (
    "career",
    "careers",
    "job",
    "jobs",
    "hr",
    "recruit",
    "recruitment",
    "recruiting",
    "recruiter",
    "talent",
    "campus",
    "university",
    "graduate",
    "earlycareer",
    "early.career",
    "early career",
    "placement",
    "hiring",
)

LOCAL_PART_HIRING_KEYWORDS = (
    "campus",
    "recruitment",
    "recruiting",
    "recruit",
    "careers",
    "career",
    "jobs",
    "job",
    "hiring",
    "talent",
    "hr",
    "university",
    "graduate",
    "earlycareer",
    "early.career",
    "placement",
)

EXCLUDED_KEYWORDS = (
    "disability",
    "assistance",
    "accessibility",
    "accommodation",
    "privacy",
    "legal",
    "fraud",
    "scam",
    "report",
    "abuse",
    "security",
    "phishing",
    "webmaster",
    "noreply",
    "no-reply",
    "donotreply",
    "unsubscribe",
    "media",
    "press",
    "investor",
    "grievance",
    "complaints",
)

EVIDENCE_OVERRIDE_EXCLUDED_KEYWORDS = (
    "disability",
    "accessibility",
    "accommodation",
    "reasonable accommodation",
    "special need",
)

GENERIC_PREFIXES = {"info", "contact", "hello", "support"}

OFFICIAL_DOMAIN_ALIASES = {
    "american express": {"americanexpress.com", "aexp.com"},
    "americanexpress": {"americanexpress.com", "aexp.com"},
    "aexp.com": {"americanexpress.com", "aexp.com"},
    "americanexpress.com": {"americanexpress.com", "aexp.com"},
}


def clean_email(email):
    return (email or "").strip().strip(".,;:()[]<>").lower()


def is_valid_email(email):
    return bool(EMAIL_RE.match(clean_email(email)))


def email_local(email):
    return clean_email(email).split("@", 1)[0] if "@" in clean_email(email) else ""


def email_domain(email):
    return clean_email(email).split("@", 1)[1] if "@" in clean_email(email) else ""


def text_has_hiring_keyword(text):
    lower = (text or "").lower()
    return any(keyword in lower for keyword in HIRING_KEYWORDS)


def local_part_has_hiring_keyword(local):
    compact = re.sub(r"[^a-z0-9]+", "", (local or "").lower())
    lower = (local or "").lower()
    return any(keyword.replace(".", "") in compact or keyword in lower for keyword in LOCAL_PART_HIRING_KEYWORDS)


def text_has_excluded_keyword(text):
    lower = (text or "").lower()
    return any(keyword in lower for keyword in EXCLUDED_KEYWORDS)


def evidence_has_override_exclusion(text):
    lower = (text or "").lower()
    return any(keyword in lower for keyword in EVIDENCE_OVERRIDE_EXCLUDED_KEYWORDS)


def company_domain_aliases(company_domain="", company_name=""):
    aliases = set()
    domain = (company_domain or "").lower().strip()
    name_key = " ".join((company_name or "").lower().split())
    if domain:
        aliases.add(domain)
        aliases.update(OFFICIAL_DOMAIN_ALIASES.get(domain, set()))
    if name_key:
        aliases.update(OFFICIAL_DOMAIN_ALIASES.get(name_key, set()))
    return aliases


def _source_host(source_url):
    if not source_url:
        return ""
    host = urlparse(source_url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_official_source(source_url, company_domain, company_name=""):
    if not source_url:
        return False
    host = _source_host(source_url)
    aliases = company_domain_aliases(company_domain, company_name)
    return any(host == alias or host.endswith(f".{alias}") for alias in aliases)


def is_careers_or_contact_page(source_url):
    parsed = urlparse(source_url or "")
    haystack = f"{parsed.netloc} {parsed.path}".lower()
    return any(
        token in haystack
        for token in (
            "career",
            "jobs",
            "job",
            "contact",
            "campus",
            "graduate",
            "early-career",
            "early_career",
            "recruit",
        )
    )


def role_title_for(email, evidence):
    haystack = f"{email} {evidence}".lower()
    if "campus" in haystack or "university" in haystack or "graduate" in haystack:
        return "Campus recruitment contact"
    if "career" in haystack or "job" in haystack:
        return "Careers contact"
    return "Recruitment contact"


def classify_contact(contact):
    source_type = contact.get("source_type") or ""
    if source_type == "guessed_role_email":
        return {
            "email": clean_email(contact.get("email")),
            "email_category": "guessed_role_email",
            "confidence_score": 40,
            "role_title": "Manual review guessed role email",
            "contact_name": "",
            "allowed_for_auto_outreach": 0,
            "is_valid": is_valid_email(contact.get("email")),
            "excluded": False,
            "source_type": "guessed_role_email",
            "reject_reason": "",
        }

    email = clean_email(contact.get("email"))
    evidence = contact.get("evidence_text") or ""
    source_url = contact.get("source_url") or ""
    company_domain = (contact.get("company_domain") or "").lower()
    company_name = contact.get("company_name") or ""

    if not email or not is_valid_email(email):
        return {
            "email": email,
            "email_category": "invalid_email",
            "confidence_score": 0,
            "role_title": "",
            "contact_name": "",
            "allowed_for_auto_outreach": 0,
            "is_valid": False,
            "excluded": True,
            "source_type": contact.get("source_type") or "",
            "reject_reason": "invalid_email",
        }

    local = email_local(email)
    domain = email_domain(email)
    local_hiring = local_part_has_hiring_keyword(local)
    evidence_hiring = text_has_hiring_keyword(evidence)
    local_excluded = text_has_excluded_keyword(local)
    evidence_excluded = text_has_excluded_keyword(evidence)

    if (
        local_excluded
        or evidence_has_override_exclusion(evidence)
        or (evidence_excluded and not local_hiring and not evidence_hiring)
    ):
        return {
            "email": email,
            "email_category": "excluded_non_hiring_email",
            "confidence_score": 0,
            "role_title": "",
            "contact_name": "",
            "allowed_for_auto_outreach": 0,
            "is_valid": True,
            "excluded": True,
            "source_type": contact.get("source_type") or "",
            "reject_reason": "excluded_non_hiring_email",
        }

    aliases = company_domain_aliases(company_domain, company_name)
    source_host = _source_host(source_url)
    official_email = bool(domain and (domain in aliases or domain == source_host))
    official_page = is_official_source(source_url, company_domain, company_name)
    official_careers_page = official_page and is_careers_or_contact_page(source_url)
    generic = local.split(".", 1)[0] in GENERIC_PREFIXES

    if local_hiring or evidence_hiring:
        score = 90 if official_email and official_careers_page else 85
        return {
            "email": email,
            "email_category": "hiring_email",
            "confidence_score": score,
            "role_title": role_title_for(email, evidence),
            "contact_name": "",
            "allowed_for_auto_outreach": int(score >= 90),
            "is_valid": True,
            "excluded": False,
            "source_type": "official_hiring_email" if score >= 85 else (contact.get("source_type") or ""),
            "reject_reason": "",
        }

    if generic and official_email:
        return {
            "email": email,
            "email_category": "generic_contact_email",
            "confidence_score": 55,
            "role_title": "Generic company contact",
            "contact_name": "",
            "allowed_for_auto_outreach": 0,
            "is_valid": True,
            "excluded": False,
            "source_type": contact.get("source_type") or "official_public_page",
            "reject_reason": "",
        }

    return {
        "email": email,
        "email_category": "other_public_email",
        "confidence_score": 0,
        "role_title": "",
        "contact_name": "",
        "allowed_for_auto_outreach": 0,
        "is_valid": True,
        "excluded": True,
        "source_type": contact.get("source_type") or "",
        "reject_reason": "not_hiring_or_generic_contact",
    }
