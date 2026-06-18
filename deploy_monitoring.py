#!/usr/bin/env python3
"""
deploy_monitoring.py — Deploy monitoring system for Job Hunter v2.

Creates:  monitoring.py, templates/health.html, tools/endpoint_checker.py
Patches:  collector.py, app.py, scheduler.py, .env.example

Run from repo root:  python3 deploy_monitoring.py
"""
import re
import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))

def patch_file(path, old, new, desc=""):
    full = os.path.join(REPO, path)
    with open(full) as f:
        content = f.read()
    if old not in content:
        print(f"  ⚠️  SKIP {path}: pattern not found — {desc}")
        return False
    if new in content:
        print(f"  ✓  SKIP {path}: already patched — {desc}")
        return False
    content = content.replace(old, new, 1)
    with open(full, "w") as f:
        f.write(content)
    print(f"  ✅ PATCHED {path} — {desc}")
    return True


def patch_collector():
    """Instrument collector.py with monitoring hooks."""
    print("\n── Patching collector.py ──")

    # 1. Add import
    patch_file("collector.py",
        "import db\n",
        "import db\nimport monitoring\n",
        "add monitoring import"
    )

    # 2. Replace run() function
    old_run = '''def run() -> dict:
    db.init_db()
    companies = db.resolved_companies()
    started = time.time()
    results = []
    new_total = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(collect_company, c) for c in companies]
        for fut in as_completed(futs):
            res = fut.result()
            results.append(res)
            new_total += res.get("new", 0)
    purged = db.purge_old_jobs()
    elapsed = round(time.time() - started, 1)
    ok = sum(1 for r in results if r["status"] == "ok")
    unchanged = sum(1 for r in results if r["status"] == "unchanged")
    errors = sum(1 for r in results if r["status"].startswith("err"))
    # max requests sent to any single host this run — proof we're polite
    busiest = max(_host_counts.items(), key=lambda kv: kv[1], default=("none", 0))
    summary = {
        "companies": len(companies), "ok": ok, "unchanged": unchanged,
        "errors": errors, "new_jobs": new_total, "purged": purged,
        "elapsed_s": elapsed, "busiest_host": f"{busiest[0]} ({busiest[1]} reqs)",
    }
    print(f"[collector] {summary}")
    _host_counts.clear()
    return summary'''

    new_run = '''def run() -> dict:
    db.init_db()
    monitoring.init_monitoring()
    run_id = monitoring.start_run("api")
    companies = db.resolved_companies()
    started = time.time()
    results = []
    new_total = 0
    error_details = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(collect_company, c): c for c in companies}
        for fut in as_completed(futs):
            c = futs[fut]
            res = fut.result()
            results.append(res)
            new_total += res.get("new", 0)
            # Track company health
            if res["status"] == "ok":
                monitoring.update_company_health(c["company_name"], True, job_count=res.get("total", 0))
            elif res["status"].startswith("err"):
                monitoring.update_company_health(c["company_name"], False, error=res["status"])
                error_details.append({"company": c["company_name"], "error": res["status"]})
    purged = db.purge_old_jobs()
    elapsed = round(time.time() - started, 1)
    ok = sum(1 for r in results if r["status"] == "ok")
    unchanged = sum(1 for r in results if r["status"] == "unchanged")
    errors = sum(1 for r in results if r["status"].startswith("err"))
    # max requests sent to any single host this run — proof we're polite
    busiest = max(_host_counts.items(), key=lambda kv: kv[1], default=("none", 0))
    # Log to monitoring
    monitoring.finish_run(run_id, {
        "run_type": "api",
        "companies_attempted": len(companies),
        "companies_succeeded": ok + unchanged,
        "companies_failed": errors,
        "jobs_inserted": new_total,
        "jobs_purged": purged,
        "duration_secs": elapsed,
        "errors": error_details,
    })
    summary = {
        "companies": len(companies), "ok": ok, "unchanged": unchanged,
        "errors": errors, "new_jobs": new_total, "purged": purged,
        "elapsed_s": elapsed, "busiest_host": f"{busiest[0]} ({busiest[1]} reqs)",
    }
    print(f"[collector] {summary}")
    _host_counts.clear()
    return summary'''

    patch_file("collector.py", old_run, new_run, "instrument run() with monitoring")

    # 3. Instrument run_playwright()
    old_pw = '''def run_playwright() -> dict:
    """Run Playwright-based collection only (iCIMS, Taleo, PageUp). Called on its own schedule."""
    db.init_db()
    companies = db.resolved_companies()
    pw_companies = [c for c in companies if c["ats"] in ("icims", "taleo", "pageup")]
    if not pw_companies:
        print("[collector] No Playwright companies to collect")
        return {"playwright": 0, "pw_new": 0}
    started = time.time()
    pw_new = 0
    try:
        from playwright_adapter import scrape_batch
        pw_results = scrape_batch(pw_companies)
        with db.get_conn() as con:
            for c in pw_companies:
                cname = c["company_name"]
                r = pw_results.get(cname, {})
                if r.get("status") != "ok":
                    continue
                seen_ids = set()
                for p in r.get("jobs", []):
                    if not p.get("ext_id"):
                        continue
                    seen_ids.add(p["ext_id"])
                    job = {"ats": c["ats"], "company": cname, "brand": c["brand"],
                           "cap_exempt": c["cap_exempt"], "sponsor": c["sponsor"], **p}
                    if db.upsert_job(con, job):
                        pw_new += 1
                db.mark_missing_inactive(con, c["ats"], cname, seen_ids)
    except Exception as e:
        print(f"[collector] playwright error: {e}")
    elapsed = round(time.time() - started, 1)
    summary = {"playwright": len(pw_companies), "pw_new": pw_new, "elapsed_s": elapsed}
    print(f"[collector:playwright] {summary}")
    return summary'''

    new_pw = '''def run_playwright() -> dict:
    """Run Playwright-based collection only (iCIMS, Taleo, PageUp). Called on its own schedule."""
    db.init_db()
    monitoring.init_monitoring()
    run_id = monitoring.start_run("playwright")
    companies = db.resolved_companies()
    pw_companies = [c for c in companies if c["ats"] in ("icims", "taleo", "pageup")]
    if not pw_companies:
        print("[collector] No Playwright companies to collect")
        monitoring.finish_run(run_id, {
            "run_type": "playwright", "companies_attempted": 0,
            "companies_succeeded": 0, "companies_failed": 0,
            "jobs_inserted": 0, "duration_secs": 0, "errors": [],
            "notes": "no playwright companies",
        })
        return {"playwright": 0, "pw_new": 0}
    started = time.time()
    pw_new = 0
    pw_ok = 0
    pw_fail = 0
    error_details = []
    try:
        from playwright_adapter import scrape_batch
        pw_results = scrape_batch(pw_companies)
        with db.get_conn() as con:
            for c in pw_companies:
                cname = c["company_name"]
                r = pw_results.get(cname, {})
                if r.get("status") != "ok":
                    pw_fail += 1
                    err_msg = r.get("status", "unknown")
                    monitoring.update_company_health(cname, False, error=err_msg)
                    error_details.append({"company": cname, "error": err_msg})
                    continue
                pw_ok += 1
                seen_ids = set()
                for p in r.get("jobs", []):
                    if not p.get("ext_id"):
                        continue
                    seen_ids.add(p["ext_id"])
                    job = {"ats": c["ats"], "company": cname, "brand": c["brand"],
                           "cap_exempt": c["cap_exempt"], "sponsor": c["sponsor"], **p}
                    if db.upsert_job(con, job):
                        pw_new += 1
                db.mark_missing_inactive(con, c["ats"], cname, seen_ids)
                monitoring.update_company_health(cname, True, job_count=len(seen_ids))
    except Exception as e:
        print(f"[collector] playwright error: {e}")
        error_details.append({"company": "GLOBAL", "error": str(e)})
    elapsed = round(time.time() - started, 1)
    monitoring.finish_run(run_id, {
        "run_type": "playwright",
        "companies_attempted": len(pw_companies),
        "companies_succeeded": pw_ok,
        "companies_failed": pw_fail,
        "jobs_inserted": pw_new,
        "duration_secs": elapsed,
        "errors": error_details,
    })
    summary = {"playwright": len(pw_companies), "pw_new": pw_new, "elapsed_s": elapsed}
    print(f"[collector:playwright] {summary}")
    return summary'''

    patch_file("collector.py", old_pw, new_pw, "instrument run_playwright() with monitoring")


