"""
notifier/telegram.py - Send alerts to a Telegram group.

Setup:
  1. Talk to @BotFather -> /newbot -> copy token -> paste in config.yaml
  2. Add the bot to your placement team Telegram group
  3. Send a message in the group, then open:
     https://api.telegram.org/bot<TOKEN>/getUpdates
  4. Copy the chat id (negative number) -> paste in config.yaml
"""
import logging
from datetime import datetime
from html import escape

import requests

logger = logging.getLogger(__name__)


def _send(token, chat_id, text):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        return resp.ok
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False


def _is_configured(config):
    tg = config.get("telegram", {})
    token   = tg.get("bot_token", "")
    chat_id = tg.get("chat_id", "")
    if not tg.get("enabled", True):
        return False
    if "YOUR_BOT_TOKEN" in token or not token:
        logger.info("Telegram not configured (token missing)")
        return False
    if "YOUR_CHAT_ID" in chat_id or not chat_id:
        logger.info("Telegram not configured (chat_id missing)")
        return False
    return True


def _contact_lines(config, job, db_path):
    if not db_path:
        return []
    try:
        from database import db
        enrichment = config.get("enrichment", {})
        min_score = int(enrichment.get("min_confidence_for_alert", 70))
        max_contacts = min(int(enrichment.get("max_contacts_per_opportunity", 3)), 2)
        contacts = db.get_contacts_for_opportunity(
            db_path,
            job["id"],
            min_confidence=min_score,
            limit=max_contacts,
        )
    except Exception as exc:
        logger.info("Could not load contacts for Telegram alert: %s", exc)
        return []

    lines = []
    for contact in contacts:
        source_type = escape(contact.get("source_type") or "public source")
        source_url = escape(contact.get("source_url") or "")
        email = escape(contact.get("email") or "")
        score = int(contact.get("confidence_score") or 0)
        lines.append(
            f"  Contact: {email} ({score}/100, {source_type})\n"
            f"  Source: <a href='{source_url}'>public page</a>"
        )
    return lines


def notify_new_jobs(config, jobs, db_path=None):
    if not _is_configured(config) or not jobs:
        return
    tg = config["telegram"]
    for i in range(0, len(jobs), 5):
        chunk = jobs[i:i+5]
        lines = [f"<b>{len(jobs)} new job lead(s) found!</b>\n"]
        for j in chunk:
            icon = "LinkedIn" if j["source"]=="linkedin" else j["source"].capitalize()
            lines.append(
                f"<b>{escape(j['title'])}</b>\n"
                f"  {escape(j['company'])} | {escape(j.get('location','') or '')}\n"
                f"  [{escape(icon)}] <a href='{escape(j['url'])}'>View</a>"
            )
            contact_lines = _contact_lines(config, j, db_path)
            if contact_lines:
                lines.extend(contact_lines)
            lines.append("")
        _send(tg["bot_token"], tg["chat_id"], "\n".join(lines))


def notify_institute_changes(config, results):
    if not _is_configured(config):
        return
    tg = config["telegram"]
    for r in results:
        if not r["changed"] and not r["new_pdfs"]:
            continue
        lines = [f"<b>{r['name']} update detected!</b>"]
        if r["changed"]:
            lines.append("Placement page content changed.")
        if r["new_pdfs"]:
            lines.append(f"{len(r['new_pdfs'])} new PDF(s):")
            for p in r["new_pdfs"]:
                lines.append(f"  <a href='{p['url']}'>{p['filename']}</a>")
        _send(tg["bot_token"], tg["chat_id"], "\n".join(lines))


def send_daily_summary(config, db_path):
    if not _is_configured(config):
        return
    from database import db
    tg    = config["telegram"]
    stats = db.get_job_stats(db_path)
    out   = db.get_outreach_stats(db_path)
    today = datetime.now().strftime("%d %b %Y")
    lines = [
        f"<b>DoHSS Placement Digest - {today}</b>",
        f"Jobs: {stats['total']} total | {stats['today']} new today",
        "",
    ]
    for s in stats.get("by_source", []):
        lines.append(f"  {s['source']}: {s['n']}")
    if out:
        lines.append("\nOutreach:")
        for s in out:
            lines.append(f"  {s['status']}: {s['n']}")
    _send(tg["bot_token"], tg["chat_id"], "\n".join(lines))


def send_test_message(config):
    if not _is_configured(config):
        print("Telegram not configured. Edit config.yaml with your bot_token and chat_id.")
        return
    tg = config["telegram"]
    ok = _send(tg["bot_token"], tg["chat_id"],
               "<b>DoHSS Placement Pipeline</b> connected successfully!\n"
               "You will receive job alerts and daily digests here.")
    print("Test message sent!" if ok else "Failed - check bot_token and chat_id in config.yaml")
