"""EasyLeadz API integration for known HR LinkedIn profiles.

This module does not scrape LinkedIn. It only submits LinkedIn profile URLs that
DoHSS already knows about to an explicitly configured EasyLeadz account.
Returned phone/email data is handled by the webhook service and is intentionally
kept outside the public pipeline database.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Iterable

import requests

API_URL = "https://app.easyleadz.com/api/prod/"
LINKEDIN_RE = re.compile(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[^/?#]+/?(?:[?#].*)?$", re.I)


class EasyLeadzError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("EASYLEADZ_API_KEY", "").strip()
    if not key:
        raise EasyLeadzError("EASYLEADZ_API_KEY is not configured")
    return key


def normalize_linkedin_url(url: str) -> str:
    url = (url or "").strip()
    if not LINKEDIN_RE.match(url):
        raise EasyLeadzError(f"Invalid public LinkedIn profile URL: {url!r}")
    return url.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def submit_contact(linkedin_url: str, callback_url: str, timeout: int = 30) -> dict:
    """Submit one public LinkedIn profile URL for EasyLeadz enrichment."""
    url = normalize_linkedin_url(linkedin_url)
    callback_url = (callback_url or "").strip()
    if not callback_url.startswith("https://"):
        raise EasyLeadzError("callback_url must be a public HTTPS URL")

    body = {"data": {"url": url, "callbackUrl": callback_url}}
    response = requests.get(
        API_URL,
        headers={
            "Enapi-Key": _api_key(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(body),
        timeout=timeout,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise EasyLeadzError(f"EasyLeadz returned non-JSON HTTP {response.status_code}") from exc

    if response.status_code >= 400 or str(payload.get("status")) != "1":
        message = payload.get("message") or f"HTTP {response.status_code}"
        raise EasyLeadzError(str(message))
    return payload


def submit_batch(
    linkedin_urls: Iterable[str],
    callback_url: str,
    *,
    max_contacts: int = 100,
    calls_per_second: float = 5.0,
) -> list[dict]:
    """Submit a bounded, rate-limited batch.

    The vendor documents a 10 calls/second ceiling. DoHSS defaults to 5/second
    to leave headroom for retries and other account activity.
    """
    if calls_per_second <= 0 or calls_per_second > 10:
        raise ValueError("calls_per_second must be > 0 and <= 10")
    delay = 1.0 / calls_per_second
    results = []
    seen = set()
    for raw_url in linkedin_urls:
        if len(results) >= max_contacts:
            break
        url = normalize_linkedin_url(raw_url)
        if url in seen:
            continue
        seen.add(url)
        results.append(submit_contact(url, callback_url))
        time.sleep(delay)
    return results
