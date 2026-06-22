# Job Hunter v2 — Clean Linear Project Context

## 1. Project Summary

Job Hunter v2 is a self-hosted job monitoring system running on a GCP VM. It tracks H-1B sponsor and cap-exempt employers by scraping job postings directly from company career pages and ATS platforms instead of relying on LinkedIn, Indeed, or other aggregators.

The goal is to detect fresh job postings as early as possible, store them in a central database, and show them through a login-protected web dashboard where users can search, filter, track, and export jobs.

Current scope:

* Tracks around 893 companies.
* Runs on GCP project `job-hunter-498800`.
* Production path: `/opt/job-hunterv2`.
* Uses FastAPI, SQLite, APScheduler, Playwright, and multiple ATS adapters.
* Current active job count has grown significantly over time, with the latest context mentioning around 124K active jobs.
* The system is used by a small trusted group, not public users.
* No public signup is intended.

---

## 2. Production Environment

### GCP VM

```text
Project: job-hunter-498800
Zone: us-central1-a
App path: /opt/job-hunterv2
Linux user: amer_arafath1
Python venv: /opt/job-hunterv2/venv/bin/python
Database: /opt/job-hunterv2/jobhunter.db
```

The app was originally tested on smaller VM sizes, but Playwright and large scraping workloads require more memory. The project currently assumes a stronger VM than the original e2-micro plan.

---

## 3. Main Services

The project runs as two separate systemd services.

### 1. Web service

```text
jobhunter-web
```

Purpose:

* Runs the FastAPI web dashboard.
* Uses Uvicorn.
* Binds to:

```text
127.0.0.1:8080
```

Main file:

```text
app.py
```

### 2. Collector service

```text
jobhunter-collector
```

Purpose:

* Runs the scheduler.
* Executes API scraping jobs.
* Executes Playwright scraping jobs.
* Executes Universal scraper jobs.
* Runs health checks and retention/archive jobs.

Main file:

```text
scheduler.py
```

---

## 4. Main Project Files

```text
app.py
```

FastAPI web app. Handles login, dashboard pages, filters, job tracking, Excel export, and the admin health dashboard.

```text
scheduler.py
```

APScheduler-based scheduler. Defines all collection jobs and run times. Uses Eastern Time.

```text
collector.py
```

Main collection engine. Contains API-based ATS adapters and dispatches Playwright and Universal scraping when needed.

```text
playwright_adapter.py
```

Browser-based scraping for platforms that require JavaScript or DOM extraction.

```text
universal_scraper.py
```

General-purpose scraper for companies without a supported ATS adapter. Uses JSON-LD, sitemap parsing, ATS fingerprinting, and heuristic DOM extraction.

```text
auto_resolve.py
```

Serper.dev-powered ATS discovery tool. Searches the web for company career pages, fingerprints ATS platforms, validates endpoints, and updates the database.

```text
monitoring.py
```

Monitoring and alerting system. Tracks collection runs, company health, endpoint drift, system health, and Discord alerts.

```text
db.py
```

SQLite helper layer. Enables WAL mode and uses a threading lock to prevent concurrent write problems.

---

## 5. Database

The system uses SQLite:

```text
/opt/job-hunterv2/jobhunter.db
```

SQLite is configured with WAL mode so the collector can write while the web app reads.

A global collection lock prevents overlapping collection runs from corrupting or blocking database writes.

Important lock:

```python
_collection_lock = threading.Lock()
```

---

## 6. Main Database Tables

### `companies`

Stores all employer records.

Important fields:

```text
company_name
brand
cap_exempt
sponsor
priority_tier
careers_url
ats
resolved_token
endpoint
job_count
resolve_status
updated_at
last_successful_at
prev_job_count
consecutive_failures
last_error
health_status
```

Important rule:

```text
company_name is the legal-name primary key.
brand is only the display name.
```

Always query by legal company name before updating rows.

### `jobs`

Stores active job postings.

Important fields:

```text
ats
company
brand
cap_exempt
sponsor
ext_id
title
location
department
url
posted_at
first_seen
last_seen
active
```

Uniqueness rule:

```text
UNIQUE(ats, company, ext_id)
```

This prevents duplicate jobs from being inserted repeatedly.

### `jobs_archive`

Stores jobs that aged out of active search.

Purpose:

* Preserve historical job data.
* Support future hiring-trend analysis.
* Avoid losing old postings after active retention expires.

### `collection_runs`

Tracks every collector run.

Stores:

```text
run_type
start_time
end_time
duration
companies_attempted
companies_succeeded
companies_failed
jobs_inserted
errors
```

### `endpoint_checks`

Stores weekly endpoint drift checks.

Used to detect:

* Broken endpoints.
* HTTP errors.
* Redirected domains.
* Endpoints returning zero jobs.
* ATS structure changes.

---

## 7. Supported ATS Platforms

The system supports a mix of API-based and browser-based collection.

### API-based adapters

These are handled mostly inside `collector.py`.

| ATS                       | Method            |
| ------------------------- | ----------------- |
| Greenhouse                | Public JSON API   |
| Lever                     | Public JSON API   |
| Ashby                     | Public JSON API   |
| SmartRecruiters           | Public JSON API   |
| Workable                  | Widget JSON API   |
| Workday                   | CXS POST JSON API |
| Jibe / iCIMS Talent Cloud | JSON API          |

### Browser-based adapters

These are handled through Playwright.

| Platform                          | Method                                      |
| --------------------------------- | ------------------------------------------- |
| iCIMS legacy                      | Browser scraping                            |
| iCIMS Talent Cloud custom domains | DOM scraping                                |
| Taleo                             | Search page automation and link extraction  |
| PageUp                            | Job-link extraction                         |
| Custom portals                    | Site-specific Playwright logic where needed |

### Universal scraper

Used for companies without a clean ATS adapter.

Strategies:

1. JSON-LD JobPosting extraction.
2. Sitemap job URL extraction.
3. ATS fingerprinting.
4. Heuristic DOM job-link extraction.

The Universal scraper is useful but risky because it can produce false positives if not audited.

---

## 8. Workday Adapter

Workday is one of the most important adapters because many universities, hospitals, and large companies use it.

Endpoint pattern:

```text
POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
```

Typical request body:

```json
{
  "appliedFacets": {},
  "limit": 20,
  "offset": 0,
  "searchText": ""
}
```

Each Workday employer needs:

```text
tenant
datacenter
site path
```

Workday pagination must continue until all jobs are collected.

Known issue:

Some Workday endpoints return `422`, `500`, or zero jobs if the tenant, datacenter, or site path is wrong.

---

## 9. Jibe / iCIMS Talent Cloud Adapter

Many custom iCIMS Talent Cloud domains expose a JSON endpoint:

```text
https://{domain}/api/jobs?limit=100&offset=0
```

Typical response shape:

```json
{
  "jobs": [
    {
      "data": {
        "slug": "...",
        "title": "...",
        "city": "...",
        "state": "...",
        "country": "...",
        "department": "...",
        "posted_date": "...",
        "apply_url": "..."
      }
    }
  ],
  "count": 123
}
```

Some iCIMS sites do not expose the API cleanly and require Playwright DOM scraping.

---

## 10. Playwright Setup

Playwright is required for browser-based scraping.

It must be installed inside the project venv:

```bash
./venv/bin/pip install playwright
./venv/bin/playwright install chromium
```

Important note:

After rebuilding the venv, Chromium must be installed again:

```bash
./venv/bin/playwright install chromium
```

A previous issue where Playwright was silently failing because it was missing from the venv was fixed in Session 9.

---

## 11. Scheduler

All schedules use Eastern Time.

Current schedule pattern:

### Morning blitz: 10 AM to 1 PM ET

The system runs frequent collection during the highest-value posting window.

Pattern:

* API runs frequently.
* Playwright runs between API runs.
* Universal scraper runs between heavier browser/API windows.

### Afternoon and evening

* API runs hourly from 1 PM to 9 PM ET.
* Playwright runs a few times.
* Universal scraper runs a few times.

### Overnight

* API runs at 2 AM and 6 AM ET.
* Retention/archive runs around 3:30 AM.
* Health checks run early morning.
* Endpoint drift checks run weekly.

Current total:

```text
28 runs/day
16 API runs
6 Playwright runs
6 Universal scraper runs
```

Reasoning:

* API scrapers are lightweight and can run often.
* Playwright uses more CPU and memory, so it runs less often.
* Universal scraping can be noisy, so it should run separately and less frequently.

---

## 12. Monitoring System

The project has a monitoring layer in:

```text
monitoring.py
```

It tracks:

* Every collection run.
* Job counts per run.
* Company success/failure state.
* Consecutive failures.
* Job count drops.
* Endpoint drift.
* Disk usage.
* Memory usage.
* Database size.
* Uptime.

### Discord alerts

