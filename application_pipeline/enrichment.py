"""Fail-soft job detail fetching and deterministic parsing."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .ats import detect_ats


SECTION_NAMES = {
    "responsibilities": ("responsibilities", "what you will do", "the role", "key duties"),
    "requirements": ("requirements", "required qualifications", "what you bring", "must have", "qualifications"),
    "preferred": ("preferred qualifications", "nice to have", "good to have", "preferred"),
}
SKILL_VOCABULARY = (
    "python", "r", "sql", "excel", "tableau", "power bi", "java", "javascript",
    "typescript", "aws", "azure", "gcp", "spark", "hadoop", "stata", "sas",
    "econometrics", "statistics", "machine learning", "data analysis", "git",
    "docker", "kubernetes", "react", "node.js", "c++", "communication",
)


@dataclass
class EnrichmentResult:
    status: str
    fields: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    soup = BeautifulSoup(html.unescape(str(value)), "html.parser")
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def _json_job_posting(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text() or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = payload if isinstance(payload, list) else payload.get("@graph", [payload]) if isinstance(payload, dict) else []
        for node in nodes:
            if isinstance(node, dict) and str(node.get("@type", "")).lower() == "jobposting":
                return node
    return {}


def _extract_section(text: str, names: tuple[str, ...]) -> str:
    lines = [line.strip(" :-\t") for line in text.splitlines() if line.strip()]
    start = None
    for idx, line in enumerate(lines):
        normalized = line.lower().rstrip(":")
        if any(normalized == name or normalized.startswith(name + ":") for name in names):
            start = idx + 1
            break
    if start is None:
        return ""
    section = []
    all_headers = {name for values in SECTION_NAMES.values() for name in values}
    for line in lines[start:]:
        normalized = line.lower().rstrip(":")
        if normalized in all_headers:
            break
        section.append(line)
    return "\n".join(section).strip()


def _skills(text: str) -> list[str]:
    lower = text.lower()
    found = []
    for skill in SKILL_VOCABULARY:
        if re.search(r"(?<![a-z0-9+#.])" + re.escape(skill) + r"(?![a-z0-9+#.])", lower):
            found.append(skill.title() if skill not in ("r", "sql", "aws", "gcp", "sas") else skill.upper())
    return found


def _experience_range(text: str) -> tuple[Optional[float], Optional[float]]:
    patterns = (
        r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:\+\s*)?years?",
        r"(?:at least|minimum|min\.?|requires?)\s*(\d+(?:\.\d+)?)\+?\s*years?",
        r"(\d+(?:\.\d+)?)\+\s*years?",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text, flags=re.I)
        if match:
            minimum = float(match.group(1))
            maximum = float(match.group(2)) if index == 0 else None
            return minimum, maximum
    return None, None


def _employment_type(text: str, structured: Any = "") -> str:
    haystack = f"{structured} {text}".lower().replace("_", "-")
    for label, needles in (
        ("Internship", ("internship", "intern position")),
        ("Contract", ("contract", "contractor")),
        ("Part-time", ("part-time", "part time")),
        ("Full-time", ("full-time", "full time", "permanent")),
    ):
        if any(needle in haystack for needle in needles):
            return label
    return "Unknown"


def _remote_type(text: str) -> str:
    lower = text.lower()
    if "hybrid" in lower:
        return "Hybrid"
    if "remote" in lower or "work from home" in lower:
        return "Remote"
    if "on-site" in lower or "onsite" in lower or "in office" in lower:
        return "Onsite"
    return "Unknown"


def parse_job_detail(page_html: str, source_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(page_html or "", "html.parser")
    structured = _json_job_posting(soup)
    description = _clean_text(structured.get("description"))
    if not description:
        main = soup.select_one("main, article, [class*='job-description'], [id*='job-description'], [class*='description']")
        description = _clean_text(main or soup.body or soup)
    responsibilities = _extract_section(description, SECTION_NAMES["responsibilities"])
    requirements = _extract_section(description, SECTION_NAMES["requirements"])
    preferred = _extract_section(description, SECTION_NAMES["preferred"])
    required_skills = _skills(requirements or description)
    preferred_skills = [skill for skill in _skills(preferred) if skill not in required_skills]
    exp_min, exp_max = _experience_range(requirements or description)
    education_match = re.search(
        r"(?:bachelor(?:'s)?|master(?:'s)?|ph\.?d\.?|doctorate|graduate|undergraduate)[^\n.;]{0,100}",
        requirements or description, flags=re.I,
    )
    apply_link = ""
    for anchor in soup.select("a[href]"):
        label = anchor.get_text(" ", strip=True).lower()
        if "apply" in label:
            apply_link = urljoin(source_url, anchor.get("href", ""))
            break
    application_url = str(structured.get("url") or apply_link or source_url)
    ats_provider = detect_ats(application_url, source_url)
    location = structured.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else {}
    if isinstance(location, dict):
        address = location.get("address", location)
        if isinstance(address, dict):
            location = ", ".join(str(address.get(k) or "") for k in ("addressLocality", "addressRegion", "addressCountry") if address.get(k))
    return {
        "full_description": description,
        "responsibilities": responsibilities,
        "requirements": requirements,
        "required_skills": required_skills,
        "preferred_skills": preferred_skills,
        "required_experience_min": exp_min,
        "required_experience_max": exp_max,
        "education_requirement": education_match.group(0).strip() if education_match else "",
        "employment_type": _employment_type(description, structured.get("employmentType")),
        "remote_type": _remote_type(description + " " + str(structured.get("jobLocationType") or "")),
        "location": str(location or ""),
        "application_url": application_url,
        "ats_provider": ats_provider,
        "company_career_url": source_url if ats_provider == "Custom/Company ATS" else "",
    }


class JobDetailEnricher:
    def __init__(self, timeout: int = 15, fetcher: Optional[Callable[[str], str]] = None):
        self.timeout = timeout
        self.fetcher = fetcher or self._fetch

    def _fetch(self, url: str) -> str:
        response = requests.get(
            url,
            timeout=self.timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DoHSSJobPreparation/1.0)"},
        )
        response.raise_for_status()
        return response.text

    def enrich(self, job: Mapping[str, Any]) -> EnrichmentResult:
        source_url = str(job.get("application_url") or job.get("url") or "").strip()
        if not source_url:
            return EnrichmentResult("FAILED", error="job has no URL")
        try:
            fields = parse_job_detail(self.fetcher(source_url), source_url)
            if not fields.get("full_description"):
                return EnrichmentResult("PARTIAL", fields, "no job description found")
            return EnrichmentResult("ENRICHED", fields)
        except Exception as exc:  # Enrichment must never stop discovery/preparation batches.
            return EnrichmentResult(
                "FAILED",
                fields={"application_url": source_url, "ats_provider": detect_ats(source_url)},
                error=f"{type(exc).__name__}: {exc}",
            )


def enriched_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
