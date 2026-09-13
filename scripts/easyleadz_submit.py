#!/usr/bin/env python3
"""Submit known HR LinkedIn profiles to EasyLeadz.

Safe by default: without EASYLEADZ_LIVE_MODE=true the script only prints what
would be submitted and does not call the vendor API.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from enrichment.easyleadz import EasyLeadzError, normalize_linkedin_url, submit_contact


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _load_from_db(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, name, company, designation, linkedin_url
        FROM hr_contacts
        WHERE linkedin_url IS NOT NULL AND TRIM(linkedin_url) <> ''
        ORDER BY added_at DESC, id DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_from_csv(path: str, min_confidence: int) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = []
        for row in csv.DictReader(f):
            raw = row.get("Overall Confidence %") or row.get("overall_confidence") or ""
            try:
                score = int(float(raw)) if str(raw).strip() else 100
            except ValueError:
                score = 0
            if score < min_confidence:
                continue
            linkedin = (
                row.get("HR LinkedIn")
                or row.get("linkedin_url")
                or row.get("LinkedIn")
                or row.get("linkedin")
                or ""
            )
            rows.append(
                {
                    "id": "",
                    "name": row.get("HR Name") or row.get("name") or row.get("Candidate Name") or "",
                    "company": row.get("Company") or row.get("company") or "",
                    "designation": row.get("Likely HR Role") or row.get("designation") or row.get("role") or "",
                    "linkedin_url": linkedin,
                    "confidence": score,
                }
            )
        return rows


def _state(path: Path) -> dict:
    if not path.exists():
        return {"submitted": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("submitted"), dict):
            return data
    except Exception:
        pass
    return {"submitted": {}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/pipeline.db")
    parser.add_argument("--csv", help="Optional CSV source instead of hr_contacts table")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--min-confidence", type=int, default=80, help="Applied when CSV has Overall Confidence %")
    parser.add_argument("--state", default="data/private/easyleadz_submit_state.json")
    args = parser.parse_args()

    callback_url = os.environ.get("EASYLEADZ_CALLBACK_URL", "").strip()
    live = _truthy(os.environ.get("EASYLEADZ_LIVE_MODE", ""))
    if live and not callback_url.startswith("https://"):
        raise SystemExit("EASYLEADZ_CALLBACK_URL must be a public HTTPS URL in live mode")

    contacts = _load_from_csv(args.csv, args.min_confidence) if args.csv else _load_from_db(args.db)

    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = _state(state_path)
    submitted = state["submitted"]

    queue = []
    for c in contacts:
        try:
            url = normalize_linkedin_url(c.get("linkedin_url", ""))
        except EasyLeadzError:
            continue
        fp = _fingerprint(url)
        if fp in submitted:
            continue
        c = dict(c)
        c["linkedin_url"] = url
        c["fingerprint"] = fp
        queue.append(c)
        if len(queue) >= max(0, args.limit):
            break

    print(f"Eligible new profiles: {len(queue)}")
    if not live:
        print("DRY RUN: EASYLEADZ_LIVE_MODE is not true; no credits can be consumed.")
        for c in queue[:20]:
            print(f"- {c.get('company','')} | {c.get('name','')} | {c['linkedin_url']}")
        return 0

    failures = 0
    for c in queue:
        try:
            response = submit_contact(c["linkedin_url"], callback_url)
            data = response.get("data") or {}
            request_id = data.get("request_id") or ""
            submitted[c["fingerprint"]] = {"request_id": request_id}
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            print(f"submitted {c.get('company','')} | {c.get('name','')} | request_id={request_id}")
        except Exception as exc:
            failures += 1
            print(f"FAILED {c.get('company','')} | {c.get('name','')}: {exc}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
