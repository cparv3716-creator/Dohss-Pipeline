"""
scrapers/telegram_hiring.py
===========================
Telethon-based hiring-channel harvester.

End to end, with zero manual work after first login:
  1. DISCOVER channels - seeds from config + global keyword search + a
                         "snowball" crawl that follows forwarded-message
                         sources to find new channels automatically.
  2. JOIN channels     - auto-joins discovered channels, throttled with
                         random delays to protect the account.
  3. READ messages     - incrementally pulls new posts from every joined
                         channel (remembers last message id per channel).
  4. EXTRACT contacts  - pulls recruiter emails / phones / @handles out of
                         hiring posts and detects hiring intent.
  5. PERSIST           - writes a job row + contact rows into the existing
                         pipeline DB, via the existing classify_contact()
                         / contact_policy gates.

Everything above the "TELETHON NETWORKING LAYER" banner imports NO telethon,
so it can be unit-tested offline.

First-time setup (one time, interactive):
    python main.py tg-login        # enter phone number + the OTP Telegram sends
Then to run the harvester:
    python main.py fetch-telegram
"""
import asyncio
import logging
import os
import random
import re
import sqlite3

logger = logging.getLogger(__name__)


# ===========================================================================
# PURE / TESTABLE LAYER  (no telethon, no network)
# ===========================================================================

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# Indian mobiles: optional +91/91/0 prefix, 6-9 lead digit + 9 more, allowing one
# space/hyphen separator inside (e.g. 98765 43210).
PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\-\s]?|0)?([6-9]\d{4}[\s\-]?\d{5})(?!\d)")
HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{4,31})\b")

HANDLE_STOPLIST = {
    "gmail", "outlook", "yahoo", "hotmail", "everyone", "channel", "admin",
    "all", "here", "telegram",
}

DEFAULT_INTENT_CUES = (
    "we are hiring", "we're hiring", "share your resume", "share resume",
    "send your cv", "drop your cv", "dm me", "apply at", "mail your resume",
    "send cv to", "looking for", "urgently hiring", "now hiring", "hiring for",
)

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "outlook.com",
    "hotmail.com", "rediffmail.com", "icloud.com", "proton.me", "protonmail.com",
}

ROLE_HINTS = (
    ("research analyst", "Research Analyst"),
    ("policy analyst", "Policy Analyst"),
    ("data analyst", "Data Analyst"),
    ("data scientist", "Data Scientist"),
    ("consultant", "Consultant"),
    ("economist", "Economist"),
    ("software", "Software Engineer"),
    ("developer", "Developer"),
    ("intern", "Intern"),
    ("analyst", "Analyst"),
    ("associate", "Associate"),
    ("engineer", "Engineer"),
    ("manager", "Manager"),
)


def detect_intent(text, cues=None):
    """True if the post reads like an active hiring post (recruiter reachable)."""
    if not text:
        return False
    cues = cues or DEFAULT_INTENT_CUES
    low = text.lower()
    return any(cue in low for cue in cues)


def guess_role(text):
    low = (text or "").lower()
    for needle, label in ROLE_HINTS:
        if needle in low:
            return label
    return ""


def _domain_of(email):
    return email.split("@", 1)[1].lower() if "@" in email else ""


def _company_from_email(email):
    """Best-effort company name + domain from a non-free email address."""
    dom = _domain_of(email)
    if not dom or dom in FREE_MAIL:
        return "", ""
    core = dom.split(".")[0]
    return core.replace("-", " ").title(), dom


def extract_contacts(text):
    """Pure extraction from one message body."""
    text = text or ""
    emails, seen = [], set()
    for m in EMAIL_RE.findall(text):
        e = m.strip().strip(".").lower()
        if e not in seen:
            seen.add(e)
            emails.append(e)

    phones, pseen = [], set()
    for m in PHONE_RE.findall(text):
        digits = re.sub(r"[\s\-]", "", m)
        if len(digits) == 10 and digits not in pseen:
            pseen.add(digits)
            phones.append(digits)

    handles, hseen = [], set()
    for h in HANDLE_RE.findall(text):
        hl = h.lower()
        if hl in HANDLE_STOPLIST or hl in hseen:
            continue
        if any(("@" + h).lower() in e for e in emails):
            continue
        hseen.add(hl)
        handles.append(h)

    return {
        "emails": emails,
        "phones": phones,
        "handles": handles,
        "has_intent": detect_intent(text),
        "role": guess_role(text),
    }


