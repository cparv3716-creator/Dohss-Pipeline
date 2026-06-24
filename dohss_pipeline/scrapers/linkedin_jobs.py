"""
scrapers/linkedin_jobs.py
Scrape LinkedIn public job search - no login required.
Uses the guest jobs API endpoint.
"""
import logging
import random
import time

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

GUEST_API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0",
]


def _headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.linkedin.com/",
    }


def _parse_card(card, keyword):
    try:
        title_tag   = card.find("h3") or card.find(class_="base-search-card__title")
        company_tag = card.find("h4") or card.find(class_="base-search-card__subtitle")
        loc_tag     = card.find(class_="job-search-card__location")
        link_tag    = card.find("a", href=True)
        date_tag    = card.find("time")

        title   = title_tag.get_text(strip=True)   if title_tag   else "Unknown"
        company = company_tag.get_text(strip=True)  if company_tag else "Unknown"
        loc     = loc_tag.get_text(strip=True)      if loc_tag     else ""
        url     = link_tag["href"].split("?")[0]    if link_tag    else ""
        posted  = date_tag.get("datetime", "")      if date_tag    else ""

        if not title or title == "Unknown":
            return None
        return {"title": title, "company": company, "location": loc, "url": url,
                "source": "linkedin", "keyword_matched": keyword,
                "salary": "", "description": "", "posted_at": posted}
    except Exception:
        return None


def scrape_keyword(keyword, location="India", max_results=50):
    jobs = []
    seen = set()
    start = 0
    while start < max_results:
        try:
            resp = requests.get(GUEST_API, headers=_headers(), timeout=15,
                params={"keywords": keyword, "location": location,
                        "f_TPR": "r86400", "start": start})
            if resp.status_code == 429:
                time.sleep(60)
                continue
            if resp.status_code != 200:
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.find_all("li")
            if not cards:
                break
            new = 0
            for card in cards:
                j = _parse_card(card, keyword)
                if j and j["url"] not in seen:
                    seen.add(j["url"])
                    jobs.append(j)
                    new += 1
            if new < 10:
                break
            start += 25
            time.sleep(random.uniform(2, 4))
        except requests.RequestException as e:
            logger.error(f"LinkedIn error for '{keyword}': {e}")
            break
    return jobs


def run_all_keywords(config):
    cfg = config.get("linkedin", {})
    keywords = cfg.get("keywords", [])
    location = cfg.get("location", "India")
    max_res  = cfg.get("max_results_per_keyword", 50)

    seen_urls = set()
    all_jobs  = []
    for kw in keywords:
        logger.info(f"LinkedIn: {kw}")
        for j in scrape_keyword(kw, location=location, max_results=max_res):
            if j["url"] and j["url"] not in seen_urls:
                seen_urls.add(j["url"])
                all_jobs.append(j)
        time.sleep(random.uniform(3, 6))
    logger.info(f"LinkedIn total: {len(all_jobs)}")
    return all_jobs
