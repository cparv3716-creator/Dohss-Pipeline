#!/usr/bin/env python3
"""Prepare a private CSV queue for EasyLeadz dashboard / Bulk Upload.

This is the default path when the account has a normal EasyLeadz subscription
but no API key. The output is private and should not be committed.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from pathlib import Path

CONF_RE = re.compile(r"confidence=(\d+)", re.I)


def confidence_from_notes(notes: str) -> int:
    m = CONF_RE.search(notes or "")
    return int(m.group(1)) if m else 100


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/pipeline.db")
    p.add_argument("--out", default="data/private/easyleadz_upload.csv")
    p.add_argument("--min-confidence", type=int, default=80)
    p.add_argument("--limit", type=int, default=500)
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, name, company, designation, linkedin_url, notes
        FROM hr_contacts
        WHERE linkedin_url IS NOT NULL AND TRIM(linkedin_url) <> ''
        ORDER BY added_at DESC, id DESC
        """
    ).fetchall()
    conn.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    selected = []
    seen = set()
    for row in rows:
        url = (row["linkedin_url"] or "").strip().rstrip("/")
        if not url or url in seen:
            continue
        score = confidence_from_notes(row["notes"] or "")
        if score < args.min_confidence:
            continue
        seen.add(url)
        selected.append({
            "LinkedIn URL": url,
            "Name": row["name"] or "",
            "Company": row["company"] or "",
            "Designation": row["designation"] or "",
            "Confidence": score,
        })
        if len(selected) >= max(args.limit, 0):
            break

    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["LinkedIn URL", "Name", "Company", "Designation", "Confidence"],
        )
        writer.writeheader()
        writer.writerows(selected)

    print(f"Prepared {len(selected)} contacts at {out}")
    print("Upload this file (or its LinkedIn URL column) through EasyLeadz Bulk Upload/EasySearch if your plan supports it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
