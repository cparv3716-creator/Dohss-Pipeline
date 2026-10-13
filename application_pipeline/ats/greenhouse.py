"""Greenhouse inspection and safe-fill adapter."""
from __future__ import annotations

from .base import ApplicationATSAdapter


class GreenhouseAdapter(ApplicationATSAdapter):
    provider = "Greenhouse"

    def can_handle(self, url: str) -> bool:
        lower = str(url or "").lower()
        return "greenhouse.io" in lower or "boards.greenhouse" in lower or "job-boards.greenhouse" in lower
