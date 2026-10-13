"""
LinkedIn HR discovery via search APIs.

Uses approved search APIs (SerpAPI / Bing / Brave) with
`site:linkedin.com/in` queries to surface HR, Talent Acquisition, and
Campus Recruiter profiles at target companies.

No direct LinkedIn scraping — only public search snippets and index data
are used.  linkedin.com pages are never fetched.
"""
import logging
import os
import re
import time
import random

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
DEFAULT_MAX_RESULTS = 5

# Search templates ordered by specificity.  {company} is substituted.
_QUERY_TEMPLATES = [
    'site:linkedin.com/in "{company}" "Talent Acquisition"',
    'site:linkedin.com/in "{company}" "Campus Recruiter"',
    'site:linkedin.com/in "{company}" "University Relations"',
    'site:linkedin.com/in "{company}" "Campus Hiring"',
    'site:linkedin.com/in "{company}" "Recruiter"',
    'site:linkedin.com/in "{company}" "HR"',
    'site:linkedin.com/in "{company}" "Early Careers"',
    'site:linkedin.com/in "{company}" "Graduate Recruitment"',
    'site:linkedin.com/in "{company}" "Talent Partner"',
    'site:linkedin.com/in "{company}" "People"',
]

_LINKEDIN_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%./-]+",
    re.IGNORECASE,
)

_HR_TERMS = {
    "talent acquisition", "campus recruiter", "campus recruiting",
    "university relations", "campus hiring", "early careers",
    "graduate recruitment", "graduate recruiting", "recruiter",
    "recruitment", "hr", "human resources", "people team",
    "hiring manager", "talent partner", "talent management",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Search provider helpers
# ---------------------------------------------------------------------------

def _serpapi_search(query, key, max_results, timeout):
    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={"engine": "google", "q": query, "api_key": key, "num": max_results},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            logger.debug("SerpAPI HTTP %s for query: %s", resp.status_code, query)
            return []
        return [
            {
                "url": item.get("link") or "",
                "title": item.get("title") or "",
                "snippet": item.get("snippet") or "",
            }
            for item in resp.json().get("organic_results", [])
            if item.get("link")
        ]
    except Exception as exc:
        logger.debug("SerpAPI skipped: %s", exc)
        return []


def _brave_search(query, key, max_results, timeout):
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results, "search_lang": "en"},
            headers={**_HEADERS, "X-Subscription-Token": key},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            logger.debug("Brave HTTP %s for query: %s", resp.status_code, query)
            return []
        data = resp.json()
        return [
            {
                "url": item.get("url") or "",
                "title": item.get("title") or "",
                "snippet": item.get("description") or "",
            }
            for item in data.get("web", {}).get("results", [])
            if item.get("url")
        ]
    except Exception as exc:
        logger.debug("Brave skipped: %s", exc)
        return []


