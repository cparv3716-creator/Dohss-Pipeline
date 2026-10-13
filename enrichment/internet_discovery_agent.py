"""
Bounded internet HR/contact discovery agent.

The agent uses approved search APIs/RSS feeds and public company pages only.
It does not scrape Google/Bing result pages, bypass logins, scrape hidden
LinkedIn data, collect phone numbers, or brute-force personal email patterns.
"""
import logging
import os
import random
import re
import time
import urllib.robotparser
from html import unescape
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from enrichment.company_resolver import (
    is_bad_company_name,
    is_rejected_domain,
    resolve_company,
)
from enrichment.contact_classifier import classify_contact
from enrichment.contact_finder import extract_domain

logger = logging.getLogger(__name__)

DEFAULT_MAX_COMPANIES = 50
DEFAULT_MAX_PAGES = 10
DEFAULT_TIMEOUT = 10
DEFAULT_MAX_RESULTS = 5

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
    "feedspot.com",
    "www.feedspot.com",
}

DIRECT_SKIP_DOMAINS = {"linkedin.com", "www.linkedin.com"}

OFFICIAL_PATHS = (
    "",
    "/careers",
    "/jobs",
    "/contact",
    "/about",
    "/team",
    "/people",
    "/campus",
    "/early-careers",
    "/graduates",
)

ROLE_EMAILS = (
    "careers",
    "hr",
    "jobs",
    "recruitment",
    "talent",
    "campus",
    "universityrelations",
)

GENERIC_EMAILS = ("info", "contact", "hello")

