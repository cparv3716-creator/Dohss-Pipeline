#!/usr/bin/env python3
"""Import EasyLeadz dashboard/bulk-export results and build a private Excel file.

This is the default no-API workflow for EasyLeadz subscription users:
1) export vetted HR LinkedIn URLs with easyleadz_prepare_csv.py
2) upload/enrich them in EasyLeadz
3) download the resulting CSV
4) run this script to merge phone/email data back by LinkedIn URL

Sensitive phone/email data is stored only under data/private/ by default.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import xlsxwriter


PRIVATE_DB_DEFAULT = "data/private/easyleadz_contacts.db"
EXCEL_DEFAULT = "data/private/hr_contacts_enriched.xlsx"


def norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def norm_linkedin(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    value = value.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if "linkedin.com/in/" not in value.lower():
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value.lstrip("/")
    return value


def first_nonempty(row: dict, aliases: list[str]) -> str:
    mapped = {norm_key(k): (v or "").strip() for k, v in row.items() if k}
    for alias in aliases:
        value = mapped.get(norm_key(alias), "")
        if value:
            return value
    return ""


def parse_confidence(notes: str) -> int:
    match = re.search(r"confidence=(\d+)", notes or "", re.I)
    return int(match.group(1)) if match else 100


def init_private_db(path: str) -> None:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS easyleadz_contacts (
            linkedin_url   TEXT PRIMARY KEY,
            name           TEXT,
            company        TEXT,
            designation    TEXT,
            confidence     INTEGER DEFAULT 0,
            phone1         TEXT,
            phone2         TEXT,
            work_email     TEXT,
            personal_email TEXT,
            source_file    TEXT,
            imported_at    TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def load_hr_index(public_db: str) -> dict[str, dict]:
    conn = sqlite3.connect(public_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT name, company, designation, linkedin_url, notes
        FROM hr_contacts
        WHERE linkedin_url IS NOT NULL AND TRIM(linkedin_url) <> ''
        """
    ).fetchall()
    conn.close()
    index = {}
    for row in rows:
        url = norm_linkedin(row["linkedin_url"] or "")
        if not url:
            continue
        index[url] = {
            "name": row["name"] or "",
            "company": row["company"] or "",
            "designation": row["designation"] or "",
            "confidence": parse_confidence(row["notes"] or ""),
        }
    return index


def import_results(csv_path: str, public_db: str, private_db: str) -> tuple[int, int, int]:
    hr_index = load_hr_index(public_db)
    init_private_db(private_db)
    imported = 0
    unmatched = 0
    skipped = 0
    now = datetime.now(timezone.utc).isoformat()

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        conn = sqlite3.connect(private_db)
        try:
            for row in reader:
                linkedin = norm_linkedin(first_nonempty(row, [
                    "LinkedIn URL", "LinkedIn", "linkedin_url", "profile_url",
                    "linkedin profile", "linkedin_profile_url", "url"
                ]))
                if not linkedin:
                    skipped += 1
                    continue

                base = hr_index.get(linkedin)
                if not base:
                    unmatched += 1
                    base = {
                        "name": first_nonempty(row, ["Name", "Full Name", "HR Name"]),
                        "company": first_nonempty(row, ["Company", "Company Name", "Organization"]),
                        "designation": first_nonempty(row, ["Designation", "Title", "Job Title", "Role"]),
                        "confidence": 0,
                    }

                phone1 = first_nonempty(row, [
                    "phone1", "phone", "mobile", "mobile number", "phone number",
                    "direct dial", "contact number", "primary phone"
                ])
                phone2 = first_nonempty(row, [
                    "phone2", "secondary phone", "alternate phone", "alternate mobile"
                ])
                work_email = first_nonempty(row, [
                    "work email", "business email", "official email", "professional email",
                    "email", "email1", "company email"
                ])
                personal_email = first_nonempty(row, [
                    "personal email", "private email", "email2", "secondary email"
                ])

                if not any([phone1, phone2, work_email, personal_email]):
                    skipped += 1
                    continue

                conn.execute(
                    """
                    INSERT INTO easyleadz_contacts
                    (linkedin_url,name,company,designation,confidence,phone1,phone2,
                     work_email,personal_email,source_file,imported_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(linkedin_url) DO UPDATE SET
                        name=excluded.name,
                        company=excluded.company,
                        designation=excluded.designation,
                        confidence=MAX(easyleadz_contacts.confidence, excluded.confidence),
                        phone1=CASE WHEN excluded.phone1<>'' THEN excluded.phone1 ELSE easyleadz_contacts.phone1 END,
                        phone2=CASE WHEN excluded.phone2<>'' THEN excluded.phone2 ELSE easyleadz_contacts.phone2 END,
                        work_email=CASE WHEN excluded.work_email<>'' THEN excluded.work_email ELSE easyleadz_contacts.work_email END,
                        personal_email=CASE WHEN excluded.personal_email<>'' THEN excluded.personal_email ELSE easyleadz_contacts.personal_email END,
                        source_file=excluded.source_file,
                        imported_at=excluded.imported_at
                    """,
                    (
                        linkedin, base["name"], base["company"], base["designation"],
                        int(base["confidence"] or 0), phone1, phone2, work_email,
                        personal_email, Path(csv_path).name, now,
                    ),
                )
                imported += 1
            conn.commit()
        finally:
            conn.close()
    return imported, unmatched, skipped


