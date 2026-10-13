"""Conservative generic ATS inspector; automatic filling is disabled."""
from __future__ import annotations

from .base import ApplicationATSAdapter


class GenericATSAdapter(ApplicationATSAdapter):
    provider = "Custom/Company ATS"
    auto_fill_enabled = False

    def can_handle(self, url: str) -> bool:
        return True
