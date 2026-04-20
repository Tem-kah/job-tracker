"""
Job posting scraper that checks target companies for new roles matching
configured job titles, persists seen jobs, and emails new ones.
"""

import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration: companies + the titles you care about.
# Most modern ATS platforms (Greenhouse, Lever, Ashby) expose a JSON endpoint,
# which is much more reliable than scraping the HTML career site.
# ---------------------------------------------------------------------------

TARGET_TITLES = [
    "machine learning engineer",
    "ml engineer",
    "mlops engineer",
    "data scientist",
]

COMPANIES = [
    {
        "name": "Anthropic",
        "type": "greenhouse",
        "slug": "anthropic",
    },
    {
        "name": "Databricks",
        "type": "greenhouse",
        "slug": "databricks",
    },
]

SEEN_JOBS_FILE = Path("seen_jobs.json")
USER_AGENT = "Mozilla/5.0 (compatible; JobTrackerBot/1.0)"


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------


def fetch_greenhouse(slug: str) -> list[dict]:
    """Greenhouse exposes a public JSON board for every company."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        {
            "id": str(job["id"]),
            "title": job["title"],
            "location": job.get("location", {}).get("name", "N/A"),
            "url": job["absolute_url"],
        }
        for job in jobs
    ]


def fetch_lever(slug: str) -> list[dict]:
    """Lever also has a public JSON endpoint."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return [
        {
            "id": job["id"],
            "title": job["text"],
            "location": job.get("categories", {}).get("location", "N/A"),
            "url": job["hostedUrl"],
        }
        for job in resp.json()
    ]


def fetch_html(url: str, title_selector: str, link_selector: str) -> list[dict]:
    """Generic HTML fallback for companies without a public API."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    jobs = []
    for title_el, link_el in zip(soup.select(title_selector), soup.select(link_selector)):
        title = title_el.get_text(strip=True)
        href = link_el.get("href", "")
        if href.startswith("/"):
            # Resolve relative URLs against the careers page origin.
            from urllib.parse import urljoin
            href = urljoin(url, href)
        jobs.append({
            "id": href,  # URL is a stable ID for HTML scrapes
            "title": title,
            "location": "N/A",
            "url": href,
        })
    return jobs


def fetch_company(company: dict) -> list[dict]:
    kind = company["type"]
    if kind == "greenhouse":
        return fetch_greenhouse(company["slug"])
    if kind == "lever":
        return fetch_lever(company["slug"])
    if kind == "html":
        return fetch_html(company["url"], company["title_selector"], company["link_selector"])
    raise ValueError(f"Unknown scraper type: {kind}")


# ---------------------------------------------------------------------------
# Filtering + state
# ---------------------------------------------------------------------------


def matches_target(title: str) -> bool:
    low = title.lower()
    return any(t in low for t in TARGET_TITLES)


def load_seen() -> dict:
    if SEEN_JOBS_FILE.exists():
        return json.loads(SEEN_JOBS_FILE.read_text())
    return {}


def save_seen(seen: dict) -> None:
    SEEN_JOBS_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def send_email(new_jobs: list[dict]) -> None:
    sender = os.environ["EMAIL_SENDER"]
    password = os.environ["EMAIL_PASSWORD"]
    recipient = os.environ["EMAIL_RECIPIENT"]
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    subject = f"🔔 {len(new_jobs)} new job posting(s) matched"

    # Plain text
    text_lines = [f"Found {len(new_jobs)} new matching job(s):\n"]
    for j in new_jobs:
        text_lines.append(f"- [{j['company']}] {j['title']} ({j['location']})")
        text_lines.append(f"  {j['url']}\n")
    text_body = "\n".join(text_lines)

    # HTML
    html_rows = "".join(
        f"<li><strong>{j['company']}</strong> — "
        f"<a href='{j['url']}'>{j['title']}</a> "
        f"<em>({j['location']})</em></li>"
        for j in new_jobs
    )
    html_body = f"""
    <html><body>
      <h2>New job postings matched</h2>
      <ul>{html_rows}</ul>
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
            current_ids.append(job["id"])
            if not matches_target(job["title"]):
                continue
            if job["id"] in company_seen:
                continue
            new_jobs.append({**job, "company": name})
            print(f"  NEW: {job['title']} ({job['location']})")

        # Keep the union of previously-seen + currently-listed IDs so a job
        # that briefly disappears and comes back doesn't re-alert.
        seen[name] = sorted(company_seen.union(current_ids))

    save_seen(seen)

    if new_jobs:
        send_email(new_jobs)
    else:
        print("No new matching jobs found.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
