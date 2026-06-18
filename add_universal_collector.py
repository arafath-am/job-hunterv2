#!/usr/bin/env python3
"""Append run_universal() to collector.py"""
import os

COLLECTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collector.py")

content = open(COLLECTOR).read()

if "run_universal" in content:
    print("✓ run_universal() already exists in collector.py")
    exit(0)

new_code = '''

# --------------------------------------------------------- universal scraper collection
def run_universal() -> dict:
    """Run universal scraper collection for sitemap/heuristic/jsonld companies."""
    if not _collection_lock.acquire(timeout=120):
        print("[collector] skipping Universal — another collection is running")
        return {"skipped": True}
    try:
        return _run_universal_inner()
    finally:
        _collection_lock.release()


def _run_universal_inner() -> dict:
    db.init_db()
    monitoring.init_monitoring()
    run_id = monitoring.start_run("universal")

    # Get universal scraper companies
    with db.get_conn() as con:
        companies = con.execute("""
            SELECT company_name, brand, cap_exempt, sponsor, ats, careers_url
            FROM companies
            WHERE resolve_status='resolved' AND ats IN ('sitemap', 'heuristic', 'jsonld')
        """).fetchall()
        companies = [dict(c) for c in companies]

    if not companies:
        print("[collector] No universal scraper companies to collect")
        monitoring.finish_run(run_id, {
            "run_type": "universal", "companies_attempted": 0,
            "companies_succeeded": 0, "companies_failed": 0,
            "jobs_inserted": 0, "duration_secs": 0, "errors": [],
        })
        return {"universal": 0, "new": 0}

    from tools.universal_scraper import probe_company

    started = time.time()
    total_stored = 0
    ok_count = 0
    fail_count = 0
    error_details = []

    def _collect_one_universal(c):
        name = c["company_name"]
        brand = c["brand"] or name
        url = c["careers_url"] or ""
        try:
            result = probe_company(name, brand, url)
            strategy = result.get("strategy")
            jobs = result.get("jobs", [])
            if strategy and jobs:
                new_count = 0
                seen_ids = set()
                with db.get_conn() as con:
                    for j in jobs:
                        ext_id = j.get("ext_id") or j.get("url", "")[-80:]
                        if not ext_id or ext_id in seen_ids:
                            continue
                        seen_ids.add(ext_id)
                        job_rec = {
                            "ats": c["ats"],
                            "company": name,
                            "brand": brand,
                            "cap_exempt": c["cap_exempt"],
                            "sponsor": c["sponsor"],
                            "ext_id": ext_id,
                            "title": j.get("title", ""),
                            "location": j.get("location", ""),
                            "url": j.get("url", ""),
                            "posted_at": j.get("posted_at", ""),
                        }
                        if db.upsert_job(con, job_rec):
                            new_count += 1
                    db.mark_missing_inactive(con, c["ats"], name, seen_ids)
                return {"company": name, "status": "ok", "new": new_count, "total": len(seen_ids)}
            else:
                return {"company": name, "status": "ok", "new": 0, "total": 0}
        except Exception as e:
            return {"company": name, "status": f"err:{e}", "new": 0, "total": 0}

    # 6 threads — heavier than API calls but still HTTP-only
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_collect_one_universal, c): c for c in companies}
        for fut in as_completed(futs):
            c = futs[fut]
            res = fut.result()
            if res["status"] == "ok":
                ok_count += 1
                total_stored += res["new"]
                monitoring.update_company_health(
                    c["company_name"], True, job_count=res.get("total", 0)
                )
            else:
                fail_count += 1
                monitoring.update_company_health(
                    c["company_name"], False, error=res["status"]
                )
                error_details.append({
                    "company": c["company_name"], "error": res["status"]
                })

    elapsed = round(time.time() - started, 1)
    monitoring.finish_run(run_id, {
        "run_type": "universal",
        "companies_attempted": len(companies),
        "companies_succeeded": ok_count,
        "companies_failed": fail_count,
        "jobs_inserted": total_stored,
        "duration_secs": elapsed,
        "errors": error_details,
    })
    summary = {
        "universal": len(companies), "ok": ok_count, "fail": fail_count,
        "new_jobs": total_stored, "elapsed_s": elapsed,
    }
    print(f"[collector:universal] {summary}")
    return summary
'''

content += new_code
open(COLLECTOR, "w").write(content)
print("✅ run_universal() appended to collector.py")
print(f"   Total 'run_universal' refs: {content.count('run_universal')}")
