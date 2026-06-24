"""
scrapers/pdf_parser.py
Download and parse placement report PDFs. Extracts company/role/CTC data.
"""
import logging
import os
import re
from pathlib import Path

import pdfplumber
import requests

logger = logging.getLogger(__name__)

PROFILE_KEYWORDS = {
    "Quant":      ["quantitative","quant","actuar"],
    "Research":   ["research analyst","research associate"],
    "Policy":     ["policy","government","public sector","ngo","development"],
    "Consulting": ["consultant","consulting","advisory","strategy"],
    "Finance":    ["finance","investment","banking","equity","credit","risk"],
    "Data":       ["data analyst","data science","analytics"],
    "Marketing":  ["marketing","brand","market research"],
}


def _classify_profile(role_text):
    lower = role_text.lower()
    for ptype, kws in PROFILE_KEYWORDS.items():
        if any(k in lower for k in kws):
            return ptype
    return "Other"


def _extract_ctc(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:lpa|lakh|lac|l\.p\.a)", text.lower())
    return float(m.group(1)) if m else None


def _extract_year(text, filename):
    m = re.search(r"20[12]\d", text[:500] + filename)
    return m.group(0) if m else "Unknown"


def download_pdf(url, dest_dir, filename):
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    local_path = os.path.join(dest_dir, filename)
    if os.path.exists(local_path):
        return local_path
    try:
        resp = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=30, stream=True)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        return local_path
    except Exception as e:
        logger.error(f"Download failed {url}: {e}")
        return None


def parse_pdf(local_path, institute, pdf_id):
    rows = []
    try:
        with pdfplumber.open(local_path) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages[:5])
            year = _extract_year(full_text, os.path.basename(local_path))
            for page in pdf.pages:
                tables = page.extract_tables()
                if tables:
                    for table in tables:
                        if not table or len(table) < 2:
                            continue
                        header = [str(c).lower().strip() if c else "" for c in table[0]]
                        def col(*names):
                            for n in names:
                                for i, h in enumerate(header):
                                    if n in h:
                                        return i
                            return None
                        cc = col("company","organisation","employer")
                        rc = col("role","designation","position","profile")
                        sc = col("ctc","package","salary","lpa")
                        for row in table[1:]:
                            def cell(idx):
                                return str(row[idx] or "").strip() if idx is not None and idx < len(row) else ""
                            company = cell(cc) or "Unknown"
                            role    = cell(rc) or "Unknown"
                            if company == role == "Unknown":
                                continue
                            rows.append({"institute": institute, "year": year,
                                         "company": company, "role": role,
                                         "profile_type": _classify_profile(role),
                                         "ctc_lpa": _extract_ctc(cell(sc)),
                                         "count": None, "pdf_id": pdf_id})
    except Exception as e:
        logger.error(f"PDF parse error {local_path}: {e}")
    logger.info(f"Parsed {len(rows)} rows from {os.path.basename(local_path)}")
    return rows


def process_new_pdfs(config, db_path):
    from database import db
    pdf_dir = config.get("pdf_storage", {}).get("directory", "pdfs/")
    conn = db.get_connection(db_path)
    unparsed = conn.execute("SELECT * FROM placement_pdfs WHERE parsed=0").fetchall()
    conn.close()
    for row in [dict(r) for r in unparsed]:
        local = download_pdf(row["url"], pdf_dir, row["filename"])
        if not local:
            continue
        conn = db.get_connection(db_path)
        conn.execute("UPDATE placement_pdfs SET local_path=? WHERE id=?", (local, row["id"]))
        conn.commit()
        conn.close()
        data_rows = parse_pdf(local, row["institute"], row["id"])
        if data_rows:
            db.insert_placement_data(db_path, data_rows)
        conn = db.get_connection(db_path)
        conn.execute("UPDATE placement_pdfs SET parsed=1 WHERE id=?", (row["id"],))
        conn.commit()
        conn.close()
