"""
Search-based public contact discovery.

Uses approved search APIs when keys are available, then parses only public,
visible snippets/pages for professional contact details. It never logs in,
never scrapes hidden LinkedIn data, and never collects phone numbers.
"""
import logging
import os
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from enrichment.contact_finder import extract_domain

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_SEARCH_RESULTS = 5

JOB_BOARD_DOMAINS = {
    "linkedin.com",
    "www.linkedin.com",
    "naukri.com",
    "www.naukri.com",
    "internshala.com",
    "www.internshala.com",
    "google.com",
    "www.google.com",
    "news.google.com",
    "indeed.com",
    "www.indeed.com",
    "glassdoor.com",
    "www.glassdoor.com",
}

OFFICIAL_PATHS = ("", "/careers", "/jobs", "/contact", "/about", "/team", "/people")

SEARCH_QUERY_TEMPLATES = (
    '"{company_name}" "Talent Acquisition"',
    '"{company_name}" "Campus Recruiter"',
    '"{company_name}" "University Relations"',
    '"{company_name}" "Campus Hiring"',
    '"{company_name}" "HR email"',
    '"{company_name}" "recruitment email"',
    '"{company_name}" "careers contact"',
    'site:linkedin.com/in "{company_name}" "Talent Acquisition"',
    'site:linkedin.com/in "{company_name}" "Campus Recruiter"',
    'site:linkedin.com/in "{company_name}" "Recruiter"',
)

HR_TERMS = (
    "hr",
    "human resources",
    "talent",
    "talent acquisition",
    "recruiter",
    "recruiting",
    "recruitment",
    "campus",
    "campus recruiter",
    "campus hiring",
    "university relations",
    "people",
    "hiring",
)

ROLE_EMAIL_PREFIXES = {
    "hr",
    "careers",
    "career",
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
}

GENERIC_EMAIL_PREFIXES = {"info", "contact", "hello", "admin", "support"}

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%./-]+", re.IGNORECASE)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def is_job_board_domain(domain):
    if not domain:
        return False
    lower = domain.lower()
    return lower in JOB_BOARD_DOMAINS or any(
        lower.endswith(f".{base}") for base in JOB_BOARD_DOMAINS
    )


def _config_int(config, key, default):
    try:
        return int((config or {}).get("enrichment", {}).get(key, default))
    except (TypeError, ValueError):
        return default


def request_timeout(config):
    return max(1, min(_config_int(config, "request_timeout_seconds", DEFAULT_TIMEOUT), 10))


def _search_config(config):
    return (config or {}).get("search", {})


def _hunter_config(config):
    enrichment = (config or {}).get("enrichment", {})
    hunter = enrichment.get("hunter", {})
    if isinstance(hunter, dict):
        return hunter
    return {"enabled": bool(enrichment.get("hunter_enabled", False)), "api_key": ""}


def _key_from_env_or_config(env_name, config_value):
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    key = str(config_value or "").strip()
    if not key or key.startswith("YOUR_") or key.startswith("PASTE_"):
        return ""
    return key


def generate_search_queries(company_name):
    clean = (company_name or "").strip()
    if not clean:
        return []
    return [template.format(company_name=clean) for template in SEARCH_QUERY_TEMPLATES]


def _fetch_html(url, timeout_seconds=DEFAULT_TIMEOUT):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout_seconds)
        if resp.status_code >= 400:
            return None
        content_type = resp.headers.get("content-type", "")
        if content_type and "text/html" not in content_type and "xml" not in content_type:
            return None
        return resp.text
    except requests.RequestException as exc:
        logger.debug("Search enrichment fetch skipped for %s: %s", url, exc)
        return None


def _clean_email(email):
    return (email or "").strip().strip(".,;:()[]<>").lower()


def _email_prefix(email):
    return _clean_email(email).split("@", 1)[0].split("+", 1)[0]


def _is_role_email(email):
    local = re.sub(r"[^a-z0-9._-]", "", _email_prefix(email))
    first = re.split(r"[._-]", local)[0]
    return local in ROLE_EMAIL_PREFIXES or first in ROLE_EMAIL_PREFIXES