HR_TERMS = (
    "talent acquisition",
    "campus recruiter",
    "university relations",
    "campus hiring",
    "early careers",
    "graduate recruitment",
    "recruiter",
    "recruitment",
    "human resources",
    "hr",
    "people",
    "hiring",
)

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%./-]+", re.IGNORECASE)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class InternetDiscoveryAgent:
    def __init__(self, config=None):
        self.config = config or {}
        enrichment = self.config.get("enrichment", {})
        self.max_pages = _bounded_int(enrichment.get("max_pages_per_company"), DEFAULT_MAX_PAGES, 1, 10)
        self.timeout = _bounded_int(enrichment.get("request_timeout_seconds"), DEFAULT_TIMEOUT, 1, 10)
        self.max_results = _bounded_int(enrichment.get("max_search_results_per_query"), DEFAULT_MAX_RESULTS, 1, 10)
        self.query_cache = {}
        self.feed_cache = None
        self.visited_urls = set()
        self.robots_cache = {}

    def discover_for_job(self, job):
        company = (job.get("company") or "").strip()
        source_url = job.get("url") or ""
        if not company:
            return {"company": company, "queries": 0, "urls": 0, "contacts": [], "status": "missing company"}
        if is_bad_company_name(company):
            return {"company": company, "queries": 0, "urls": 0, "contacts": [], "status": "skipped, generic feed/source name"}

        db_path = self.config.get("database", {}).get("path", "data/pipeline.db")
        resolved = resolve_company(db_path, company, source_url=source_url, config=self.config)
        domain = resolved.get("official_domain") or ""
        domain_confidence = int(resolved.get("domain_confidence") or 0)

        queries = generate_queries(company, domain)
        search_results = []
        if self.has_search_provider():
            for query in queries:
                search_results.extend(self.search(query, company))

        if not domain or domain_confidence < 85:
            contacts = []
            for result in search_results:
                contacts.extend(
                    self._contacts_from_search_result(
                        result.get("url", ""),
                        " ".join([result.get("title", ""), result.get("snippet", "")]),
                        company,
                        "",
                    )
                )
            contacts = dedupe_contacts(contacts)
            if contacts:
                return {
                    "company": company,
                    "queries": len(queries),
                    "urls": len(search_results),
                    "contacts": contacts,
                    "public_contacts": len([c for c in contacts if c.get("source_type") != "guessed_role_email"]),
                    "guesses": 0,
                    "domain": domain,
                    "domain_confidence": domain_confidence,
                    "status": "ok",
                }
            return {
                "company": company,
                "queries": len(queries),
                "urls": len(search_results),
                "contacts": [],
                "public_contacts": 0,
                "guesses": 0,
                "domain": domain,
                "domain_confidence": domain_confidence,
                "status": "no official domain, no guesses generated",
            }
        domain_confident = domain_confidence >= 85 and not is_rejected_domain(domain)

        urls = self._candidate_urls(domain, source_url, search_results)
        contacts = []
        for result in search_results:
            contacts.extend(
                self._contacts_from_search_result(
                    result.get("url", ""),
                    " ".join([result.get("title", ""), result.get("snippet", "")]),
                    company,
                    domain,
                )
            )
        for url in urls[: self.max_pages]:
            domain_for_url = extract_domain(url)
            if is_direct_skip_domain(domain_for_url):
                contacts.extend(self._contacts_from_search_result(url, "", company, domain))
                continue
            contacts.extend(self.parse_public_page(url, company, domain))

        if domain_confident:
            contacts.extend(self.guessed_role_emails(company, domain))
        contacts = dedupe_contacts(contacts)
        public_contacts = len([c for c in contacts if c.get("source_type") != "guessed_role_email"])
        guesses = len([c for c in contacts if c.get("source_type") == "guessed_role_email"])
        return {
            "company": company,
            "queries": len(queries),
            "urls": len(urls),
            "contacts": contacts,
            "public_contacts": public_contacts,
            "guesses": guesses,
            "domain": domain,
            "domain_confidence": domain_confidence,
            "status": "ok",
        }

    def debug_for_job(self, job):
        company = (job.get("company") or "").strip()
        source_url = job.get("url") or ""
        if not company or is_bad_company_name(company):
            return {
                "company": company,
                "queries": 0,
                "urls": 0,
                "contacts": [],
                "domain": "",
                "domain_confidence": 0,
                "status": "skipped",
            }

        db_path = self.config.get("database", {}).get("path", "data/pipeline.db")
        resolved = resolve_company(db_path, company, source_url=source_url, config=self.config)
        domain = resolved.get("official_domain") or ""
        domain_confidence = int(resolved.get("domain_confidence") or 0)
        queries = generate_queries(company, domain)
        search_results = []
        if self.has_search_provider():
            for query in queries:
                search_results.extend(self.search(query, company))

        contacts = []
        for result in search_results:
            contacts.extend(
                self._contacts_from_search_result(
                    result.get("url", ""),
                    " ".join([result.get("title", ""), result.get("snippet", "")]),
                    company,
                    domain,
                )
            )

        urls = self._candidate_urls(domain, source_url, search_results) if domain and domain_confidence >= 85 else []
        for url in urls[: self.max_pages]:
            domain_for_url = extract_domain(url)
            if is_direct_skip_domain(domain_for_url):
                contacts.extend(self._contacts_from_search_result(url, "", company, domain))
                continue
            contacts.extend(self.parse_public_page(url, company, domain))

        return {
            "company": company,
            "queries": len(queries),
            "urls": len(urls),
            "contacts": contacts,
            "domain": domain,
            "domain_confidence": domain_confidence,
            "status": "ok",
        }

    def has_search_provider(self):
        return bool(_env_or_config("BRAVE_SEARCH_API_KEY", self.config.get("search", {}).get("brave_api_key"))) or bool(
            _env_or_config("SERPAPI_KEY", self.config.get("search", {}).get("serpapi_key"))
        ) or bool(self.config.get("google_alerts", {}).get("feeds"))

    def discover_domain(self, company, source_url=None):
        source_domain = extract_domain(source_url)
        if source_domain and is_confident_official_domain(company, source_domain, [{"url": source_url, "title": "", "snippet": ""}]):
            return source_domain
        if not self.has_search_provider():
            return None
        for query in (f'"{company}" official careers', f'"{company}" contact', f'"{company}" campus hiring'):
            for result in self.search(query, company):
                domain = extract_domain(result["url"])
                if domain and is_confident_official_domain(company, domain, [result]):
                    return domain
        return None

    def search(self, query, company):
        if query in self.query_cache:
            return self.query_cache[query]

        results = []
        results.extend(self._brave_search(query))
        results.extend(self._serpapi_search(query))
        results.extend(self._google_alerts_fallback(query, company))

        seen = set()
        deduped = []
        for result in results:
            url = result.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(result)
            if len(deduped) >= self.max_results:
                break
        self.query_cache[query] = deduped
        return deduped

    def _brave_search(self, query):
        key = _env_or_config("BRAVE_SEARCH_API_KEY", self.config.get("search", {}).get("brave_api_key"))
        if not key:
            return []
        try:
            resp = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": self.max_results, "search_lang": "en"},
                headers={**HEADERS, "X-Subscription-Token": key},
                timeout=self.timeout,
            )
            if resp.status_code >= 400:
                logger.info("Brave Search skipped query due to HTTP %s", resp.status_code)
                return []
            data = resp.json()
        except Exception as exc:
            logger.info("Brave Search skipped query: %s", exc)
            return []
        return [
            {
                "url": item.get("url") or "",
                "title": item.get("title") or "",
                "snippet": item.get("description") or "",
                "source_type": "brave_search",
            }
            for item in data.get("web", {}).get("results", [])
            if item.get("url")
        ]

    def _serpapi_search(self, query):
        key = _env_or_config("SERPAPI_KEY", self.config.get("search", {}).get("serpapi_key"))
        if not key:
            return []
        try:
            resp = requests.get(
                "https://serpapi.com/search.json",
                params={"engine": "google", "q": query, "api_key": key, "num": self.max_results},
                timeout=self.timeout,
            )
            if resp.status_code >= 400:
                logger.info("SerpAPI skipped query due to HTTP %s", resp.status_code)
                return []
            data = resp.json()
        except Exception as exc:
            logger.info("SerpAPI skipped query: %s", exc)
            return []
        return [
            {
                "url": item.get("link") or "",
                "title": item.get("title") or "",
                "snippet": item.get("snippet") or "",
                "source_type": "serpapi",
            }
            for item in data.get("organic_results", [])
            if item.get("link")
        ]

    def _google_alerts_fallback(self, query, company):
        feeds = self.config.get("google_alerts", {}).get("feeds", [])
        if not feeds:
            return []
        if self.feed_cache is None:
            self.feed_cache = []
            for feed in feeds:
                url = feed.get("url", "")
                if not url or "PASTE_" in url:
                    continue
                try:
                    parsed = feedparser.parse(url)
                except Exception:
                    continue
                for entry in parsed.entries:
                    self.feed_cache.append(
                        {
                            "url": entry.get("link", ""),
                            "title": entry.get("title", ""),
                            "snippet": BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True),
                            "source_type": "google_alert_rss",
                        }
                    )
        terms = {t.lower() for t in re.findall(r"[A-Za-z0-9]+", query) if len(t) > 2}
        company_terms = {t.lower() for t in re.findall(r"[A-Za-z0-9]+", company) if len(t) > 2}
        results = []
        for entry in self.feed_cache:
            text = " ".join([entry.get("title", ""), entry.get("snippet", "")])
            lower = text.lower()
            if company_terms and not any(term in lower for term in company_terms):
                continue
            if terms and not any(term in lower for term in terms):
                continue
            results.append(entry)
        return results

    def _candidate_urls(self, domain, source_url, search_results):
        candidates = []
        source_domain = extract_domain(source_url)
        if source_url and source_domain and not is_job_board_domain(source_domain):
            candidates.append(source_url)
        for path in OFFICIAL_PATHS:
            candidates.append(urljoin(f"https://{domain}", path))
        for result in search_results:
            url = result.get("url", "")
            result_domain = extract_domain(url)
            if is_direct_skip_domain(result_domain):
                candidates.append(url)
            elif result_domain and not is_job_board_domain(result_domain):
                candidates.append(url)

        seen = set()
        deduped = []
        for url in candidates:
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(url)
        return deduped

    def _domain_from_results(self, company, search_results):
        for result in search_results:
            domain = extract_domain(result.get("url", ""))
            if domain and is_confident_official_domain(company, domain, [result]):
                return domain
        return None

    def parse_public_page(self, url, company, domain):
        if url in self.visited_urls:
            return []
        self.visited_urls.add(url)

        parsed_domain = extract_domain(url)
        if is_direct_skip_domain(parsed_domain):
            return []
        if parsed_domain and is_job_board_domain(parsed_domain):
            return []
        if not self._allowed_by_robots(url):
            return []

        time.sleep(random.uniform(1, 2))
        try:
            resp = requests.get(url, headers=HEADERS, timeout=self.timeout)
            if resp.status_code >= 400:
                return []
            content_type = resp.headers.get("content-type", "")
            if content_type and "text/html" not in content_type and "xml" not in content_type:
                return []
        except requests.RequestException:
            return []

        text, mailtos, linkedin_urls = visible_text_mailtos_linkedin(resp.text)
        contacts = []
        for email in sorted(set(mailtos + EMAIL_RE.findall(text))):
            email = clean_email(email)
            if not email:
                continue
            evidence = evidence_snippet(text, email)
            contacts.append(
                build_contact(
                    company=company,
                    domain=domain,
                    source_url=url,
                    source_type="official_public_page",
                    email=email,
                    evidence_text=evidence,
                )
            )

        for linkedin_url in linkedin_urls:
            evidence = evidence_snippet(text, linkedin_url) or evidence_snippet(text, "linkedin")
            if not has_hr_context(evidence):
                continue
            name, role = guess_name_role(evidence, company)
            contacts.append(
                build_contact(
                    company=company,
                    domain=domain,
                    source_url=url,
                    source_type="linkedin_public_profile",
                    linkedin_url=normalize_linkedin_url(linkedin_url),
                    contact_name=name,
                    role_title=role,
                    evidence_text=evidence,
                )
            )
        return contacts

    def _contacts_from_search_result(self, url, snippet, company, domain):
        text = " ".join([url or "", snippet or ""])
        contacts = []
        linkedin_url = normalize_linkedin_url(url)
        if linkedin_url and has_hr_context(text):
            name, role = guess_name_role(text, company)
            contacts.append(
                build_contact(
                    company=company,
                    domain=domain,
                    source_url=url,
                    source_type="linkedin_public_profile",
                    linkedin_url=linkedin_url,
                    contact_name=name,
                    role_title=role,
                    evidence_text=snippet,
                )
            )
        for email in EMAIL_RE.findall(text):
            email = clean_email(email)
            if not email:
                continue
            name, role = guess_name_role(text, company)
            contacts.append(
                build_contact(
                    company=company,
                    domain=domain,
                    source_url=url,
                    source_type="search_result_snippet",
                    email=email,
                    contact_name=name,
                    role_title=role,
                    evidence_text=snippet,
                )
            )
        return contacts

    def guessed_role_emails(self, company, domain):
        return [
            build_contact(
                company=company,
                domain=domain,
                source_url=f"https://{domain}",
                source_type="guessed_role_email",
                email=f"{prefix}@{domain}",
                evidence_text="Generated role mailbox for manual review only.",
            )
            for prefix in ROLE_EMAILS
        ]

    def _allowed_by_robots(self, url):
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self.robots_cache:
            robot = urllib.robotparser.RobotFileParser()
            robot.set_url(urljoin(base, "/robots.txt"))
            try:
                robot.read()
            except Exception:
                self.robots_cache[base] = None
            else:
                self.robots_cache[base] = robot
        robot = self.robots_cache.get(base)
        if robot is None:
            return True
        try:
            return robot.can_fetch(HEADERS["User-Agent"], url)
        except Exception:
            return True


