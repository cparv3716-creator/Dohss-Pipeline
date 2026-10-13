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


def run(cmd, env, timeout=60):
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
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
                "max_pages_per_company": 10,
                "max_search_results_per_query": 5,
                "request_timeout_seconds": 5,
                "min_telegram_contact_confidence": 80,
                "hunter": {"enabled": False, "api_key": ""},
            },
            "search": {"brave_api_key": "", "serpapi_key": "", "bing_api_key": ""},
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

        env = os.environ.copy()
        env["DOHSS_CONFIG"] = str(config_path)
        env.pop("HUNTER_API_KEY", None)

        from database import db
        from enrichment import contact_finder
        from enrichment import search_contact_discovery
        from enrichment.contact_classifier import classify_contact
        from enrichment.company_resolver import is_bad_company_name, is_rejected_domain
        from enrichment.internet_discovery_agent import InternetDiscoveryAgent, generate_queries
        from outreach.contact_policy import classify_contact_for_outreach

        assert search_contact_discovery.generate_search_queries("Example"), "search discovery import failed"
        assert len(generate_queries("Example", "example.com")) >= 30, "internet query set too small"
        assert InternetDiscoveryAgent(config).max_pages == 10, "internet agent config not loaded"
        assert classify_contact_for_outreach({"confidence_score": 40, "source_type": "guessed_role_email"}) == "manual_review"
        assert is_bad_company_name("Hire Feed"), "bad company filter failed"
        assert is_bad_company_name("unknown"), "unknown company filter failed"
        assert is_rejected_domain("in.linkedin.com"), "job-board subdomain rejection failed"
        assert is_rejected_domain("naukri.com"), "job-board domain rejection failed"
        campus = classify_contact({
            "email": "campusrecruitment@aexp.com",
            "company_domain": "aexp.com",
            "source_url": "https://www.aexp.com/en-us/careers/",
            "source_type": "official_public_page",
            "evidence_text": "campus recruitment careers",
        })
        assert campus["confidence_score"] == 90 and campus["email_category"] == "hiring_email"
        scam = classify_contact({
            "email": "reportascam@blackrock.com",
            "company_domain": "blackrock.com",
            "source_url": "https://www.blackrock.com/careers",
            "source_type": "official_public_page",
            "evidence_text": "report a scam",
        })
        assert scam["excluded"], "excluded non-hiring email was not rejected"

        root_config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        token = str(root_config.get("telegram", {}).get("bot_token", ""))
        assert not token or "YOUR_BOT_TOKEN" in token, "config.yaml contains a real Telegram token"

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
        assert "companies" in tables, "companies table migration missing"
        conn = sqlite3.connect(db_path)
        try:
            contact_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(contacts)")
            }
        finally:
            conn.close()
        assert "allowed_for_auto_outreach" in contact_cols, "allowed_for_auto_outreach migration missing"
        assert "email_category" in contact_cols, "email_category migration missing"
        conn = sqlite3.connect(db_path)
        try:
            job_cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        finally:
            conn.close()
        for column in (
            "full_description", "requirements", "required_skills", "ats_provider",
            "eligibility_status", "match_score", "application_status",
            "enrichment_status", "enrichment_error",
        ):
            assert column in job_cols, f"jobs.{column} migration missing"
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

        run([sys.executable, "main.py", "stats"], env)
        run([sys.executable, "main.py", "enrich-contacts", "1"], env)
        help_result = subprocess.run(
            [sys.executable, "main.py", "--help"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        for command in (
            "discover-contacts",
            "internet-discover-contacts",
            "import-company-seeds",
            "resolve-companies",
            "export-companies",
            "export-contacts",
            "export-guesses",
            "cleanup-contacts",
            "enrich-contacts",
            "telegram-test",
            "telegram-dohss-digest",
            "telegram-dohss-companies",
            "telegram-dohss-hr",
            "telegram-dohss-latest",
            "enrich-jobs",
            "score-jobs",
            "prepare-applications",
            "review-applications",
            "job <id>",
        ):
            assert command in help_result.stdout, f"{command} missing from help output"
        print("Smoke checks passed.")


if __name__ == "__main__":
    main()