def _is_generic_email(email):
    local = re.sub(r"[^a-z0-9._-]", "", _email_prefix(email))
    first = re.split(r"[._-]", local)[0]
    return local in GENERIC_EMAIL_PREFIXES or first in GENERIC_EMAIL_PREFIXES


def _has_hr_role(text):
    lower = (text or "").lower()
    return any(term in lower for term in HR_TERMS)


def _normalize_linkedin_url(url):
    if not url:
        return ""
    parsed = urlparse(url)
    if "linkedin.com" not in parsed.netloc.lower() or not parsed.path.lower().startswith("/in/"):
        return ""
    return f"https://www.linkedin.com{parsed.path.rstrip('/')}"


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


def _guess_name_role(text, company_name=""):
    if not text:
        return "", ""
    clean = re.sub(r"\s+", " ", text).strip()
    role = ""
    for term in HR_TERMS:
        idx = clean.lower().find(term)
        if idx >= 0:
            start = max(0, idx - 70)
            end = min(len(clean), idx + 90)
            role = clean[start:end].strip(" -|,")
            break

    name = ""
    match = re.search(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b.{0,80}?"
        r"(Talent Acquisition|Campus Recruiter|Recruiter|University Relations|Human Resources|HR|People)",
        clean,
    )
    if match:
        candidate = match.group(1).strip()
        if company_name.lower() not in candidate.lower():
            name = candidate
    return name, role


def score_contact(contact):
    email = _clean_email(contact.get("email"))
    linkedin_url = contact.get("linkedin_url") or ""
    source_url = contact.get("source_url") or ""
    source_type = contact.get("source_type") or ""
    role_title = contact.get("role_title") or ""
    contact_name = contact.get("contact_name") or ""
    company_domain = contact.get("company_domain") or ""

    has_named_hr = bool(contact_name and _has_hr_role(role_title))
    official_email = bool(email and company_domain and email.endswith(f"@{company_domain}"))
    role_email = bool(email and _is_role_email(email))
    generic_email = bool(email and _is_generic_email(email))
    official_contact_page = bool(
        email
        and company_domain
        and extract_domain(source_url) == company_domain
        and any(path in source_url.lower() for path in ("/contact", "/careers", "/jobs"))
    )

    if has_named_hr and email:
        return 95
    if official_email and role_email:
        return 85
    if official_contact_page:
        return 75
    if contact_name and linkedin_url and _has_hr_role(role_title):
        return 65
    if generic_email:
        return 60
    if source_type == "hunter_api" and email and _has_hr_role(role_title):
        return 80
    if email:
        return 55
    if linkedin_url and _has_hr_role(role_title):
        return 60
    return 0


def _build_contact(
    *,
    company_name,
    company_domain,
    source_url,
    source_type,
    email="",
    linkedin_url="",
    contact_name="",
    role_title="",
):
    contact = {
        "company_name": company_name or "",
        "company_domain": company_domain or (extract_domain(source_url) or ""),
        "contact_name": contact_name or "",
        "role_title": role_title or "",
        "email": _clean_email(email),
        "linkedin_url": _normalize_linkedin_url(linkedin_url) or linkedin_url or "",
        "source_url": source_url or "",
        "source_type": source_type or "public_search",
    }
    contact["confidence_score"] = score_contact(contact)
    return contact


def _contacts_from_text(text, source_url, source_type, company_name, company_domain):
    contacts = []
    name, role = _guess_name_role(text, company_name)
    hr_context = _has_hr_role(text)

    for email in sorted({_clean_email(e) for e in EMAIL_RE.findall(text or "") if _clean_email(e)}):
        email_domain = email.split("@", 1)[1] if "@" in email else ""
        professional_hr_email = bool(company_domain and email_domain == company_domain and hr_context)
        if not (_is_role_email(email) or _is_generic_email(email) or professional_hr_email):
            continue
        contacts.append(
            _build_contact(
                company_name=company_name,
                company_domain=company_domain,
                source_url=source_url,
                source_type=source_type,
                email=email,
                contact_name=name,
                role_title=role,
            )
        )

    for linkedin_url in sorted({_normalize_linkedin_url(u) for u in LINKEDIN_RE.findall(text or "") if u}):
        if not linkedin_url or not _has_hr_role(text):
            continue
        contacts.append(
            _build_contact(
                company_name=company_name,
                company_domain=company_domain,
                source_url=source_url,
                source_type="linkedin_public_profile",
                linkedin_url=linkedin_url,
                contact_name=name,
                role_title=role,
            )
        )
    return contacts


