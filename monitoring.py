"""
monitoring.py — Collection monitoring, company health tracking, and Discord alerting.

Layers:
  1. collection_runs table — logs every collection cycle
  2. Company health columns — tracks per-company success/failure/staleness
  3. Discord webhook — fire-and-forget alerts on failures/anomalies
  4. System health — disk, memory, DB size
  5. Health dashboard data assembly
"""
import os
import json
import shutil
import sqlite3
from datetime import datetime, timezone

import requests as _requests

import db

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ── Thresholds ──
STALE_DAYS = 3          # no successful collection in 3 days → stale
DEAD_DAYS = 7           # no success in 7 days → dead
FAILURE_THRESHOLD = 3   # consecutive failures before 'dead'
DROP_THRESHOLD = 0.5    # 50%+ job count drop triggers alert
DISK_ALERT_PCT = 80     # disk usage % alert threshold
FAILURE_RATE_ALERT = 0.10  # 10%+ failure rate triggers alert


# ══════════════════════════════════════════════════════════════ Schema
def init_monitoring():
    """Create monitoring tables and add health columns to companies."""
    with db.get_conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS collection_runs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                run_type            TEXT NOT NULL,
                started_at          TEXT NOT NULL,
                finished_at         TEXT,
                duration_secs       REAL,
                companies_attempted INTEGER DEFAULT 0,
                companies_succeeded INTEGER DEFAULT 0,
                companies_failed    INTEGER DEFAULT 0,
                jobs_inserted       INTEGER DEFAULT 0,
                jobs_updated        INTEGER DEFAULT 0,
                jobs_purged         INTEGER DEFAULT 0,
                errors              TEXT,
                status              TEXT DEFAULT 'running',
                notes               TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cr_type    ON collection_runs(run_type);
            CREATE INDEX IF NOT EXISTS idx_cr_started ON collection_runs(started_at);

            CREATE TABLE IF NOT EXISTS endpoint_checks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name    TEXT NOT NULL,
                checked_at      TEXT NOT NULL,
                endpoint        TEXT,
                http_status     INTEGER,
                response_ok     INTEGER DEFAULT 0,
                jobs_found      INTEGER DEFAULT 0,
                drift_detected  INTEGER DEFAULT 0,
                notes           TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ec_company ON endpoint_checks(company_name);
        """)
        # Add health columns to companies (idempotent)
        for col, typedef in [
            ("last_successful_at",   "TEXT"),
            ("prev_job_count",       "INTEGER DEFAULT 0"),
            ("consecutive_failures", "INTEGER DEFAULT 0"),
            ("last_error",           "TEXT"),
            ("health_status",        "TEXT DEFAULT 'unknown'"),
        ]:
            try:
                con.execute(f"ALTER TABLE companies ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # already exists


# ══════════════════════════════════════════════════════════════ Collection Run Logging
def start_run(run_type: str) -> int:
    """Log run start. Returns run_id."""
    with db.get_conn() as con:
        cur = con.execute(
            "INSERT INTO collection_runs (run_type, started_at, status) VALUES (?, ?, 'running')",
            (run_type, db.now_iso())
        )
        return cur.lastrowid


def finish_run(run_id: int, results: dict):
    """Log run completion, update status, trigger alerts."""
    now = db.now_iso()
    attempted = results.get("companies_attempted", 0)
    succeeded = results.get("companies_succeeded", 0)
    failed    = results.get("companies_failed", 0)

    if attempted == 0:
        status = "empty"
    elif failed == 0 and succeeded >= attempted:
        status = "success"
    elif succeeded > 0:
        status = "partial"
    else:
        status = "failed"

    with db.get_conn() as con:
        con.execute("""
            UPDATE collection_runs SET
                finished_at=?, duration_secs=?, companies_attempted=?,
                companies_succeeded=?, companies_failed=?,
                jobs_inserted=?, jobs_updated=?, jobs_purged=?,
                errors=?, status=?, notes=?
            WHERE id=?
        """, (
            now, results.get("duration_secs", 0),
            attempted, succeeded, failed,
            results.get("jobs_inserted", 0),
            results.get("jobs_updated", 0),
            results.get("jobs_purged", 0),
            json.dumps(results.get("errors", [])[:50]),  # cap stored errors
            status,
            results.get("notes", ""),
            run_id
        ))

    _check_run_alerts(results, status)


# ══════════════════════════════════════════════════════════════ Company Health
def update_company_health(company_name: str, success: bool,
                          job_count: int = 0, error: str = None):
    """Update health tracking for one company after collection."""
    now = db.now_iso()
    with db.get_conn() as con:
        row = con.execute(
            "SELECT job_count, prev_job_count, consecutive_failures FROM companies WHERE company_name=?",
            (company_name,)
        ).fetchone()
        if not row:
            return

        prev_count = row["job_count"] or 0

        if success:
            con.execute("""
                UPDATE companies SET
                    last_successful_at=?, prev_job_count=?, job_count=?,
                    consecutive_failures=0, last_error=NULL, health_status='healthy'
                WHERE company_name=?
            """, (now, prev_count, job_count, company_name))

            # Anomaly: big drop
            if prev_count > 100 and job_count == 0:
                _alert(f"⚠️ **{company_name}** dropped from {prev_count} to 0 jobs — endpoint may be broken")
            elif prev_count > 50 and job_count > prev_count * 10:
                _alert(f"⚠️ **{company_name}** spiked from {prev_count} to {job_count} jobs — possible wrong source")
        else:
            failures = (row["consecutive_failures"] or 0) + 1
            health = "degraded" if failures < FAILURE_THRESHOLD else "dead"
            con.execute("""
                UPDATE companies SET
                    consecutive_failures=?, last_error=?, health_status=?
                WHERE company_name=?
            """, (failures, (error or "unknown")[:200], health, company_name))


def refresh_health_statuses():
    """Periodic sweep to mark stale/dead companies. Call from scheduler."""
    with db.get_conn() as con:
        # Stale: was healthy but no success in STALE_DAYS
        stale = con.execute("""
            UPDATE companies SET health_status='stale'
            WHERE resolve_status='resolved'
              AND health_status IN ('healthy', 'unknown')
              AND last_successful_at IS NOT NULL
              AND last_successful_at < datetime('now', ?)
        """, (f"-{STALE_DAYS} days",)).rowcount

        # Dead: no success in DEAD_DAYS
        dead = con.execute("""
            UPDATE companies SET health_status='dead'
            WHERE resolve_status='resolved'
              AND last_successful_at IS NOT NULL
              AND last_successful_at < datetime('now', ?)
              AND health_status != 'dead'
        """, (f"-{DEAD_DAYS} days",)).rowcount

        if stale or dead:
            _alert(f"🏥 Health sweep: {stale} companies went stale, {dead} went dead")

        return {"stale": stale, "dead": dead}


# ══════════════════════════════════════════════════════════════ Discord
def _alert(message: str):
    """Fire-and-forget Discord webhook."""
    if not DISCORD_WEBHOOK_URL:
        print(f"[monitor:alert] {message}")
        return
    try:
        _requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=5
        )
    except Exception as e:
        print(f"[monitor:alert] Discord failed: {e}")


def _check_run_alerts(results: dict, status: str):
    """Post-run alert checks."""
    run_type = results.get("run_type", "unknown")
    attempted = results.get("companies_attempted", 0)
    failed    = results.get("companies_failed", 0)
    inserted  = results.get("jobs_inserted", 0)

    # Full failure
    if status == "failed":
        _alert(
            f"🔴 **{run_type.upper()} collection FAILED** — "
            f"0/{attempted} companies succeeded"
        )
        return

    # High failure rate
    if attempted > 10 and failed / attempted > FAILURE_RATE_ALERT:
        pct = round(failed / attempted * 100)
        _alert(
            f"🟡 **{run_type.upper()} high failure rate**: "
            f"{failed}/{attempted} companies failed ({pct}%)"
        )

    # Disk check
    disk = shutil.disk_usage("/")
    pct = disk.used / disk.total * 100
    if pct > DISK_ALERT_PCT:
        _alert(f"💾 **Disk usage {pct:.0f}%** — consider cleanup")

    # Success summary (not an alert, just info — only if webhook configured)
    if status == "success" and DISCORD_WEBHOOK_URL:
        # Only send summary for significant runs
        if inserted > 0:
            _alert(
                f"✅ **{run_type.upper()}** complete: "
                f"{attempted} companies, {inserted} new jobs, "
                f"{results.get('duration_secs', 0):.0f}s"
            )


# ══════════════════════════════════════════════════════════════ System Health
def get_system_health() -> dict:
    """Snapshot of VM vitals."""
    # Disk
    disk = shutil.disk_usage("/")

    # Memory from /proc/meminfo
    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])
        mem_total = meminfo.get("MemTotal", 0) / 1024
        mem_available = meminfo.get("MemAvailable", 0) / 1024
        mem_used = mem_total - mem_available
    except Exception:
        mem_total = mem_used = mem_available = 0

    # DB size
    db_size = os.path.getsize(db.DB_PATH) / (1024 * 1024) if os.path.exists(db.DB_PATH) else 0
    wal_path = db.DB_PATH + "-wal"
    wal_size = os.path.getsize(wal_path) / (1024 * 1024) if os.path.exists(wal_path) else 0

    # Uptime
    try:
        with open("/proc/uptime") as f:
            uptime_secs = float(f.read().split()[0])
    except Exception:
        uptime_secs = 0

    return {
        "disk_total_gb":  round(disk.total / (1024**3), 1),
        "disk_used_gb":   round(disk.used / (1024**3), 1),
        "disk_free_gb":   round(disk.free / (1024**3), 1),
        "disk_pct":       round(disk.used / disk.total * 100, 1),
        "mem_total_mb":   round(mem_total),
        "mem_used_mb":    round(mem_used),
        "mem_available_mb": round(mem_available),
        "mem_pct":        round(mem_used / mem_total * 100, 1) if mem_total else 0,
        "db_size_mb":     round(db_size, 1),
        "wal_size_mb":    round(wal_size, 1),
        "uptime_hours":   round(uptime_secs / 3600, 1),
    }


# ══════════════════════════════════════════════════════════════ Dashboard Data
def get_health_data() -> dict:
    """Assemble everything the /health page needs."""
    with db.get_conn() as con:
        # Recent runs (last 30)
        recent_runs = [dict(r) for r in con.execute(
            "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT 30"
        ).fetchall()]

        # Last successful run per type
        last_runs = {}
        for rtype in ("api", "playwright", "universal"):
            row = con.execute(
                "SELECT * FROM collection_runs WHERE run_type=? AND status!='running' "
                "ORDER BY started_at DESC LIMIT 1", (rtype,)
            ).fetchone()
            if row:
                last_runs[rtype] = dict(row)

        # 24h aggregates
        stats_24h = [dict(r) for r in con.execute("""
            SELECT run_type,
                   COUNT(*)                     AS runs,
                   SUM(companies_succeeded)      AS succeeded,
                   SUM(companies_failed)         AS failed,
                   SUM(jobs_inserted)            AS inserted,
                   ROUND(AVG(duration_secs), 1)  AS avg_duration
            FROM collection_runs
            WHERE started_at >= datetime('now', '-1 day')
            GROUP BY run_type
        """).fetchall()]

        # 7d aggregates
        stats_7d = [dict(r) for r in con.execute("""
            SELECT run_type,
                   COUNT(*)                 AS runs,
                   SUM(companies_succeeded)  AS succeeded,
                   SUM(companies_failed)     AS failed,
                   SUM(jobs_inserted)        AS inserted
            FROM collection_runs
            WHERE started_at >= datetime('now', '-7 days')
            GROUP BY run_type
        """).fetchall()]

        # Company health breakdown
        health_summary = {}
        for r in con.execute("""
            SELECT COALESCE(health_status, 'unknown') AS hs, COUNT(*) AS cnt
            FROM companies WHERE resolve_status='resolved'
            GROUP BY hs
        """).fetchall():
            health_summary[r["hs"]] = r["cnt"]

        # Problem companies
        problem_companies = [dict(r) for r in con.execute("""
            SELECT company_name, brand, ats, health_status,
                   consecutive_failures, last_error, last_successful_at, job_count
            FROM companies
            WHERE health_status IN ('degraded', 'dead', 'stale')
              AND resolve_status='resolved'
            ORDER BY
              CASE health_status WHEN 'dead' THEN 0 WHEN 'stale' THEN 1 ELSE 2 END,
              consecutive_failures DESC
            LIMIT 50
        """).fetchall()]

        # Jobs by ATS
        ats_counts = {}
        for r in con.execute(
            "SELECT ats, COUNT(*) AS cnt FROM jobs WHERE active=1 GROUP BY ats ORDER BY cnt DESC"
        ).fetchall():
            ats_counts[r["ats"]] = r["cnt"]

        # Totals
        total_jobs = con.execute("SELECT COUNT(*) AS c FROM jobs WHERE active=1").fetchone()["c"]
        total_companies = con.execute(
            "SELECT COUNT(*) AS c FROM companies WHERE resolve_status='resolved'"
        ).fetchone()["c"]
        total_all = con.execute("SELECT COUNT(*) AS c FROM companies").fetchone()["c"]

        # Recent endpoint checks
        recent_checks = [dict(r) for r in con.execute(
            "SELECT * FROM endpoint_checks ORDER BY checked_at DESC LIMIT 20"
        ).fetchall()]

    return {
        "recent_runs":       recent_runs,
        "last_runs":         last_runs,
        "stats_24h":         stats_24h,
        "stats_7d":          stats_7d,
        "health_summary":    health_summary,
        "problem_companies": problem_companies,
        "ats_counts":        ats_counts,
        "total_jobs":        total_jobs,
        "total_companies":   total_companies,
        "total_all":         total_all,
        "recent_checks":     recent_checks,
        "system":            get_system_health(),
    }
