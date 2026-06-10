#!/usr/bin/env bash
# reorganize.sh — Clean up the Job Hunter v2 repo structure
# Run from the project root: cd ~/job-hunterv2 && bash reorganize.sh
set -euo pipefail

echo "=== Job Hunter v2 — Repo Reorganization ==="
echo ""

# Safety check
if [ ! -f "app.py" ] || [ ! -f "collector.py" ]; then
    echo "ERROR: Run this from the job-hunterv2 project root (~/job-hunterv2/)"
    exit 1
fi

# ── 1. Create directories ──────────────────────────────────────────
echo "[1/6] Creating directories..."
mkdir -p tools deploy migrations

# ── 2. Move files with git mv ──────────────────────────────────────
echo "[2/6] Moving files..."

# Tools — scripts you run manually, not part of production
for f in discover.py resolver.py resolver_patch.py audit_resolved.py gen_company_report.py; do
    [ -f "$f" ] && git mv "$f" tools/ && echo "  → tools/$f"
done

# Deploy — systemd units and deploy scripts
for f in jobhunter-web.service jobhunter-collector.service deploy_perf.sh; do
    [ -f "$f" ] && git mv "$f" deploy/ && echo "  → deploy/$f"
done

# Migrations — one-off fix scripts that already ran
for f in apply_fixes.py fix_scrapers.py false_positives_fix.sql; do
    [ -f "$f" ] && git mv "$f" migrations/ && echo "  → migrations/$f"
done

# ── 3. Remove committed artifacts that should be ignored ───────────
echo "[3/6] Removing committed artifacts..."
for f in discovery_log.txt unresolved_companies.csv; do
    if git ls-files --error-unmatch "$f" &>/dev/null; then
        git rm --cached "$f" && echo "  ✗ $f (untracked, kept on disk)"
    fi
done

# ── 4. Update .gitignore ──────────────────────────────────────────
echo "[4/6] Writing .gitignore..."
cat > .gitignore << 'GITIGNORE'
# Database
*.db
*.db-wal
*.db-shm

# Python
venv/
__pycache__/
*.pyc

# Environment
.env

# Generated files
*.xlsx
discovery_log.txt
unresolved_companies.csv

# OS
.DS_Store
Thumbs.db
GITIGNORE
echo "  ✓ .gitignore updated"

# ── 5. Create .env.example ────────────────────────────────────────
echo "[5/6] Writing .env.example..."
cat > .env.example << 'ENVFILE'
# Required — session signing key (generate with: python3 -c "import secrets;print(secrets.token_hex(32))")
JOBHUNTER_SECRET=

# Required for discovery pipeline (tools/discover.py) — https://serper.dev free tier
SERPER_API_KEY=

# Optional — override default DB path (default: ./jobhunter.db)
# JOBHUNTER_DB=/path/to/jobhunter.db

# Optional — set to 0 in the web service so only the collector runs the scheduler
# JOBHUNTER_RUN_SCHEDULER=0

# Optional — job retention in days (default: 10)
# JOBHUNTER_RETENTION_DAYS=10
ENVFILE
echo "  ✓ .env.example created"

# ── 6. Rewrite README ─────────────────────────────────────────────
echo "[6/6] Writing README.md..."
cat > README.md << 'README'
# Job Hunter v2

A self-hosted job radar that monitors **893 H-1B sponsor and cap-exempt employers** for new postings across 9 ATS platforms. A scheduler polls each company's careers feed on a cadence, diffs new listings into a database, and a login-protected web app lets multiple users filter, browse, track applications, and export progress to Excel.

Currently collecting **~35,700 active jobs** from **~430 companies**.

## How it works

```
companies_enriched.csv
        │
        ▼
  ┌───────────┐    ┌────────────┐    ┌───────────┐
  │ Discovery  │───▶│  Collector  │───▶│  SQLite   │
  │ (Serper)   │    │ (9 adapters)│    │  (WAL)    │
  └───────────┘    └────────────┘    └─────┬─────┘
                                           │
                   ┌────────────┐          │
                   │ Scheduler  │──────────┤
                   │(APScheduler)│          │
                   └────────────┘          ▼
                                     ┌───────────┐
                                     │  FastAPI   │
                                     │ Dashboard  │
                                     └───────────┘
```

**ATS coverage:**

| ATS | Companies | Method |
|-----|-----------|--------|
| Workday | 254 | POST JSON API with pagination |
| Greenhouse | 33 | Public JSON API |
| Ashby | 12 | Public JSON API |
| Lever | 11 | Public JSON API |
| SmartRecruiters | 9 | Public JSON API |
| Workable | 7 | Widget JSON API |
| iCIMS | ~23 | Playwright (React SPA + legacy) |
| Taleo | ~9 | Playwright (search + extract) |
| PageUp | ~5 | Playwright (job link extraction) |

