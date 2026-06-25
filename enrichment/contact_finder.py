"""
Public contact enrichment for job opportunities.

This module intentionally collects only role-based business emails found on
public official pages or returned by approved enrichment APIs. It does not
scrape private profiles, hidden LinkedIn data, or phone numbers.
"""
import logging
import os
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
DEFAULT_MAX_CONTACTS = 3

ROLE_EMAIL_PREFIXES = {
    "careers",
    "career",
    "hr",
    "jobs",
    "job",
    "recruitment",
    "recruiting",
    "recruit",
    "talent",
    "hiring",
    "people",
    "campus",
    "placement",
    "placements",
    "contact",
}

HIRING_HINTS = (
    "career",
    "job",
    "recruit",
    "talent",
    "hiring",
    "hr",
    "people",
    "placement",
)

PUBLIC_PATHS = ("", "/careers", "/jobs", "/contact", "/about", "/team", "/people")

JOB_BOARD_DOMAINS = {
    "linkedin.com",
    "www.linkedin.com",
    "naukri.com",
    "www.naukri.com",
    "internshala.com",
    "www.internshala.com",
    "news.google.com",
    "google.com",
    "www.google.com",
}

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def extract_domain(url_or_company):
    """Return a normalized domain if the input contains one, else None."""
    if not url_or_company:
        return None
    raw = str(url_or_company).strip()
    if not raw:
        return None

    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = parsed.netloc or parsed.path.split("/")[0]
    host = host.lower().strip().strip(".")
    if not host or "." not in host or " " in host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_job_board_domain(domain):
    return bool(domain and domain in JOB_BOARD_DOMAINS)


def _is_role_email(email):
    local = email.split("@", 1)[0].lower()
    local = local.split("+", 1)[0]
    local = re.sub(r"[^a-z0-9._-]", "", local)
    first_part = re.split(r"[._-]", local)[0]
    return local in ROLE_EMAIL_PREFIXES or first_part in ROLE_EMAIL_PREFIXES


def _email_domain(email):
    return email.split("@", 1)[1].lower() if "@" in email else ""


def _fetch_html(url, timeout_seconds=DEFAULT_TIMEOUT):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout_seconds)
        if resp.status_code >= 400:
            return None
        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "xml" not in content_type and content_type:
            return None
        return resp.text
    except requests.RequestException as exc:
        logger.debug("Contact enrichment fetch failed for %s: %s", url, exc)
        return None


