# EasyLeadz HR enrichment automation

This integration enriches **known/vetted HR LinkedIn profile URLs** through the EasyLeadz API. It does not scrape LinkedIn.

## Safety model

- The public DoHSS SQLite database may contain HR names/roles/LinkedIn URLs, but EasyLeadz callback payloads can contain phone numbers and personal email addresses.
- Callback payloads are therefore stored under `data/private/`, which is git-ignored.
- API submission is **dry-run by default**. Live calls only happen when `EASYLEADZ_LIVE_MODE=true`.
- Previously submitted LinkedIn URLs are tracked so scheduled runs do not repeatedly spend credits on the same profiles.

## 1. Import vetted HR profiles

Export the `Best HR Contacts` sheet as CSV, then run locally:

```bash
python scripts/import_hr_contacts.py /path/to/hr_contacts.csv --min-confidence 80
```

This loads the profile name, company, role and LinkedIn URL into the existing `hr_contacts` table.

## 2. Host the callback receiver

EasyLeadz posts enrichment results asynchronously to a public callback URL. Deploy this command on any HTTPS-capable service with persistent/private disk:

```bash
export EASYLEADZ_WEBHOOK_TOKEN='generate-a-long-random-secret'
export EASYLEADZ_PRIVATE_STORE='/private/path/easyleadz_results.jsonl'
python scripts/easyleadz_webhook.py
```

Health check:

```text
GET /health
```

Configure the callback URL as:

```text
https://YOUR_HOST/easyleadz/YOUR_LONG_RANDOM_TOKEN
```

Do **not** place the callback results file in the public repository.

## 3. GitHub repository secrets

Set these repository secrets:

- `EASYLEADZ_API_KEY` — API key issued by EasyLeadz.
- `EASYLEADZ_CALLBACK_URL` — public HTTPS callback URL above.
- `EASYLEADZ_LIVE_MODE` — keep `false` until dry-run output is verified; set to `true` to allow paid/live enrichment.

EasyLeadz API access is plan/use-case dependent. Confirm your account has API access before switching live mode on.

## 4. Manual dry run

Without live mode, this cannot consume EasyLeadz credits:

```bash
python scripts/easyleadz_submit.py --db data/pipeline.db --limit 25
```

You can also test directly against a CSV:

```bash
python scripts/easyleadz_submit.py --csv /path/to/hr_contacts.csv --limit 25 --min-confidence 90
```

## 5. Live run

```bash
export EASYLEADZ_API_KEY='...'
export EASYLEADZ_CALLBACK_URL='https://YOUR_HOST/easyleadz/YOUR_TOKEN'
export EASYLEADZ_LIVE_MODE=true
python scripts/easyleadz_submit.py --db data/pipeline.db --limit 25
```

## 6. GitHub Actions

The workflow `.github/workflows/easyleadz-enrichment.yml` runs daily at 02:30 UTC and can also be started manually. It remains a dry run unless `EASYLEADZ_LIVE_MODE` is explicitly set to `true` in repository secrets.

The workflow restores prior submission state using GitHub Actions cache, preventing duplicate submissions of the same LinkedIn URL under normal operation.

## Vendor API behavior

The client follows EasyLeadz's documented API flow:

1. Send a GET request to `https://app.easyleadz.com/api/prod/` with JSON body containing a LinkedIn URL and `callbackUrl`.
2. Pass the API key in the `Enapi-Key` header.
3. Store the returned `request_id`.
4. EasyLeadz later POSTs phone/email results to the callback URL.
5. The documented API rate limit is 10 calls/second; DoHSS deliberately stays below that ceiling.