def generate_queries(company, domain=None):
    base_templates = [
        '"{company}" "Talent Acquisition"',
        '"{company}" "Talent Acquisition" email',
        '"{company}" "Talent Acquisition" LinkedIn',
        '"{company}" "Campus Recruiter"',
        '"{company}" "Campus Recruiter" LinkedIn',
        '"{company}" "University Relations"',
        '"{company}" "Campus Hiring"',
        '"{company}" "Early Careers"',
        '"{company}" "Graduate Recruitment"',
        '"{company}" "HR email"',
        '"{company}" "recruitment email"',
        '"{company}" "careers contact"',
        '"{company}" "people team"',
        '"{company}" "hiring team"',
        '"{company}" "recruiter email"',
        '"{company}" "campus recruitment"',
        '"{company}" "university recruiting"',
        '"{company}" "early talent"',
        '"{company}" "graduate careers"',
        '"{company}" "jobs contact"',
        'site:linkedin.com/in "{company}" "Talent Acquisition"',
        'site:linkedin.com/in "{company}" "Campus Recruiter"',
        'site:linkedin.com/in "{company}" "Recruiter"',
        'site:linkedin.com/in "{company}" "University Relations"',
        'site:linkedin.com/in "{company}" "Early Careers"',
        'site:linkedin.com/in "{company}" "Graduate Recruitment"',
        'site:linkedin.com/in "{company}" "Campus Hiring"',
        'site:linkedin.com/in "{company}" "Talent Partner"',
        'site:linkedin.com/in "{company}" "HR"',
        'site:linkedin.com/in "{company}" "People"',
    ]
    queries = [template.format(company=company) for template in base_templates]
    if domain:
        queries.extend(
            [
                f"site:{domain} careers",
                f"site:{domain} contact",
                f"site:{domain} campus",
                f"site:{domain} university recruiting",
                f"site:{domain} early careers",
                f"site:{domain} graduate recruitment",
                f"site:{domain} talent acquisition",
                f"site:{domain} recruitment email",
                f"site:{domain} careers contact",
                f"site:{domain} campus hiring",
            ]
        )
    return queries[:50]


