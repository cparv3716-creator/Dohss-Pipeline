#!/usr/bin/env python3
"""
main.py - DoHSS IITM Placement Intelligence Pipeline

Commands:
  python main.py run            Start full automated pipeline (loops forever)
  python main.py fetch-now      Run all scrapers once and exit
  python main.py dashboard      Show terminal dashboard
  python main.py add-hr         Manually add an HR contact
  python main.py test-telegram  Test Telegram bot connection
  python main.py stats          Print quick stats
"""
import logging
import sys
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


def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_db_path(config):
    return config.get("database", {}).get("path", "data/pipeline.db")


def cmd_run(config):
    from database.db import init_db
    init_db(get_db_path(config))
    import scheduler
    scheduler.start(config, get_db_path(config))


def cmd_fetch_now(config):
    from database.db import init_db, insert_job, get_unnotified_jobs, mark_jobs_notified
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
    for name, jobs in sources:
        new = sum(1 for j in jobs if insert_job(db_path, j))
        total_new += new
        print(f"  {name:15s}: {new:3d} new  (scraped {len(jobs)})")

    print(f"\n  Total new this run: {total_new}")
    print("\nChecking institute pages...")
    inst_results = run_all_institutes(config, db_path)
    changed = [r for r in inst_results if r["changed"] or r["new_pdfs"]]
    print(f"  {len(changed)} institute(s) updated")

    print("Processing PDFs...")
    process_new_pdfs(config, db_path)

    print("Sending Telegram notifications...")
    unnotified = get_unnotified_jobs(db_path)
    if unnotified:
        notify_new_jobs(config, unnotified)
        mark_jobs_notified(db_path, [j["id"] for j in unnotified])
    notify_institute_changes(config, changed)

    print("\nDone! Run  python main.py dashboard  to see results.\n")


def cmd_dashboard(config):
    from database.db import init_db
    from dashboard.terminal_dash import show
    db_path = get_db_path(config)
    init_db(db_path)
    show(db_path)


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


def cmd_stats(config):
    from database.db import init_db, get_job_stats, get_outreach_stats
    db_path = get_db_path(config)
    init_db(db_path)
    stats = get_job_stats(db_path)
    print(f"\nJobs: {stats['total']} total  |  {stats['today']} today")
    for s in stats.get("by_source", []):
        print(f"  {s['source']}: {s['n']}")
    for s in get_outreach_stats(db_path):
        print(f"  outreach/{s['status']}: {s['n']}")
    print()


COMMANDS = {
    "run": cmd_run, "fetch-now": cmd_fetch_now, "dashboard": cmd_dashboard,
    "add-hr": cmd_add_hr, "test-telegram": cmd_test_telegram, "stats": cmd_stats,
}

HELP = """
DoHSS IITM Placement Pipeline
-------------------------------
  python main.py run            Start automated pipeline (runs forever)
  python main.py fetch-now      Scrape all sources once and exit
  python main.py dashboard      Terminal dashboard
  python main.py add-hr         Manually add an HR contact
  python main.py test-telegram  Test Telegram bot
  python main.py stats          Quick stats

Sources: LinkedIn | Naukri | Internshala | Google Alerts | DSE/ISI/TISS/JNU/XLRI pages
"""

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h","--help","help"):
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