def build_contact_rows(channel, msg_id, text, opportunity_id=None):
    """
    Turn one message into contact dicts ready for db.insert_contact().
    Full message text is passed as evidence_text so the EXISTING
    classify_contact() scores it as a hiring_email (it keys off hiring words).
    """
    info = extract_contacts(text)
    if not info["has_intent"] or not info["emails"]:
        return []
    src_url = "https://t.me/%s/%s" % (channel, msg_id)
    role = info["role"] or "Telegram hiring post"
    rows = []
    for email in info["emails"]:
        company_name, company_domain = _company_from_email(email)
        rows.append({
            "opportunity_id": opportunity_id,
            "company_name": company_name,
            "company_domain": company_domain,
            "contact_name": "",
            "role_title": role,
            "email": email,
            "linkedin_url": "",
            "source_url": src_url,
            "source_type": "telegram_hiring_post",
            "evidence_text": text[:1500],
            "email_category": "",
            "allowed_for_auto_outreach": 0,
            "confidence_score": 0,
        })
    return rows


# ===========================================================================
# CHANNEL STATE  (sqlite, no network)
# ===========================================================================

def ensure_tables(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tg_channels (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                handle         TEXT UNIQUE NOT NULL,
                title          TEXT,
                joined         INTEGER DEFAULT 0,
                last_msg_id    INTEGER DEFAULT 0,
                discovered_via TEXT,
                msgs_seen      INTEGER DEFAULT 0,
                contacts_found INTEGER DEFAULT 0,
                added_at       TEXT DEFAULT (datetime('now')),
                last_read_at   TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def add_channel(db_path, handle, title="", via="seed"):
    handle = (handle or "").lstrip("@").strip()
    if not handle:
        return False
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO tg_channels (handle,title,discovered_via) VALUES (?,?,?)",
            (handle, title, via),
        )
        added = conn.total_changes > 0
        conn.commit()
        return added
    finally:
        conn.close()


def get_channels(db_path, joined=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = "SELECT * FROM tg_channels"
    if joined is True:
        q += " WHERE joined=1"
    elif joined is False:
        q += " WHERE joined=0"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_joined(db_path, handle, title=""):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tg_channels SET joined=1, title=COALESCE(NULLIF(?,''),title) WHERE handle=?",
            (title, handle.lstrip("@")),
        )
        conn.commit()
    finally:
        conn.close()


def update_progress(db_path, handle, last_msg_id, msgs_seen, contacts_found):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tg_channels SET last_msg_id=?, msgs_seen=msgs_seen+?, "
            "contacts_found=contacts_found+?, last_read_at=datetime('now') WHERE handle=?",
            (last_msg_id, msgs_seen, contacts_found, handle.lstrip("@")),
        )
        conn.commit()
    finally:
        conn.close()


# ===========================================================================
# PERSISTENCE  (uses existing pipeline db helpers)
# ===========================================================================

def _persist_message(db_path, channel, msg_id, text, posted_at):
    """Insert a job row + contact rows. Returns #contacts saved."""
    from database import db

    rows = build_contact_rows(channel, msg_id, text)
    if not rows:
        return 0

    src_url = "https://t.me/%s/%s" % (channel, msg_id)
    title = (text.strip().splitlines()[0] if text.strip() else "Telegram hiring post")[:140]
    company = rows[0]["company_name"] or "Unknown"
    try:
        db.insert_job(db_path, {
            "title": title or "Telegram hiring post",
            "company": company,
            "location": "India",
            "description": text[:4000],
            "url": src_url,
            "source": "telegram",
            "keyword_matched": guess_role(text) or "telegram",
            "salary": "",
            "posted_at": posted_at,
        })
    except Exception as exc:
        logger.debug("job insert skipped for %s: %s", src_url, exc)

    job = db.get_job_by_url(db_path, src_url)
    opp_id = job["id"] if job else None

    saved = 0
    for row in rows:
        row["opportunity_id"] = opp_id
        try:
            if db.insert_contact(db_path, row):
                saved += 1
        except Exception as exc:
            logger.debug("contact insert skipped (%s): %s", row.get("email"), exc)
    return saved


# ===========================================================================
# TELETHON NETWORKING LAYER  (imports telethon lazily)
# ===========================================================================

def _make_client(cfg):
    from telethon import TelegramClient
    api_id = int(cfg.get("api_id") or 0)
    api_hash = cfg.get("api_hash") or ""
    session = cfg.get("session") or "data/tg_session"
    if not api_id or not api_hash:
        raise RuntimeError(
            "telegram_scraper.api_id / api_hash missing. Get them free at "
            "https://my.telegram.org -> API development tools."
        )
    os.makedirs(os.path.dirname(session) or ".", exist_ok=True)
    return TelegramClient(session, api_id, api_hash)


async def login(cfg):
    """One-time interactive login (phone + OTP). Creates the .session file."""
    client = _make_client(cfg)
    await client.start()
    me = await client.get_me()
    print("Logged in as %s (@%s). Session saved." % (me.first_name, getattr(me, "username", None)))
    await client.disconnect()