def discover_for_jobs(jobs, config=None):
    agent = InternetDiscoveryAgent(config)
    limited_jobs = list(jobs)[:DEFAULT_MAX_COMPANIES]
    results = []
    for job in limited_jobs:
        results.append(agent.discover_for_job(job))
    return results


def clean_email(email):
    return (email or "").strip().strip(".,;:()[]<>").lower()


def normalize_linkedin_url(url):
    if not url:
        return ""
    parsed = urlparse(url)
    if "linkedin.com" not in parsed.netloc.lower() or not parsed.path.lower().startswith("/in/"):
        return ""
    return f"https://www.linkedin.com{parsed.path.rstrip('/')}"


def is_job_board_domain(domain):
    if not domain:
        return False
    lower = domain.lower()
    return lower in JOB_BOARD_DOMAINS or any(
        lower.endswith(f".{base}") for base in JOB_BOARD_DOMAINS
    )


def is_direct_skip_domain(domain):
    if not domain:
        return False
    lower = domain.lower()
    return lower in DIRECT_SKIP_DOMAINS or any(
        lower.endswith(f".{base}") for base in DIRECT_SKIP_DOMAINS
    )


def normalize_company(company):
    return re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).strip()


def company_tokens(company):
    return {
        token
        for token in normalize_company(company).split()
        if len(token) >= 2 and token not in {"the", "and", "in", "of", "india", "global", "services", "private", "limited"}
    }


