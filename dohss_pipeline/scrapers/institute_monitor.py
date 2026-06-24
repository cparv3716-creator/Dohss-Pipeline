"""
scrapers/institute_monitor.py
Monitor DSE, ISI, TISS, JNU etc. placement pages for changes and new PDFs.
"""
import hashlib
import logging
import random
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch_page(url, timeout=20):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        logger.warning(f"Could not fetch {url}: {e}")
        return None


def _page_hash(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script","style","nav","footer","header"]):
        tag.decompose()
    return hashlib.sha256(soup.get_text(separator=" ", strip=True).encode()).hexdigest()


def _find_pdf_links(html, base_url, keywords):
    soup = BeautifulSoup(html, "html.parser")
    pdf_links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().endswith(".pdf"):
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        combined = (a.get_text(strip=True) + " " + href).lower()
        if any(k.lower() in combined for k in keywords + ["placement","report","brochure"]):
            pdf_links.append({"url": full_url, "filename": href.rstrip("/").split("/")[-1]})
            seen.add(full_url)
    return pdf_links


def check_institute(institute, db_path):
    from database import db
    name     = institute["name"]
    page_url = institute["placement_page"]
    keywords = institute.get("pdf_keywords", ["placement"])
    logger.info(f"Checking: {name}")

    html = _fetch_page(page_url)
    if html is None:
        return {"name": name, "changed": False, "new_pdfs": [], "current_hash": None}

    current_hash = _page_hash(html)
    stored_hash  = db.get_institute_hash(db_path, name)
    changed = (stored_hash is not None) and (stored_hash != current_hash)
    db.update_institute_hash(db_path, name, current_hash, changed)
    if changed:
        logger.info(f"  *** {name}: PAGE CHANGED ***")

    new_pdfs = []
    for pdf in _find_pdf_links(html, page_url, keywords):
        is_new = db.insert_pdf(db_path, {"institute": name, "filename": pdf["filename"],
                                          "url": pdf["url"], "local_path": ""})
        if is_new:
            new_pdfs.append(pdf)
            logger.info(f"  New PDF: {pdf['filename']}")

    return {"name": name, "changed": changed, "new_pdfs": new_pdfs, "current_hash": current_hash}


def run_all_institutes(config, db_path):
    from database import db
    institutes = config.get("institutes", {}).get("list", [])
    results = []
    for inst in institutes:
        db.upsert_institute(db_path, {"name": inst["name"],
                                       "full_name": inst.get("full_name", inst["name"]),
                                       "placement_page": inst["placement_page"]})
        results.append(check_institute(inst, db_path))
        time.sleep(random.uniform(2, 5))
    changed = sum(1 for r in results if r["changed"])
    new_pdfs = sum(len(r["new_pdfs"]) for r in results)
    logger.info(f"Institutes done. Changed: {changed}, New PDFs: {new_pdfs}")
    return results
