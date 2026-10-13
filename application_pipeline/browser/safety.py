"""Central answer, resume, and final-submission safety policy."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from candidate.answer_bank import AnswerEntry


SENSITIVE_TYPES = {
    "CURRENT_CTC", "EXPECTED_CTC", "VISA_SPONSORSHIP", "RELOCATION",
}
SENSITIVE_PATTERNS = (
    r"criminal|conviction|background check",
    r"disab|self[- ]identif|veteran|race|ethnicity|gender|sexual orientation|demographic",
    r"conflict of interest|relative.*(company|employee)|family.*(company|employee)",
    r"legal attestation|certif(?:y|ication)|declare|under penalty|electronic signature",
    r"consent|privacy notice|terms and conditions|arbitration|non[- ]disclosure",
)
SUBMIT_TEXT = re.compile(r"\b(submit application|submit|final apply|send application|complete application)\b", re.I)
SUBMISSION_URL = re.compile(r"(?:/applications?(?:/|$)|/submit(?:/|$)|/candidate/apply(?:/|$))", re.I)


@dataclass(frozen=True)
class FillDecision:
    allowed: bool
    reason: str


@dataclass
class SafeFillPolicy:
    minimum_confidence: int = 85
    allowed_sensitive_types: set[str] = field(default_factory=set)
    allowed_sensitivities: set[str] = field(default_factory=lambda: {"public", "private", "personal"})

    def evaluate(self, question: str, question_type: str, answer: Optional[AnswerEntry], field_sensitivity: str = "private") -> FillDecision:
        lower_question = str(question or "").lower()
        if question_type in SENSITIVE_TYPES and question_type not in self.allowed_sensitive_types:
            return FillDecision(False, "SENSITIVE_QUESTION_BLOCKED")
        if any(re.search(pattern, lower_question, re.I) for pattern in SENSITIVE_PATTERNS):
            return FillDecision(False, "SENSITIVE_QUESTION_BLOCKED")
        if field_sensitivity not in self.allowed_sensitivities and question_type not in self.allowed_sensitive_types:
            return FillDecision(False, "SENSITIVITY_POLICY_BLOCKED")
        if answer is None or answer.value in (None, ""):
            return FillDecision(False, "UNKNOWN_ANSWER")
        if not answer.verified:
            return FillDecision(False, "ANSWER_NOT_VERIFIED")
        if answer.confidence < self.minimum_confidence:
            return FillDecision(False, "CONFIDENCE_BELOW_THRESHOLD")
        if not answer.auto_answer_allowed:
            return FillDecision(False, "AUTO_ANSWER_NOT_ALLOWED")
        if answer.sensitivity not in self.allowed_sensitivities and question_type not in self.allowed_sensitive_types:
            return FillDecision(False, "SENSITIVITY_POLICY_BLOCKED")
        return FillDecision(True, "SAFE_VERIFIED_ANSWER")


@dataclass(frozen=True)
class ResumeArtifact:
    path: Path


class ResumePolicy:
    def __init__(self, configured_path: str = "", allowed_roots: Iterable[str] = ("data/private/resumes",)):
        self.configured_path = str(configured_path or "").strip()
        self.allowed_roots = tuple(Path(root).resolve() for root in allowed_roots)

    def validate(self) -> Optional[ResumeArtifact]:
        if not self.configured_path:
            return None
        path = Path(self.configured_path).resolve()
        if not path.is_file():
            raise ValueError(f"Configured resume does not exist: {path}")
        if path.suffix.lower() not in (".pdf", ".doc", ".docx", ".rtf", ".txt"):
            raise ValueError("Configured resume file type is not allowed")
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise ValueError("Configured resume is outside allowed private resume directories")
        try:
            ignored = subprocess.run(
                ["git", "check-ignore", "--quiet", str(path)],
                capture_output=True,
                check=False,
            ).returncode == 0
        except OSError:
            ignored = False
        if not ignored:
            raise ValueError("Configured resume is not protected by .gitignore")
        return ResumeArtifact(path)


class SubmissionGuard:
    """Independent DOM and network guard; adapters never expose submit()."""

    INIT_SCRIPT = r"""
    (() => {
      if (window.__dohssSubmissionGuardInstalled) return;
      window.__dohssSubmissionGuardInstalled = true;
      window.__dohssBlockedSubmissions = [];
      const record = (kind, detail) => window.__dohssBlockedSubmissions.push({kind, detail, at: new Date().toISOString()});
      document.addEventListener('submit', event => {
        event.preventDefault(); event.stopImmediatePropagation(); record('submit_event', event.target?.action || location.href);
      }, true);
      const nativeSubmit = HTMLFormElement.prototype.submit;
      HTMLFormElement.prototype.submit = function() { record('form_submit', this.action || location.href); };
      if (HTMLFormElement.prototype.requestSubmit) {
        HTMLFormElement.prototype.requestSubmit = function() { record('request_submit', this.action || location.href); };
      }
      document.addEventListener('click', event => {
        const target = event.target?.closest?.('button, input, [role="button"]');
        if (!target) return;
        const text = (target.innerText || target.value || target.getAttribute('aria-label') || '').trim();
        const isSubmit = target.type === 'submit' || /submit application|final apply|send application|complete application/i.test(text);
        if (isSubmit) { event.preventDefault(); event.stopImmediatePropagation(); record('submit_click', text); }
      }, true);
    })();
    """

    def __init__(self):
        self.blocked_requests: list[str] = []

    @staticmethod
    def is_submission_request(method: str, url: str, post_data: str = "") -> bool:
        if str(method).upper() not in {"POST", "PUT", "PATCH"}:
            return False
        if SUBMISSION_URL.search(str(url)):
            return True
        payload = str(post_data or "").lower()
        return any(marker in payload for marker in ('"submit"', "submit_application", "candidate_application"))

    def install(self, context: Any) -> None:
        context.add_init_script(self.INIT_SCRIPT)

        def route_handler(route: Any) -> None:
            request = route.request
            if self.is_submission_request(request.method, request.url, request.post_data or ""):
                self.blocked_requests.append(request.url)
                route.abort("blockedbyclient")
            else:
                route.continue_()

        context.route("**/*", route_handler)

    @staticmethod
    def submit_control_texts(page: Any) -> list[str]:
        try:
            return page.locator('button, input[type="submit"], [role="button"]').evaluate_all(
                "els => els.filter(e => { const s=getComputedStyle(e); return s.display!=='none' && s.visibility!=='hidden'; })"
                ".map(e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim())"
                ".filter(t => /submit application|submit|final apply|send application|complete application/i.test(t))"
            )
        except Exception:
            return []
