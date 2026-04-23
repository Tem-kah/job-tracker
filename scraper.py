"""
Job posting scraper that checks target companies for new roles matching
configured titles, filters by level + YoE + freshness, persists seen jobs,
and emails new ones with LinkedIn recruiter search links.
"""

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Titles we're looking for (case-insensitive substring match)
TARGET_TITLES = [
    "machine learning engineer",
    "ml engineer",
    "mlops engineer",
    "data scientist",
]

# Titles to EXCLUDE — filters out senior-level roles
EXCLUDED_TITLE_KEYWORDS = [
    "senior", "sr.", "sr ",
    "staff", "principal", "lead",
    "director", "head of", "vp ", "vice president",
    "manager",
]

# Max years of experience acceptable (parsed from job description)
MAX_YOE = 4

# Only alert on jobs posted within this window (days)
FRESHNESS_DAYS = 14

COMPANIES = [
    {"name": "Anthropic",  "type": "greenhouse", "slug": "anthropic"},
    {"name": "OpenAI",     "type": "greenhouse", "slug": "openai"},
    {"name": "Scale AI",   "type": "greenhouse", "slug": "scaleai"},
    {"name": "Databricks", "type": "greenhouse", "slug": "databricks"},
    {"name": "Stripe",     "type": "greenhouse", "slug": "stripe"},
    {"name": "Airbnb",     "type": "greenhouse", "slug": "airbnb"},
    {"name": "Pinterest",  "type": "greenhouse", "slug": "pinterest"},
]

SEEN_JOBS_FILE = Path("seen_jobs.json")
USER_AGENT = "Mozilla/5.0 (compatible; JobTrackerBot/1.0)"


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------


