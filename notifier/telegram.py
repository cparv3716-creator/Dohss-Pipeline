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
import csv
import json
import os
import sqlite3
from datetime import datetime
from datetime import timedelta
from html import escape
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

MAX_TELEGRAM_CHARS = 3900
STATE_PATH = Path("data/telegram_dohss_state.json")
ROLE_FAMILIES = (
    "data_analytics",
    "finance_risk",
    "policy_research",
    "consulting",
    "esg_sustainability",
    "product_analytics",
    "csr_impact",
)
EXPORT_FILES = (
    ("target_companies_dohss.csv", Path("exports/target_companies_dohss.csv")),
    ("company_roles_dohss.csv", Path("exports/company_roles_dohss.csv")),
    ("hr_contacts_dohss.csv", Path("exports/hr_contacts_dohss.csv")),
)


def _message_chunks(text, max_chars=MAX_TELEGRAM_CHARS):
    chunks = []
    current = []
    length = 0
    for line in str(text or "").splitlines():
        extra = len(line) + 1
        if current and length + extra > max_chars:
            chunks.append("\n".join(current))
            current = []
            length = 0
        if extra > max_chars:
            for start in range(0, len(line), max_chars):
                if current:
                    chunks.append("\n".join(current))
                    current = []
                    length = 0
                chunks.append(line[start:start + max_chars])
            continue
        current.append(line)
        length += extra
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def _send(token, chat_id, text):
    ok = True
    for chunk in _message_chunks(text):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=10,
            )
            ok = resp.ok and ok
            if not resp.ok:
                logger.error("Telegram send failed with HTTP %s", resp.status_code)
        except Exception as e:
            logger.error("Telegram error: %s", e)
            ok = False
    return ok


def _env_or_value(env_name, value):
    env_value = os.environ.get(env_name, "").strip()
    return env_value or str(value or "").strip()


def _first_env_or_value(env_names, value):
    for env_name in env_names:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            return env_value
    return str(value or "").strip()


def _candidate_telegram_configs(config):
    notifications = config.get("notifications", {}) or {}
    return [
        cfg for cfg in (
            config.get("telegram", {}),
            config.get("telegram_bot", {}),
            notifications.get("telegram", {}) if isinstance(notifications, dict) else {},
        )
        if isinstance(cfg, dict)
    ]


def _telegram_settings(config):
    for tg in _candidate_telegram_configs(config):
        token = _first_env_or_value(
            ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "BOT_TOKEN"),
            tg.get("bot_token") or tg.get("token") or tg.get("api_token"),
        )
        chat_id = _first_env_or_value(
            ("TELEGRAM_CHAT_ID", "TELEGRAM_CHANNEL_ID", "TELEGRAM_CHANNEL", "CHAT_ID"),
            tg.get("chat_id") or tg.get("channel_id") or tg.get("channel"),
        )
        if token or chat_id:
            return {"enabled": bool(tg.get("enabled", True)), "bot_token": token, "chat_id": chat_id}
    return {"enabled": False, "bot_token": "", "chat_id": ""}


def _is_placeholder(value):
    upper = str(value or "").upper()
    return not value or "YOUR_" in upper or "PASTE_" in upper or "<KEY>" in upper


def _is_configured(config):
    tg = _telegram_settings(config)
    token = tg.get("bot_token", "")
    chat_id = tg.get("chat_id", "")
    if not tg.get("enabled", True):
        return False
    if _is_placeholder(token):
        logger.info("Telegram not configured (token missing)")
        return False
    if _is_placeholder(chat_id):
        logger.info("Telegram not configured (chat_id missing)")
        return False
    return True


def _send_configured(config, text):
    if not _is_configured(config):
        return False
    tg = _telegram_settings(config)
    return _send(tg["bot_token"], tg["chat_id"], text)


def _parse_json_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        return [part.strip() for part in str(value).split(",") if part.strip()]


def _fmt_list(value, limit=4):
    items = [str(x) for x in _parse_json_list(value) if str(x).strip()]
    return ", ".join(items[:limit]) if items else "-"


