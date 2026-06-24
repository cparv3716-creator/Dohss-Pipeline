"""
scrapers/google_alerts.py
Consume Google Alerts RSS feeds.

Setup: go to google.com/alerts, create alerts, set delivery to RSS feed,
paste the URL into config.yaml under google_alerts.feeds
"""
import logging
from datetime import datetime

import feedparser
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def parse_feed(feed_url, label=""):
    if "PASTE_GOOGLE_ALERT" in feed_url:
        logger.info(f"Skipping placeholder feed: {label}")
        return []
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as e:
        logger.error(f"Feed error ({label}): {e}")
        return []
    leads = []
    for entry in parsed.entries:
        title   = entry.get("title", "No title")
        link    = entry.get("link", "")
        summary = entry.get("summary", "")
        pub     = entry.get("published", datetime.now().isoformat())
        clean   = BeautifulSoup(summary, "html.parser").get_text()
        leads.append({"title": title, "company": "Unknown", "location": "",
                      "description": clean[:500], "url": link,
                      "source": "google_alert", "keyword_matched": label,
                      "salary": "", "posted_at": pub})
    logger.info(f"Google Alerts [{label}]: {len(leads)} entries")
    return leads


def run_all_feeds(config):
    feeds = config.get("google_alerts", {}).get("feeds", [])
    seen = set()
    all_leads = []
    for feed in feeds:
        for lead in parse_feed(feed.get("url",""), feed.get("label","")):
            if lead["url"] and lead["url"] not in seen:
                seen.add(lead["url"])
                all_leads.append(lead)
    logger.info(f"Google Alerts total: {len(all_leads)}")
    return all_leads
