"""Form extraction, question mapping, and authentication blocker detection."""
from __future__ import annotations

import json
import hashlib
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
  const kind = (el.type || '').toLowerCase();
  const customSelect = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox';
  return kind !== 'hidden' && (customSelect || !['submit','button','reset','image'].includes(kind));
}).map((el, index) => {
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  const visible = !el.hidden && el.getAttribute('aria-hidden') !== 'true' && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  const labels = el.labels ? Array.from(el.labels).map(x => x.innerText.trim()).filter(Boolean) : [];
  const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
    .map(id => document.getElementById(id)?.innerText?.trim() || '').filter(Boolean);
  const fieldset = el.closest('fieldset');
  const legend = fieldset?.querySelector('legend')?.innerText?.trim() || '';
  const nearby = el.closest('[data-field-id], .field, .application-field, .form-group, .question, [class*="field"]');
  const nearbyLabel = nearby?.querySelector(':scope > label, :scope > legend, label[for]') || null;
  const nearbyText = nearbyLabel?.innerText?.trim() || '';
  const uploadLabel = el.type === 'file' ? el.closest('.file-upload')?.querySelector('.upload-label, .label')?.innerText?.trim() || '' : '';
  const labelSource = uploadLabel || labels[0] || labelledBy.join(' ') || el.getAttribute('aria-label') || legend || nearbyText || el.placeholder || el.name || el.id || '';
  const label = labelSource || `field_${index}`;
  const ariaLabel = el.getAttribute('aria-label') || '';
  const selector = el.id ? `[id=${JSON.stringify(el.id)}]` : el.name ? `${el.tagName.toLowerCase()}[name=${JSON.stringify(el.name)}]` : ariaLabel ? `${el.tagName.toLowerCase()}[aria-label=${JSON.stringify(ariaLabel)}]` : `${el.tagName.toLowerCase()}:nth-of-type(${index + 1})`;
  const isCustomSelect = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox';
  const controlled = el.getAttribute('aria-controls');
  const optionRoot = controlled ? document.getElementById(controlled) : null;
  const customOptions = optionRoot ? Array.from(optionRoot.querySelectorAll('[role=option]')).map(o => ({value: o.getAttribute('data-value') || o.innerText.trim(), label: o.innerText.trim()})) : [];
  const options = el.tagName === 'SELECT' ? Array.from(el.options).map(o => ({value: o.value, label: o.text.trim()})) : customOptions;
  const markerText = [labels[0] || '', labelledBy.join(' '), legend, nearbyText].join(' ');
  const requiredSource = el.required ? 'native' : el.getAttribute('aria-required') === 'true' ? 'aria' : /(^|\s)\*(\s|$)/.test(markerText) ? 'label_marker' : '';
  const groupRoot = fieldset || nearby || el.parentElement;
  const groupIndex = groupRoot ? Array.from(document.querySelectorAll('fieldset, [data-field-id], .field, .application-field, .form-group, .question, [class*="field"]')).indexOf(groupRoot) : -1;
  const groupKey = groupRoot?.getAttribute('data-field-id') || groupRoot?.id || (groupIndex >= 0 ? `container_${groupIndex}` : '') || el.name || el.id || '';
  return {
    id: el.id || el.name || `field_${index}`,
    name: el.name || '', tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(),
    label, legend, selector, required: !!requiredSource, required_source: requiredSource,
    visible, enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true', group_key: groupKey,
    meaningful_label: !!labelSource,
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
    for index, element in enumerate(soup.select("input, textarea, select, [role=combobox], button[aria-haspopup=listbox]")):
        input_type = str(element.get("type") or "").lower()
        custom_select = str(element.get("role") or "").lower() == "combobox" or str(element.get("aria-haspopup") or "").lower() == "listbox"
        if input_type == "hidden" or (input_type in ("submit", "button", "reset", "image") and not custom_select):
            continue
        label = _label_for(soup, element)
        if not label:
            continue
        fieldset = element.find_parent("fieldset")
        legend = normalize_question(fieldset.find("legend").get_text(" ", strip=True)) if fieldset and fieldset.find("legend") else ""
        own_label = element.find_parent("label")
        marker_text = " ".join(part for part in (label, legend, own_label.get_text(" ", strip=True) if own_label else "") if part)
        has_required_marker = bool(re.search(r"(^|\s)\*(\s|$)", marker_text))
        required_source = "native" if element.has_attr("required") else "aria" if str(element.get("aria-required") or "").lower() == "true" else "label_marker" if has_required_marker else ""
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
        elif custom_select and element.get("aria-controls"):
            option_root = soup.find(id=str(element.get("aria-controls")))
            if option_root:
                options = [
                    {"value": str(option.get("data-value") or option.get_text(" ", strip=True)), "label": normalize_question(option.get_text(" ", strip=True))}
                    for option in option_root.select('[role="option"]')
                ]
        rows.append(
            {
                "id": element_id, "name": name, "tag": element.name,
                "type": input_type, "label": label, "selector": selector,
                "legend": legend,
                "required": bool(required_source), "required_source": required_source,
                "visible": not any(
                    node.has_attr("hidden")
                    or str(node.get("aria-hidden") or "").lower() == "true"
                    or re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", str(node.get("style") or ""), re.I)
                    for node in [element, *element.parents] if isinstance(node, Tag)
                ),
                "enabled": not element.has_attr("disabled"),
                "group_key": str((fieldset or element.find_parent(["div", "li"]) or element).get("data-field-id") or (fieldset or element.find_parent(["div", "li"]) or element).get("id") or name or element_id),
                "value": element.has_attr("checked") if input_type in ("radio", "checkbox") else str(element.get("value") or element.text or ""),
                "option_value": str(element.get("value") or ""),
                "option_label": label or str(element.get("value") or ""),
                "options": options,
                "custom_select": custom_select,
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
    if question_type in {"PHONE", "CURRENT_CITY"}:
        return "personal"
    if question_type in {"CGPA", "RELOCATION"}:
        return "sensitive"
    return "private"


def _semantic_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalize_question(value).lower()).strip()


def _logical_id(frame_url: str, row: Mapping[str, Any], label: str, field_type: str, discriminator: str = "") -> str:
    """Create an identity that survives React IDs while preserving distinct questions."""
    group = _semantic_text(str(row.get("group_key") or row.get("name") or row.get("id") or discriminator))
    semantic = "|".join((_semantic_text(frame_url), _semantic_text(label), field_type, group, discriminator))
    return "field:" + hashlib.sha256(semantic.encode("utf-8")).hexdigest()[:20]


def normalize_fields(rows: Iterable[Mapping[str, Any]], frame_url: str = "") -> list[ApplicationField]:
    raw = list(rows)
    groups: OrderedDict[tuple[str, str], list[Mapping[str, Any]]] = OrderedDict()
    non_groups = []
    for row in raw:
        if row.get("meaningful_label", True) is False:
            continue
        kind = str(row.get("type") or "").lower()
        if kind in {"radio", "checkbox"} and row.get("name"):
            key = str(row.get("group_key") or row.get("name") or row.get("id") or kind)
            groups.setdefault((kind, key), []).append(row)
        else:
            non_groups.append(row)
    fields: list[ApplicationField] = []
    candidates: list[ApplicationField] = []
    for row in non_groups:
        label = normalize_question(str(row.get("label") or row.get("id") or ""))
        classified = classify_question(label)
        field_type = _field_type(row)
        candidates.append(
            ApplicationField(
                field_id=str(row.get("id") or f"field_{len(fields)}"),
                label=label, raw_question=label, field_type=field_type,
                required=bool(row.get("required")), options=list(row.get("options") or []),
                current_value=row.get("value"), canonical_question_type=classified.question_type,
                skill=classified.skill, sensitivity=_sensitivity(classified.question_type, label),
                confidence=classified.confidence, selector=str(row.get("selector") or ""), frame_url=frame_url,
                control_kind="combobox" if row.get("custom_select") else "native",
                visible=bool(row.get("visible", True)), enabled=bool(row.get("enabled", True)),
                required_source=str(row.get("required_source") or ""),
                logical_id=_logical_id(frame_url, row, label, field_type),
                semantic_key="|".join((_semantic_text(label), _semantic_text(str(row.get("group_key") or row.get("name") or "")))),
            )
        )
    # React select widgets often expose both a combobox and a companion text
    # input for one question. Prefer the semantic combobox without merging
    # same-labelled controls from different containers.
    by_semantic: OrderedDict[str, ApplicationField] = OrderedDict()
    for item in candidates:
        previous = by_semantic.get(item.semantic_key)
        if previous is None:
            by_semantic[item.semantic_key] = item
        elif item.control_kind == "combobox" and previous.control_kind != "combobox":
            by_semantic[item.semantic_key] = item
        elif previous.control_kind == "combobox" and item.control_kind != "combobox":
            continue
        elif item.logical_id != previous.logical_id and item.selector != previous.selector:
            # Same words can be two real questions. Their container/name is
            # part of semantic_key, so reaching this branch means a genuine
            # distinct control without a stable shared group.
            by_semantic[f"{item.semantic_key}|{item.logical_id}"] = item
    fields.extend(by_semantic.values())
    for (kind, name), group in groups.items():
        first = group[0]
        label = normalize_question(str(first.get("legend") or first.get("label") or name))
        classified = classify_question(label)
        options = [
            {"value": str(row.get("option_value") or ""), "label": normalize_question(str(row.get("option_label") or row.get("option_value") or ""))}
            for row in group
        ]
        selected = next((row.get("option_value") for row in group if row.get("value") is True), "")
        field_type = FieldType.RADIO.value if kind == "radio" else FieldType.CHECKBOX.value
        fields.append(
            ApplicationField(
                field_id=name, label=label, raw_question=label, field_type=field_type,
                required=any(bool(row.get("required")) for row in group), options=options,
                current_value=selected, canonical_question_type=classified.question_type,
                skill=classified.skill, sensitivity=_sensitivity(classified.question_type, label),
                confidence=classified.confidence, selector=f'input{_css_attribute("name", str(first.get("name") or name))}', frame_url=frame_url,
                control_kind=f"{kind}_group",
                visible=any(bool(row.get("visible", True)) for row in group),
                enabled=any(bool(row.get("enabled", True)) for row in group),
                required_source=next((str(row.get("required_source")) for row in group if row.get("required_source")), ""),
                logical_id=_logical_id(frame_url, first, label, field_type),
                semantic_key="|".join((_semantic_text(label), _semantic_text(name))),
            )
        )
    for field in fields:
        if not field.visible or not field.enabled:
            field.answer_status = AnswerStatus.HIDDEN_INACTIVE.value
            field.answer_reason = "HIDDEN_OR_INACTIVE_CONTROL"
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
        key = item.logical_id or (item.frame_url, item.field_id, item.field_type)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def field_identity(item: ApplicationField) -> str:
    return item.logical_id or "|".join((item.frame_url, item.field_id, item.raw_question, item.field_type))


def merge_observed_fields(
    observed: dict[str, ApplicationField],
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
        item.logical_id = previous.logical_id or item.logical_id
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
        blockers = detect_authentication_blockers(page.content())
        if AuthenticationBlocker.CAPTCHA_REQUIRED.value in blockers:
            solved = page.locator(
                'textarea[name="g-recaptcha-response"], input[name="g-recaptcha-response"], '
                'textarea[name="h-captcha-response"], input[name="h-captcha-response"], '
                'textarea[name="cf-turnstile-response"], input[name="cf-turnstile-response"]'
            ).evaluate_all("els => els.some(e => String(e.value || '').trim().length > 20)")
            if solved:
                blockers.remove(AuthenticationBlocker.CAPTCHA_REQUIRED.value)
        return blockers
    except Exception:
        return []
