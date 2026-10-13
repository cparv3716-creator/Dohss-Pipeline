"""Company domain resolver for the DoHSS pipeline."""
import csv
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from enrichment.contact_finder import extract_domain

BAD_COMPANY_NAMES = {
    "",
    "unknown",
    "hire feed",
    "linkedin",
    "naukri",
    "internshala",
    "google alerts",
    "google alert",
}

REJECT_DOMAINS = {
    "linkedin.com",
    "naukri.com",
    "internshala.com",
    "google.com",
    "news.google.com",
    "indeed.com",
    "glassdoor.com",
    "feedspot.com",
}

COMMON_DOMAINS = {
    "ey": "ey.com",
    "ernst young": "ey.com",
    "mastercard": "mastercard.com",
    "bcg": "bcg.com",
    "boston consulting group": "bcg.com",
    "blackrock": "blackrock.com",
    "citi": "citi.com",
    "citibank": "citi.com",
    "wns": "wns.com",
    "wri india": "wri-india.org",
    "world resources institute india": "wri-india.org",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def normalize_company_name(name):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).split())


def is_bad_company_name(name):
    normalized = normalize_company_name(name)
    if normalized in BAD_COMPANY_NAMES:
        return True
    return any(fragment in normalized for fragment in ("job feed", "jobs feed", "hire feed", "alert feed"))


def is_rejected_domain(domain):
    if not domain:
        return True
    lower = domain.lower()
    return lower in REJECT_DOMAINS or any(lower.endswith(f".{base}") for base in REJECT_DOMAINS)


def company_tokens(name):
    return {
        token
        for token in normalize_company_name(name).split()
        if len(token) >= 2 and token not in {"the", "and", "of", "in", "india", "global", "services", "private", "limited"}
    }


def domain_matches_company(company, domain):
    if not company or not domain:
        return False
    first_label = re.sub(r"[^a-z0-9]+", "", domain.lower().split(".", 1)[0])
    full_domain = re.sub(r"[^a-z0-9]+", "", domain.lower())
    for token in company_tokens(company):
        if token in first_label or token in full_domain:
            return True
    return False


def evidence_matches_company(company, evidence):
    haystack = normalize_company_name(evidence)
    return bool(haystack and any(token in haystack for token in company_tokens(company)))


def _env_or_config(env_name, value):
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    key = str(value or "").strip()
    if not key or key.startswith("YOUR_") or key.startswith("PASTE_"):
        return ""
    return key


def _search_config(config):
    return (config or {}).get("search", {})


def search_company(company, config=None):
    queries = [
        f'"{company}" official website',
        f'"{company}" careers',
        f'"{company}" contact',
    ]
    results = []
    for query in queries:
        results.extend(_brave(query, config))
        results.extend(_serpapi(query, config))
    seen = set()
    deduped = []
    for item in results:
        url = item.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(item)
    return deduped


def _brave(query, config):
    key = _env_or_config("BRAVE_SEARCH_API_KEY", _search_config(config).get("brave_api_key"))
    if not key:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 5, "search_lang": "en"},
            headers={**HEADERS, "X-Subscription-Token": key},
            timeout=10,
        )
        if resp.status_code >= 400:
            return []
        data = resp.json()
    except Exception:
        return []
    return [
        {"url": r.get("url", ""), "title": r.get("title", ""), "snippet": r.get("description", ""), "source_type": "brave_search"}
        for r in data.get("web", {}).get("results", [])
        if r.get("url")
    ]


def _serpapi(query, config):
    key = _env_or_config("SERPAPI_KEY", _search_config(config).get("serpapi_key"))
    if not key:
        return []
    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={"engine": "google", "q": query, "api_key": key, "num": 5},
            timeout=10,
        )
        if resp.status_code >= 400:
            return []
        data = resp.json()
    except Exception:
        return []
    return [
        {"url": r.get("link", ""), "title": r.get("title", ""), "snippet": r.get("snippet", ""), "source_type": "serpapi"}
        for r in data.get("organic_results", [])
        if r.get("link")
    ]


