"""Playwright Chromium session with private artifacts and safety guards."""
from __future__ import annotations

import json
import hashlib
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup, NavigableString

from .safety import SubmissionGuard


@dataclass(frozen=True)
class BrowserConfig:
    headless: bool = True
    timeout_ms: int = 30_000
    navigation_timeout_ms: int = 45_000
    slow_mo_ms: int = 0
    artifacts_root: str = "data/private/browser_runs"
    manual_handoff: bool = False
    save_html: bool = True
    max_fill_passes: int = 3
    dom_settle_ms: int = 800
    manual_handoff_timeout_seconds: int = 300


class BrowserArtifacts:
    def __init__(self, root: str | Path, application_id: int):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        private_root = Path(root).resolve()
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", str(private_root)],
            capture_output=True,
            check=False,
        ).returncode == 0
        if not ignored:
            raise ValueError(f"Browser artifacts root is not protected by .gitignore: {private_root}")
        self.directory = private_root / f"application_{int(application_id)}" / timestamp
        self.directory.mkdir(parents=True, exist_ok=False)
        self.log_path = self.directory / "browser.jsonl"

    def log(self, event: str, **details: Any) -> None:
        def safe(value: Any, key: str = "") -> Any:
            if isinstance(value, dict):
                return {item_key: safe(item_value, item_key) for item_key, item_value in value.items()}
            if isinstance(value, (list, tuple)):
                return [safe(item, key) for item in value]
            if not isinstance(value, str):
                return value
            text = value
            if key.lower().endswith("url"):
                text = re.sub(r"[?#].*$", "", text)
            text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[REDACTED_EMAIL]", text, flags=re.I)
            text = re.sub(r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)", "[REDACTED_PHONE]", text)
            return text
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **{key: safe(value, key) for key, value in details.items()},
        }
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def path(self, name: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "artifact"
        return self.directory / safe


def sanitize_html_snapshot(content: str) -> str:
    soup = BeautifulSoup(content or "", "html.parser")
    for tag in soup.select("script, noscript"):
        tag.decompose()
    for tag in soup.select("input"):
        if tag.has_attr("value"):
            tag["value"] = "[REDACTED]"
        tag.attrs.pop("checked", None)
    for tag in soup.select("textarea"):
        tag.clear()
        tag.string = "[REDACTED]"
    for tag in soup.select("option"):
        tag.attrs.pop("selected", None)
    for tag in soup.select('[class*="single-value"], [class*="multi-value"], [role="option"][aria-selected="true"]'):
        tag.clear()
        tag.string = "[REDACTED]"
    for tag in soup.find_all(True):
        for attribute in list(tag.attrs):
            if any(marker in attribute.lower() for marker in ("token", "secret", "csrf", "auth")):
                tag[attribute] = "[REDACTED]"
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue
        text = str(node)
        redacted = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[REDACTED_EMAIL]", text, flags=re.I)
        redacted = re.sub(r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)", "[REDACTED_PHONE]", redacted)
        redacted = re.sub(r"\b[^<>\r\n]{1,100}\.(?:pdf|docx?|rtf)\b", "[REDACTED_FILE]", redacted, flags=re.I)
        if redacted != text:
            node.replace_with(redacted)
    return str(soup)


class BrowserSession:
    def __init__(self, config: BrowserConfig, application_id: int):
        self.config = config
        self.application_id = application_id
        self.artifacts = BrowserArtifacts(config.artifacts_root, application_id)
        self.guard = SubmissionGuard()
        self._playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None

    def start(self) -> "BrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install -r requirements.txt && "
                "python -m playwright install chromium"
            ) from exc
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            headless=self.config.headless,
            slow_mo=self.config.slow_mo_ms,
        )
        self.context = self.browser.new_context(
            accept_downloads=False,
            service_workers="block",
            viewport={"width": 1440, "height": 1000},
        )
        self.context.set_default_timeout(self.config.timeout_ms)
        self.context.set_default_navigation_timeout(self.config.navigation_timeout_ms)
        self.guard.install(self.context)
        self.page = self.context.new_page()
        self.page.on("dialog", lambda dialog: dialog.dismiss())
        self.artifacts.log("browser_started", headless=self.config.headless)
        return self

    def open(self, url: str) -> Any:
        self.artifacts.log("navigation_started", url=url)
        response = self.page.goto(url, wait_until="domcontentloaded")
        self.page.evaluate(SubmissionGuard.INIT_SCRIPT)
        self.artifacts.log(
            "navigation_completed", url=self.page.url,
            status=response.status if response else None,
        )
        return response

    def screenshot(self, name: str, full_page: bool = True) -> str:
        path = self.artifacts.path(name if name.endswith(".png") else f"{name}.png")
        masks = self.page.locator(
            'input, textarea, select, [contenteditable="true"], [role="combobox"], '
            '[class*="single-value"], [class*="multi-value"]'
        )
        self.page.screenshot(
            path=str(path), full_page=full_page, animations="disabled",
            mask=[masks], mask_color="#606060",
        )
        self.artifacts.log("screenshot_saved", path=str(path))
        return str(path)

    def form_signature(self) -> str:
        structure = self.page.locator("input, textarea, select, [role=combobox]").evaluate_all(
            "els => els.filter(e => { const s=getComputedStyle(e); const r=e.getBoundingClientRect(); "
            "return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; })"
            ".map(e => [e.tagName,e.type||'',e.name||'',e.id||'',e.getAttribute('aria-label')||'',!!e.required])"
        )
        return hashlib.sha256(json.dumps(structure, sort_keys=True).encode("utf-8")).hexdigest()

    def wait_for_form_change(self, previous_signature: str) -> bool:
        self.page.wait_for_timeout(max(0, self.config.dom_settle_ms))
        try:
            return self.form_signature() != previous_signature
        except Exception:
            return False

    def html_snapshot(self, name: str = "page.html") -> str:
        if not self.config.save_html:
            return ""
        path = self.artifacts.path(name)
        path.write_text(sanitize_html_snapshot(self.page.content()), encoding="utf-8")
        self.artifacts.log("html_snapshot_saved", path=str(path))
        return str(path)

    def _challenge_clearance_evidence(self, reasons: list[str]) -> bool:
        """Require positive page evidence; a hidden CAPTCHA alone is insufficient."""
        if not self.page or self.page.is_closed():
            return False
        if "CAPTCHA_REQUIRED" in reasons:
            try:
                solved = self.page.locator(
                    'textarea[name="g-recaptcha-response"], input[name="g-recaptcha-response"], '
                    'textarea[name="h-captcha-response"], input[name="h-captcha-response"], '
                    'textarea[name="cf-turnstile-response"], input[name="cf-turnstile-response"]'
                ).evaluate_all("els => els.some(e => String(e.value || '').trim().length > 20)")
            except Exception:
                solved = False
            if not solved:
                return False
        if any(reason in reasons for reason in ("LOGIN_REQUIRED", "MFA_REQUIRED", "OTP_REQUIRED")):
            try:
                controls = self.page.locator("form input, form select, form textarea").count()
                password = self.page.locator('input[type="password"]:visible').count()
            except Exception:
                return False
            if controls == 0 or password:
                return False
        return True

    def manual_user_handoff(self, reasons: list[str], blocker_detector: Any = None) -> bool:
        if self.config.headless or not self.config.manual_handoff:
            return False
        self.artifacts.log("manual_handoff_started", reasons=reasons)
        print("Browser requires manual action: " + ", ".join(reasons))
        print("Complete login/MFA/CAPTCHA in the headed browser. Do not submit the application.")
        print("The page will resume only after challenge-clearance evidence is detected.")
        deadline = time.monotonic() + max(1, int(self.config.manual_handoff_timeout_seconds))
        stable_checks = 0
        outcome = "TIMED_OUT"
        while time.monotonic() < deadline:
            try:
                remaining = list(blocker_detector() if blocker_detector else reasons)
                cleared = not remaining and self._challenge_clearance_evidence(reasons)
                stable_checks = stable_checks + 1 if cleared else 0
                if stable_checks >= 2:
                    outcome = "CLEARED"
                    break
                self.page.wait_for_timeout(1_000)
            except Exception:
                outcome = "BROWSER_CLOSED" if not self.page or self.page.is_closed() else "FAILED"
                break
        resumed = outcome == "CLEARED"
        self.artifacts.log("manual_handoff_finished", resumed=resumed, outcome=outcome)
        return resumed

    def close(self) -> None:
        self.artifacts.log("browser_closing", blocked_requests=self.guard.blocked_requests)
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self._playwright:
            self._playwright.stop()
        self.page = self.context = self.browser = self._playwright = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
