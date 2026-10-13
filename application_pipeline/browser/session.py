"""Playwright Chromium session with private artifacts and safety guards."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup

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
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **details,
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
    for tag in soup.select('input[type="hidden"], input[type="password"]'):
        tag["value"] = "[REDACTED]"
    for tag in soup.find_all(True):
        for attribute in list(tag.attrs):
            if any(marker in attribute.lower() for marker in ("token", "secret", "csrf", "auth")):
                tag[attribute] = "[REDACTED]"
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
        self.page.screenshot(path=str(path), full_page=full_page, animations="disabled")
        self.artifacts.log("screenshot_saved", path=str(path))
        return str(path)

    def html_snapshot(self, name: str = "page.html") -> str:
        if not self.config.save_html:
            return ""
        path = self.artifacts.path(name)
        path.write_text(sanitize_html_snapshot(self.page.content()), encoding="utf-8")
        self.artifacts.log("html_snapshot_saved", path=str(path))
        return str(path)

    def manual_user_handoff(self, reasons: list[str]) -> bool:
        if self.config.headless or not self.config.manual_handoff:
            return False
        self.artifacts.log("manual_handoff_started", reasons=reasons)
        print("Browser requires manual action: " + ", ".join(reasons))
        print("Complete login/MFA/CAPTCHA in the headed browser. Do not submit the application.")
        response = input("Press Enter to resume inspection, or type 'abort': ").strip().lower()
        resumed = response != "abort"
        self.artifacts.log("manual_handoff_finished", resumed=resumed)
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