Discord webhook alerts are intended for:

* Full collection failure.
* High failure rate.
* Company dropping to zero jobs.
* Disk usage above threshold.
* Endpoint drift.

Environment variable:

```text
DISCORD_WEBHOOK_URL
```

Known issue:

The webhook URL had a duplicate prefix and needed correction.

Also, `monitoring.py` reads the webhook from environment variables, so the systemd service must load it correctly.

---

## 13. Health Dashboard

The app has an admin-only health dashboard:

```text
/health
```

Restricted to user:

```text
arafath
```

The dashboard shows:

* Active job count.
* System vitals.
* Company health breakdown.
* Recent collection runs.
* Jobs by ATS.
* 24-hour stats.
* Problem companies.
* Endpoint check results.

This is important because scraper failures can otherwise happen silently.

---

## 14. Web App Features

The dashboard is built with FastAPI and Jinja2.

Main features:

* Login-protected dashboard.
* No public signup.
* Search by keyword.
* Filter by company.
* Filter by location.
* Filter by cap-exempt status.
* Filter by date range.
* Focus filter for tech and professional roles.
* Sort by newest, company, or opening count.
* Application tracking.
* User notes.
* Excel export.
* Health dashboard for admin.

Templates:

```text
templates/base.html
templates/login.html
templates/jobs.html
templates/tracker.html
templates/health.html
```

Static files:

```text
static/style.css
```

---

## 15. Focus Filter

The focus filter is designed to reduce noisy jobs.

Focus mode:

```text
Tech & Professional
```

It excludes jobs such as:

* Nurse
* Physician
* Surgeon
* Therapist
* Medical Assistant
* Patient Care
* Cafeteria
* Cook
* Cashier
* Custodian
* Janitor
* Housekeeper
* Forklift
* CDL
* Receptionist
* Stocker
* Veterinary
* Childcare
* Lifeguard
* Athletic roles
* Other non-target jobs

This is implemented using SQL `NOT LIKE` clauses inside job query logic.

---

## 16. ATS Discovery System

ATS discovery is handled by tools such as:

```text
tools/auto_resolve.py
tools/discover.py
tools/rediscover.py
tools/audit_resolved.py
```

Discovery process:

1. Search for company career page using Serper.dev.
2. Inspect search result URLs.
3. Fingerprint ATS platform.
4. Extract endpoint or board token.
5. Validate endpoint with live probe.
6. Apply name-matching guard to avoid false positives.
7. Update `companies` table.

Important lesson:

Early resolver versions created false positives by guessing slugs too aggressively.

Examples of bad matches found earlier:

* Dell matched the wrong company.
* Best Buy matched a Canada endpoint.
* Paine College matched Thompson Hospitality.
* Some university names matched unrelated career sites.

The resolver now uses stronger validation and name matching.

---

## 17. Universal Scraper

The Universal scraper was created for companies that do not have a supported ATS adapter.

File:

```text
tools/universal_scraper.py
```

Strategies:

1. JSON-LD structured job data.
2. Sitemap parsing.
3. ATS fingerprinting.
4. Heuristic repeated job-link detection.

A cleaned extraction script was later built:

```text
tools/extract_clean.py
```

Reason:

The Universal scraper found many jobs, but some were false positives from aggregator pages, wrong-company matches, or duplicated sites.

False positives were audited and excluded before storing clean results.

---

## 18. CI/CD

GitHub repo:

```text
https://github.com/arafath-am/job-hunterv2
```

Deployment flow:

```text
Push to main → GitHub Actions → GCP deploy
```

The deployment uses:

* GitHub Actions.
* Workload Identity.
* IAP tunnel.
* Systemd service restart.

On deployment, both services are restarted:

```text
jobhunter-web
jobhunter-collector
```

---

## 19. Environment Variables

The `.env` file contains sensitive configuration.

Important variables:

```text
DISCORD_WEBHOOK_URL
SERPER_API_KEY
SESSION_SECRET
```

Possible additional secret name used in app context:

```text
JOBHUNTER_SECRET
```

Important security rule:

Do not commit `.env`, database files, Excel exports, logs, or tokens.

---

## 20. Gitignored Runtime Files

These should stay out of Git:

```text
jobhunter.db
jobhunter.db-wal
jobhunter.db-shm
venv/
__pycache__/
*.xlsx
.env
discovery_log.txt
unresolved_companies.csv
report files
```

---

## 21. Current Known Issues

### 1. Some companies dropped to zero jobs

