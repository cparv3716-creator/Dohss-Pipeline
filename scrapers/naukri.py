"""
scrapers/naukri.py
Scrape Naukri.com via their internal search API + HTML fallback.
No login required.
"""
import logging
import random
import time

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

API_URL     = "https://www.naukri.com/jobapi/v3/search"
SEARCH_PAGE = "https://www.naukri.com/{slug}-jobs-in-india"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.naukri.com/",
    "appid": "109",
    "systemid": "109",
}


def _keyword_to_slug(keyword):
    return "-".join(keyword.lower().split())


def _scrape_api(keyword, location="india", page=1, count=20):
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=15,
            params={"noOfResults": count, "urlType": "search_by_key_loc",
                    "searchType": "adv", "keyword": keyword,
                    "location": location, "pageNo": page,
                    "myNaukri": "false", "quickApply": "false"})
        if resp.status_code == 200:
            return resp.json().get("jobDetails", [])
        logger.warning(f"Naukri API {resp.status_code} for '{keyword}'")
        return []
    except Exception as e:
        logger.warning(f"Naukri API error '{keyword}': {e}")
        return []


def _scrape_html(keyword):
    slug = _keyword_to_slug(keyword)
    url  = SEARCH_PAGE.format(slug=slug)
    hdrs = {k: v for k, v in HEADERS.items() if k not in ("appid","systemid")}
    hdrs["Accept"] = "text/html"
    jobs = []
    try:
        resp = requests.get(url, headers=hdrs, timeout=15)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("article.jobTuple") or soup.select("div.jobTupleHeader")
        for card in cards:
            t = card.select_one("a.title")
            c = card.select_one("a.subTitle") or card.select_one(".companyInfo a")
            l = card.select_one("li.location span") or card.select_one(".location")
            if not t:
                continue
            href = t.get("href", "")
            jobs.append({"title": t.get_text(strip=True),
                         "company": c.get_text(strip=True) if c else "Unknown",
                         "location": l.get_text(strip=True) if l else "",
                         "url": href})
    except Exception as e:
        logger.warning(f"Naukri HTML error '{keyword}': {e}")
    return jobs


def scrape_keyword(keyword, location="india", max_pages=3):
    jobs = []
    seen = set()
    for page in range(1, max_pages + 1):
        raw = _scrape_api(keyword, location=location, page=page)
        if raw:
            for item in raw:
                url = item.get("jdURL") or item.get("jobId", "")
                if url and not url.startswith("http"):
                    url = "https://www.naukri.com" + url
                if url in seen:
                    continue
                seen.add(url)
                locs = item.get("placeholders", [])
                loc_text = next((p.get("label","") for p in locs if p.get("type")=="location"), "")
                sal_text = next((p.get("label","") for p in locs if p.get("type")=="salary"), "")
                jobs.append({"title": item.get("title","Unknown"),
                             "company": item.get("companyName","Unknown"),
                             "location": loc_text, "description": item.get("jobDescription","")[:500],
                             "url": url, "source": "naukri", "keyword_matched": keyword,
                             "salary": sal_text, "posted_at": str(item.get("footerPlaceholderLabel",""))})
            if len(raw) < 20:
                break
        else:
            if page == 1:
                for j in _scrape_html(keyword):
                    if j["url"] not in seen:
                        seen.add(j["url"])
                        j.update({"description":"","source":"naukri","keyword_matched":keyword,"salary":"","posted_at":""})
                        jobs.append(j)
            break
        time.sleep(random.uniform(1.5, 3))
    return jobs


def run_all_keywords(config):
    cfg      = config.get("naukri", {})
    keywords = cfg.get("keywords", config.get("linkedin", {}).get("keywords", []))
    location = cfg.get("location", "india")
    max_pages= cfg.get("max_pages", 3)
    seen = set()
    all_jobs = []
    for kw in keywords:
        logger.info(f"Naukri: {kw}")
        for j in scrape_keyword(kw, location=location, max_pages=max_pages):
            if j["url"] and j["url"] not in seen:
                seen.add(j["url"])
                all_jobs.append(j)
        time.sleep(random.uniform(2, 5))
    logger.info(f"Naukri total: {len(all_jobs)}")
    return all_jobs