def export_excel(private_db: str, output_path: str) -> int:
    conn = sqlite3.connect(private_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT company,name,designation,linkedin_url,phone1,phone2,work_email,
               personal_email,confidence,source_file,imported_at
        FROM easyleadz_contacts
        ORDER BY confidence DESC, company COLLATE NOCASE, name COLLATE NOCASE
        """
    ).fetchall()
    conn.close()

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(out)
    ws = workbook.add_worksheet("HR Contacts")

    header_fmt = workbook.add_format({
        "bold": True, "font_color": "white", "bg_color": "#17365D",
        "align": "center", "valign": "vcenter", "text_wrap": True,
        "border": 1,
    })
    cell_fmt = workbook.add_format({"valign": "top", "text_wrap": True})
    pct_fmt = workbook.add_format({"valign": "top", "align": "center"})
    link_fmt = workbook.get_default_url_format()

    headers = [
        "Company", "HR Name", "HR Role", "LinkedIn", "Phone 1", "Phone 2",
        "Work Email", "Personal Email", "HR Confidence %", "Enrichment Status",
        "Source File", "Imported At"
    ]
    for col, header in enumerate(headers):
        ws.write(0, col, header, header_fmt)

    for r_idx, row in enumerate(rows, start=1):
        status = "Phone + Email" if (row["phone1"] or row["phone2"]) and (row["work_email"] or row["personal_email"]) else (
            "Phone found" if (row["phone1"] or row["phone2"]) else "Email found"
        )
        values = [
            row["company"], row["name"], row["designation"], row["linkedin_url"],
            row["phone1"], row["phone2"], row["work_email"], row["personal_email"],
            row["confidence"], status, row["source_file"], row["imported_at"],
        ]
        for c_idx, value in enumerate(values):
            if c_idx == 3 and value:
                ws.write_url(r_idx, c_idx, value, link_fmt, string=value)
            elif c_idx == 8:
                ws.write_number(r_idx, c_idx, int(value or 0), pct_fmt)
            else:
                ws.write(r_idx, c_idx, value or "", cell_fmt)

    widths = [28,24,26,42,18,18,32,32,16,18,24,28]
    for i, width in enumerate(widths):
        ws.set_column(i, i, width)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, max(len(rows), 1), len(headers) - 1)
    ws.conditional_format(1, 8, max(len(rows), 1), 8, {
        "type": "3_color_scale", "min_color": "#FCE8E6",
        "mid_color": "#FFF2CC", "max_color": "#D9EAD3",
    })
    workbook.close()
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("results_csv", help="CSV downloaded/exported from EasyLeadz")
    p.add_argument("--db", default="data/pipeline.db", help="DoHSS public pipeline DB")
    p.add_argument("--private-db", default=PRIVATE_DB_DEFAULT)
    p.add_argument("--out", default=EXCEL_DEFAULT)
    args = p.parse_args()

    if not Path(args.results_csv).exists():
        raise SystemExit(f"Result CSV not found: {args.results_csv}")
    if not Path(args.db).exists():
        raise SystemExit(f"DoHSS DB not found: {args.db}")

    imported, unmatched, skipped = import_results(
        args.results_csv, args.db, args.private_db
    )
    total = export_excel(args.private_db, args.out)
    print(f"Imported/updated: {imported}")
    print(f"Unmatched LinkedIn profiles retained for review: {unmatched}")
    print(f"Skipped rows with no usable profile/contact data: {skipped}")
    print(f"Private enriched contacts in workbook: {total}")
    print(f"Excel written to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
