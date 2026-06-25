#!/usr/bin/env python3
"""Offline smoke checks for the DoHSS pipeline."""
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(cmd, env):
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit(result.returncode)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "pipeline.db"
        config_path = tmp_path / "config.yaml"

        config = {
            "linkedin": {"keywords": [], "location": "India", "max_results_per_keyword": 0},
            "naukri": {"keywords": [], "location": "india", "max_pages": 0},
            "internshala": {"keywords": [], "pages_per_keyword": 0},
            "google_alerts": {"feeds": []},
            "institutes": {"list": []},
            "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
            "notifications": {"new_job_found": False, "new_institute_update": False},
            "database": {"path": str(db_path)},
            "pdf_storage": {"directory": str(tmp_path / "pdfs")},
            "enrichment": {
                "enabled": True,
                "max_contacts_per_opportunity": 3,
                "timeout_seconds": 5,
                "min_confidence_for_alert": 70,
                "hunter_enabled": False,
            },
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

        env = os.environ.copy()
        env["DOHSS_CONFIG"] = str(config_path)
        env.pop("HUNTER_API_KEY", None)

        from database import db
        from enrichment import contact_finder

        db.init_db(str(db_path))
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()
        assert "contacts" in tables, "contacts table migration missing"
        db.insert_contact(
            str(db_path),
            {
                "opportunity_id": None,
                "company_name": "Example",
                "company_domain": "example.com",
                "email": "careers@example.com",
                "source_url": "https://example.com/careers",
                "source_type": "public_page",
                "confidence_score": 85,
            },
        )
        db.insert_contact(
            str(db_path),
            {
                "opportunity_id": None,
                "company_name": "Example",
                "company_domain": "example.com",
                "email": "careers@example.com",
                "source_url": "https://example.com/careers",
                "source_type": "public_page",
                "confidence_score": 80,
            },
        )
        assert db.get_contact_stats(str(db_path))["total"] == 1, "contact dedupe failed"

        original_fetch = contact_finder._fetch_html
        contact_finder._fetch_html = lambda *_args, **_kwargs: "<html><body>No email here</body></html>"
        try:
            assert contact_finder.extract_public_emails_from_url("https://example.com") == []
        finally:
            contact_finder._fetch_html = original_fetch

        run([sys.executable, "main.py", "fetch-now"], env)
        run([sys.executable, "main.py", "enrich-contacts", "1"], env)
        print("Smoke checks passed.")


if __name__ == "__main__":
    main()
