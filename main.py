#!/usr/bin/env python3
"""
main.py - DoHSS IITM Placement Intelligence Pipeline

Commands:
  python main.py run            Start full automated pipeline (loops forever)
  python main.py fetch-now      Run all scrapers once and exit
  python main.py dashboard      Show terminal dashboard
  python main.py add-hr         Manually add an HR contact
  python main.py enrich-contacts Enrich recent opportunities with public contacts
  python main.py discover-contacts Discover contacts using search APIs
  python main.py internet-discover-contacts Internet-discover contacts
  python main.py linkedin-hr-search [limit] Search LinkedIn for HR contacts at pipeline companies
  python main.py import-company-seeds companies.csv
  python main.py resolve-companies Resolve company domains
  python main.py export-companies Export resolved companies
  python main.py contacts       Show enriched contacts
  python main.py export-contacts Export contacts to CSV
  python main.py export-guesses Export guessed emails to CSV
  python main.py cleanup-contacts Cleanup/rescore stored contacts
  python main.py debug-company-contacts "Company Name"
  python main.py test-telegram  Test Telegram bot connection
  python main.py tg-login       One-time Telegram login for the harvester
  python main.py fetch-telegram Harvest recruiter contacts from hiring channels
  python main.py stats          Print quick stats
"""
import csv
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def _deep_merge(base, override):
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path=None):
    path = path or os.environ.get("DOHSS_CONFIG", "config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # Keep secrets in ignored config.local.yaml while preserving config.yaml defaults.
    local_path = Path("config.local.yaml")
    if Path(path).name != "config.local.yaml" and local_path.exists():
        with local_path.open("r", encoding="utf-8") as f:
            config = _deep_merge(config, yaml.safe_load(f) or {})
    elif Path(path).name == "config.local.yaml" and Path("config.yaml").exists():
        with open("config.yaml", "r", encoding="utf-8") as f:
            base = yaml.safe_load(f) or {}
        config = _deep_merge(base, config)
    return config


def get_db_path(config):
    return config.get("database", {}).get("path", "data/pipeline.db")


def _enrichment_enabled(config):
    return config.get("enrichment", {}).get("enabled", True)


def _save_contacts(config, db_path, job, contacts):
    from database import db
    max_contacts = int(config.get("enrichment", {}).get("max_contacts_per_opportunity", 3))
    inserted = 0
    for contact in contacts[:max_contacts]:
        contact["opportunity_id"] = job["id"]
        contact["company_name"] = job.get("company", contact.get("company_name", ""))
        if db.insert_contact(db_path, contact):
            inserted += 1
    return inserted


def _enrich_jobs(config, db_path, jobs, use_page_finder=True, use_search_finder=True):
    if not _enrichment_enabled(config) or not jobs:
        return 0
    from enrichment.contact_finder import find_company_contacts
    from enrichment.search_contact_discovery import discover_contacts

    inserted = 0
    total = len(jobs)
    for idx, job in enumerate(jobs, start=1):
        company = job.get("company", "Unknown")
        try:
            found = []
            if use_page_finder:
                found.extend(find_company_contacts(company, source_url=job.get("url", ""), config=config))
            if use_search_finder:
                found.extend(discover_contacts(company, source_url=job.get("url", ""), config=config))
            found = _dedupe_contacts(found)
            inserted += _save_contacts(config, db_path, job, found)
        except Exception as exc:
            print(f"[{idx}/{total}] {company} -> skipped, enrichment error")
            logger.info("Contact enrichment skipped for %s: %s", company, exc)
    return inserted


def _dedupe_contacts(contacts):
    deduped = {}
    for contact in contacts:
        email = (contact.get("email") or "").lower()
        linkedin_url = contact.get("linkedin_url") or ""
        if not email and not linkedin_url:
            continue
        if int(contact.get("confidence_score") or 0) <= 0:
            continue
        key = (email or linkedin_url, contact.get("company_domain") or "", contact.get("source_url") or "")
        existing = deduped.get(key)
        if existing and not (int(contact.get("confidence_score") or 0) > int(existing.get("confidence_score") or 0)):
            continue
        deduped[key] = contact
    return sorted(deduped.values(), key=lambda c: int(c.get("confidence_score") or 0), reverse=True)


def cmd_run(config):
    from database.db import init_db
    init_db(get_db_path(config))
    import scheduler
    scheduler.start(config, get_db_path(config))


def cmd_fetch_now(config):
    from database.db import init_db, insert_job, get_job_by_url, get_unnotified_jobs, mark_jobs_notified
    from scrapers.linkedin_jobs   import run_all_keywords as li_fetch
    from scrapers.naukri          import run_all_keywords as nk_fetch
    from scrapers.internshala     import run_all_keywords as is_fetch
    from scrapers.google_alerts   import run_all_feeds
    from scrapers.institute_monitor import run_all_institutes
    from scrapers.pdf_parser      import process_new_pdfs
    from notifier.telegram        import notify_new_jobs, notify_institute_changes

    db_path = get_db_path(config)
    init_db(db_path)

    print("\nScraping all sources...")
    sources = [
        ("LinkedIn",      li_fetch(config)),
        ("Naukri",        nk_fetch(config)),
        ("Internshala",   is_fetch(config)),
        ("Google Alerts", run_all_feeds(config)),
    ]
    total_new = 0
    new_jobs = []
    for name, jobs in sources:
        new = 0
        for j in jobs:
            if insert_job(db_path, j):
                new += 1
                saved = get_job_by_url(db_path, j.get("url", ""))
                if saved:
                    new_jobs.append(saved)
        total_new += new
        print(f"  {name:15s}: {new:3d} new  (scraped {len(jobs)})")

    print(f"\n  Total new this run: {total_new}")
    if new_jobs and _enrichment_enabled(config):
        print("Enriching new opportunities with public contacts...")
        contacts_added = _enrich_jobs(config, db_path, new_jobs)
        print(f"  {contacts_added} contact record(s) added/updated")

    print("\nChecking institute pages...")
    inst_results = run_all_institutes(config, db_path)
    changed = [r for r in inst_results if r["changed"] or r["new_pdfs"]]
    print(f"  {len(changed)} institute(s) updated")

    print("Processing PDFs...")
    process_new_pdfs(config, db_path)

    print("Sending Telegram notifications...")
    unnotified = get_unnotified_jobs(db_path)
    if unnotified:
        notify_new_jobs(config, unnotified, db_path=db_path)
        mark_jobs_notified(db_path, [j["id"] for j in unnotified])
    notify_institute_changes(config, changed)
    try:
        from notifier.telegram import maybe_send_dohss_update
        maybe_send_dohss_update(config, db_path, reason="fetch-now")
    except Exception as exc:
        logger.info("DoHSS Telegram update skipped: %s", exc)

    print("\nDone! Run  python main.py dashboard  to see results.\n")


def cmd_dashboard(config):
    from database.db import init_db
    from dashboard.terminal_dash import show
    db_path = get_db_path(config)
    init_db(db_path)
    show(db_path)


def cmd_enrich_contacts(config):
    from database.db import init_db, get_recent_jobs_for_enrichment
    db_path = get_db_path(config)
    init_db(db_path)
    limit = 50
    if len(sys.argv) > 2:
        try:
            limit = int(sys.argv[2])
        except ValueError:
            print("Usage: python main.py enrich-contacts [limit]")
            return
    jobs = get_recent_jobs_for_enrichment(db_path, limit=limit)
    print(f"\nEnriching {len(jobs)} recent opportunity/opportunities...")
    added = _enrich_jobs(config, db_path, jobs, use_page_finder=True, use_search_finder=True)
    print(f"Added/updated {added} contact record(s).\n")


def cmd_discover_contacts(config):
    from database.db import init_db, get_recent_jobs_for_enrichment
    db_path = get_db_path(config)
    init_db(db_path)
    limit = 50
    if len(sys.argv) > 2:
        try:
            limit = int(sys.argv[2])
        except ValueError:
            print("Usage: python main.py discover-contacts [limit]")
            return
    jobs = get_recent_jobs_for_enrichment(db_path, limit=limit)
    print(f"\nDiscovering contacts for {len(jobs)} recent opportunity/opportunities...")
    added = _enrich_jobs(config, db_path, jobs, use_page_finder=False, use_search_finder=True)
    print(f"Added/updated {added} contact record(s).\n")


def cmd_internet_discover_contacts(config):
    from database.db import init_db, get_recent_jobs_for_enrichment
    from enrichment.internet_discovery_agent import InternetDiscoveryAgent
    db_path = get_db_path(config)
    init_db(db_path)
    limit = 50
    if len(sys.argv) > 2:
        try:
            limit = min(int(sys.argv[2]), 50)
        except ValueError:
            print("Usage: python main.py internet-discover-contacts [limit]")
            return
    jobs = get_recent_jobs_for_enrichment(db_path, limit=limit)
    agent = InternetDiscoveryAgent(config)
    print(f"\nInternet-discovering contacts for {len(jobs)} opportunity/opportunities...")
    inserted = 0
    seen_companies = set()
    for idx, job in enumerate(jobs, start=1):
        company_key = _company_key(job.get("company", ""))
        if company_key in seen_companies:
            print(f"[{idx}/{len(jobs)}] {job.get('company', 'Unknown')} -> skipped, duplicate company in run")
            continue
        seen_companies.add(company_key)
        result = agent.discover_for_job(job)
        contacts = _dedupe_contacts(result["contacts"])
        inserted += _save_contacts(config, db_path, job, contacts)
        if result["status"] == "ok":
            print(f"[{idx}/{len(jobs)}] {job.get('company', 'Unknown')} -> domain {result.get('domain') or '-'} {result.get('domain_confidence', 0)} -> {result.get('public_contacts', 0)} public contacts, {result.get('guesses', 0)} guesses")
        else:
            print(f"[{idx}/{len(jobs)}] {job.get('company', 'Unknown')} -> {result['status']}")
    print(f"Added/updated {inserted} contact record(s).\n")


def cmd_import_company_seeds(config):
    from database.db import init_db
    from enrichment.company_resolver import import_company_seeds
    if len(sys.argv) < 3:
        print("Usage: python main.py import-company-seeds companies.csv")
        return
    db_path = get_db_path(config)
    init_db(db_path)
    rows = import_company_seeds(db_path, sys.argv[2])
    print(f"Imported {len(rows)} company seed(s).")


def cmd_resolve_companies(config):
    from database import db
    from enrichment.company_resolver import is_bad_company_name, resolve_company
    db_path = get_db_path(config)
    db.init_db(db_path)
    limit = 25
    if len(sys.argv) > 2:
        try:
            limit = int(sys.argv[2])
        except ValueError:
            print("Usage: python main.py resolve-companies [limit]")
            return
    companies = db.get_recent_companies_from_jobs(db_path, limit=limit)
    seen = set()
    printed = 0
    for item in companies:
        company = item.get("name", "")
        key = _company_key(company)
        if key in seen:
            printed += 1
            print(f"[{printed}/{len(companies)}] {company} -> skipped duplicate")
            continue
        seen.add(key)
        printed += 1
        if is_bad_company_name(company):
            print(f"[{printed}/{len(companies)}] {company} -> skipped bad company name")
            continue
        resolved = resolve_company(db_path, company, source_url=item.get("source_url", ""), config=config, prefer_cache=True)
        domain = resolved.get("official_domain") or "-"
        confidence = int(resolved.get("domain_confidence") or 0)
        source = resolved.get("source_type") or resolved.get("status") or "unknown"
        if domain == "-" or confidence < 85:
            print(f"[{printed}/{len(companies)}] {company} -> no official domain")
            continue
        print(f"[{printed}/{len(companies)}] {company} -> {domain} confidence {confidence} via {source}")


def cmd_export_companies(config):
    from database import db
    db_path = get_db_path(config)
    db.init_db(db_path)
    rows = db.get_companies(db_path)
    export_dir = Path("exports")
    export_dir.mkdir(parents=True, exist_ok=True)
    out_path = export_dir / "companies.csv"
    fields = [
        "id", "name", "normalized_name", "official_domain", "domain_confidence",
        "careers_url", "contact_url", "ats_platform", "source_url", "source_type",
        "evidence_text", "created_at", "updated_at",
    ]
    written = _write_csv_with_fallback(out_path, rows, fields)
    print(f"Exported {len(rows)} compan(y/ies) to {written}")


def _company_key(company):
    return " ".join(str(company or "").lower().split())


def cmd_contacts(config):
    from database.db import init_db, get_contacts
    from rich.console import Console
    from rich.table import Table

    db_path = get_db_path(config)
    init_db(db_path)
    min_confidence = 0
    include_guesses = "--include-guesses" in sys.argv[2:]
    numeric_args = [arg for arg in sys.argv[2:] if arg != "--include-guesses"]
    if numeric_args:
        try:
            min_confidence = int(numeric_args[0])
        except ValueError:
            print("Usage: python main.py contacts [min_confidence] [--include-guesses]")
            return
    include_guesses = include_guesses and min_confidence <= 40
    rows = get_contacts(db_path, min_confidence=min_confidence, limit=100, include_guesses=include_guesses)
    suffix = " including guesses" if include_guesses else ""
    table = Table(title=f"Contacts (confidence >= {min_confidence}{suffix})")
    table.add_column("Score", justify="right")
    table.add_column("Company")
    table.add_column("Name / Title")
    table.add_column("Email / LinkedIn")
    table.add_column("Source")
    table.add_column("Opportunity")
    for r in rows:
        person = " - ".join(part for part in (r.get("contact_name") or "", r.get("role_title") or "") if part) or "-"
        reachable = r["email"] or r.get("linkedin_url") or "-"
        table.add_row(
            str(r["confidence_score"]),
            r["company_name"] or "-",
            person,
            reachable,
            r["source_type"] or "-",
            (r.get("opportunity_title") or "-")[:45],
        )
    Console().print(table)


def _write_csv_with_fallback(out_path, rows, fields):
    try:
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return out_path
    except PermissionError:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = out_path.with_name(f"{out_path.stem}_{stamp}{out_path.suffix}")
        with fallback.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return fallback


def _contact_export_fields():
    return [
        "id", "opportunity_id", "company_name", "company_domain", "contact_name",
        "role_title", "email", "linkedin_url", "source_url", "source_type",
        "evidence_text", "email_category", "allowed_for_auto_outreach",
        "outreach_action", "confidence_score", "last_seen_at", "created_at",
        "opportunity_title", "opportunity_url",
    ]


def cmd_export_contacts(config):
    from database.db import init_db, get_contacts
    from outreach.contact_policy import classify_contact_for_outreach
    db_path = get_db_path(config)
    init_db(db_path)
    export_dir = Path("exports")
    export_dir.mkdir(parents=True, exist_ok=True)
    out_path = export_dir / "contacts.csv"
    min_confidence = 0
    if len(sys.argv) > 2:
        try:
            min_confidence = int(sys.argv[2])
        except ValueError:
            print("Usage: python main.py export-contacts [min_confidence]")
            return
    rows = get_contacts(db_path, min_confidence=min_confidence, limit=100000, include_guesses=False)
    for row in rows:
        row["outreach_action"] = classify_contact_for_outreach(row)
    written = _write_csv_with_fallback(out_path, rows, _contact_export_fields())
    print(f"Exported {len(rows)} contact(s) to {written}")


def cmd_export_guesses(config):
    from database.db import init_db, get_contact_guesses
    from outreach.contact_policy import classify_contact_for_outreach
    db_path = get_db_path(config)
    init_db(db_path)
    export_dir = Path("exports")
    export_dir.mkdir(parents=True, exist_ok=True)
    out_path = export_dir / "contact_guesses.csv"
    rows = get_contact_guesses(db_path)
    for row in rows:
        row["outreach_action"] = classify_contact_for_outreach(row)
    written = _write_csv_with_fallback(out_path, rows, _contact_export_fields())
    print(f"Exported {len(rows)} guessed contact(s) to {written}")


def cmd_cleanup_contacts(config):
    from database import db
    from enrichment.contact_classifier import classify_contact
    db_path = get_db_path(config)
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    rows = conn.execute("SELECT * FROM contacts").fetchall()
    removed = 0
    updated = 0
    try:
        for row in rows:
            contact = dict(row)
            classified = classify_contact(contact) if contact.get("email") else None
            if not classified:
                if int(contact.get("confidence_score") or 0) > 0:
                    updated += 1
                continue
            if (classified["is_valid"] and classified["excluded"]) or classified["confidence_score"] <= 0:
                conn.execute("DELETE FROM contacts WHERE id=?", (contact["id"],))
                removed += 1
                continue
            conn.execute(
                """
                UPDATE contacts
                SET email=?,
                    email_category=?,
                    confidence_score=?,
                    role_title=?,
                    contact_name=?,
                    allowed_for_auto_outreach=?
                WHERE id=?
                """,
                (classified["email"], classified["email_category"], classified["confidence_score"],
                 classified["role_title"], classified["contact_name"],
                 classified["allowed_for_auto_outreach"], contact["id"]),
            )
            updated += 1
        conn.commit()
    finally:
        conn.close()
    print(f"Cleanup complete: removed {removed}, rescored {updated}.")


def cmd_debug_company_contacts(config):
    from database import db
    from enrichment.internet_discovery_agent import InternetDiscoveryAgent

    if len(sys.argv) < 3:
        print('Usage: python main.py debug-company-contacts "Company Name"')
        return
    company = " ".join(sys.argv[2:]).strip().strip('"')
    db_path = get_db_path(config)
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE lower(company)=lower(?)
               OR lower(company) LIKE lower(?)
            ORDER BY found_at DESC
            LIMIT 3
            """,
            (company, f"%{company}%"),
        ).fetchall()
    finally:
        conn.close()
    jobs = [dict(r) for r in rows] or [{"company": company, "url": ""}]
    agent = InternetDiscoveryAgent(config)
    seen = set()
    print(f"\nDebug contacts for {company}")
    for idx, job in enumerate(jobs, start=1):
        result = agent.debug_for_job(job)
        print(f"[{idx}/{len(jobs)}] {job.get('company') or company} -> domain {result.get('domain') or '-'} {result.get('domain_confidence', 0)} -> {len(result.get('contacts', []))} raw candidate(s)")
        for contact in result.get("contacts", []):
            email = contact.get("email") or "-"
            if email in seen:
                continue
            seen.add(email)
            category = contact.get("email_category") or "-"
            score = contact.get("confidence_score", 0)
            reason = contact.get("reject_reason") or "accepted"
            source_type = contact.get("source_type") or "-"
            source_url = contact.get("source_url") or "-"
            print(f"  {email} | {category} | score {score} | {source_type} | {reason} | {source_url}")
    print()


def cmd_add_hr(config):
    from database.db import init_db, insert_hr
    db_path = get_db_path(config)
    init_db(db_path)
    print("\n-- Add HR Contact --")
    name        = input("Full name:         ").strip()
    company     = input("Company:           ").strip()
    designation = input("Designation/Role:  ").strip()
    email       = input("Email:             ").strip()
    linkedin    = input("LinkedIn URL:      ").strip()
    notes       = input("Notes (optional):  ").strip()
    added = insert_hr(db_path, {"name": name, "company": company,
                                "designation": designation, "email": email,
                                "linkedin_url": linkedin, "source": "manual", "notes": notes})
    print(f"\nAdded: {name} @ {company}" if added else "\nContact already exists.")


def cmd_test_telegram(config):
    from notifier.telegram import send_test_message
    send_test_message(config)


def cmd_telegram_test(config):
    from notifier.telegram import send_dohss_test_message
    send_dohss_test_message(config)


def cmd_telegram_dohss_digest(config):
    from database.db import init_db
    from notifier.telegram import send_dohss_digest
    db_path = get_db_path(config)
    init_db(db_path)
    ok = send_dohss_digest(config, db_path, update_state=True)
    print("DoHSS Telegram digest sent." if ok else "DoHSS Telegram digest failed.")


def cmd_telegram_dohss_companies(config):
    from database.db import init_db
    from notifier.telegram import send_dohss_companies
    db_path = get_db_path(config)
    init_db(db_path)
    flags, _ = _parse_flags(sys.argv[2:])
    limit = int(flags.get("limit", 30))
    ok = send_dohss_companies(config, db_path, limit=limit)
    print("DoHSS company list sent." if ok else "DoHSS company list failed.")


def cmd_telegram_dohss_hr(config):
    from database.db import init_db
    from notifier.telegram import send_dohss_hr
    db_path = get_db_path(config)
    init_db(db_path)
    flags, _ = _parse_flags(sys.argv[2:])
    min_confidence = int(flags.get("min-confidence", 70))
    limit = int(flags.get("limit", 50))
    ok = send_dohss_hr(config, db_path, min_confidence=min_confidence, limit=limit)
    print("DoHSS HR contacts sent." if ok else "DoHSS HR contacts failed.")


def cmd_telegram_dohss_latest(config):
    from database.db import init_db
    from notifier.telegram import send_dohss_latest
    db_path = get_db_path(config)
    init_db(db_path)
    flags, _ = _parse_flags(sys.argv[2:])
    since_hours = int(flags.get("since-hours", 24))
    ok = send_dohss_latest(config, db_path, since_hours=since_hours)
    print("DoHSS latest changes sent." if ok else "DoHSS latest changes failed.")


def cmd_tg_login(config):
    from scrapers.telegram_hiring import run_login
    run_login(config)


def cmd_fetch_telegram(config):
    from database.db import init_db
    from scrapers.telegram_hiring import run
    db_path = get_db_path(config)
    init_db(db_path)
    stats = run(config, db_path)
    if stats.get("skipped"):
        print("telegram_scraper is disabled. Set telegram_scraper.enabled: true in config.yaml")
        return
    print("\nTelegram harvest complete:")
    print(f"  seeded channels : {stats.get('seeded', 0)}")
    print(f"  messages read   : {stats.get('messages', 0)}")
    print(f"  contacts saved  : {stats.get('contacts', 0)}")
    print(f"  channels via fwd: {stats.get('fwd_channels', 0)}\n")


def cmd_extract_placement_contacts(config):
    """(Re)mine named SPOC/recruiter contacts from already-downloaded placement PDFs."""
    from database import db
    from enrichment.placement_contacts import extract_from_pdf, save_placement_contacts
    db_path = get_db_path(config)
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    pdfs = [dict(r) for r in conn.execute(
        "SELECT * FROM placement_pdfs WHERE local_path IS NOT NULL AND local_path<>''").fetchall()]
    conn.close()
    if not pdfs:
        print("No downloaded placement PDFs found. Run `python main.py fetch-now` first.")
        return
    total = 0
    for row in pdfs:
        try:
            contacts = extract_from_pdf(row["local_path"], institute=row["institute"], source_url=row["url"])
            saved = save_placement_contacts(db_path, row["institute"], contacts)
            total += saved
            print(f"  {row['filename']}: {len(contacts)} found, {saved} saved")
        except Exception as exc:
            print(f"  {row['filename']}: error {exc}")
    print(f"\nSaved {total} named placement contact(s) to hr_contacts.\n")


def cmd_linkedin_hr_search(config):
    """
    Search LinkedIn for HR / Talent Acquisition contacts at every company
    already in the pipeline (jobs table + placement_data table).

    Discovered profiles are saved to both hr_contacts and contacts tables.

    Requires at least one search API key in config.yaml:
      search:
        serpapi_key: "..."   # https://serpapi.com  (free tier: 100 searches/month)
        brave_api_key: "..."  # https://api.search.brave.com  (free tier: 2k req/month)
        bing_api_key: "..."   # Azure Cognitive Services - Bing Search
    """
    from database import db
    from enrichment.linkedin_hr_search import search_hr_for_company, has_search_provider

    db_path = get_db_path(config)
    db.init_db(db_path)

    if not has_search_provider(config):
        print(
            "\nNo search API key configured — LinkedIn HR search requires one of:\n"
            "  search:\n"
            "    serpapi_key:   \"<key>\"   # https://serpapi.com  (100 free/month)\n"
            "    brave_api_key: \"<key>\"   # https://api.search.brave.com  (2k free/month)\n"
            "    bing_api_key:  \"<key>\"   # Azure Bing Search\n\n"
            "Add the key to config.yaml, then re-run:\n"
            "  python main.py linkedin-hr-search\n"
        )
        return

    limit = 50
    if len(sys.argv) > 2:
        try:
            limit = int(sys.argv[2])
        except ValueError:
            print("Usage: python main.py linkedin-hr-search [limit]")
            return

    # Collect companies from jobs table
    job_companies = db.get_recent_companies_from_jobs(db_path, limit=limit)

    # Also collect companies from placement_data table (discovered from PDFs)
    conn = db.get_connection(db_path)
    placement_rows = conn.execute(
        """
        SELECT company AS name, MAX(added_at) AS last_found
        FROM placement_data
        WHERE company IS NOT NULL AND trim(company) != ''
        GROUP BY lower(trim(company))
        ORDER BY MAX(added_at) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()

    placement_companies = [{"name": r["name"], "source_url": ""} for r in placement_rows]

    # Merge and deduplicate
    seen_keys = set()
    companies = []
    for c in job_companies + placement_companies:
        key = _company_key(c.get("name", ""))
        if key and key not in seen_keys:
            seen_keys.add(key)
            companies.append(c)

    if not companies:
        print("No companies found in pipeline DB.  Run `python main.py fetch-now` first.")
        return

    print(f"\nSearching LinkedIn HRs for {len(companies)} compan(y/ies)...\n")

    total_hr = 0
    total_contacts = 0

    for idx, company_row in enumerate(companies, start=1):
        company = company_row.get("name", "").strip()
        if not company:
            continue

        contacts = search_hr_for_company(company, config=config)
        saved_hr = 0
        saved_contacts = 0

        for contact in contacts:
            # Save to hr_contacts (outreach table)
            hr_record = {
                "name": contact.get("contact_name") or "Unknown",
                "company": company,
                "designation": contact.get("role_title") or "",
                "email": contact.get("email") or "",
                "linkedin_url": contact.get("linkedin_url") or "",
                "source": "linkedin_hr_search",
                "notes": (contact.get("evidence_text") or "")[:300],
            }
            if hr_record["linkedin_url"] or hr_record["email"]:
                if db.insert_hr(db_path, hr_record):
                    saved_hr += 1

            # Save to contacts table (enrichment layer)
            if db.insert_contact(db_path, contact):
                saved_contacts += 1

        total_hr += saved_hr
        total_contacts += saved_contacts

        status = f"{len(contacts)} profile(s) found, {saved_hr} HR saved, {saved_contacts} contacts saved"
        print(f"[{idx}/{len(companies)}] {company:40s}  {status}")

    print(f"\nDone. {total_hr} HR contact(s) added to hr_contacts, {total_contacts} to contacts.\n")


def _parse_flags(args):
    """Parse --key value and --flag style args from a list, returns (flags_dict, positional_list)."""
    flags = {}
    positional = []
    i = 0
    while i < len(args):
        if args[i].startswith("--"):
            key = args[i][2:]
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                flags[key] = args[i + 1]
                i += 2
            else:
                flags[key] = True
                i += 1
        else:
            positional.append(args[i])
            i += 1
    return flags, positional


def cmd_discover_target_companies(config):
    """
    Populate target_companies and company_roles from the DoHSS seed universe.

    Usage: python main.py discover-target-companies [--institutes dse,isi,igidr,mse,iitd] [--limit 300]
    """
    from database import db
    from enrichment.target_company_discovery import discover_target_companies

    db_path = get_db_path(config)
    db.init_db(db_path)

    flags, _ = _parse_flags(sys.argv[2:])
    limit = int(flags.get("limit", 300))
    inst_arg = flags.get("institutes", "")
    institutes = [i.strip() for i in inst_arg.split(",")] if inst_arg else []

    print(f"\nDiscover DoHSS target companies (institutes={institutes or 'all'}, limit={limit})...")
    companies_saved, roles_saved = discover_target_companies(db_path, institutes=institutes, limit=limit)
    stats = db.get_target_company_stats(db_path)
    print(f"\n  Companies upserted this run : {companies_saved}")
    print(f"  Company roles inserted      : {roles_saved}")
    print(f"  Total target companies now  : {stats['total']}")
    print(f"  Total company roles now     : {stats['total_roles']}")
    print(f"\n  By category:")
    for cat in stats["by_category"]:
        print(f"    {cat['company_category']:<22s}: {cat['n']}")
    print(f"\n  By priority:")
    print(f"    High (>=90)  : {stats['by_priority']['high']}")
    print(f"    Mid  (70-89) : {stats['by_priority']['mid']}")
    print(f"    Low  (<70)   : {stats['by_priority']['low']}")
    print()
    try:
        from notifier.telegram import maybe_send_dohss_update
        maybe_send_dohss_update(config, db_path, reason="discover-target-companies")
    except Exception as exc:
        logger.info("DoHSS Telegram update skipped: %s", exc)


def cmd_target_company_status(config):
    """Print a summary table of all DoHSS target companies."""
    from database import db
    from rich.console import Console
    from rich.table import Table
    from rich import box

    db_path = get_db_path(config)
    db.init_db(db_path)
    rows = db.get_target_companies(db_path, min_priority=0, limit=500)
    stats = db.get_target_company_stats(db_path)

    t = Table(title=f"DoHSS Target Companies ({stats['total']} total)", box=box.ROUNDED, header_style="bold cyan")
    t.add_column("Priority", justify="right", max_width=8)
    t.add_column("Company", style="white", max_width=32)
    t.add_column("Category", style="cyan", max_width=20)
    t.add_column("Size", style="dim", max_width=8)
    t.add_column("Institutes", style="yellow", max_width=30)
    t.add_column("Role Families", style="green", max_width=36)

    import json
    for r in rows:
        try:
            insts = json.loads(r["source_institutes"]) if r["source_institutes"] else []
        except Exception:
            insts = []
        try:
            fams = json.loads(r["likely_role_families"]) if r["likely_role_families"] else []
        except Exception:
            fams = []
        t.add_row(
            str(r["priority_score"]),
            r["company"] or "-",
            r["company_category"] or "-",
            r["size_tier"] or "-",
            ", ".join(insts[:4]),
            ", ".join(f[:18] for f in fams[:3]),
        )

    Console().print(t)


def cmd_export_target_companies(config):
    """Export target_companies and company_roles to CSV."""
    from database import db
    import json

    db_path = get_db_path(config)
    db.init_db(db_path)
    export_dir = Path("exports")
    export_dir.mkdir(parents=True, exist_ok=True)

    companies = db.get_target_companies(db_path, limit=5000)
    out = export_dir / "target_companies_dohss.csv"
    fields = [
        "id", "company", "normalized_company", "domain", "company_category",
        "size_tier", "source_institutes", "likely_role_families",
        "specific_roles_offered", "evidence_url", "evidence_text",
        "priority_score", "confidence_score", "notes", "created_at", "updated_at",
    ]
    written = _write_csv_with_fallback(out, companies, fields)
    print(f"Exported {len(companies)} target companies -> {written}")

    roles = db.get_company_roles(db_path, limit=50000)
    out2 = export_dir / "company_roles_dohss.csv"
    fields2 = [
        "id", "company_id", "company", "role_title", "role_family",
        "source_institute", "evidence_url", "confidence_score", "notes", "created_at",
    ]
    written2 = _write_csv_with_fallback(out2, roles, fields2)
    print(f"Exported {len(roles)} company roles   -> {written2}")
    try:
        from notifier.telegram import maybe_send_dohss_update
        maybe_send_dohss_update(config, db_path, reason="export-target-companies")
    except Exception as exc:
        logger.info("DoHSS Telegram update skipped: %s", exc)


def cmd_discover_hr_dohss(config):
    """
    Search LinkedIn for HR/TA contacts at DoHSS target companies.

    Usage: python main.py discover-hr [--fit dohss] [--limit 500] [--min-priority 85]

    Requires a search API key in config.yaml:
      search:
        serpapi_key / brave_api_key / bing_api_key
    """
    from database import db
    from enrichment.dohss_hr_search import search_hr_dohss, has_search_provider

    db_path = get_db_path(config)
    db.init_db(db_path)

    flags, _ = _parse_flags(sys.argv[2:])
    limit = int(flags.get("limit", 500))
    min_priority = int(flags.get("min-priority", 80))

    if not has_search_provider(config):
        print(
            "\nNo search API key configured — discover-hr requires one of:\n"
            "  search:\n"
            "    serpapi_key:   \"<key>\"   # https://serpapi.com  (100 free/month)\n"
            "    brave_api_key: \"<key>\"   # https://api.search.brave.com  (2k free/month)\n"
            "    bing_api_key:  \"<key>\"   # Azure Bing Search\n\n"
            "Add the key to config.yaml, then re-run:\n"
            "  python main.py discover-hr --fit dohss\n"
        )
        return

    companies = db.get_target_companies(db_path, min_priority=min_priority, limit=limit)
    if not companies:
        print("No target companies found. Run `python main.py discover-target-companies` first.")
        return

    import json
    print(f"\nSearching HR contacts for {len(companies)} target companies (min-priority {min_priority})...\n")

    total_saved = 0
    for idx, company_row in enumerate(companies, start=1):
        company = company_row["company"]
        try:
            fams = json.loads(company_row["likely_role_families"]) if company_row["likely_role_families"] else []
        except Exception:
            fams = []
        try:
            insts = json.loads(company_row["source_institutes"]) if company_row["source_institutes"] else []
        except Exception:
            insts = []

        contacts = search_hr_dohss(company, config=config, role_families=fams, source_institutes=insts)
        saved = 0
        for c in contacts:
            if db.insert_hr_extended(db_path, c):
                saved += 1
        total_saved += saved
        status = f"{len(contacts)} found, {saved} saved"
        print(f"[{idx:3d}/{len(companies)}] {company:<40s}  {status}")

    print(f"\nDone. {total_saved} new HR contact(s) added.\n")
    print("Run `python main.py hr-status --fit dohss` to review.\n")
    try:
        from notifier.telegram import maybe_send_dohss_update
        maybe_send_dohss_update(config, db_path, reason="discover-hr")
    except Exception as exc:
        logger.info("DoHSS Telegram update skipped: %s", exc)


def cmd_hr_status(config):
    """
    Show DoHSS HR contacts summary.

    Usage: python main.py hr-status [--fit dohss] [--min-confidence 70]
    """
    from database import db
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    import json

    db_path = get_db_path(config)
    db.init_db(db_path)

    flags, _ = _parse_flags(sys.argv[2:])
    min_confidence = int(flags.get("min-confidence", 70))

    stats = db.get_hr_stats_dohss(db_path)
    rows = db.get_hr_contacts_dohss(db_path, min_confidence=min_confidence, limit=200)

    Console().print(Panel(
        f"[bold]Total HR contacts:[/bold] {stats['total']}  |  "
        f"[bold green]High confidence (>=90):[/bold green] {stats['high_confidence']}  |  "
        f"[bold yellow]Mid (70-89):[/bold yellow] {stats['mid_confidence']}  |  "
        f"[dim]Needs review: {stats['needs_review']}  |  Auto-ready: {stats['auto_ready']}[/dim]",
        title="[bold cyan]DoHSS HR Contacts Status",
        border_style="cyan",
    ))

    t = Table(
        title=f"HR Contacts (confidence >= {min_confidence}, showing top 200)",
        box=box.ROUNDED, header_style="bold magenta",
    )
    t.add_column("Score", justify="right", max_width=5)
    t.add_column("Name", style="white", max_width=22)
    t.add_column("Company", style="cyan", max_width=28)
    t.add_column("Designation", style="yellow", max_width=32)
    t.add_column("Role Families", style="green", max_width=28)
    t.add_column("LinkedIn", style="dim", max_width=36)

    for r in rows:
        try:
            fams = json.loads(r["relevant_role_families"]) if r.get("relevant_role_families") else []
        except Exception:
            fams = []
        t.add_row(
            str(r.get("confidence_score") or "-"),
            r["name"] or "-",
            r["company"] or "-",
            (r.get("designation") or "-")[:32],
            ", ".join(f[:14] for f in fams[:2]),
            (r.get("linkedin_url") or "-")[-36:],
        )

    Console().print(t)
    print(f"\nBy company (top 15):")
    for entry in stats["by_company"][:15]:
        print(f"  {entry['company']:<35s}: {entry['n']} HR contact(s)")
    print()


def cmd_export_hr_contacts_dohss(config):
    """
    Export DoHSS-aligned HR contacts to CSV.

    Usage: python main.py export-hr-contacts [--fit dohss] [--min-confidence 70]
    """
    from database import db

    db_path = get_db_path(config)
    db.init_db(db_path)

    flags, _ = _parse_flags(sys.argv[2:])
    min_confidence = int(flags.get("min-confidence", 70))

    export_dir = Path("exports")
    export_dir.mkdir(parents=True, exist_ok=True)
    out = export_dir / "hr_contacts_dohss.csv"

    rows = db.get_hr_contacts_dohss(db_path, min_confidence=min_confidence, limit=100000)
    fields = [
        "id", "name", "company", "designation", "email", "linkedin_url",
        "source", "company_role_fit", "relevant_role_families", "source_institutes",
        "verified_current_company", "discovered_for_role_family", "evidence_url",
        "confidence_score", "manual_review_required", "allowed_for_auto_outreach",
        "notes", "added_at",
    ]
    written = _write_csv_with_fallback(out, rows, fields)
    print(f"Exported {len(rows)} HR contacts (confidence>={min_confidence}) -> {written}")
    try:
        from notifier.telegram import maybe_send_dohss_update
        maybe_send_dohss_update(config, db_path, reason="export-hr-contacts")
    except Exception as exc:
        logger.info("DoHSS Telegram update skipped: %s", exc)


def cmd_stats(config):
    from database.db import init_db, get_job_stats, get_outreach_stats, get_contact_stats
    db_path = get_db_path(config)
    init_db(db_path)
    stats = get_job_stats(db_path)
    contacts = get_contact_stats(db_path)
    print(f"\nJobs: {stats['total']} total  |  {stats['today']} today")
    print(f"Contacts: {contacts['total']} total  |  {contacts['high_confidence']} high confidence")
    for s in stats.get("by_source", []):
        print(f"  {s['source']}: {s['n']}")
    for s in get_outreach_stats(db_path):
        print(f"  outreach/{s['status']}: {s['n']}")
    print()


def cmd_enrich_jobs(config):
    from application_pipeline.cli import enrich_jobs
    results = enrich_jobs(config, sys.argv[2:])
    for row in results:
        suffix = f" - {row['error']}" if row.get("error") else ""
        print(f"Job {row['job_id']}: {row['status']} - {row['company']} / {row['title']}{suffix}")
    print(f"Enriched {len(results)} job(s); failures are recorded and did not stop the batch.")


def cmd_score_jobs(config):
    from application_pipeline.cli import display_result, score_jobs
    results = score_jobs(config, sys.argv[2:])
    for result in results:
        print(display_result(result))
        print()
    print(f"Scored {len(results)} job(s). Candidate-derived results are in the private database only.")


def cmd_prepare_applications(config):
    from application_pipeline.cli import display_result, prepare_applications
    results, errors = prepare_applications(config, sys.argv[2:])
    for result in results:
        print("\n" + display_result(result))
    if errors:
        print("\nNon-fatal errors:")
        for error in errors:
            print(f"  - {error}")
    print(f"\nPrepared {len(results)} application record(s). No applications were submitted.")


def cmd_review_applications(config):
    from application_pipeline.cli import json_list, review_applications
    rows = review_applications(config, sys.argv[2:])
    if not rows:
        print("No private application-preparation records found.")
        return
    for row in rows:
        unanswered = json_list(row.get("unanswered_questions"))
        print(
            f"Application {row['id']} | Job {row['job_id']} | {row['status']} | "
            f"Match {row.get('match_score') if row.get('match_score') is not None else '-'} | "
            f"ATS {row.get('ats_provider') or 'Unknown'} | "
            f"Unanswered {', '.join(unanswered) if unanswered else '-'}"
        )


def cmd_job(config):
    from application_pipeline.cli import job_detail, json_list
    if len(sys.argv) < 3:
        print("Usage: python main.py job <id>")
        return
    try:
        job_id = int(sys.argv[2])
    except ValueError:
        print("Job id must be an integer.")
        return
    detail = job_detail(config, job_id)
    if not detail:
        print(f"Job {job_id} not found.")
        return
    job = detail["job"]
    print(f"Job {job['id']}: {job.get('title') or '-'} at {job.get('company') or '-'}")
    print(f"Location: {job.get('location') or '-'} | Arrangement: {job.get('remote_type') or 'Unknown'}")
    print(f"ATS: {job.get('ats_provider') or 'Unknown'} | URL: {job.get('application_url') or job.get('url') or '-'}")
    print(f"Enrichment: {job.get('enrichment_status') or 'PENDING'}")
    assessment = detail.get("assessment")
    if assessment:
        print(
            f"Eligibility: {assessment['eligibility_status']} | Match: {assessment.get('match_score')}/100 | "
            f"Recommendation: {assessment.get('application_recommendation')}"
        )
    application = detail.get("application")
    if application:
        unanswered = json_list(application.get("unanswered_questions"))
        print(f"Application status: {application['status']} | Missing answers: {', '.join(unanswered) if unanswered else '-'}")


COMMANDS = {
    "run": cmd_run, "fetch-now": cmd_fetch_now, "dashboard": cmd_dashboard,
    "add-hr": cmd_add_hr, "enrich-contacts": cmd_enrich_contacts,
    "discover-contacts": cmd_discover_contacts, "contacts": cmd_contacts,
    "internet-discover-contacts": cmd_internet_discover_contacts,
    "linkedin-hr-search": cmd_linkedin_hr_search,
    "import-company-seeds": cmd_import_company_seeds,
    "resolve-companies": cmd_resolve_companies,
    "export-companies": cmd_export_companies,
    "export-contacts": cmd_export_contacts, "export-guesses": cmd_export_guesses,
    "cleanup-contacts": cmd_cleanup_contacts,
    "debug-company-contacts": cmd_debug_company_contacts,
    "extract-placement-contacts": cmd_extract_placement_contacts,
    "test-telegram": cmd_test_telegram,
    "telegram-test": cmd_telegram_test,
    "telegram-dohss-digest": cmd_telegram_dohss_digest,
    "telegram-dohss-companies": cmd_telegram_dohss_companies,
    "telegram-dohss-hr": cmd_telegram_dohss_hr,
    "telegram-dohss-latest": cmd_telegram_dohss_latest,
    "tg-login": cmd_tg_login,
    "fetch-telegram": cmd_fetch_telegram,
    "stats": cmd_stats,
    "enrich-jobs": cmd_enrich_jobs,
    "score-jobs": cmd_score_jobs,
    "prepare-applications": cmd_prepare_applications,
    "review-applications": cmd_review_applications,
    "job": cmd_job,
    # DoHSS target company + HR commands
    "discover-target-companies": cmd_discover_target_companies,
    "target-company-status": cmd_target_company_status,
    "export-target-companies": cmd_export_target_companies,
    "discover-hr": cmd_discover_hr_dohss,
    "hr-status": cmd_hr_status,
    "export-hr-contacts": cmd_export_hr_contacts_dohss,
}

HELP = """
DoHSS Placement Intelligence Pipeline
--------------------------------------
  python main.py run            Start automated pipeline (runs forever)
  python main.py fetch-now      Scrape all sources once and exit
  python main.py dashboard      Terminal dashboard
  python main.py stats          Quick stats

--- DoHSS Target Company + HR Discovery ---
  python main.py discover-target-companies [--institutes dse,isi,igidr,mse,iitd] [--limit 300]
                              Populate DoHSS-aligned target company universe from seed list
  python main.py target-company-status
                              Show all target companies with priority scores
  python main.py export-target-companies
                              Export to exports/target_companies_dohss.csv + company_roles_dohss.csv
  python main.py discover-hr [--fit dohss] [--limit 500] [--min-priority 80]
                              Search LinkedIn for HR/TA at target companies
                              (requires SerpAPI / Brave / Bing key in config.yaml)
  python main.py hr-status [--fit dohss] [--min-confidence 70]
                              Show HR contacts summary table
  python main.py export-hr-contacts [--fit dohss] [--min-confidence 70]
                              Export to exports/hr_contacts_dohss.csv

--- Contacts & Enrichment ---
  python main.py add-hr         Manually add an HR contact
  python main.py enrich-contacts [limit]
                              Enrich recent opportunities with public pages + search
  python main.py discover-contacts [limit]
                              Discover contacts using search providers
  python main.py internet-discover-contacts [limit]
                              Bounded internet discovery
  python main.py linkedin-hr-search [limit]
                              Legacy LinkedIn HR search at pipeline companies
  python main.py contacts [min_confidence] [--include-guesses]
                              Show enriched contacts
  python main.py export-contacts [min_confidence]
                              Export to exports/contacts.csv
  python main.py export-guesses
                              Export guessed role emails
  python main.py cleanup-contacts
                              Remove invalid contacts and rescore
  python main.py debug-company-contacts "Company Name"

--- Job Application Preparation (never submits) ---
  python main.py enrich-jobs [--limit 25] [--all]
                              Fetch and parse public job details; failures are non-fatal
  python main.py score-jobs [--limit 25]
                              Run private hard eligibility + deterministic matching
  python main.py prepare-applications [--limit 25] [--no-discovery]
                              Discover, enrich, score, and prepare; never submit
  python main.py review-applications [--limit 100]
                              List private READY_TO_APPLY / NEEDS_REVIEW records
  python main.py job <id>     Show one public job plus private preparation status

--- Companies ---
  python main.py import-company-seeds companies.csv
  python main.py resolve-companies [limit]
  python main.py export-companies

--- Placement PDFs ---
  python main.py extract-placement-contacts

--- Telegram ---
  python main.py test-telegram
  python main.py telegram-test
  python main.py telegram-dohss-digest
  python main.py telegram-dohss-companies --limit 30
  python main.py telegram-dohss-hr --min-confidence 70 --limit 50
  python main.py telegram-dohss-latest --since-hours 24
  python main.py tg-login
  python main.py fetch-telegram

Sources: DSE | ISI | IGIDR | MSE | IITD | TISS | JNU | XLRI | LinkedIn | Naukri | Internshala
"""

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(HELP)
        sys.exit(0)
    cmd = sys.argv[1].lower()
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}\n{HELP}")
        sys.exit(1)
    try:
        cfg = load_config()
    except FileNotFoundError:
        print("config.yaml not found. Run from the project root directory.")
        sys.exit(1)
    try:
        COMMANDS[cmd](cfg)
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}")
        sys.exit(2)