def _bing_search(query, key, max_results, timeout):
    try:
        resp = requests.get(
            "https://api.bing.microsoft.com/v7.0/search",
            params={"q": query, "count": max_results},
            headers={**_HEADERS, "Ocp-Apim-Subscription-Key": key},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            logger.debug("Bing HTTP %s for query: %s", resp.status_code, query)
            return []
        return [
            {
                "url": item.get("url") or "",
                "title": item.get("name") or "",
                "snippet": item.get("snippet") or "",
            }
            for item in resp.json().get("webPages", {}).get("value", [])
            if item.get("url")
        ]
    except Exception as exc:
        logger.debug("Bing skipped: %s", exc)
        return []


def _get_key(env_name, config_value):
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    key = str(config_value or "").strip()
    if not key or key.startswith("YOUR_") or key.startswith("PASTE_"):
        return ""
    return key


# ---------------------------------------------------------------------------
# Core extraction helpers
# ---------------------------------------------------------------------------

def _normalize_linkedin_url(url):
    if not url or "linkedin.com" not in url.lower():
        return ""
    match = re.search(r"linkedin\.com/in/([A-Za-z0-9_%./-]+)", url, re.IGNORECASE)
    if not match:
        return ""
    slug = match.group(1).rstrip("/")
    return f"https://www.linkedin.com/in/{slug}"


def _has_hr_context(text):
    lower = (text or "").lower()
    return any(term in lower for term in _HR_TERMS)


def _extract_name_and_role(text):
    """Parse 'First Last - Role at Company' style LinkedIn snippets."""
    clean = re.sub(r"\s+", " ", text or "").strip()

    # Role: scan for known HR terms
    role = ""
    for term in sorted(_HR_TERMS, key=len, reverse=True):
        idx = clean.lower().find(term)
        if idx >= 0:
            start = max(0, idx - 60)
            end = min(len(clean), idx + len(term) + 80)
            role = clean[start:end].strip(" -|,.")
            break

    # Name: look for 'FirstName LastName - Role' pattern
    name = ""
    match = re.match(
        r"^([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){1,3})\s*[-–|]",
        clean,
    )
    if match:
        candidate = match.group(1).strip()
        # Reject if it looks like a company name token embedded in title
        if len(candidate.split()) <= 4 and not any(
            stop in candidate.lower()
            for stop in ("private", "limited", "pvt", "india", "global", "inc")
        ):
            name = candidate

    return name, role


def _results_to_contacts(results, company):
    contacts = []
    seen_urls = set()
    for result in results:
        url = result.get("url") or ""
        title = result.get("title") or ""
        snippet = result.get("snippet") or ""
        combined = f"{title} {snippet}"

        linkedin_url = _normalize_linkedin_url(url)
        if not linkedin_url or linkedin_url in seen_urls:
            continue
        if not _has_hr_context(combined):
            continue
        seen_urls.add(linkedin_url)

        name, role = _extract_name_and_role(combined)
        contacts.append(
            {
                "contact_name": name,
                "role_title": role,
                "linkedin_url": linkedin_url,
                "company_name": company,
                "company_domain": "",
                "email": "",
                "source_url": url,
                "source_type": "linkedin_public_profile",
                "evidence_text": combined[:300],
                "confidence_score": 70 if name else 60,
            }
        )
    return contacts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_hr_for_company(company, config=None):
    """
    Run LinkedIn HR searches for a single company.

    Returns a list of contact dicts (no database writes).
    Each dict has: contact_name, role_title, linkedin_url, company_name,
    source_url, source_type, evidence_text, confidence_score.
    """
    cfg = config or {}
    search_cfg = cfg.get("search", {})
    enrichment_cfg = cfg.get("enrichment", {})

    serpapi_key = _get_key("SERPAPI_KEY", search_cfg.get("serpapi_key"))
    brave_key = _get_key("BRAVE_SEARCH_API_KEY", search_cfg.get("brave_api_key"))
    bing_key = _get_key("BING_SEARCH_API_KEY", search_cfg.get("bing_api_key"))

    if not any([serpapi_key, brave_key, bing_key]):
        return []

    timeout = max(1, min(int(enrichment_cfg.get("request_timeout_seconds", DEFAULT_TIMEOUT)), 15))
    max_results = max(1, min(int(enrichment_cfg.get("max_search_results_per_query", DEFAULT_MAX_RESULTS)), 10))

    all_contacts = []
    seen_linkedin = set()

    for template in _QUERY_TEMPLATES:
        query = template.format(company=company)
        results = []
        if serpapi_key:
            results = _serpapi_search(query, serpapi_key, max_results, timeout)
        if not results and brave_key:
            results = _brave_search(query, brave_key, max_results, timeout)
        if not results and bing_key:
            results = _bing_search(query, bing_key, max_results, timeout)

        for contact in _results_to_contacts(results, company):
            key = contact["linkedin_url"]
            if key not in seen_linkedin:
                seen_linkedin.add(key)
                all_contacts.append(contact)

        # Polite delay between search queries
        time.sleep(random.uniform(0.8, 1.5))

    return sorted(all_contacts, key=lambda c: c["confidence_score"], reverse=True)


def has_search_provider(config):
    """Return True if at least one search API key is configured."""
    search_cfg = (config or {}).get("search", {})
    return bool(
        _get_key("SERPAPI_KEY", search_cfg.get("serpapi_key"))
        or _get_key("BRAVE_SEARCH_API_KEY", search_cfg.get("brave_api_key"))
        or _get_key("BING_SEARCH_API_KEY", search_cfg.get("bing_api_key"))
    )
