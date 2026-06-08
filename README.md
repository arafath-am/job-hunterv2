# Job Hunter

A self-hosted job radar for H-1B sponsor & cap-exempt employers. A scheduler
pulls postings from each company's ATS feed on a cadence, stores them, and a
login-protected web app lets you filter, browse,
track applications, and export progress to Excel.

## What's here

| File             | Role |
|------------------|------|
| `db.py`          | SQLite (WAL) data layer — jobs, users, applications, http-cache |
| `collector.py`   | ATS adapters + per-host rate limiting + backoff + diff into DB |
| `scheduler.py`   | APScheduler: 25-min active window, hourly overnight, daily purge |
| `export.py`      | Per-user Excel (.xlsx) progress tracker |
| `app.py`         | FastAPI web app (login, filters, tracking, export) |
| `templates/`     | Login, jobs dashboard, tracker (responsive: PC + phone) |
| `static/`        | Stylesheet |

It expects `jobhunter.db` produced by the resolver (the `companies` table with
`resolve_status='resolved'`). Put this folder next to that DB, or set
`JOBHUNTER_DB=/path/to/jobhunter.db`.

## Setup (on your GCP VM)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export JOBHUNTER_SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
export JOBHUNTER_DB="/path/to/jobhunter.db"      # the resolver's DB


# run (starts the web app AND the scheduler)
uvicorn app:app --host 0.0.0.0 --port 8080
```

Open `http://<vm-ip>:8080`. Put it behind your Cloudflare Tunnel for HTTPS.

## Cadence

- **09:00–20:00 ET** — collect every 25 min
- **20:00–09:00 ET** — collect hourly
- **03:30 ET** — delete jobs older than `JOBHUNTER_RETENTION_DAYS` (default 10)

Change retention with `JOBHUNTER_RETENTION_DAYS`. The collector only touches
companies marked `resolved`, so the 699 (Workday/Taleo/custom) plug in later
with no code change once their adapters + tokens are added.

## Safety / politeness

The collector rate-limits **per host** (the only thing ATS platforms throttle),
runs parallel across hosts but gentle within one, honors `Retry-After` on
429/503 with exponential backoff, and sends conditional requests (ETag /
If-Modified-Since) so unchanged feeds return cheap 304s. Each run logs the
busiest host's request count so you can confirm you're nowhere near a limit.

## Run scheduler separately (optional)

Set `JOBHUNTER_RUN_SCHEDULER=0` for the web app and run the collector as its own
service: `python scheduler.py`.
