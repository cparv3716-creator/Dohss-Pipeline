"""
scheduler.py - Runs all scrapers on schedule. Launch with: python main.py run
"""
import logging
import time
import schedule

logger = logging.getLogger(__name__)


def _insert_and_notify(jobs, config, db_path):
    from database import db
    from notifier import telegram
    new_jobs = []
    new_count = 0
    for job in jobs:
        if db.insert_job(db_path, job):
            new_count += 1
            saved = db.get_job_by_url(db_path, job.get("url", ""))
            if saved:
                new_jobs.append(saved)
    if new_count:
        logger.info(f"  -> {new_count} new jobs")
        if config.get("enrichment", {}).get("enabled", True):
            try:
                from enrichment.contact_finder import find_company_contacts
                contacts_added = 0
                for job in new_jobs:
                    for contact in find_company_contacts(
                        job.get("company", ""),
                        source_url=job.get("url", ""),
                        config=config,
                    ):
                        contact["opportunity_id"] = job["id"]
                        contact["company_name"] = job.get("company", contact.get("company_name", ""))
                        if db.insert_contact(db_path, contact):
                            contacts_added += 1
                logger.info("  -> %s contact record(s) enriched", contacts_added)
            except Exception as exc:
                logger.info("  -> Contact enrichment skipped: %s", exc)
        unnotified = db.get_unnotified_jobs(db_path)
        if unnotified and config.get("notifications",{}).get("new_job_found", True):
            telegram.notify_new_jobs(config, unnotified, db_path=db_path)
            db.mark_jobs_notified(db_path, [j["id"] for j in unnotified])
    else:
        logger.info("  -> No new jobs this run.")


def _run_linkedin(config, db_path):
    logger.info("==> LinkedIn")
    from scrapers.linkedin_jobs import run_all_keywords
    _insert_and_notify(run_all_keywords(config), config, db_path)


def _run_naukri(config, db_path):
    logger.info("==> Naukri")
    from scrapers.naukri import run_all_keywords
    _insert_and_notify(run_all_keywords(config), config, db_path)


def _run_internshala(config, db_path):
    logger.info("==> Internshala")
    from scrapers.internshala import run_all_keywords
    _insert_and_notify(run_all_keywords(config), config, db_path)


def _run_google_alerts(config, db_path):
    logger.info("==> Google Alerts")
    from scrapers.google_alerts import run_all_feeds
    _insert_and_notify(run_all_feeds(config), config, db_path)


def _run_institute_monitor(config, db_path):
    logger.info("==> Institute monitor")
    from scrapers.institute_monitor import run_all_institutes
    from scrapers.pdf_parser import process_new_pdfs
    from notifier import telegram
    results = run_all_institutes(config, db_path)
    if config.get("notifications",{}).get("new_institute_update", True):
        telegram.notify_institute_changes(config, results)
    process_new_pdfs(config, db_path)


def _run_daily_summary(config, db_path):
    logger.info("==> Daily summary")
    from notifier import telegram
    telegram.send_daily_summary(config, db_path)


def start(config, db_path):
    li_hrs   = config.get("linkedin",    {}).get("check_interval_hours", 6)
    nk_hrs   = config.get("naukri",      {}).get("check_interval_hours", 6)
    is_hrs   = config.get("internshala", {}).get("check_interval_hours", 8)
    ga_hrs   = config.get("google_alerts",{}).get("check_interval_hours", 4)
    in_hrs   = config.get("institutes",  {}).get("check_interval_hours", 24)
    sum_time = config.get("notifications",{}).get("daily_summary_time", "09:00")

    schedule.every(li_hrs).hours.do(_run_linkedin,          config=config, db_path=db_path)
    schedule.every(nk_hrs).hours.do(_run_naukri,            config=config, db_path=db_path)
    schedule.every(is_hrs).hours.do(_run_internshala,       config=config, db_path=db_path)
    schedule.every(ga_hrs).hours.do(_run_google_alerts,     config=config, db_path=db_path)
    schedule.every(in_hrs).hours.do(_run_institute_monitor, config=config, db_path=db_path)
    schedule.every().day.at(sum_time).do(_run_daily_summary, config=config, db_path=db_path)

    logger.info(f"Scheduler running: LinkedIn={li_hrs}h | Naukri={nk_hrs}h | Internshala={is_hrs}h | Alerts={ga_hrs}h | Institutes={in_hrs}h | Digest={sum_time}")

    # Run everything once immediately on startup
    _run_linkedin(config, db_path)
    _run_naukri(config, db_path)
    _run_internshala(config, db_path)
    _run_google_alerts(config, db_path)
    _run_institute_monitor(config, db_path)

    while True:
        schedule.run_pending()
        time.sleep(30)
