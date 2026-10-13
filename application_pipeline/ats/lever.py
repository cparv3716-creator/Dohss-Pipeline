"""Lever inspection and safe-fill adapter."""
from __future__ import annotations

from urllib.parse import urljoin

from .base import ApplicationATSAdapter


class LeverAdapter(ApplicationATSAdapter):
    provider = "Lever"

    def can_handle(self, url: str) -> bool:
        return "lever.co" in str(url or "").lower()

    def open_application(self, session, url: str):
        response = super().open_application(session, url)
        if not session.page.url.rstrip("/").endswith("/apply"):
            links = session.page.locator('a[href*="/apply"]')
            if links.count():
                href = links.first.get_attribute("href")
                if href:
                    response = super().open_application(session, urljoin(session.page.url, href))
        return response
