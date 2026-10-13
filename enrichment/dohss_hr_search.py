"""
enrichment/dohss_hr_search.py

DoHSS-aligned LinkedIn HR discovery.

Uses approved search APIs (SerpAPI / Bing / Brave) with site:linkedin.com/in
queries to find HR, Talent Acquisition, and Campus Recruiter profiles at
target companies — filtered by role family.

Extends linkedin_hr_search.py with:
  - Role-family-specific query templates
  - relevant_role_families field in returned contacts
  - source_institutes field (from target_companies table)
  - confidence_score calibrated to HR role specificity
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

# ---------------------------------------------------------------------------
# Query templates by role family
# ---------------------------------------------------------------------------

_GENERIC_QUERIES = [
    'site:linkedin.com/in "{company}" "Talent Acquisition" India',
    'site:linkedin.com/in "{company}" "Campus Recruiter" India',
    'site:linkedin.com/in "{company}" "University Recruiting" India',
    'site:linkedin.com/in "{company}" "Early Careers" India',
    'site:linkedin.com/in "{company}" "Graduate Recruitment" India',
    'site:linkedin.com/in "{company}" "Campus Hiring" India',
    'site:linkedin.com/in "{company}" "Recruiter" India',
    'site:linkedin.com/in "{company}" "Talent Partner" India',
    'site:linkedin.com/in "{company}" "HR" India',
    'site:linkedin.com/in "{company}" "People" India',
]

_ROLE_FAMILY_QUERIES = {
    "data_analytics": [
        'site:linkedin.com/in "{company}" "Recruiter" "Data Science" India',
        'site:linkedin.com/in "{company}" "Recruiter" "Analytics" India',
        'site:linkedin.com/in "{company}" "Talent Acquisition" "Data" India',
    ],
    "finance_risk_quant": [
        'site:linkedin.com/in "{company}" "Recruiter" "Risk" India',
        'site:linkedin.com/in "{company}" "Recruiter" "Quant" India',
        'site:linkedin.com/in "{company}" "Talent Acquisition" "Finance" India',
    ],
    "esg_sustainability": [
        'site:linkedin.com/in "{company}" "Recruiter" "ESG" India',
        'site:linkedin.com/in "{company}" "HR" "Sustainability" India',
        'site:linkedin.com/in "{company}" "Talent" "Climate" India',
    ],
    "policy_research": [
        'site:linkedin.com/in "{company}" "HR" "Research" India',
        'site:linkedin.com/in "{company}" "Recruiter" "Policy" India',
        'site:linkedin.com/in "{company}" "Talent" "Research" India',
    ],
    "consulting": [
        'site:linkedin.com/in "{company}" "Campus Recruiter" "Consulting" India',
        'site:linkedin.com/in "{company}" "Recruiter" "Consulting" India',
        'site:linkedin.com/in "{company}" "Talent Acquisition" "Consulting" India',
    ],
}

_LINKEDIN_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%./-]+",
    re.IGNORECASE,
)

_HR_TERMS = {
    "talent acquisition", "campus recruiter", "campus recruiting",
    "university relations", "campus hiring", "early careers",
    "graduate recruitment", "graduate recruiting", "recruiter",
    "recruitment", "hr", "human resources", "people team",
    "talent partner", "talent management", "people operations",
    "hiring manager",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Search provider helpers (reuse from linkedin_hr_search)
# ---------------------------------------------------------------------------

def _serpapi_search(query, key, max_results, timeout):
    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={"engine": "google", "q": query, "api_key": key, "num": max_results},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            return []
        return [{"url": i.get("link",""), "title": i.get("title",""), "snippet": i.get("snippet","")}
                for i in resp.json().get("organic_results", []) if i.get("link")]
    except Exception as exc:
        logger.debug("SerpAPI error: %s", exc)
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
            return []
        return [{"url": i.get("url",""), "title": i.get("title",""), "snippet": i.get("description","")}
                for i in resp.json().get("web", {}).get("results", []) if i.get("url")]
    except Exception as exc:
        logger.debug("Brave error: %s", exc)
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
            return []
        return [{"url": i.get("url",""), "title": i.get("name",""), "snippet": i.get("snippet","")}
                for i in resp.json().get("webPages", {}).get("value", []) if i.get("url")]
    except Exception as exc:
        logger.debug("Bing error: %s", exc)
        return []


def _get_key(env_name, config_value):
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    key = str(config_value or "").strip()
    if not key or key.startswith("YOUR_") or key.startswith("PASTE_"):
        return ""
    return key


def has_search_provider(config):
    sc = (config or {}).get("search", {})
    return bool(
        _get_key("SERPAPI_KEY", sc.get("serpapi_key"))
        or _get_key("BRAVE_SEARCH_API_KEY", sc.get("brave_api_key"))
        or _get_key("BING_SEARCH_API_KEY", sc.get("bing_api_key"))
    )


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _normalize_linkedin_url(url):
    if not url or "linkedin.com" not in url.lower():
        return ""
    m = re.search(r"linkedin\.com/in/([A-Za-z0-9_%./-]+)", url, re.IGNORECASE)
    if not m:
        return ""
    return f"https://www.linkedin.com/in/{m.group(1).rstrip('/')}"


def _has_hr_context(text):
    lower = (text or "").lower()
    return any(term in lower for term in _HR_TERMS)


def _extract_name(text):
    clean = re.sub(r"\s+", " ", text or "").strip()
    m = re.match(r"^([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){1,3})\s*[-–|]", clean)
    if m:
        candidate = m.group(1).strip()
        if len(candidate.split()) <= 4 and not any(
            stop in candidate.lower()
            for stop in ("private", "limited", "pvt", "india", "global", "inc", "ltd")
        ):
            return candidate
    return ""


def _extract_role(text):
    lower = (text or "").lower()
    for term in sorted(_HR_TERMS, key=len, reverse=True):
        idx = lower.find(term)
        if idx >= 0:
            start = max(0, idx - 50)
            end = min(len(text), idx + len(term) + 70)
            return text[start:end].strip(" -|,.")
    return ""


def _results_to_contacts(results, company, relevant_role_families, source_institutes):
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

        name = _extract_name(combined)
        role = _extract_role(combined)
        score = 90 if name and role else (80 if name or role else 70)

        contacts.append({
            "name": name or "Unknown",
            "company": company,
            "designation": role or "HR / Talent Acquisition",
            "linkedin_url": linkedin_url,
            "source": "dohss_hr_search",
            "notes": (combined[:200]),
            "relevant_role_families": relevant_role_families,
            "source_institutes": source_institutes,
            "company_role_fit": ", ".join(relevant_role_families[:2]),
            "discovered_for_role_family": relevant_role_families[0] if relevant_role_families else "",
            "evidence_url": url,
            "confidence_score": score,
            "manual_review_required": 1,
            "allowed_for_auto_outreach": 0,
        })
    return contacts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_hr_dohss(company, config=None, role_families=None, source_institutes=None):
    """
    Search LinkedIn for HR/TA contacts at a target company.

    company         : company name string
    config          : pipeline config dict (for API keys)
    role_families   : list of role family keys to bias search (or None for all)
    source_institutes: list of institute names that recruited from this company

    Returns list of hr contact dicts (no DB writes).
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

    relevant_families = role_families or list(_ROLE_FAMILY_QUERIES.keys())
    insts = source_institutes or []

    # Build query list: generic + role-family-specific
    queries = list(_GENERIC_QUERIES)
    for fam in relevant_families:
        queries.extend(_ROLE_FAMILY_QUERIES.get(fam, []))

    all_contacts = []
    seen_linkedin = set()

    for template in queries:
        query = template.format(company=company)
        results = []
        if serpapi_key:
            results = _serpapi_search(query, serpapi_key, max_results, timeout)
        if not results and brave_key:
            results = _brave_search(query, brave_key, max_results, timeout)
        if not results and bing_key:
            results = _bing_search(query, bing_key, max_results, timeout)

        for c in _results_to_contacts(results, company, relevant_families, insts):
            if c["linkedin_url"] not in seen_linkedin:
                seen_linkedin.add(c["linkedin_url"])
                all_contacts.append(c)

        time.sleep(random.uniform(0.8, 1.5))

    return sorted(all_contacts, key=lambda c: c["confidence_score"], reverse=True)