def domain_matches_company(company, domain):
    clean_domain = (domain or "").lower()
    compact_domain = re.sub(r"[^a-z0-9]", "", clean_domain.split(".", 1)[0])
    tokens = company_tokens(company)
    if not tokens:
        return False
    return any(token in clean_domain or token in compact_domain for token in tokens)


def result_has_company_evidence(company, results):
    tokens = company_tokens(company)
    if not tokens:
        return False
    for result in results or []:
        haystack = " ".join(
            str(result.get(key, "") or "").lower()
            for key in ("url", "title", "snippet")
        )
        if any(token in haystack for token in tokens):
            return True
    return False


def is_confident_official_domain(company, domain, results=None):
    if not domain or is_job_board_domain(domain):
        return False
    lower_domain = domain.lower()
    if any(part in lower_domain for part in ("feed", "alert", "search", "google", "linkedin", "naukri", "internshala")):
        return False
    if company and domain_matches_company(company, domain):
        return True
    return bool(company and result_has_company_evidence(company, results or []))


def is_role_email(email):
    prefix = clean_email(email).split("@", 1)[0].lower() if "@" in clean_email(email) else ""
    return prefix in ROLE_EMAILS


def is_generic_email(email):
    prefix = clean_email(email).split("@", 1)[0].lower() if "@" in clean_email(email) else ""
    return prefix in GENERIC_EMAILS


def has_hr_context(text):
    lower = (text or "").lower()
    return any(term in lower for term in HR_TERMS)


