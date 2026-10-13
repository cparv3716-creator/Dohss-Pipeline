# Job Application Preparation Milestone

This pipeline discovers, enriches, evaluates, scores, prepares, and safely
inspects application records. Browser dry runs can fill approved fields for
review, but cannot submit an application.

## Private setup

1. Copy `candidate/profile.example.yaml` to `candidate/profile.yaml`.
2. Copy `candidate/application_answers.example.yaml` to
   `candidate/application_answers.yaml`.
3. Fill only known facts. A fact is usable by matching only when
   `verified: true` and `unknown: false`.
4. An answer is eligible for automatic preparation only when both
   `verified: true` and `auto_answer_allowed: true`.

Both real YAML files and `data/private/` are git-ignored. The tracked
`data/pipeline.db` contains public discovery and job-page facts only.
Candidate-derived eligibility, matching, and prepared answers are stored in
`data/private/applications.db`.

## Commands

```text
python main.py enrich-jobs --limit 25
python main.py score-jobs --limit 25
python main.py prepare-applications --limit 25
python main.py prepare-applications --no-discovery --limit 25
python main.py review-applications
python main.py job 123
python main.py inspect-application 1
python main.py dry-run-application 1
python main.py dry-run-application 1 --headed --manual-handoff
```

`prepare-applications` uses the existing discovery scrapers unless
`--no-discovery` is passed. Individual discovery and enrichment failures are
recorded or reported without aborting the full batch.

Telegram preparation alerts are disabled by default. Set
`applications.notify_telegram: true` in the ignored `config.local.yaml` to send
only `READY_TO_APPLY` and `NEEDS_REVIEW` summaries; ineligible jobs are omitted.

## Browser dry-run setup

Install Chromium once after installing the Python dependencies:

```text
python -m playwright install chromium
```

Browser settings live under `applications.browser` in `config.yaml`. Put the
selected resume only at the configured `resume_path` under an allowed, ignored
private root (by default `data/private/resumes/`). Reports, screenshots,
sanitized HTML snapshots, and structured logs are written under the ignored
`data/private/browser_runs/` directory.

`inspect-application` only reads and normalizes a form. `dry-run-application`
fills answers only when their provenance is verified, automatic answering is
allowed, classification confidence meets the threshold, and the central
sensitivity policy permits the field. Greenhouse and Lever have dedicated
adapters. Generic ATS pages are inspection-only.

In headed mode, `--manual-handoff` lets a person complete a legitimate login,
MFA, OTP, or CAPTCHA and then resume inspection. The automation never attempts
to solve or bypass those controls.

## Safety boundary

The state API rejects `SUBMITTED`, and the private SQLite database has triggers
that reject `SUBMITTED`, `submitted_at`, or `submission_reference` writes.
Unknown profile data remains unknown and causes review where it affects a hard
rule or required application answer. The ATS interface intentionally has no
submission method. Browser guards cancel form submission events, block likely
submission network requests, never press Enter to advance a form, and report
final submit controls as `FINAL_SUBMIT_BLOCKED_BY_POLICY`. Authentication and
anti-bot challenges become `NEEDS_USER_ACTION`; they are never bypassed.
