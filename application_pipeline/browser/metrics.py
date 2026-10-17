"""Measured semantic field-detection metrics for manually labelled fixtures."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .models import ApplicationField


def _key(label: str, field_type: str) -> tuple[str, str]:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(label or "").casefold()).strip()
    return normalized, str(field_type)


@dataclass(frozen=True)
class DetectionMetrics:
    true_positive: int
    false_positive: int
    false_negative: int
    precision: float
    recall: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "precision": self.precision,
            "recall": self.recall,
        }


def required_detection_metrics(
    expected: Iterable[Mapping[str, Any]],
    detected: Iterable[ApplicationField],
) -> DetectionMetrics:
    """Calculate multiset precision/recall without hiding duplicate errors."""
    expected_keys = Counter(
        _key(item["label"], item["field_type"])
        for item in expected if item.get("required") and item.get("visible", True)
    )
    detected_keys = Counter(
        _key(item.label, item.field_type)
        for item in detected if item.required and item.visible
    )
    true_positive = sum((expected_keys & detected_keys).values())
    false_positive = sum((detected_keys - expected_keys).values())
    false_negative = sum((expected_keys - detected_keys).values())
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    return DetectionMetrics(true_positive, false_positive, false_negative, precision, recall)
