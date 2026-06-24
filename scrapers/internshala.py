"""
scrapers/internshala.py
Scrape Internshala for HSS internships and fresher jobs. No login needed.
"""
import logging
import random
import time

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

INTERNSHIP_URL = "https://internshala.com/internships/keywords-{keyword}/"
JOBS_URL       = "https://internshala.com/jobs/keywords-{keyword}/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://internshala.com/",
}


def _slug(keyword):
    return "-".join(keyword.lower().split())


def _parse_cards(soup, keyword, source_label):
    results = []
    cards = (soup.select("div.individual_internship") or
             soup.select("div.internship-list-container .internship") or
             soup.select("div.container-fluid .internship_meta"))
    for card in cards:
        t = (card.select_one("h3.job-internship-name a") or
             card.select_one("a.job-title") or card.select_one("h3 a"))
        c = (card.select_one("p.company-name a") or card.select_one(".company-name"))
        l = (card.select_one("p.location-name") or card.select_one(".location"))
        s = card.select_one("span.stipend") or card.select_one(".stipend")
        lk= (card.select_one("h3.job-internship-name a") or
             card.select_one("a[href*='/internship/detail']") or
             card.select_one("a[href*='/job/detail']"))
        title = t.get_text(strip=True) if t else None
        if not title:
            continue
        href = lk.get("href","") if lk else ""
        if href and not href.startswith("http"):
            href = "https://internshala.com" + href
        results.append({"title": title,
                        "company": c.get_text(strip=True) if c else "Unknown",
                        "location": l.get_text(strip=True) if l else "",
                        "description": "", "url": href,
                        "source": source_label, "keyword_matched": keyword,
                        "salary": s.get_text(strip=True) if s else "",
                        "posted_at": ""})
    return results


def scrape_internships(keyword, pages=2):
    jobs = []
    slug = _slug(keyword)
    for page in range(1, pages + 1):
        url = INTERNSHIP_URL.format(keyword=slug)
        if page > 1:
            url = url.rstrip("/") + f"/page-{page}/"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                break
            cards = _parse_cards(BeautifulSoup(resp.text,"html.parser"), keyword, "internshala_internship")
            if not cards:
                break
            jobs.extend(cards)
            time.sleep(random.uniform(1.5, 3))
        except Exception as e:
            logger.warning(f"Internshala internship error '{keyword}': {e}")
            break
    return jobs


def scrape_jobs(keyword, pages=2):
    jobs = []
    slug = _slug(keyword)
    for page in range(1, pages + 1):
        url = JOBS_URL.format(keyword=slug)
        if page > 1:
            url = url.rstrip("/") + f"/page-{page}/"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                break
            cards = _parse_cards(BeautifulSoup(resp.text,"html.parser"), keyword, "internshala_job")
            if not cards:
                break
            jobs.extend(cards)
            time.sleep(random.uniform(1.5, 3))
        except Exception as e:
            logger.warning(f"Internshala job error '{keyword}': {e}")
            break
    return jobs


def run_all_keywords(config):
    cfg      = config.get("internshala", {})
    keywords = cfg.get("keywords", ["research analyst","policy analyst","economics","social science","data analyst"])
    pages    = cfg.get("pages_per_keyword", 2)
    seen = set()
    all_jobs = []
    for kw in keywords:
        logger.info(f"Internshala: {kw}")
        for j in scrape_internships(kw, pages=pages) + scrape_jobs(kw, pages=pages):
            if j["url"] and j["url"] not in seen:
                seen.add(j["url"])
                all_jobs.append(j)
        time.sleep(random.uniform(2, 4))
    logger.info(f"Internshala total: {len(all_jobs)}")
    return all_jobs
