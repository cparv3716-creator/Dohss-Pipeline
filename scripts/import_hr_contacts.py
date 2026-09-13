#!/usr/bin/env python3
"""Import vetted HR contacts from CSV into the DoHSS hr_contacts table.

Expected columns from the cleaned workbook export include Company, HR Name,
HR LinkedIn, Likely HR Role, and optionally Overall Confidence %.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from database.db import init_db, insert_hr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--db", default="data/pipeline.db")
    parser.add_argument("--min-confidence", type=int, default=80)
    args = parser.parse_args()

    path = Path(args.csv_path)
    if not path.exists():
        raise SystemExit(f"CSV not found: {path}")

    init_db(args.db)
    inserted = 0
    skipped = 0
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            raw_score = row.get("Overall Confidence %") or row.get("overall_confidence") or ""
            try:
                score = int(float(raw_score)) if str(raw_score).strip() else 100
            except ValueError:
                score = 0
            linkedin = (row.get("HR LinkedIn") or row.get("LinkedIn") or row.get("linkedin_url") or "").strip()
            name = (row.get("HR Name") or row.get("Candidate Name") or row.get("name") or "").strip()
            company = (row.get("Company") or row.get("company") or "").strip()
            designation = (row.get("Likely HR Role") or row.get("designation") or row.get("role") or "").strip()
            if score < args.min_confidence or not linkedin or not name:
                skipped += 1
                continue
            notes = f"Imported vetted HR lead; confidence={score}"
            if insert_hr(
                args.db,
                {
                    "name": name,
                    "company": company,
                    "designation": designation,
                    "email": "",
                    "linkedin_url": linkedin,
                    "source": "vetted_csv",
                    "notes": notes,
                },
            ):
                inserted += 1

    print(f"Imported {inserted} HR contacts; skipped {skipped} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
