"""Deterministic applicant-tracking-system detection from public URLs."""
from __future__ import annotations

from urllib.parse import urlparse


ATS_PATTERNS = (
    ("Greenhouse", ("greenhouse.io", "boards.greenhouse", "job-boards.greenhouse")),
    ("Lever", ("lever.co", "jobs.lever")),
    ("Ashby", ("ashbyhq.com", "jobs.ashby")),
    ("Workday", ("myworkdayjobs.com", "workday.com", "wd1.myworkdaysite", "wd3.myworkdaysite", "wd5.myworkdaysite")),
    ("SmartRecruiters", ("smartrecruiters.com", "jobs.smartrecruiters")),
    ("LinkedIn", ("linkedin.com",)),
    ("Internshala", ("internshala.com",)),
    ("Naukri", ("naukri.com",)),
)

CAREER_HINTS = ("career", "careers", "jobs", "job", "join-us", "vacanc", "recruit")


def detect_ats(*urls: str) -> str:
    clean_urls = [str(url or "").strip().lower() for url in urls if str(url or "").strip()]
    for name, patterns in ATS_PATTERNS:
        if any(pattern in url for url in clean_urls for pattern in patterns):
            return name
    for url in clean_urls:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        if host and any(hint in path or hint in host for hint in CAREER_HINTS):
            return "Custom/Company ATS"
    return "Unknown"