Examples mentioned:

* DoorDash
* Stripe
* Other large companies

Likely causes:

* Endpoint changed.
* ATS API changed.
* Wrong token.
* Blocking/rate limiting.
* Site moved to another ATS.

Needs investigation through health dashboard and endpoint checker.

---

### 2. Discord alerts need verification

Known pending work:

* Fix duplicate webhook prefix.
* Confirm webhook works.
* Confirm systemd service loads environment variables correctly.
* Consider using `python-dotenv` or explicit `EnvironmentFile`.

---

### 3. Desktop UI layout still needs cleanup

The dashboard works, but the desktop filter area is too spread out.

Likely files:

```text
templates/jobs.html
static/style.css
```

Likely area:

```css
@media (min-width: 769px)
```

Expected desktop behavior:

* Compact filter layout.
* Filters visible by default.
* No oversized spacing.
* Mobile behavior should remain clean.

---

### 4. HTTPS and Cloudflare Tunnel pending

Current access is not fully production-hardened.

Pending:

* Cloudflare Tunnel.
* Firewall restriction.
* Disable direct public exposure where possible.
* Keep sslip.io only as temporary access.

---

### 5. Oracle Cloud HCM adapter pending

Around 25 companies may use Oracle Cloud HCM.

This adapter is not built yet.

Likely high-value targets include universities, hospitals, and large enterprise employers.

---

### 6. SelectMinds adapter pending

SelectMinds is not supported yet.

Potential high-value employers:

* Stanford
* MD Anderson
* University of Iowa
* Other Oracle/Taleo-like career portals.

---

### 7. SuccessFactors adapter pending

SuccessFactors companies are identified but not collecting.

Possible targets include:

* Johns Hopkins
* Baylor
* Purdue
* SAP
* Wipro
* HCL

This likely needs Playwright or a dedicated API reverse-engineering approach.

---

### 8. Custom scraper work pending

Some companies do not follow the normal patterns.

Previously mentioned targets:

```text
jobs.kent.edu
careers.msu.edu
employment.ucsd.edu
```

These need individual DOM inspection and custom extraction.

---

## 22. Current Priority List

### Priority 1 — Verify monitoring and alerts

Tasks:

1. Fix `DISCORD_WEBHOOK_URL`.
2. Confirm webhook sends a test alert.
3. Confirm systemd loads environment variables.
4. Check `/health`.
5. Let all collectors complete a full cycle.
6. Confirm company health statuses populate.

---

### Priority 2 — Investigate companies dropping to zero jobs

Use monitoring data to identify:

* Companies that used to have jobs.
* Companies now returning zero.
* Companies with repeated failures.
* Endpoints with HTTP errors.

Then test each endpoint manually.

---

### Priority 3 — Commit and push latest work

Session 8 and Session 9 work should be committed and pushed.

Include:

* Monitoring system.
* Universal scraper integration.
* Scheduler rewrite.
* Playwright fix.
* Health dashboard.
* Endpoint checker.
* Clean extraction script.

---

### Priority 4 — Fix desktop UI

Files:

```text
templates/jobs.html
static/style.css
```

Goal:

* Compact desktop filters.
* Keep mobile layout working.
* Avoid bloated spacing.

---

### Priority 5 — Add Oracle Cloud HCM adapter

Build a dedicated adapter for Oracle Cloud HCM companies.

This should be separate from Taleo because Oracle Cloud HCM behaves differently.

---

### Priority 6 — Add SelectMinds adapter

SelectMinds may unlock major cap-exempt employers.

Need to inspect:

* Public search endpoints.
* HTML structure.
* Pagination.
* Job detail URL patterns.

---

### Priority 7 — Expand Workday and Jibe coverage

Use Universal scraper detections and rediscovery output to migrate companies into stronger native adapters when possible.

Native adapter is preferred over Universal scraping because it is cleaner and easier to monitor.

---

### Priority 8 — HTTPS and firewall hardening

Recommended final production setup:

1. Configure Cloudflare Tunnel.
2. Restrict direct VM ingress.
3. Confirm app is only reachable through intended route.
4. Keep secrets outside Git.
5. Add failed-login logging.

---

### Priority 9 — Create sanitized showcase repo

Create a public portfolio version:

```text
job-hunter-showcase
```

Include:

* Architecture diagram.
* Screenshots.
* Data pipeline explanation.
* ATS adapter design.
* Scheduler design.
* Monitoring design.
* Security notes.
* No production database.
* No secrets.
* No private user data.

