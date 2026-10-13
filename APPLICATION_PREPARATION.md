# Job Application Preparation Milestone

This milestone discovers, enriches, evaluates, scores, and prepares application
records. It does not open an application form or submit anything.

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
```

`prepare-applications` uses the existing discovery scrapers unless
`--no-discovery` is passed. Individual discovery and enrichment failures are
recorded or reported without aborting the full batch.

Telegram preparation alerts are disabled by default. Set
`applications.notify_telegram: true` in the ignored `config.local.yaml` to send
only `READY_TO_APPLY` and `NEEDS_REVIEW` summaries; ineligible jobs are omitted.

## Safety boundary

The state API rejects `SUBMITTED`, and the private SQLite database has triggers
that reject `SUBMITTED`, `submitted_at`, or `submission_reference` writes.
Unknown profile data remains unknown and causes review where it affects a hard
rule or required application answer. No CAPTCHA, authentication, MFA, browser
automation, or form submission code exists in this milestone.