def _visible_text_and_mailtos(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    mailtos = []
    for a in soup.find_all("a", href=True):
        href = unescape(a["href"]).strip()
        if href.lower().startswith("mailto:"):
            email = href[7:].split("?", 1)[0].strip()
            if email:
                mailtos.append(email)

    return soup.get_text(" ", strip=True), mailtos


def _build_contact(email, source_url, source_type, company_name="", company_domain=""):
    contact = {
        "company_name": company_name or "",
        "company_domain": company_domain or _email_domain(email),
        "contact_name": "",
        "role_title": "",
        "email": email.lower(),
        "linkedin_url": "",
        "source_url": source_url,
        "source_type": source_type,
    }
    contact["confidence_score"] = score_contact(contact)
    return contact


def extract_public_emails_from_url(url):
    """Extract role-based public business emails from a visible public page."""
    html = _fetch_html(url)
    if not html:
        return []

    text, mailtos = _visible_text_and_mailtos(html)
    emails = set()
    for email in mailtos + EMAIL_RE.findall(text):
        clean = email.strip().strip(".,;:()[]<>").lower()
        if clean and _is_role_email(clean):
            emails.add(clean)
    return sorted(emails)


def score_contact(contact):
    """Score a public contact from 0-100 based on source and hiring relevance."""
    score = 25
    email = (contact.get("email") or "").lower()
    source_url = (contact.get("source_url") or "").lower()
    source_type = (contact.get("source_type") or "").lower()
    company_domain = (contact.get("company_domain") or "").lower()

    if email:
        score += 20
    if _is_role_email(email):
        score += 25
    if company_domain and email.endswith(f"@{company_domain}"):
        score += 15
    if any(hint in source_url for hint in HIRING_HINTS):
        score += 10
    if source_type == "mailto":
        score += 5
    if source_type == "hunter":
        score += 10
    if not _is_role_email(email):
        score -= 30
    return max(0, min(100, score))


def _contacts_from_url(url, company_name="", company_domain="", timeout_seconds=DEFAULT_TIMEOUT):
    html = _fetch_html(url, timeout_seconds=timeout_seconds)
    if not html:
        return []

    text, mailtos = _visible_text_and_mailtos(html)
    contacts = []
    seen = set()
    for email in mailtos + EMAIL_RE.findall(text):
        clean = email.strip().strip(".,;:()[]<>").lower()
        if not clean or clean in seen or not _is_role_email(clean):
            continue
        seen.add(clean)
        source_type = "mailto" if clean in {m.lower() for m in mailtos} else "public_page"
        contacts.append(_build_contact(clean, url, source_type, company_name, company_domain))
    return contacts


def _get_hunter_key(config=None):
    key = os.environ.get("HUNTER_API_KEY", "").strip()
    if key:
        return key
    if not config:
        return ""
    cfg_key = str(config.get("enrichment", {}).get("hunter_api_key", "")).strip()
    if not cfg_key or "YOUR_" in cfg_key or "PASTE_" in cfg_key:
        return ""
    return cfg_key


def _hunter_contacts(company_name, company_domain, config=None, timeout_seconds=DEFAULT_TIMEOUT):
    hunter_requested = bool(os.environ.get("HUNTER_API_KEY", "").strip())
    if config:
        hunter_requested = hunter_requested or config.get("enrichment", {}).get("hunter_enabled", False)
    if not hunter_requested:
        return []
    api_key = _get_hunter_key(config)
    if not api_key or not company_domain:
        return []

    try:
        resp = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": company_domain, "api_key": api_key},
            timeout=timeout_seconds,
        )
        if resp.status_code >= 400:
            logger.info("Hunter enrichment skipped for %s: HTTP %s", company_domain, resp.status_code)
            return []
        data = resp.json().get("data", {})
    except Exception as exc:
        logger.info("Hunter enrichment skipped for %s: %s", company_domain, exc)
        return []

    contacts = []
    for item in data.get("emails", []):
        email = (item.get("value") or "").lower()
        if item.get("type") == "personal" or not _is_role_email(email):
            continue
        contact = _build_contact(
            email=email,
            source_url=f"https://hunter.io/search/{company_domain}",
            source_type="hunter",
            company_name=company_name,
            company_domain=company_domain,
        )
        contact["role_title"] = item.get("department") or ""
        contacts.append(contact)
    return contacts


def _candidate_urls(company_domain, source_url=None):
    urls = []
    if source_url:
        urls.append(source_url)

    if company_domain and not _is_job_board_domain(company_domain):
        for path in PUBLIC_PATHS:
            urls.append(urljoin(f"https://{company_domain}", path))

    seen = set()
    deduped = []
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


def find_company_contacts(company_name, company_domain=None, source_url=None, config=None):
    """Find public role-based hiring contacts for one company/opportunity."""
    cfg = (config or {}).get("enrichment", {})
    timeout_seconds = int(cfg.get("timeout_seconds", DEFAULT_TIMEOUT))
    max_contacts = int(cfg.get("max_contacts_per_opportunity", DEFAULT_MAX_CONTACTS))

    inferred_domain = company_domain or extract_domain(company_name)
    source_domain = extract_domain(source_url)
    if not inferred_domain and source_domain and not _is_job_board_domain(source_domain):
        inferred_domain = source_domain

    contacts = []
    for url in _candidate_urls(inferred_domain, source_url=source_url):
        try:
            contacts.extend(_contacts_from_url(url, company_name, inferred_domain, timeout_seconds))
        except Exception as exc:
            logger.debug("Contact parsing skipped for %s: %s", url, exc)

    contacts.extend(_hunter_contacts(company_name, inferred_domain, config, timeout_seconds))

    deduped = {}
    for contact in contacts:
        key = (contact.get("email"), contact.get("company_domain"), contact.get("source_url"))
        if not key[0]:
            continue
        previous = deduped.get(key)
        if not previous or contact["confidence_score"] > previous["confidence_score"]:
            deduped[key] = contact

    return sorted(deduped.values(), key=lambda c: c["confidence_score"], reverse=True)[:max_contacts]