def patch_app():
    """Add /health route to app.py."""
    print("\n── Patching app.py ──")

    # Add monitoring import near the top db import
    patch_file("app.py",
        "import db\n",
        "import db\nimport monitoring\n",
        "add monitoring import"
    )

    # Add /health route — find the last @app route and append after it
    # We'll add it before the createuser block or at the end
    health_route = '''

# ── Health Dashboard ──
@app.get("/health", response_class=HTMLResponse)
async def health_page(request: Request):
    user = _get_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    monitoring.init_monitoring()
    data = monitoring.get_health_data()
    return templates.TemplateResponse(request, "health.html", {"data": data})

'''
    # Try to insert before "if __name__" or "def create_user" or at end
    app_path = os.path.join(REPO, "app.py")
    with open(app_path) as f:
        content = f.read()

    if "/health" in content:
        print("  ✓  SKIP app.py: /health route already exists")
        return

    # Insert before the if __name__ block
    if 'if __name__' in content:
        content = content.replace('if __name__', health_route + 'if __name__', 1)
    else:
        content += health_route

    with open(app_path, "w") as f:
        f.write(content)
    print("  ✅ PATCHED app.py — added /health route")


def patch_scheduler():
    """Add health refresh and endpoint checker to scheduler."""
    print("\n── Patching scheduler.py ──")

    # Add monitoring import
    patch_file("scheduler.py",
        "import collector\n",
        "import collector\nimport monitoring\n",
        "add monitoring import"
    )

    # Add health refresh job — append to the schedule setup
    sched_path = os.path.join(REPO, "scheduler.py")
    with open(sched_path) as f:
        content = f.read()

    if "refresh_health" in content:
        print("  ✓  SKIP scheduler.py: health jobs already exist")
        return

    # Add the health refresh function and its schedule
    health_jobs = '''
# ── Monitoring health refresh ──
def _health_refresh():
    """Refresh company health statuses and check for stale endpoints."""
    try:
        monitoring.init_monitoring()
        result = monitoring.refresh_health_statuses()
        print(f"[scheduler] health refresh: {result}")
    except Exception as e:
        print(f"[scheduler] health refresh error: {e}")

def _endpoint_check():
    """Weekly endpoint drift check."""
    try:
        from tools.endpoint_checker import check_endpoint, store_check
        import db
        monitoring.init_monitoring()
        companies = db.resolved_companies()
        drifted = 0
        for c in companies:
            result = check_endpoint(c["company_name"], c["ats"], c["endpoint"], c.get("resolved_token"))
            store_check(result)
            if result["drift_detected"]:
                drifted += 1
            import time; time.sleep(0.5)
        if drifted > 0:
            monitoring._alert(f"🔍 Weekly endpoint check: {drifted}/{len(companies)} endpoints drifted")
        print(f"[scheduler] endpoint check done: {drifted} drifted")
    except Exception as e:
        print(f"[scheduler] endpoint check error: {e}")

'''

    # Find where scheduler.start() is called and add jobs before it
    if "scheduler.start()" in content:
        # Add functions before the scheduler start section
        # And add the cron jobs
        job_schedules = '''
    # Health refresh: daily at 4 AM ET
    scheduler.add_job(_health_refresh, "cron", hour=4, minute=0,
                      timezone=TZ, id="health_refresh", replace_existing=True)
    # Endpoint drift check: every Sunday at 5 AM ET
    scheduler.add_job(_endpoint_check, "cron", day_of_week="sun", hour=5, minute=0,
                      timezone=TZ, id="endpoint_check", replace_existing=True)
'''
        # Insert health_jobs functions before the scheduler setup
        # Insert job schedules before scheduler.start()
        content = content.replace("scheduler.start()", job_schedules + "    scheduler.start()")

        # Add the function definitions earlier in the file
        # Find a good insertion point — before the main setup function
        # Insert after the last function definition that's before scheduler setup
        if "def start_scheduler" in content:
            content = content.replace("def start_scheduler", health_jobs + "def start_scheduler")
        elif "def main" in content:
            content = content.replace("def main", health_jobs + "def main")
        else:
            # Just prepend before scheduler.start line area
            content = health_jobs + content

        with open(sched_path, "w") as f:
            f.write(content)
        print("  ✅ PATCHED scheduler.py — added health refresh + endpoint check jobs")
    else:
        print("  ⚠️  SKIP scheduler.py: couldn't find scheduler.start() — patch manually")


