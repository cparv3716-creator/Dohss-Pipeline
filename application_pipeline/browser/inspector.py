"""Form extraction, question mapping, and authentication blocker detection."""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Any, Iterable, Mapping, Optional

from bs4 import BeautifulSoup, Tag

from candidate import AnswerBank, CandidateProfile
from application_pipeline.questions import classify_question

from .models import AnswerStatus, ApplicationField, AuthenticationBlocker, FieldType
from .safety import SafeFillPolicy


LIVE_EXTRACTION_SCRIPT = r"""
els => els.filter(el => {
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return !el.disabled && el.type !== 'hidden' && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
}).map((el, index) => {
  const labels = el.labels ? Array.from(el.labels).map(x => x.innerText.trim()).filter(Boolean) : [];
  const fieldset = el.closest('fieldset');
  const legend = fieldset?.querySelector('legend')?.innerText?.trim() || '';
  const nearby = el.closest('.field, .application-field, .form-group, [class*="field"]');
  const nearbyText = nearby?.querySelector('label')?.innerText?.trim() || '';
  const uploadLabel = el.type === 'file' ? el.closest('.file-upload')?.querySelector('.upload-label, .label')?.innerText?.trim() || '' : '';
  const label = uploadLabel || labels[0] || el.getAttribute('aria-label') || nearbyText || legend || el.placeholder || el.name || el.id || `field_${index}`;
  const ariaLabel = el.getAttribute('aria-label') || '';
  const selector = el.id ? `[id=${JSON.stringify(el.id)}]` : el.name ? `${el.tagName.toLowerCase()}[name=${JSON.stringify(el.name)}]` : ariaLabel ? `${el.tagName.toLowerCase()}[aria-label=${JSON.stringify(ariaLabel)}]` : `${el.tagName.toLowerCase()}:nth-of-type(${index + 1})`;
  const isCustomSelect = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox';
  const controlled = el.getAttribute('aria-controls');
  const optionRoot = controlled ? document.getElementById(controlled) : null;
  const customOptions = optionRoot ? Array.from(optionRoot.querySelectorAll('[role=option]')).map(o => ({value: o.getAttribute('data-value') || o.innerText.trim(), label: o.innerText.trim()})) : [];
  const options = el.tagName === 'SELECT' ? Array.from(el.options).map(o => ({value: o.value, label: o.text.trim()})) : customOptions;
  return {
    id: el.id || el.name || `field_${index}`,
    name: el.name || '', tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(),
    label, legend, selector, required: !!el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(labels[0] || nearbyText),
    custom_select: isCustomSelect,
    value: el.type === 'checkbox' || el.type === 'radio' ? !!el.checked : el.value,
    option_value: el.value || '', option_label: labels[0] || el.value || '', options
  };
})
"""


def normalize_question(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("*", " ")).strip(" :-\t")


def _css_attribute(name: str, value: str) -> str:
    return f'[{name}={json.dumps(str(value))}]'


def _label_for(soup: BeautifulSoup, element: Tag) -> str:
    element_id = element.get("id")
    if element_id:
        label = soup.find("label", attrs={"for": element_id})
        if label:
            return normalize_question(label.get_text(" ", strip=True))
    parent_label = element.find_parent("label")
    if parent_label:
        return normalize_question(parent_label.get_text(" ", strip=True))
    container = element.find_parent(["fieldset", "div", "li"])
    if container:
        candidate = container.find(["legend", "label"])
        if candidate:
            return normalize_question(candidate.get_text(" ", strip=True))
    return normalize_question(element.get("aria-label") or element.get("placeholder") or element.get("name") or element_id or "")


