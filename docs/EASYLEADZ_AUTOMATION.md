# EasyLeadz HR enrichment automation

The default DoHSS workflow now supports a normal **EasyLeadz subscription without API access**.

DoHSS does **not** automate LinkedIn browsing or scrape LinkedIn. It works from HR LinkedIn URLs that are already known/vetted.

## Default workflow: no API required

1. Import vetted HR profiles into `hr_contacts`.
2. Generate a private upload file for EasyLeadz.
3. Upload/enrich that file in the EasyLeadz dashboard / EasySearch / Bulk Upload available on your subscription.
4. Download the EasyLeadz result CSV.
5. Import that CSV back into DoHSS.
6. DoHSS matches results by LinkedIn URL and creates a private Excel workbook with phone/email data.

Phone numbers and personal emails are stored under `data/private/`, which is git-ignored. Do not commit those files to this public repository.

## 1. Import vetted HR profiles

Export the cleaned `Best HR Contacts` sheet as CSV and run:

```bash
python scripts/import_hr_contacts.py /path/to/hr_contacts.csv --min-confidence 80
```

This loads name, company, role, LinkedIn URL and confidence into the existing `hr_contacts` table.

## 2. Prepare the EasyLeadz upload file

```bash
python scripts/easyleadz_prepare_csv.py \
  --db data/pipeline.db \
  --min-confidence 80 \
  --out data/private/easyleadz_upload.csv
```

Output columns:

- `LinkedIn URL`
- `Name`
- `Company`
- `Designation`
- `Confidence`

Use the file (or its LinkedIn URL column) with the EasyLeadz dashboard feature available on your subscription.

## 3. Enrich inside EasyLeadz

Upload/search the generated LinkedIn URLs in EasyLeadz, then download/export the result as CSV.

The exact EasyLeadz column names may vary. The DoHSS importer accepts common variants such as:

- LinkedIn: `LinkedIn URL`, `LinkedIn`, `profile_url`, `linkedin_profile_url`
- Phone: `Phone`, `phone1`, `Mobile`, `Phone Number`, `Direct Dial`
- Secondary phone: `phone2`, `Alternate Phone`
- Work email: `Work Email`, `Business Email`, `Official Email`, `Email`, `email1`
- Personal email: `Personal Email`, `email2`, `Secondary Email`

## 4. Import EasyLeadz results and create Excel

```bash
python scripts/easyleadz_import_results.py /path/to/easyleadz_results.csv
```

Defaults:

- private SQLite: `data/private/easyleadz_contacts.db`
- final Excel: `data/private/hr_contacts_enriched.xlsx`

The Excel contains:

- Company
- HR Name
- HR Role
- LinkedIn
- Phone 1
- Phone 2
- Work Email
- Personal Email
- HR Confidence %
- Enrichment Status
- Source File
- Imported At

The importer is idempotent: importing a later EasyLeadz export updates existing LinkedIn records rather than creating duplicates.

## Privacy model

The main DoHSS repository is public and currently persists `data/pipeline.db`, so enriched phone/email fields are intentionally **not** written into that public database.

Sensitive data is kept in:

```text
data/private/
```

That directory is excluded by `.gitignore`.

## Optional API mode

API support remains in the repository only for accounts that separately receive EasyLeadz API access.

Without API access, ignore:

- `EASYLEADZ_API_KEY`
- `EASYLEADZ_CALLBACK_URL`
- `EASYLEADZ_LIVE_MODE`
- `scripts/easyleadz_submit.py`
- `scripts/easyleadz_webhook.py`

The normal subscription workflow above does not require any of them.

If API access is later enabled, the existing manual GitHub Actions API job can be used. There is no scheduled live API run by default.