def parse_public_page_for_contacts(url, company_name="", company_domain="", config=None):
    html = _fetch_html(url, timeout_seconds=request_timeout(config))
    if not html:
        return []
    text, mailtos = _visible_text_and_mailtos(html)
    contacts = _contacts_from_text(text, url, "official_public_page", company_name, company_domain)
    for email in mailtos:
        contact = _build_contact(
            company_name=company_name,
            company_domain=company_domain,
            source_url=url,
            source_type="mailto_public_page",
            email=email,
        )
        contacts.append(contact)
    return contacts


def _serpapi_results(query, config):
    search_cfg = _search_config(config)
    key = _key_from_env_or_config("SERPAPI_KEY", search_cfg.get("serpapi_key"))
    if not key:
        return []
    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google",
                "q": query,
                "api_key": key,
                "num": _config_int(config, "max_search_results_per_query", DEFAULT_MAX_SEARCH_RESULTS),
            },
            timeout=request_timeout(config),
        )
        if resp.status_code >= 400:
            logger.info("SerpAPI skipped query due to HTTP %s", resp.status_code)
            return []
        data = resp.json()
    except Exception as exc:
        logger.info("SerpAPI skipped query: %s", exc)
        return []
    results = []
    for item in data.get("organic_results", []):
        link = item.get("link") or ""
        snippet = " ".join([item.get("title") or "", item.get("snippet") or ""])
        if link:
            results.append({"url": link, "snippet": snippet, "source_type": "serpapi"})
    return results


def _bing_results(query, config):
    search_cfg = _search_config(config)
    key = _key_from_env_or_config("BING_SEARCH_API_KEY", search_cfg.get("bing_api_key"))
    if not key:
        return []
    try:
        resp = requests.get(
            "https://api.bing.microsoft.com/v7.0/search",
            params={"q": query, "count": _config_int(config, "max_search_results_per_query", DEFAULT_MAX_SEARCH_RESULTS)},
            headers={**HEADERS, "Ocp-Apim-Subscription-Key": key},
            timeout=request_timeout(config),
        )
        if resp.status_code >= 400:
            logger.info("Bing Search skipped query due to HTTP %s", resp.status_code)
            return []
        data = resp.json()
    except Exception as exc:
        logger.info("Bing Search skipped query: %s", exc)
        return []
    results = []
    for item in data.get("webPages", {}).get("value", []):
        link = item.get("url") or ""
        snippet = " ".join([item.get("name") or "", item.get("snippet") or ""])
        if link:
            results.append({"url": link, "snippet": snippet, "source_type": "bing_search"})
    return results


def search_web(query, config=None):
    results = []
    results.extend(_serpapi_results(query, config))
    results.extend(_bing_results(query, config))
    seen = set()
    deduped = []
    max_results = _config_int(config, "max_search_results_per_query", DEFAULT_MAX_SEARCH_RESULTS)
    for result in results:
        url = result["url"]
        if url in seen:
            continue
        seen.add(url)
        deduped.append(result)
        if len(deduped) >= max_results:
            break
    return deduped


def discover_company_domain(company_name, source_url=None, config=None):
    source_domain = extract_domain(source_url)
    if source_domain and not is_job_board_domain(source_domain):
        return source_domain

    for query in (
        f'"{company_name}" official careers',
        f'"{company_name}" contact',
        f'"{company_name}" jobs',
    ):
        for result in search_web(query, config):
            domain = extract_domain(result["url"])
            if domain and not is_job_board_domain(domain):
                return domain
    return None


def official_candidate_urls(company_domain, source_url=None, config=None):
    urls = []
    source_domain = extract_domain(source_url)
    if source_url and source_domain and not is_job_board_domain(source_domain):
        urls.append(source_url)
    if company_domain:
        for path in OFFICIAL_PATHS:
            urls.append(urljoin(f"https://{company_domain}", path))
    seen = set()
    deduped = []
    max_pages = _config_int(config, "max_pages_per_company", DEFAULT_MAX_PAGES)
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(url)
        if len(deduped) >= max_pages:
            break
    return deduped