def _raw_fields_from_html(content: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(content or "", "html.parser")
    rows = []
    for index, element in enumerate(soup.select("input, textarea, select")):
        input_type = str(element.get("type") or "").lower()
        if input_type in ("hidden", "submit", "button", "reset", "image") or element.has_attr("disabled"):
            continue
        label = _label_for(soup, element)
        fieldset = element.find_parent("fieldset")
        legend = normalize_question(fieldset.find("legend").get_text(" ", strip=True)) if fieldset and fieldset.find("legend") else ""
        required_marker_container = element.find_parent(["label", "fieldset", "div"])
        has_required_marker = bool(required_marker_container and "*" in required_marker_container.get_text(" ", strip=True))
        name = str(element.get("name") or "")
        element_id = str(element.get("id") or name or f"field_{index}")
        if element.get("id"):
            selector = _css_attribute("id", str(element["id"]))
        elif name:
            selector = f"{element.name}{_css_attribute('name', name)}"
        else:
            selector = f"{element.name}:nth-of-type({index + 1})"
        options = []
        if element.name == "select":
            options = [
                {"value": str(option.get("value") or option.get_text(" ", strip=True)), "label": normalize_question(option.get_text(" ", strip=True))}
                for option in element.find_all("option")
            ]
        rows.append(
            {
                "id": element_id, "name": name, "tag": element.name,
                "type": input_type, "label": label, "selector": selector,
                "legend": legend,
                "required": element.has_attr("required") or str(element.get("aria-required") or "").lower() == "true" or has_required_marker,
                "value": element.has_attr("checked") if input_type in ("radio", "checkbox") else str(element.get("value") or element.text or ""),
                "option_value": str(element.get("value") or ""),
                "option_label": label or str(element.get("value") or ""),
                "options": options,
            }
        )
    return rows


def _field_type(row: Mapping[str, Any]) -> str:
    tag = str(row.get("tag") or "").lower()
    kind = str(row.get("type") or "").lower()
    if row.get("custom_select"):
        return FieldType.SELECT.value
    if tag == "textarea":
        return FieldType.TEXTAREA.value
    if tag == "select":
        return FieldType.SELECT.value
    return {
        "email": FieldType.EMAIL.value,
        "tel": FieldType.PHONE.value,
        "phone": FieldType.PHONE.value,
        "radio": FieldType.RADIO.value,
        "checkbox": FieldType.CHECKBOX.value,
        "file": FieldType.FILE.value,
        "number": FieldType.NUMBER.value,
        "date": FieldType.DATE.value,
        "text": FieldType.TEXT.value,
        "": FieldType.TEXT.value,
    }.get(kind, FieldType.UNKNOWN.value)


def _sensitivity(question_type: str, question: str) -> str:
    if question_type in {
        "CURRENT_CTC", "EXPECTED_CTC", "WORK_AUTHORIZATION", "VISA_SPONSORSHIP",
        "PRONOUNS", "DEMOGRAPHIC_SELF_ID", "CONSENT",
    }:
        return "highly_sensitive"
    if re.search(r"criminal|disab|veteran|race|ethnicity|gender|conflict|relative|consent|attest|signature", question, re.I):
        return "highly_sensitive"
    if question_type in {"PHONE", "CURRENT_CITY", "CGPA", "RELOCATION"}:
        return "sensitive"
    return "private"


def normalize_fields(rows: Iterable[Mapping[str, Any]], frame_url: str = "") -> list[ApplicationField]:
    raw = list(rows)
    radio_groups: OrderedDict[str, list[Mapping[str, Any]]] = OrderedDict()
    non_radios = []
    for row in raw:
        if str(row.get("type") or "").lower() == "radio":
            key = str(row.get("name") or row.get("id") or "radio")
            radio_groups.setdefault(key, []).append(row)
        else:
            non_radios.append(row)
    fields: list[ApplicationField] = []
    for row in non_radios:
        label = normalize_question(str(row.get("label") or row.get("id") or ""))
        classified = classify_question(label)
        fields.append(
            ApplicationField(
                field_id=str(row.get("id") or f"field_{len(fields)}"),
                label=label, raw_question=label, field_type=_field_type(row),
                required=bool(row.get("required")), options=list(row.get("options") or []),
                current_value=row.get("value"), canonical_question_type=classified.question_type,
                skill=classified.skill, sensitivity=_sensitivity(classified.question_type, label),
                confidence=classified.confidence, selector=str(row.get("selector") or ""), frame_url=frame_url,
                control_kind="combobox" if row.get("custom_select") else "native",
            )
        )
    for name, group in radio_groups.items():
        first = group[0]
        label = normalize_question(str(first.get("legend") or first.get("label") or name))
        classified = classify_question(label)
        options = [
            {"value": str(row.get("option_value") or ""), "label": normalize_question(str(row.get("option_label") or row.get("option_value") or ""))}
            for row in group
        ]
        selected = next((row.get("option_value") for row in group if row.get("value") is True), "")
        fields.append(
            ApplicationField(
                field_id=name, label=label, raw_question=label, field_type=FieldType.RADIO.value,
                required=any(bool(row.get("required")) for row in group), options=options,
                current_value=selected, canonical_question_type=classified.question_type,
                skill=classified.skill, sensitivity=_sensitivity(classified.question_type, label),
                confidence=classified.confidence, selector=f'input{_css_attribute("name", name)}', frame_url=frame_url,
                control_kind="radio_group",
            )
        )
    return fields


def extract_fields_from_html(content: str, frame_url: str = "") -> list[ApplicationField]:
    return normalize_fields(_raw_fields_from_html(content), frame_url)


def inspect_page_fields(page: Any, pass_number: int = 1) -> list[ApplicationField]:
    fields: list[ApplicationField] = []
    for frame in page.frames:
        try:
            rows = frame.locator("input, textarea, select, [role=combobox], button[aria-haspopup=listbox]").evaluate_all(LIVE_EXTRACTION_SCRIPT)
            normalized = normalize_fields(rows, frame.url)
            for item in normalized:
                item.detected_pass = pass_number
            fields.extend(normalized)
        except Exception:
            continue
    seen = set()
    unique = []
    for item in fields:
        key = (item.frame_url, item.field_id, item.field_type)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def field_identity(item: ApplicationField) -> tuple[str, str, str, str]:
    return (item.frame_url, item.field_id, item.raw_question, item.field_type)


def merge_observed_fields(
    observed: dict[tuple[str, str, str, str], ApplicationField],
    current: list[ApplicationField],
) -> tuple[list[ApplicationField], int]:
    """Keep prior safe-fill results while adding newly revealed controls."""
    current_keys = {field_identity(item) for item in current}
    for item in observed.values():
        item.visible = field_identity(item) in current_keys
    new_count = 0
    for item in current:
        key = field_identity(item)
        previous = observed.get(key)
        if previous is None:
            observed[key] = item
            new_count += 1
            continue
        item.answer_source = previous.answer_source
        item.answer_verified = previous.answer_verified
        item.auto_fill_allowed = previous.auto_fill_allowed
        item.answer_status = previous.answer_status
        item.answer_reason = previous.answer_reason
        item.proposed_value = previous.proposed_value
        item.confidence = previous.confidence
        if previous.answer_status in (AnswerStatus.FILLED.value, AnswerStatus.UPLOADED.value):
            item.current_value = previous.current_value
        observed[key] = item
    return list(observed.values()), new_count


def resolve_answers(fields: list[ApplicationField], profile: CandidateProfile, bank: AnswerBank, policy: SafeFillPolicy) -> list[ApplicationField]:
    for item in fields:
        if not item.visible or item.answer_status in (AnswerStatus.FILLED.value, AnswerStatus.UPLOADED.value):
            continue
        if item.field_type == FieldType.FILE.value:
            item.answer_status = AnswerStatus.UNRESOLVED.value
            item.answer_reason = "FILE_REQUIRES_CONFIGURED_ARTIFACT"
            continue
        answer = bank.lookup(item.canonical_question_type, skill=item.skill, profile=profile)
        decision = policy.evaluate(item.raw_question, item.canonical_question_type, answer, item.sensitivity)
        if answer:
            item.proposed_value = answer.value
            item.answer_source = answer.source
            item.answer_verified = answer.verified
            item.auto_fill_allowed = answer.auto_answer_allowed
            item.confidence = min(item.confidence, answer.confidence)
            item.sensitivity = answer.sensitivity or item.sensitivity
        if decision.allowed:
            item.answer_status = AnswerStatus.PROPOSED.value
            item.answer_reason = decision.reason
        elif decision.reason == "UNKNOWN_ANSWER":
            item.answer_status = AnswerStatus.SKIPPED_UNKNOWN.value
            item.answer_reason = decision.reason
        elif "SENSITIVE" in decision.reason or "SENSITIVITY" in decision.reason:
            item.answer_status = AnswerStatus.SKIPPED_SENSITIVE.value
            item.answer_reason = decision.reason
        else:
            item.answer_status = AnswerStatus.SKIPPED_POLICY.value
            item.answer_reason = decision.reason
    return fields


def detect_authentication_blockers(content: str) -> list[str]:
    soup = BeautifulSoup(content or "", "html.parser")
    text = normalize_question(soup.get_text(" ", strip=True)).lower()
    blockers: list[str] = []
    if soup.select_one('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], .g-recaptcha, .h-captcha, [data-sitekey]') or "captcha" in text or "confirm you are not a robot" in text:
        blockers.append(AuthenticationBlocker.CAPTCHA_REQUIRED.value)
    if soup.select_one('input[type="password"]') and re.search(r"\b(log[ -]?in|sign[ -]?in)\b", text):
        blockers.append(AuthenticationBlocker.LOGIN_REQUIRED.value)
    if re.search(r"multi[- ]factor|two[- ]factor|\bmfa\b", text):
        blockers.append(AuthenticationBlocker.MFA_REQUIRED.value)
    if re.search(r"one[- ]time password|verification code|\botp\b", text) and soup.select_one("input"):
        blockers.append(AuthenticationBlocker.OTP_REQUIRED.value)
    if re.search(r"bot traffic|checking your browser|cloudflare|unusual traffic", text):
        blockers.append(AuthenticationBlocker.BOT_DETECTION.value)
    return list(dict.fromkeys(blockers))


def detect_page_blockers(page: Any) -> list[str]:
    try:
        return detect_authentication_blockers(page.content())
    except Exception:
        return []
