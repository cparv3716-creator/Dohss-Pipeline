# Phase 3 implementation plan: scheduled preparation and review

Phase 3 automates discovery through review preparation only. It does not add a
submission API, a submit browser action, or a `SUBMITTED` state transition.

## Safety boundary

- Preserve the database trigger, state-machine restriction, browser submit
  control guard, Enter-key guard, and final-submission network guard.
- Treat CAPTCHA, MFA, OTP, login, legal attestations, demographics, sensitive
  questions, unknown facts, and ambiguous controls as human work.
- Run candidate-aware work only on a private machine or private worker with an
  encrypted private database and ignored candidate artifacts. The public
  GitHub Actions discovery database must never receive candidate data.
- Make every scheduled operation idempotent and auditable. A retry may inspect
  or prepare again, but it must never convert preparation into submission.

## Proposed architecture

```text
Source schedules
  -> discovery + canonical URL deduplication
  -> liveness/expiry check
  -> detail enrichment + ATS detection
  -> hard eligibility rules
  -> provider-independent match scoring
  -> preparation work queue
  -> Greenhouse/Lever safe browser dry-run
  -> persistent review queue
  -> Telegram review notification
  -> local headed review/manual handoff
  -> stop before final submission
```

Use a small persistent scheduler service rather than coupling candidate work to
the existing scraper callbacks. Suggested modules:

- `application_pipeline/orchestration/scheduler.py`: interval and quiet-hours
  policy, single-instance lease, graceful shutdown.
- `application_pipeline/orchestration/work_queue.py`: durable jobs, attempts,
  next-attempt timestamps, idempotency keys, and dead-letter state.
- `application_pipeline/orchestration/discovery.py`: source execution,
  canonicalization, deduplication, liveness, and expiry.
- `application_pipeline/orchestration/preparation.py`: enrichment,
  eligibility, score threshold, and preparation dispatch.
- `application_pipeline/orchestration/browser_worker.py`: bounded ATS dry-runs,
  artifact retention, rate limits, and review-queue updates.
- `application_pipeline/orchestration/notifications.py`: deduplicated Telegram
  notices and reminder policy.
- `application_pipeline/orchestration/leases.py`: SQLite-backed process and
  item leases so laptop sleep/restart cannot duplicate work.

## Persistence changes

Keep discovery data in the public database and add only non-candidate fields
needed for canonical URLs, first/last seen timestamps, expiry status, and
source fingerprints. Keep all assessments, browser runs, answers, artifacts,
notification state, and queue state in the private database.

Private queue records should include:

- operation type and idempotency key;
- application/job ID, state, priority, attempt count, and lease expiry;
- `not_before`, last error category, and sanitized diagnostic code;
- timestamps for enrichment, scoring, dry-run, review, and notification;
- artifact/report identifiers, never arbitrary machine paths or candidate
  values.

Add migrations only; never replace either database.

## Scheduling and retry policy

- Windows first: run a single `python main.py preparation-worker` process at
  login through Task Scheduler, restart on failure, and wake at configured
  intervals. Store logs and private data under `data/private/` with restrictive
  local permissions.
- Give each source and ATS its own concurrency and minimum-delay limit. Start
  with one browser worker and no parallel requests to the same ATS/company.
- Retry transient navigation, timeout, and 5xx failures with exponential
  backoff and jitter. Do not retry CAPTCHA, login, forbidden/rate-limited, hard
  eligibility, unknown-required-answer, or policy-blocked states automatically.
- Recheck liveness before browser preparation. Mark expired jobs without
  deleting historical records.
- Use a configurable verified `minimum_match_score`; hard eligibility always
  takes precedence.

## Unattended versus human operations

May run unattended:

- discovery, deduplication, expiry checks, enrichment, ATS detection;
- deterministic eligibility and matching;
- supported-form inspection, verified safe factual filling, configured resume
  upload, conditional reinspection, sanitized reports, readiness calculation;
- queue maintenance and Telegram notification.

Must remain human-controlled:

- CAPTCHA/MFA/OTP/login completion;
- unknown, ambiguous, legal, demographic, sensitive, or policy-blocked answers;
- approval of grounded free-text drafts;
- final review and every final application submission action outside this
  system.

## Telegram review notifications

Send one deduplicated notification on material state change, containing role,
company, ATS, eligibility, match score, readiness score, unresolved count,
resume state, blocker codes, and a local review command. Never include candidate
answers, contact values, resume contents, screenshots, session data, or secrets.
Use reminder cooldowns and acknowledgement state to prevent spam.

## Browser-session recovery

Persist preparation state and sanitized artifacts, not passwords, OTPs,
CAPTCHA tokens, cookies, or browser profiles by default. After a restart,
reopen the application and replay only verified safe preparation. A headed
manual-handoff session may remain open locally while attended, but an expired
or closed session returns to the review queue.

## Secure hosted-worker option

After the Windows worker is stable, use a private VM/container runner with an
encrypted persistent volume, OS secret store, restricted service account,
outbound allow-listing where practical, and log redaction. The public repository
workflow is suitable for non-sensitive discovery only; candidate-aware browser
work requires a private runner and private artifacts. Hosted interactive CAPTCHA
handoff needs a secure remote desktop channel and explicit operator presence.

## Delivery sequence

1. Add queue schema, migrations, leases, idempotency, expiry, and dry-run CLI.
2. Route existing discovery into queue items without changing notifications.
3. Add enrichment/eligibility/matching worker and score threshold.
4. Add rate-limited Greenhouse/Lever preparation worker and recovery.
5. Add review-state Telegram notifications and local review commands.
6. Add Windows Task Scheduler installation/status scripts.
7. Soak-test locally, then design the private hosted runner.

## Acceptance criteria

- Repeated runs create no duplicate job, application, browser-run, or
  notification work for the same idempotency key.
- Expired/ineligible jobs never reach browser preparation.
- Every opened form was enriched, eligibility-checked, and score-filtered.
- Supported forms retain Phase 2.5 detection/fill accuracy and zero observed
  incorrect automatic fills.
- Unknown or sensitive required questions enter the review queue unchanged.
- Laptop restart/sleep recovers queued work without losing audit history.
- Rate limits, backoff, leases, dead-letter handling, and sanitized telemetry
  have automated tests.
- Telegram notifications are deduplicated and contain no private values.
- All submission guards remain independently tested, and the system contains
  no callable submit capability or `SUBMITTED` transition.