def visible_text_mailtos_linkedin(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    mailtos = []
    linkedin_urls = set()
    for a in soup.find_all("a", href=True):
        href = unescape(a["href"]).strip()
        if href.lower().startswith("mailto:"):
            mailtos.append(href[7:].split("?", 1)[0])
        linkedin = normalize_linkedin_url(href)
        if linkedin:
            linkedin_urls.add(linkedin)
    text = soup.get_text(" ", strip=True)
    for linkedin in LINKEDIN_RE.findall(text):
        normalized = normalize_linkedin_url(linkedin)
        if normalized:
            linkedin_urls.add(normalized)
    return text, mailtos, sorted(linkedin_urls)


def evidence_snippet(text, needle, radius=180):
    if not text or not needle:
        return ""
    lower = text.lower()
    idx = lower.find(str(needle).lower())
    if idx < 0:
        return ""
    start = max(0, idx - radius)
    end = min(len(text), idx + len(str(needle)) + radius)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def guess_name_role(text, company):
    clean = re.sub(r"\s+", " ", text or "").strip()
    role = ""
    for term in HR_TERMS:
        idx = clean.lower().find(term)
        if idx >= 0:
            role = clean[max(0, idx - 60): min(len(clean), idx + 90)].strip(" -|,")
            break
    name = ""
    match = re.search(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b.{0,100}?"
        r"(Talent Acquisition|Campus Recruiter|Recruiter|University Relations|Human Resources|HR|People)",
        clean,
    )
    if match:
        candidate = match.group(1).strip()
        if company.lower() not in candidate.lower():
            name = candidate
    return name, role


def build_contact(
    *,
    company,
    domain,
    source_url,
    source_type,
    email="",
    linkedin_url="",
    contact_name="",
    role_title="",
    evidence_text="",
):
    contact = {
        "contact_name": contact_name or "",
        "role_title": role_title or "",
        "email": clean_email(email),
        "linkedin_url": normalize_linkedin_url(linkedin_url) or "",
        "company_name": company or "",
        "company_domain": domain or "",
        "source_url": source_url or "",
        "source_type": source_type or "",
        "evidence_text": evidence_text or "",
    }
    if contact["email"]:
        classified = classify_contact(contact)
        contact["email"] = classified["email"]
        contact["email_category"] = classified["email_category"]
        contact["confidence_score"] = classified["confidence_score"]
        contact["role_title"] = classified["role_title"] or contact["role_title"]
        contact["contact_name"] = classified["contact_name"] or contact["contact_name"]
        contact["source_type"] = classified.get("source_type") or contact["source_type"]
        contact["reject_reason"] = classified.get("reject_reason", "")
        contact["allowed_for_auto_outreach"] = classified["allowed_for_auto_outreach"]
    else:
        contact["email_category"] = ""
        contact["confidence_score"] = score_contact(contact)
        contact["reject_reason"] = ""
        contact["allowed_for_auto_outreach"] = int(outreach_action(contact) == "auto_send_allowed")
    return contact


def score_contact(contact):
    email = clean_email(contact.get("email"))
    source_type = contact.get("source_type") or ""
    domain = contact.get("company_domain") or ""
    evidence = contact.get("evidence_text") or contact.get("role_title") or ""
    has_public_evidence = bool(evidence or contact.get("source_url"))
    named_hr = bool(contact.get("contact_name") and has_hr_context(evidence))

    if named_hr and has_public_evidence:
        return 95
    if email and domain and email.endswith(f"@{domain}") and is_role_email(email) and source_type != "guessed_role_email":
        return 90
    if email and domain and email.endswith(f"@{domain}") and is_generic_email(email) and source_type != "guessed_role_email":
        if extract_domain(contact.get("source_url")) == domain:
            return 80
        return 55
    if contact.get("linkedin_url") and has_hr_context(evidence):
        return 70
    if source_type == "guessed_role_email":
        return 40
    if email and is_generic_email(email):
        return 55
    return 0


def outreach_action(contact):
    source_type = contact.get("source_type") or ""
    score = int(contact.get("confidence_score") or 0)
    do_not_contact = "do not contact" in (contact.get("evidence_text") or "").lower()
    official_public = source_type in {
        "official_public_page",
        "mailto_public_page",
        "search_result_snippet",
        "brave_search",
        "serpapi",
        "google_alert_rss",
        "official_hiring_email",
    }
    if score >= 90 and official_public and source_type != "guessed_role_email" and not do_not_contact:
        return "auto_send_allowed"
    if 70 <= score <= 89:
        return "draft_only"
    return "manual_review"


def dedupe_contacts(contacts):
    deduped = {}
    for contact in contacts:
        email = clean_email(contact.get("email"))
        linkedin = contact.get("linkedin_url") or ""
        if not email and not linkedin:
            continue
        if int(contact.get("confidence_score") or 0) <= 0:
            continue
        key = (email or linkedin, contact.get("company_domain") or "", contact.get("source_url") or "")
        existing = deduped.get(key)
        if not existing or int(contact.get("confidence_score") or 0) > int(existing.get("confidence_score") or 0):
            deduped[key] = contact
    return sorted(deduped.values(), key=lambda c: int(c.get("confidence_score") or 0), reverse=True)


def _env_or_config(env_name, config_value):
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    key = str(config_value or "").strip()
    if not key or key.startswith("YOUR_") or key.startswith("PASTE_"):
        return ""
    return key


def _bounded_int(value, default, low, high):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))