def hunter_contacts(company_name, company_domain, config=None):
    hunter_cfg = _hunter_config(config)
    key = _key_from_env_or_config("HUNTER_API_KEY", hunter_cfg.get("api_key"))
    enabled = bool(os.environ.get("HUNTER_API_KEY", "").strip()) or bool(hunter_cfg.get("enabled"))
    if not enabled or not key or not company_domain:
        return []
    try:
        resp = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": company_domain, "api_key": key},
            timeout=request_timeout(config),
        )
        if resp.status_code >= 400:
            logger.info("Hunter skipped %s due to HTTP %s", company_domain, resp.status_code)
            return []
        data = resp.json().get("data", {})
    except Exception as exc:
        logger.info("Hunter skipped %s: %s", company_domain, exc)
        return []

    contacts = []
    for item in data.get("emails", []):
        email = _clean_email(item.get("value"))
        role_text = " ".join(
            str(v or "")
            for v in (
                item.get("position"),
                item.get("department"),
                item.get("seniority"),
                item.get("type"),
            )
        )
        if item.get("type") == "personal" and not _has_hr_role(role_text):
            continue
        if not email or not (_has_hr_role(role_text) or _is_role_email(email)):
            continue
        contact_name = " ".join(
            p for p in (item.get("first_name") or "", item.get("last_name") or "") if p
        )
        contacts.append(
            _build_contact(
                company_name=company_name,
                company_domain=company_domain,
                source_url=f"https://hunter.io/search/{company_domain}",
                source_type="hunter_api",
                email=email,
                contact_name=contact_name,
                role_title=role_text.strip(),
            )
        )
    return contacts


def discover_contacts(company_name, company_domain=None, source_url=None, config=None):
    company_name = (company_name or "").strip()
    if not company_name:
        return []

    domain = company_domain or discover_company_domain(company_name, source_url, config)
    contacts = []

    if domain:
        for url in official_candidate_urls(domain, source_url, config):
            contacts.extend(parse_public_page_for_contacts(url, company_name, domain, config))
        contacts.extend(hunter_contacts(company_name, domain, config))

    search_results = []
    for query in generate_search_queries(company_name):
        search_results.extend(search_web(query, config))

    max_pages = _config_int(config, "max_pages_per_company", DEFAULT_MAX_PAGES)
    for result in search_results[:max_pages]:
        url = result["url"]
        snippet = result.get("snippet") or ""
        result_domain = extract_domain(url)
        result_source = result.get("source_type") or "search_result"
        result_company_domain = domain or (result_domain if not is_job_board_domain(result_domain) else "")
        contacts.extend(
            _contacts_from_text(snippet, url, result_source, company_name, result_company_domain)
        )
        linkedin_url = _normalize_linkedin_url(url)
        if linkedin_url and _has_hr_role(snippet):
            name, role = _guess_name_role(snippet, company_name)
            contacts.append(
                _build_contact(
                    company_name=company_name,
                    company_domain=result_company_domain,
                    source_url=url,
                    source_type="linkedin_public_profile",
                    linkedin_url=linkedin_url,
                    contact_name=name,
                    role_title=role,
                )
            )
        if result_domain and not is_job_board_domain(result_domain):
            contacts.extend(parse_public_page_for_contacts(url, company_name, result_company_domain, config))

    return _dedupe_contacts(contacts)


def _dedupe_contacts(contacts):
    deduped = {}
    for contact in contacts:
        email = _clean_email(contact.get("email"))
        linkedin_url = contact.get("linkedin_url") or ""
        if not email and not linkedin_url:
            continue
        key = (
            email or linkedin_url,
            contact.get("company_domain") or "",
            contact.get("source_url") or "",
        )
        existing = deduped.get(key)
        if not existing or int(contact.get("confidence_score") or 0) > int(existing.get("confidence_score") or 0):
            deduped[key] = contact
    return sorted(
        deduped.values(),
        key=lambda item: int(item.get("confidence_score") or 0),
        reverse=True,
    )