async def _discover_by_search(client, db_path, cfg):
    """Global keyword search -> add matching public broadcast channels."""
    from telethon.tl.functions.contacts import SearchRequest
    found = 0
    for kw in cfg.get("keywords", []):
        try:
            res = await client(SearchRequest(q=kw, limit=20))
            for chat in getattr(res, "chats", []):
                handle = getattr(chat, "username", None)
                if handle and getattr(chat, "broadcast", False):
                    if add_channel(db_path, handle, getattr(chat, "title", ""), via="search"):
                        found += 1
            await asyncio.sleep(random.uniform(2, 4))
        except Exception as exc:
            logger.warning("search '%s' failed: %s", kw, exc)
    logger.info("discovery(search): %d new channels", found)
    return found


async def _join_pending(client, db_path, cfg):
    """Join not-yet-joined channels, throttled."""
    from telethon.tl.functions.channels import JoinChannelRequest
    limit = int(cfg.get("max_new_joins_per_run", 15))
    delay = cfg.get("join_delay_seconds") or [40, 90]
    lo, hi = float(delay[0]), float(delay[1])
    pending = get_channels(db_path, joined=False)[:limit]
    joined = 0
    for ch in pending:
        try:
            entity = await client.get_entity(ch["handle"])
            await client(JoinChannelRequest(entity))
            mark_joined(db_path, ch["handle"], getattr(entity, "title", ""))
            joined += 1
            logger.info("joined @%s", ch["handle"])
            await asyncio.sleep(random.uniform(lo, hi))
        except Exception as exc:
            logger.warning("join @%s failed: %s", ch["handle"], exc)
            if "already" in str(exc).lower():
                mark_joined(db_path, ch["handle"])
    logger.info("joined %d new channels", joined)
    return joined


async def _read_channels(client, db_path, cfg):
    """Read new messages from joined channels; extract + persist + snowball."""
    max_msgs = int(cfg.get("max_messages_per_channel", 80))
    max_ch = int(cfg.get("max_channels_per_run", 60))
    follow_fwd = bool(cfg.get("discover_via_forwards", True))
    cues = tuple(c.lower() for c in cfg.get("intent_cues", DEFAULT_INTENT_CUES))

    channels = get_channels(db_path, joined=True)[:max_ch]
    total_contacts = total_msgs = new_from_fwd = 0

    for ch in channels:
        handle = ch["handle"]
        last_id = ch.get("last_msg_id") or 0
        seen = found = 0
        max_seen_id = last_id
        try:
            async for msg in client.iter_messages(handle, limit=max_msgs, min_id=last_id):
                seen += 1
                max_seen_id = max(max_seen_id, msg.id)
                text = msg.message or ""

                if follow_fwd and msg.forward is not None:
                    try:
                        fchat = await msg.forward.get_chat()
                        fu = getattr(fchat, "username", None)
                        if fu and add_channel(db_path, fu, getattr(fchat, "title", ""), via="forward"):
                            new_from_fwd += 1
                    except Exception:
                        pass

                if detect_intent(text, cues):
                    found += _persist_message(
                        db_path, handle, msg.id, text,
                        msg.date.isoformat() if msg.date else "",
                    )
            update_progress(db_path, handle, max_seen_id, seen, found)
            total_contacts += found
            total_msgs += seen
            await asyncio.sleep(random.uniform(1.5, 3.5))
        except Exception as exc:
            logger.warning("read @%s failed: %s", handle, exc)

    logger.info("read %d msgs, saved %d contacts, +%d channels via forwards",
                total_msgs, total_contacts, new_from_fwd)
    return {"messages": total_msgs, "contacts": total_contacts, "fwd_channels": new_from_fwd}


async def _run_async(cfg, db_path):
    ensure_tables(db_path)
    seeded = 0
    for h in cfg.get("seed_channels", []):
        if add_channel(db_path, h, via="seed"):
            seeded += 1
    logger.info("seeded %d channels", seeded)

    client = _make_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Not logged in. Run `python main.py tg-login` once to authorize.")

    try:
        if cfg.get("discover_via_search", True):
            await _discover_by_search(client, db_path, cfg)
        if cfg.get("auto_join", True):
            await _join_pending(client, db_path, cfg)
        stats = await _read_channels(client, db_path, cfg)
    finally:
        await client.disconnect()

    stats["seeded"] = seeded
    return stats


# ===========================================================================
# PUBLIC ENTRY POINTS  (sync, called by main.py / scheduler)
# ===========================================================================

def run(config, db_path):
    """Run one full discover->join->read cycle. Returns a stats dict."""
    cfg = (config or {}).get("telegram_scraper", {})
    if not cfg.get("enabled"):
        logger.info("telegram_scraper disabled in config; skipping.")
        return {"skipped": True}
    return asyncio.run(_run_async(cfg, db_path))


def run_login(config):
    cfg = (config or {}).get("telegram_scraper", {})
    asyncio.run(login(cfg))