def fetch_greenhouse(slug: str) -> list[dict]:
    """Greenhouse JSON board with per-job content (description) included."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    out = []
    for job in jobs:
        # Content is HTML-encoded; strip tags so our regex runs on clean text.
        content_html = job.get("content", "") or ""
        description = BeautifulSoup(content_html, "html.parser").get_text(" ", strip=True)
        out.append({
            "id": str(job["id"]),
            "title": job["title"],
            "location": job.get("location", {}).get("name", "N/A"),
            "url": job["absolute_url"],
            "updated_at": job.get("updated_at"),  # ISO 8601
            "description": description,
        })
    return out


def fetch_lever(slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    out = []
    for job in resp.json():
        out.append({
            "id": job["id"],
            "title": job["text"],
            "location": job.get("categories", {}).get("location", "N/A"),
            "url": job["hostedUrl"],
            "updated_at": datetime.fromtimestamp(
                job.get("createdAt", 0) / 1000, tz=timezone.utc
            ).isoformat() if job.get("createdAt") else None,
            "description": job.get("descriptionPlain", ""),
        })
    return out


def fetch_company(company: dict) -> list[dict]:
    kind = company["type"]
    if kind == "greenhouse":
        return fetch_greenhouse(company["slug"])
    if kind == "lever":
        return fetch_lever(company["slug"])
    raise ValueError(f"Unknown scraper type: {kind}")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def matches_target(title: str) -> bool:
    low = title.lower()
    return any(t in low for t in TARGET_TITLES)


def is_excluded_level(title: str) -> bool:
    """True if the title contains a senior-level keyword we want to skip."""
    low = f" {title.lower()} "  # pad so 'vp ' / 'sr ' match at boundaries
    return any(kw in low for kw in EXCLUDED_TITLE_KEYWORDS)


# Matches phrases like "5+ years", "minimum 7 years of experience",
# "5-10 years of relevant experience", "at least 6 years of industry experience"
YOE_PATTERN = re.compile(
    r"(\d{1,2})\s*(?:\+|-|to|–|—)?\s*\d{0,2}\s*\+?\s*years?\s+"
    r"(?:of\s+)?(?:relevant\s+|professional\s+|industry\s+|work\s+)?"
    r"(?:experience|exp\b)",
    re.IGNORECASE,
)


def extract_min_yoe(description: str) -> int | None:
    """Return the smallest YoE number mentioned in the description, or None."""
    if not description:
        return None
    matches = YOE_PATTERN.findall(description)
    if not matches:
        return None
    years = [int(m) for m in matches if m.isdigit()]
    return min(years) if years else None


def exceeds_yoe_cap(description: str, cap: int = MAX_YOE) -> bool:
    """True if the job requires strictly more than `cap` years of experience."""
    yoe = extract_min_yoe(description)
    return yoe is not None and yoe > cap


def is_fresh(updated_at: str | None, window_days: int = FRESHNESS_DAYS) -> bool:
    """True if the posting was updated within the freshness window."""
    if not updated_at:
        return True  # if unknown, don't exclude
    try:
        posted = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    return posted >= cutoff


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_seen() -> dict:
    if SEEN_JOBS_FILE.exists():
        return json.loads(SEEN_JOBS_FILE.read_text())
    return {}


def save_seen(seen: dict) -> None:
    SEEN_JOBS_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# LinkedIn helper
# ---------------------------------------------------------------------------


def linkedin_recruiter_search_url(company: str) -> str:
    """Pre-filled LinkedIn people search for recruiters at this company."""
    query = quote_plus(f'"{company}" recruiter')
    return f"https://www.linkedin.com/search/results/people/?keywords={query}"


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def send_email(new_jobs: list[dict]) -> None:
    sender = os.environ["EMAIL_SENDER"]
    password = os.environ["EMAIL_PASSWORD"]
    recipient = os.environ["EMAIL_RECIPIENT"]
    smtp_server = os.environ.get("SMTP_SERVER") or "smtp.gmail.com"
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")

    subject = f"🔔 {len(new_jobs)} new job posting(s) matched"

    # Plain text body
    text_lines = [f"Found {len(new_jobs)} new matching job(s):\n"]
    for j in new_jobs:
        text_lines.append(f"- [{j['company']}] {j['title']} ({j['location']})")
        text_lines.append(f"  Apply: {j['url']}")
        text_lines.append(f"  Recruiters: {linkedin_recruiter_search_url(j['company'])}\n")
    text_body = "\n".join(text_lines)

    # HTML body
    html_rows = []
    for j in new_jobs:
        li_url = linkedin_recruiter_search_url(j["company"])
        html_rows.append(f"""
          <li style="margin-bottom: 16px;">
            <strong>{j['company']}</strong> —
            <a href="{j['url']}">{j['title']}</a>
            <em>({j['location']})</em>
            <br>
            <a href="{li_url}">🔍 Find recruiters on LinkedIn</a>
          </li>
        """)
    html_body = f"""
    <html><body style="font-family: -apple-system, sans-serif;">
      <h2>New job postings matched</h2>
      <ul>{"".join(html_rows)}</ul>
      <p style="color:#888;font-size:12px;">
        Filters: title match, excludes senior+ levels, max {MAX_YOE} YoE,
        posted within {FRESHNESS_DAYS} days.
      </p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)

    print(f"Email sent to {recipient} with {len(new_jobs)} new job(s).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    seen = load_seen()
    new_jobs = []
    stats = {"scanned": 0, "title_match": 0, "excluded_level": 0,
             "excluded_yoe": 0, "stale": 0, "already_seen": 0, "new": 0}

    for company in COMPANIES:
        name = company["name"]
        print(f"Checking {name}...")
        try:
            jobs = fetch_company(company)
        except Exception as e:
            print(f"  ERROR fetching {name}: {e}", file=sys.stderr)
            continue

        company_seen = set(seen.get(name, []))
        current_ids = []

        for job in jobs:
            stats["scanned"] += 1
            current_ids.append(job["id"])

            # Stage 1: target title match
            if not matches_target(job["title"]):
                continue
            stats["title_match"] += 1

            # Stage 2: level filter
            if is_excluded_level(job["title"]):
                stats["excluded_level"] += 1
                continue

            # Stage 3: YoE filter (uses description)
            if exceeds_yoe_cap(job.get("description", "")):
                stats["excluded_yoe"] += 1
                print(f"  skip (YoE > {MAX_YOE}): {job['title']}")
                continue

            # Stage 4: freshness filter
            if not is_fresh(job.get("updated_at")):
                stats["stale"] += 1
                continue

            # Stage 5: dedup against seen state
            if job["id"] in company_seen:
                stats["already_seen"] += 1
                continue

            stats["new"] += 1
            new_jobs.append({**job, "company": name})
            print(f"  NEW: {job['title']} ({job['location']})")

        seen[name] = sorted(company_seen.union(current_ids))

    save_seen(seen)

    print(f"\nStats: {stats}")

    if new_jobs:
        send_email(new_jobs)
    else:
        print("No new matching jobs found.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