def _short(value, limit=160):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def _safe_link(url, label="source"):
    clean = str(url or "").strip()
    if not clean:
        return "-"
    return f"<a href=\"{escape(clean, quote=True)}\">{escape(label)}</a>"


def _csv_row_count(path):
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            return max(sum(1 for _ in csv.reader(f)) - 1, 0)
    except Exception:
        return 0


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _count(conn, table, where="", params=()):
    if not _table_exists(conn, table):
        return 0
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(sql, params).fetchone()[0])


def _rows(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def _dohss_counts(db_path):
    conn = _connect(db_path)
    try:
        company_ids = [int(r["id"]) for r in _rows(conn, "SELECT id FROM target_companies ORDER BY id") if r.get("id") is not None]
        hr_ids = [int(r["id"]) for r in _rows(conn, "SELECT id FROM hr_contacts ORDER BY id") if r.get("id") is not None]
        return {
            "company_count": _count(conn, "target_companies"),
            "role_count": _count(conn, "company_roles"),
            "hr_count": _count(conn, "hr_contacts"),
            "company_ids": company_ids,
            "hr_ids": hr_ids,
        }
    finally:
        conn.close()


def _load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _update_state_from_counts(state, counts):
    state.update(
        {
            "last_digest_sent_at": state.get("last_digest_sent_at", ""),
            "last_company_count": counts["company_count"],
            "last_hr_count": counts["hr_count"],
            "last_role_count": counts["role_count"],
            "last_sent_company_ids": counts["company_ids"],
            "last_sent_hr_ids": counts["hr_ids"],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    return state


def _render_company(row, include_roles=True):
    lines = [
        f"<b>{int(row.get('priority_score') or 0)}</b> - {escape(row.get('company') or '-')}",
        f"  Category: {escape(row.get('company_category') or '-')} | Size: {escape(row.get('size_tier') or '-')}",
        f"  Institutes: {escape(_fmt_list(row.get('source_institutes'), 5))}",
        f"  Families: {escape(_fmt_list(row.get('likely_role_families'), 5))}",
    ]
    if include_roles and row.get("specific_roles_offered"):
        lines.append(f"  Roles: {escape(_short(_fmt_list(row.get('specific_roles_offered'), 5), 180))}")
    if row.get("evidence_url"):
        lines.append(f"  Evidence: {_safe_link(row.get('evidence_url'), 'source')}")
    return "\n".join(lines)


def _render_hr(row):
    auto_ready = bool(row.get("allowed_for_auto_outreach"))
    status = "Auto-ready" if auto_ready else "Manual review required"
    lines = [
        f"<b>{int(row.get('confidence_score') or 0)}</b> - {escape(row.get('name') or '-')}",
        f"  {escape(row.get('company') or '-')} | {escape(_short(row.get('designation') or '-', 90))}",
        f"  Families: {escape(_fmt_list(row.get('relevant_role_families'), 4))}",
        f"  Status: {escape(status)}",
    ]
    if row.get("linkedin_url"):
        lines.append(f"  LinkedIn: {escape(row.get('linkedin_url'))}")
    return "\n".join(lines)


def _get_digest_data(db_path):
    conn = _connect(db_path)
    try:
        categories = _rows(conn, "SELECT company_category, COUNT(*) AS n FROM target_companies GROUP BY company_category ORDER BY n DESC")
        source_institutes = set()
        for row in _rows(conn, "SELECT source_institutes FROM target_companies"):
            source_institutes.update(_parse_json_list(row.get("source_institutes")))
        role_counts = {family: 0 for family in ROLE_FAMILIES}
        for row in _rows(conn, "SELECT role_family, COUNT(*) AS n FROM company_roles GROUP BY role_family"):
            if row.get("role_family") in role_counts:
                role_counts[row["role_family"]] = int(row.get("n") or 0)
        return {
            "target": {
                "total": _count(conn, "target_companies"),
                "high": _count(conn, "target_companies", "priority_score >= 90"),
                "mid": _count(conn, "target_companies", "priority_score >= 75 AND priority_score < 90"),
                "categories": categories,
                "source_institute_count": len([s for s in source_institutes if s]),
            },
            "roles": role_counts,
            "hr": {
                "total": _count(conn, "hr_contacts"),
                "high": _count(conn, "hr_contacts", "confidence_score >= 90"),
                "mid": _count(conn, "hr_contacts", "confidence_score >= 70 AND confidence_score < 90"),
                "manual_review": _count(conn, "hr_contacts", "manual_review_required=1"),
                "auto_ready": _count(conn, "hr_contacts", "allowed_for_auto_outreach=1"),
            },
            "companies": _rows(conn, "SELECT * FROM target_companies ORDER BY priority_score DESC, company LIMIT 20"),
            "hr_contacts": _rows(
                conn,
                """
                SELECT h.*, tc.priority_score AS company_priority
                FROM hr_contacts h
                LEFT JOIN target_companies tc ON lower(trim(h.company)) = tc.normalized_company
                ORDER BY h.confidence_score DESC, tc.priority_score DESC, h.added_at DESC
                LIMIT 20
                """,
            ),
        }
    finally:
        conn.close()


def _render_exports():
    return [
        f"  {escape(label)}: {_csv_row_count(path)} row(s) - {escape(str(path))}"
        for label, path in EXPORT_FILES
    ]


def build_dohss_digest(db_path):
    data = _get_digest_data(db_path)
    target = data["target"]
    hr = data["hr"]
    today = datetime.now().strftime("%d %b %Y")
    lines = [
        f"📊 <b>DoHSS Pipeline Digest</b> - {escape(today)}",
        "",
        "🏢 <b>Target Companies</b>",
        f"  Total target companies: {target['total']}",
        f"  High priority companies >=90: {target['high']}",
        f"  Mid priority companies 75-89: {target['mid']}",
        f"  Categories count: {len(target['categories'])}",
        f"  Source institutes count: {target['source_institute_count']}",
        "",
        "🎯 <b>Role Families</b>",
    ]
    for family in ROLE_FAMILIES:
        lines.append(f"  {escape(family)}: {data['roles'].get(family, 0)}")
    lines.extend(
        [
            "",
            "👥 <b>HR Contacts</b>",
            f"  Total HR contacts: {hr['total']}",
            f"  High confidence >=90: {hr['high']}",
            f"  Mid confidence 70-89: {hr['mid']}",
            f"  Manual review required: {hr['manual_review']}",
            f"  Auto-ready contacts: {hr['auto_ready']}",
            "",
            "🔥 <b>Top Target Companies</b>",
        ]
    )
    lines.extend(_render_company(row) for row in data["companies"][:20]) if data["companies"] else lines.append("  No target companies found.")
    lines.extend(["", "👤 <b>Top HR Contacts</b>"])
    lines.extend(_render_hr(row) for row in data["hr_contacts"][:20]) if data["hr_contacts"] else lines.append("  No HR contacts found.")
    lines.extend(["", "📁 <b>Latest Exports</b>"])
    lines.extend(_render_exports())
    return "\n".join(lines)


def send_dohss_digest(config, db_path, update_state=True):
    ok = _send_configured(config, build_dohss_digest(db_path))
    if ok and update_state:
        state = _update_state_from_counts(_load_state(), _dohss_counts(db_path))
        state["last_digest_sent_at"] = datetime.now().isoformat(timespec="seconds")
        _save_state(state)
    return ok


def send_dohss_companies(config, db_path, limit=30):
    conn = _connect(db_path)
    try:
        rows = _rows(conn, "SELECT * FROM target_companies ORDER BY priority_score DESC, company LIMIT ?", (int(limit),))
    finally:
        conn.close()
    lines = [f"🏢 <b>DoHSS Target Companies</b> - top {len(rows)}", ""]
    lines.extend(_render_company(row) for row in rows)
    return _send_configured(config, "\n".join(lines))


def send_dohss_hr(config, db_path, min_confidence=70, limit=50):
    conn = _connect(db_path)
    try:
        rows = _rows(
            conn,
            """
            SELECT h.*, tc.priority_score AS company_priority
            FROM hr_contacts h
            LEFT JOIN target_companies tc ON lower(trim(h.company)) = tc.normalized_company
            WHERE h.confidence_score >= ?
            ORDER BY h.confidence_score DESC, tc.priority_score DESC, h.added_at DESC
            LIMIT ?
            """,
            (int(min_confidence), int(limit)),
        )
    finally:
        conn.close()
    lines = [f"👥 <b>DoHSS HR Contacts</b> - confidence >= {int(min_confidence)}, showing {len(rows)}", ""]
    lines.extend(_render_hr(row) for row in rows)
    return _send_configured(config, "\n".join(lines))


def _latest_rows(db_path, table, time_col, since_hours, order_col, limit=30):
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, table):
            return []
        cutoff = datetime.now() - timedelta(hours=int(since_hours))
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT 500").fetchall()]
        fresh = []
        for row in rows:
            raw = str(row.get(time_col) or "")
            parsed = None
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(raw.split("+", 1)[0], fmt)
                    break
                except ValueError:
                    continue
            if parsed and parsed >= cutoff:
                fresh.append(row)
        return fresh[:limit]
    finally:
        conn.close()


def send_dohss_latest(config, db_path, since_hours=24):
    companies = _latest_rows(db_path, "target_companies", "updated_at", since_hours, "updated_at", 10)
    roles = _latest_rows(db_path, "company_roles", "created_at", since_hours, "created_at", 10)
    hrs = _latest_rows(db_path, "hr_contacts", "added_at", since_hours, "added_at", 10)
    lines = [f"🆕 <b>DoHSS Latest Changes</b> - last {int(since_hours)} hour(s)", ""]
    lines.append(f"Target companies: {len(companies)}")
    lines.extend(_render_company(row, include_roles=False) for row in companies)
    lines.extend(["", f"Company roles: {len(roles)}"])
    for row in roles:
        lines.append(f"  {escape(row.get('company') or '-')} | {escape(row.get('role_family') or '-')} | {escape(_short(row.get('role_title') or '-', 100))}")
    lines.extend(["", f"HR contacts: {len(hrs)}"])
    lines.extend(_render_hr(row) for row in hrs)
    return _send_configured(config, "\n".join(lines))


def maybe_send_dohss_update(config, db_path, reason="pipeline"):
    if not _is_configured(config):
        return False
    state = _load_state()
    counts = _dohss_counts(db_path)
    last_digest = state.get("last_digest_sent_at")
    due_daily = True
    if last_digest:
        try:
            due_daily = datetime.fromisoformat(last_digest) <= datetime.now() - timedelta(hours=20)
        except ValueError:
            due_daily = True
    if due_daily:
        return send_dohss_digest(config, db_path, update_state=True)

    old_company_ids = set(int(x) for x in state.get("last_sent_company_ids", []))
    old_hr_ids = set(int(x) for x in state.get("last_sent_hr_ids", []))
    new_company_ids = [x for x in counts["company_ids"] if x not in old_company_ids]
    new_hr_ids = [x for x in counts["hr_ids"] if x not in old_hr_ids]
    counts_changed = (
        counts["company_count"] != int(state.get("last_company_count", -1))
        or counts["hr_count"] != int(state.get("last_hr_count", -1))
        or counts["role_count"] != int(state.get("last_role_count", -1))
    )
    if not counts_changed:
        _save_state(_update_state_from_counts(state, counts))
        return False

    conn = _connect(db_path)
    try:
        new_companies = []
        if new_company_ids:
            placeholders = ",".join("?" for _ in new_company_ids[:10])
            new_companies = _rows(conn, f"SELECT * FROM target_companies WHERE id IN ({placeholders}) ORDER BY priority_score DESC, company", tuple(new_company_ids[:10]))
        new_hrs = []
        if new_hr_ids:
            placeholders = ",".join("?" for _ in new_hr_ids[:10])
            new_hrs = _rows(conn, f"SELECT * FROM hr_contacts WHERE id IN ({placeholders}) ORDER BY confidence_score DESC, added_at DESC", tuple(new_hr_ids[:10]))
    finally:
        conn.close()

    lines = [
        f"📌 <b>DoHSS Compact Update</b> ({escape(reason)})",
        f"  Target companies: {counts['company_count']}",
        f"  Company roles: {counts['role_count']}",
        f"  HR contacts: {counts['hr_count']}",
    ]
    if new_companies:
        lines.extend(["", "New target companies:"])
        lines.extend(_render_company(row, include_roles=False) for row in new_companies[:10])
    if new_hrs:
        lines.extend(["", "New HR contacts:"])
        lines.extend(_render_hr(row) for row in new_hrs[:10])
    ok = _send_configured(config, "\n".join(lines))
    if ok:
        _save_state(_update_state_from_counts(state, counts))
    return ok


def _contact_lines(config, job, db_path):
    if not db_path:
        return []
    try:
        from database import db
        enrichment = config.get("enrichment", {})
        min_score = int(
            enrichment.get(
                "min_telegram_contact_confidence",
                enrichment.get("min_confidence_for_alert", 80),
            )
        )
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
        linkedin_url = escape(contact.get("linkedin_url") or "")
        name = escape(contact.get("contact_name") or "")
        role = escape(contact.get("role_title") or "")
        score = int(contact.get("confidence_score") or 0)
        identity = " - ".join(part for part in (name, role) if part)
        reachable = email or (f"<a href='{linkedin_url}'>LinkedIn profile</a>" if linkedin_url else "")
        if not reachable:
            continue
        lines.append(
            f"  Contact: {identity + ' | ' if identity else ''}{reachable} ({score}/100, {source_type})\n"
            f"  Source: <a href='{source_url}'>public source</a>"
        )
    return lines


def notify_new_jobs(config, jobs, db_path=None):
    if not _is_configured(config) or not jobs:
        return
    tg = _telegram_settings(config)
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
    if db_path:
        maybe_send_dohss_update(config, db_path, reason="job notification flow")


def notify_prepared_applications(config, prepared_results):
    """Send opt-in preparation summaries, excluding clearly ineligible jobs."""
    if not _is_configured(config):
        return False
    eligible = [
        result for result in prepared_results
        if result["eligibility"].status != "INELIGIBLE"
        and result.get("application", {}).get("status") in ("READY_TO_APPLY", "NEEDS_REVIEW")
    ]
    if not eligible:
        return False
    lines = [f"<b>{len(eligible)} application preparation update(s)</b>", ""]
    for result in eligible:
        job = result["job"]
        match = result["match"]
        eligibility = result["eligibility"]
        application = result["application"]
        strengths = ", ".join(match.strengths[:3]) or "-"
        gaps = ", ".join(match.gaps[:2]) or "-"
        lines.extend(
            [
                f"<b>{escape(job.get('title') or '-')}</b> at {escape(job.get('company') or '-')}",
                f"  Match: {match.match_score}/100 | {escape(match.recommendation)}",
                f"  Eligibility: {escape(eligibility.status)}",
                f"  Strengths: {escape(_short(strengths, 220))}",
                f"  Gaps: {escape(_short(gaps, 180))}",
                f"  ATS: {escape(job.get('ats_provider') or 'Unknown')}",
                f"  Status: <b>{escape(application.get('status') or 'NEEDS_REVIEW')}</b>",
                f"  Apply: {_safe_link(job.get('application_url') or job.get('url'), 'application link')}",
                "",
            ]
        )
    return _send_configured(config, "\n".join(lines))


def notify_institute_changes(config, results):
    if not _is_configured(config):
        return
    tg = _telegram_settings(config)
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
    tg = _telegram_settings(config)
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
    send_dohss_digest(config, db_path, update_state=True)


def send_test_message(config):
    ok = _send_configured(
        config,
        "<b>DoHSS Placement Pipeline</b> connected successfully!\n"
        "You will receive job alerts and daily digests here.",
    )
    print("Test message sent!" if ok else "Failed - check existing Telegram config in config.yaml/config.local.yaml")
    return ok


def send_dohss_test_message(config):
    ok = _send_configured(config, "✅ DoHSS Pipeline connected to existing Telegram job bot.")
    print("DoHSS Telegram test sent!" if ok else "DoHSS Telegram test failed.")
    return ok