---

## 23. Useful Commands

### Restart services

```bash
sudo systemctl daemon-reload
sudo systemctl restart jobhunter-web
sudo systemctl restart jobhunter-collector
sudo systemctl status jobhunter-web jobhunter-collector --no-pager
```

### View logs

```bash
sudo journalctl -u jobhunter-web -f
sudo journalctl -u jobhunter-collector -f
sudo journalctl -u jobhunter-collector --since '30 min ago' --no-pager | tail -30
```

### Count total active jobs

```bash
python3 -c "import sqlite3; c=sqlite3.connect('jobhunter.db'); print('Total:', c.execute('SELECT COUNT(*) FROM jobs WHERE active=1').fetchone()[0])"
```

### Count jobs by ATS

```bash
python3 -c "import sqlite3; c=sqlite3.connect('jobhunter.db'); [print(f'{r[0]:18} {r[1]:>6} jobs') for r in c.execute('SELECT ats,COUNT(*) FROM jobs WHERE active=1 GROUP BY ats ORDER BY COUNT(*) DESC')]"
```

### Top companies by active jobs

```bash
python3 -c "import sqlite3; c=sqlite3.connect('jobhunter.db'); [print(f'{r[0]:45} {r[1]:>6}') for r in c.execute('SELECT brand, COUNT(*) FROM jobs WHERE active=1 GROUP BY company ORDER BY COUNT(*) DESC LIMIT 20')]"
```

### Manual API collection

```bash
python3 -c "import collector; print(collector.run())"
```

### Manual Playwright collection

```bash
python3 -c "import collector; print(collector.run_playwright())"
```

### Manual Universal collection

```bash
python3 -c "import collector; print(collector.run_universal())"
```

### Test individual scraper

```bash
python3 -c "from playwright_adapter import scrape_one; print(scrape_one('https://careers-zs.icims.com', 'icims'))"
```

### Test Jibe API

```bash
curl -s -H "User-Agent: Mozilla/5.0" "https://{domain}/api/jobs?limit=1" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('count',0),'jobs')"
```

### Create user

```bash
python3 app.py createuser <username> <password>
```

### Git push

```bash
cd /opt/job-hunterv2
git add -A
git commit -m "message"
git push
```

---

## 24. Short Context for Next AI Chat

Use this short version when starting a new chat:

We are continuing Job Hunter v2, a self-hosted FastAPI + SQLite job monitoring system running on a GCP VM in project `job-hunter-498800`. It tracks around 893 H-1B sponsor and cap-exempt employers by scraping jobs directly from company ATS platforms. The project uses API adapters for Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Workday, and Jibe/iCIMS Talent Cloud, plus Playwright scrapers for iCIMS, Taleo, PageUp, and custom career portals. It also has a Universal scraper for JSON-LD, sitemap, ATS fingerprinting, and heuristic job extraction.

The app runs from `/opt/job-hunterv2` with two systemd services: `jobhunter-web` for the FastAPI dashboard and `jobhunter-collector` for APScheduler-based collection. SQLite uses WAL mode and a threading lock to prevent concurrent write issues. Monitoring has been added through `monitoring.py`, including `collection_runs`, company health tracking, endpoint drift checks, `/health` admin dashboard, and Discord alerts.

Recent work added auto-resolve, Universal scraper integration, monitoring, endpoint checking, and fixed Playwright missing from the venv. Current work should resume from: fixing/verifying Discord alerts, checking companies that dropped to zero jobs, committing and pushing Session 8/9 work, fixing desktop UI CSS, building Oracle Cloud HCM and SelectMinds adapters, expanding Workday/Jibe coverage, and finishing HTTPS/Cloudflare/firewall hardening.

---

## 25. Final Current State

Job Hunter v2 started as a simple H-1B sponsor job scraper and evolved into a full multi-ATS ingestion platform.

It now has:

* Direct ATS polling.
* ATS discovery.
* False-positive auditing.
* API collectors.
* Playwright collectors.
* Universal scraper.
* Workday support.
* Jibe/iCIMS Talent Cloud support.
* SQLite WAL mode.
* Job archive system.
* Multi-user login.
* Application tracking.
* Excel export.
* Health dashboard.
* Collection monitoring.
* Discord alert design.
* Endpoint drift checker.
* GitHub Actions deployment.
* Separate web and collector services.

The core system works. The remaining work is mostly reliability, coverage expansion, UI polish, production security, and turning the project into a clean portfolio showcase.
