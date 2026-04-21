# Job Tracker

GitHub Actions workflow that scrapes company career pages 4x/day and emails you when a new posting matches your target titles.

## What it does

- Runs at **01:00, 07:00, 13:00, 19:00 UTC** daily (`cron: "0 1,7,13,19 * * *"`)
- Checks each company in `COMPANIES` (default: Anthropic, Databricks)
- Filters jobs by titles in `TARGET_TITLES` (default: ML/MLOps/Data Scientist roles)
- Compares against `seen_jobs.json` and emails you only brand-new postings
- Commits the updated state back to the repo so we never re-alert

## One-time setup

### 1. Create a Gmail App Password (recommended)

Regular Gmail passwords won't work with SMTP. You need an app password:

1. Turn on 2-Step Verification: https://myaccount.google.com/security
2. Generate an app password: https://myaccount.google.com/apppasswords
3. Copy the 16-character password — you'll paste it into GitHub Secrets

(If you use a different provider, set `SMTP_SERVER` and `SMTP_PORT` secrets too.)

### 2. Add GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name       | Value                               |
|-------------------|-------------------------------------|
| `EMAIL_SENDER`    | Your Gmail address (e.g. `you@gmail.com`) |
| `EMAIL_PASSWORD`  | The 16-char app password            |
| `EMAIL_RECIPIENT` | Where to send alerts (can be same address) |
| `SMTP_SERVER`     | *(optional)* defaults to `smtp.gmail.com` |
| `SMTP_PORT`       | *(optional)* defaults to `587`      |

### 3. Push to GitHub and enable Actions

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Then go to the **Actions** tab and click **Run workflow** on "Job Tracker" to test it.

## Customizing

### Change target titles
Edit `TARGET_TITLES` in `scraper.py`:
```python
TARGET_TITLES = ["machine learning engineer", "mlops engineer"]
```
Matching is case-insensitive substring match.

### Change companies
Edit `COMPANIES` in `scraper.py`. Three scraper types supported:

**Greenhouse** (most common — Anthropic, Databricks, Airbnb, Stripe, etc.)
```python
{"name": "Stripe", "type": "greenhouse", "slug": "stripe"}
```
The slug is in the URL of their jobs page: `boards.greenhouse.io/<slug>`.

**Lever** (Netflix, Shopify, etc.)
```python
{"name": "Netflix", "type": "lever", "slug": "netflix"}
```
The slug is in `jobs.lever.co/<slug>`.

**Custom HTML** (fallback for sites without an API)
```python
{
  "name": "SomeCo",
  "type": "html",
  "url": "https://someco.com/careers",
  "title_selector": ".job-title",
  "link_selector": "a.job-link",
}
```

## First run

The very first run will save *all* currently-listed matching jobs as "seen" and email you zero new jobs. From then on, only genuinely new postings trigger an alert.

If you'd rather get an initial dump of what currently matches, delete `seen_jobs.json` before the first run.

## Local testing

```bash
pip install -r requirements.txt
export EMAIL_SENDER="you@gmail.com"
export EMAIL_PASSWORD="xxxx xxxx xxxx xxxx"
export EMAIL_RECIPIENT="you@gmail.com"
python scraper.py
```
