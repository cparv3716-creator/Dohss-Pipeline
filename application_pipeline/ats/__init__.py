"""ATS detection and safe inspection adapters."""

from .detection import ATS_PATTERNS, detect_ats
from .base import ApplicationATSAdapter
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter
from .generic import GenericATSAdapter


def adapter_for(url: str, ats_provider: str = "") -> ApplicationATSAdapter:
    for adapter in (GreenhouseAdapter(), LeverAdapter()):
        if adapter.can_handle(url) or adapter.provider.lower() == str(ats_provider or "").lower():
            return adapter
    return GenericATSAdapter()


__all__ = [
    "ATS_PATTERNS", "ApplicationATSAdapter", "GenericATSAdapter",
    "GreenhouseAdapter", "LeverAdapter", "adapter_for", "detect_ats",
]