def patch_env():
    """Add Discord webhook to .env.example."""
    print("\n── Patching .env.example ──")
    env_path = os.path.join(REPO, ".env.example")
    if os.path.exists(env_path):
        with open(env_path) as f:
            content = f.read()
        if "DISCORD_WEBHOOK_URL" not in content:
            content += "\n# Monitoring — Discord alerts (optional)\nDISCORD_WEBHOOK_URL=\n"
            with open(env_path, "w") as f:
                f.write(content)
            print("  ✅ PATCHED .env.example — added DISCORD_WEBHOOK_URL")
        else:
            print("  ✓  SKIP .env.example: already has DISCORD_WEBHOOK_URL")
    else:
        print("  ⚠️  .env.example not found")


def main():
    print("=" * 60)
    print("Deploying Job Hunter v2 Monitoring System")
    print("=" * 60)

    patch_collector()
    patch_app()
    patch_scheduler()
    patch_env()

    print("\n" + "=" * 60)
    print("DONE. Next steps:")
    print("  1. Set up Discord webhook and add to .env:")
    print("     DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...")
    print("  2. Restart services:")
    print("     sudo systemctl restart jobhunter-web jobhunter-collector")
    print("  3. Visit https://35-222-128-202.sslip.io/health")
    print("=" * 60)


if __name__ == "__main__":
    main()