## Project structure

```
job-hunterv2/
│
│  # ── Runtime (production) ──────────────────────
├── app.py                  FastAPI web app (login, dashboard, tracking, export)
├── db.py                   SQLite data layer (WAL mode, schema, queries, auth)
├── collector.py            ATS adapters + per-host rate limiter + backoff + diff
├── playwright_adapter.py   Browser scrapers for iCIMS / Taleo / PageUp
├── scheduler.py            APScheduler (API + Playwright + purge schedules)
├── export.py               Per-user Excel tracker generation
│
│  # ── Tools (run manually) ──────────────────────
├── tools/
│   ├── discover.py             ATS discovery via Serper.dev Google Search API
│   ├── resolver.py             Original stdlib-only ATS resolver
│   ├── resolver_patch.py       Board-name validation helpers
│   ├── audit_resolved.py       False-positive detection for resolved companies
│   └── gen_company_report.py   Company coverage report (Excel)
│
│  # ── Deployment ────────────────────────────────
├── deploy/
│   ├── jobhunter-web.service       systemd: web server (uvicorn)
│   ├── jobhunter-collector.service systemd: scheduler + collector
│   └── deploy_perf.sh              Service split deploy script
│
│  # ── Migrations (already applied, archived) ────
├── migrations/
│   ├── apply_fixes.py          Session 3 pagination + index fixes
│   ├── fix_scrapers.py         Session 3 iCIMS + Taleo scraper fixes
│   └── false_positives_fix.sql SQL to demote false-positive ATS matches
│
│  # ── Web UI ────────────────────────────────────
├── templates/                  Jinja2: base, login, jobs (paginated), tracker
├── static/style.css            Responsive dark theme (amber accent)
│
│  # ── Data & config ─────────────────────────────
├── companies_enriched.csv      893 companies (source of truth)
├── requirements.txt            Python dependencies
├── .env.example                Required environment variables
└── .gitignore
```

## Setup

```bash
# Clone and install
git clone https://github.com/arafath-am/job-hunterv2.git
cd job-hunterv2
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# For browser-based scrapers (iCIMS/Taleo/PageUp)
pip install playwright && playwright install chromium --with-deps

# Configure
cp .env.example .env
# Edit .env — at minimum set JOBHUNTER_SECRET

# Create users (no public signup)
python3 app.py createuser <username> <password>

# Run (web + scheduler in one process)
uvicorn app:app --host 0.0.0.0 --port 8080
```

### Production (two-service split)

The web server and collector run as separate systemd services so collection never blocks dashboard requests:

```bash
# Copy service files
sudo cp deploy/jobhunter-web.service /etc/systemd/system/
sudo cp deploy/jobhunter-collector.service /etc/systemd/system/
sudo systemctl daemon-reload

# Start
sudo systemctl enable --now jobhunter-web jobhunter-collector
```

## Schedule

| Window | Cadence | What |
|--------|---------|------|
| 9:00 AM – 8:00 PM ET | Every 25 min | API collection (Workday, Greenhouse, etc.) |
| 8:00 PM – 9:00 AM ET | Every 60 min | API collection |
| Specific times ET | 7 runs/day | Playwright collection (iCIMS, Taleo, PageUp) |
| 3:30 AM ET | Daily | Purge jobs older than 10 days |

## Rate limiting & politeness

The collector rate-limits **per ATS host** — parallel across different hosts, throttled within each (~3 req/s). Honors `Retry-After` on 429/503 with exponential backoff. Sends conditional requests (ETag / If-Modified-Since) so unchanged feeds return cheap 304s. Each run logs the busiest host's request count.

## Running tools

All tools are run from the project root:

```bash
# ATS discovery (requires SERPER_API_KEY)
python3 tools/discover.py

# Audit resolved companies for false positives
python3 tools/audit_resolved.py

# Generate coverage report
python3 tools/gen_company_report.py
```

## Environment variables

See `.env.example` for the full list. Key ones:

| Variable | Required | Description |
|----------|----------|-------------|
| `JOBHUNTER_SECRET` | Yes | Session signing key |
| `SERPER_API_KEY` | For discovery | Serper.dev API key |
| `JOBHUNTER_RUN_SCHEDULER` | Production | Set to `0` in web service |
| `JOBHUNTER_RETENTION_DAYS` | No | Days to keep jobs (default: 10) |
README
echo "  ✓ README.md rewritten"

# ── Stage everything ───────────────────────────────────────────────
echo ""
echo "=== Staging changes ==="
git add -A
echo ""
echo "Done! Review with:"
echo "  git status"
echo "  git diff --staged --stat"
echo ""
echo "Then commit and push:"
echo "  git commit -m 'chore: reorganize repo — tools/, deploy/, migrations/, updated README'"
echo "  git push"
