#!/usr/bin/env python3
"""
Extract jobs from universal scraper — only for audited/approved companies.
Reads the audited report to skip excluded companies, re-probes the rest, stores jobs.
"""
import json
import os
import sys
import time
import sqlite3

# Add parent dir so we can import
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tools.universal_scraper import probe_company, store_jobs, update_company_status

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "jobhunter.db")
AUDIT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "universal_scraper_report_audited.json")
FETCH_DELAY = 1.5

def get_approved_companies():
    """Load audited report, return only non-excluded companies that had jobs."""
    with open(AUDIT_PATH) as f:
        data = json.load(f)
    
    approved = []
    excluded = []
    for r in data.get("results", []):
        if r.get("excluded"):
            excluded.append(r["company"])
            continue
        strategy = r.get("strategy")
        if strategy and strategy not in ("none", "ats_only") and r.get("job_count", 0) > 0:
            approved.append(r)
    
    print(f"Loaded audit: {len(approved)} approved, {len(excluded)} excluded")
    return approved

def get_company_details(db_path, company_name):
    """Fetch cap_exempt and sponsor from DB."""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT cap_exempt, sponsor FROM companies WHERE company_name=?",
        (company_name,)
    ).fetchone()
    conn.close()
    return row if row else (0, 0)

def main():
    approved = get_approved_companies()
    total = len(approved)
    total_stored = 0
    success = 0
    failed = 0
    
    print(f"\nExtracting jobs for {total} companies...\n")
    
    for i, co in enumerate(approved, 1):
        name = co["company"]
        brand = co.get("brand", name)
        url = co.get("careers_url", "")
        prev_count = co.get("job_count", 0)
        
        print(f"[{i}/{total}] {brand} (prev: {prev_count} jobs)")
        
        try:
            result = probe_company(name, brand, url)
            strategy = result["strategy"]
            job_count = result["job_count"]
            jobs = result.get("jobs", [])
            
            if strategy and job_count > 0 and jobs:
                # Update company status
                update_company_status(DB_PATH, name, strategy, result["ats_detected"], job_count)
                
                # Get company details from DB
                cap_exempt, sponsor = get_company_details(DB_PATH, name)
                
                # Store jobs
                stored = store_jobs(DB_PATH, name, brand, cap_exempt, sponsor, jobs, strategy)
                total_stored += stored
                success += 1
                print(f"  ✅ {stored} jobs stored ({strategy})")
            elif strategy and job_count > 0:
                update_company_status(DB_PATH, name, strategy, result["ats_detected"], job_count)
                print(f"  ⚠️  {job_count} found but no extractable job data ({strategy})")
                success += 1
            else:
                print(f"  ❌ No jobs on re-probe")
                failed += 1
            
            time.sleep(FETCH_DELAY)
            
        except KeyboardInterrupt:
            print("\n\n⛔ Interrupted")
            break
        except Exception as e:
            print(f"  ⚠️  Error: {e}")
            failed += 1
    
    print(f"\n{'='*60}")
    print(f"DONE: {success} succeeded, {failed} failed")
    print(f"Total jobs stored: {total_stored:,}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