def pick_search_domain(company, results):
    best = None
    best_score = 0
    for result in results:
        domain = extract_domain(result.get("url", ""))
        if not domain or is_rejected_domain(domain):
            continue
        evidence = " ".join([result.get("url", ""), result.get("title", ""), result.get("snippet", "")])
        score = 0
        if domain_matches_company(company, domain):
            score += 70
        if evidence_matches_company(company, evidence):
            score += 20
        if any(word in evidence.lower() for word in ("official", "careers", "contact")):
            score += 10
        if score > best_score:
            best = (domain, result, score)
            best_score = score
    return best


def resolve_company(db_path, company, source_url=None, config=None, prefer_cache=True):
    from database import db

    if is_bad_company_name(company):
        return {"name": company, "status": "skipped_bad_company", "official_domain": "", "domain_confidence": 0}

    normalized = normalize_company_name(company)
    cached = db.get_company(db_path, company)
    if cached and prefer_cache and int(cached.get("domain_confidence") or 0) >= 85:
        return {**cached, "status": "cached"}

    if normalized in COMMON_DOMAINS:
        domain = COMMON_DOMAINS[normalized]
        record = build_company_record(
            company,
            domain,
            100,
            source_type="manual_seed",
            source_url="builtin_seed",
            evidence_text="Built-in seed for common recruiting target.",
        )
        db.upsert_company(db_path, record)
        return {**record, "status": "manual_seed"}

    source_domain = extract_domain(source_url)
    if source_domain and not is_rejected_domain(source_domain):
        confidence = 90 if domain_matches_company(company, source_domain) else 70
        if confidence >= 85:
            record = build_company_record(
                company,
                source_domain,
                confidence,
                source_type="job_url",
                source_url=source_url or "",
                evidence_text=f"Extracted official-looking domain from job URL: {source_url}",
            )
            db.upsert_company(db_path, record)
            return {**record, "status": "job_url"}

    results = search_company(company, config)
    picked = pick_search_domain(company, results)
    if picked:
        domain, result, score = picked
        confidence = min(95, score)
        if confidence >= 85:
            record = build_company_record(
                company,
                domain,
                confidence,
                source_type=result.get("source_type", "search"),
                source_url=result.get("url", ""),
                evidence_text=" ".join([result.get("title", ""), result.get("snippet", "")]).strip(),
            )
            db.upsert_company(db_path, record)
            return {**record, "status": result.get("source_type", "search")}

    if cached:
        return {**cached, "status": "cached_low_confidence"}
    return {"name": company, "normalized_name": normalized, "status": "no_domain", "official_domain": "", "domain_confidence": 0}


def build_company_record(company, domain, confidence, source_type, source_url="", evidence_text="", careers_url="", contact_url=""):
    return {
        "name": company,
        "normalized_name": normalize_company_name(company),
        "official_domain": domain,
        "domain_confidence": int(confidence),
        "careers_url": careers_url or urljoin(f"https://{domain}", "/careers"),
        "contact_url": contact_url or urljoin(f"https://{domain}", "/contact"),
        "ats_platform": "",
        "source_url": source_url,
        "source_type": source_type,
        "evidence_text": evidence_text,
    }


def import_company_seeds(db_path, csv_path):
    from database import db

    rows = []
    with Path(csv_path).open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            company = (row.get("company") or "").strip()
            domain = extract_domain(row.get("official_domain") or "")
            if not company or not domain or is_rejected_domain(domain):
                continue
            record = build_company_record(
                company,
                domain,
                100,
                source_type="manual_seed",
                source_url=row.get("official_domain") or domain,
                careers_url=row.get("careers_url") or "",
                contact_url=row.get("contact_url") or "",
                evidence_text=row.get("notes") or "Manual CSV seed.",
            )
            db.upsert_company(db_path, record)
            rows.append(record)
    return rows
