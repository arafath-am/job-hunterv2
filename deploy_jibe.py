#!/usr/bin/env python3
"""
deploy_jibe.py — Adds Jibe (iCIMS Talent Cloud) JSON API adapter
and migrates custom-domain iCIMS companies to use it.

Run from ~/job-hunterv2:
    python3 deploy_jibe.py --dry-run    # preview
    python3 deploy_jibe.py              # apply
"""

import sqlite3, sys, os, re
from datetime import datetime, timezone

DB_PATH = os.environ.get("JOBHUNTER_DB", "jobhunter.db")
DRY_RUN = "--dry-run" in sys.argv

# ═══════════════════════════════════════════════════════════════
# 1. PATCH collector.py — add Jibe adapter
# ═══════════════════════════════════════════════════════════════

JIBE_ADAPTER = '''

# ── Jibe (iCIMS Talent Cloud) JSON API adapter ──────────────────

def parse_jibe(data, base_url=""):
    """Parse Jibe API response into normalized job dicts."""
    jobs = []
    for item in data.get("jobs", []):
        j = item.get("data", {})
        slug = j.get("slug", "")
        job_url = f"{base_url}/{slug}?lang=en-us" if base_url else j.get("apply_url", "")
        loc_parts = [j.get("city", ""), j.get("state", ""), j.get("country", "")]
        location = ", ".join(p for p in loc_parts if p and p != "?")
        if not location:
            location = j.get("location_name", "") or j.get("full_location", "")
        jobs.append({
            "ext_id": str(slug),
            "title": j.get("title", ""),
            "location": location,
            "department": j.get("department", "") or (j.get("categories", [None]) or [None])[0] or "",
            "url": job_url,
            "posted_at": j.get("posted_date", ""),
        })
    return jobs


def collect_jibe(c) -> dict:
    """Fetch all jobs from a Jibe (iCIMS Talent Cloud) JSON API."""
    company, endpoint = c["company_name"], c["endpoint"]
    m = re.match(r"(https?://[\\w.-]+)", endpoint)
    if not m:
        return {"company": company, "status": "skip", "new": 0}
    domain_base = m.group(1)
    api_url = f"{domain_base}/api/jobs"
    page_base = endpoint.rstrip("/")

    all_postings = []
    offset = 0
    PAGE = 100
    while True:
        url = f"{api_url}?limit={PAGE}&offset={offset}"
        status, data = _fetch(url, use_cache=False)
        if status != 200 or not isinstance(data, dict):
            if offset == 0:
                return {"company": company, "status": f"err:{status}", "new": 0}
            break
        batch = parse_jibe(data, page_base)
        if not batch:
            break
        all_postings.extend(batch)
        total = data.get("count", 0) or data.get("totalCount", 0)
        offset += PAGE
        if offset >= total:
            break

    if not all_postings:
        return {"company": company, "status": "empty", "new": 0}

    new_count = 0
    seen_ids = set()
    with db.get_conn() as con:
        for p in all_postings:
            if not p.get("ext_id"):
                continue
            seen_ids.add(p["ext_id"])
            job = {"ats": "jibe", "company": company, "brand": c["brand"],
                   "cap_exempt": c["cap_exempt"], "sponsor": c["sponsor"], **p}
            if db.upsert_job(con, job):
                new_count += 1
        db.mark_missing_inactive(con, "jibe", company, seen_ids)
    return {"company": company, "status": "ok", "new": new_count, "total": len(all_postings)}

'''

print("=== Jibe API Adapter Deployment ===\\n")

# Check if adapter already exists
with open("collector.py", "r") as f:
    code = f.read()

if "collect_jibe" in code:
    print("[1/3] ✓ Jibe adapter already in collector.py")
else:
    # Insert before the collect_company function
    insertion_point = "def collect_company(c) -> dict:"
    if insertion_point in code:
        code = code.replace(insertion_point, JIBE_ADAPTER + "\n" + insertion_point)

        # Also add jibe to the dispatch in collect_company
        old_dispatch = '    if ats == "workday":\n        return collect_workday(c)\n    if ats in ("icims", "taleo", "pageup"):'
        new_dispatch = '    if ats == "workday":\n        return collect_workday(c)\n    if ats == "jibe":\n        return collect_jibe(c)\n    if ats in ("icims", "taleo", "pageup"):'
        if old_dispatch in code:
            code = code.replace(old_dispatch, new_dispatch)
        else:
            # Try simpler pattern
            code = code.replace(
                '    if ats == "workday":\n        return collect_workday(c)',
                '    if ats == "workday":\n        return collect_workday(c)\n    if ats == "jibe":\n        return collect_jibe(c)'
            )

        if not DRY_RUN:
            with open("collector.py", "w") as f:
                f.write(code)
        print("[1/3] ✓ Jibe adapter added to collector.py")
    else:
        print("[1/3] ⚠ Could not find insertion point in collector.py")
        sys.exit(1)

# ═══════════════════════════════════════════════════════════════
# 2. Verify syntax
# ═══════════════════════════════════════════════════════════════

if not DRY_RUN:
    try:
        import ast
        ast.parse(open("collector.py").read())
        print("[2/3] ✓ collector.py syntax valid")
    except SyntaxError as e:
        print(f"[2/3] ✗ Syntax error: {e}")
        print("  Restore backup and investigate")
        sys.exit(1)
else:
    print("[2/3] ✓ Skipped (dry run)")

# ═══════════════════════════════════════════════════════════════
# 3. Migrate companies from icims → jibe
# ═══════════════════════════════════════════════════════════════

# Companies with confirmed Jibe API endpoints
JIBE_COMPANIES = [
    {"brand_pattern": "Advanced Micro Devices", "alt": ["AMD"],
     "endpoint": "https://careers.amd.com/careers-home/jobs"},
    {"brand_pattern": "Auburn Univ",
     "endpoint": "https://jobs.auburn.edu/auburn-careers-home/jobs"},
    {"brand_pattern": "Icahn School", "alt": ["Mount Sinai"], "match_all": True,
     "endpoint": "https://careers.mountsinai.org/jobs"},
    {"brand_pattern": "Docusign", "alt": ["DocuSign"],
     "endpoint": "https://careers.docusign.com/careers-home/jobs"},
    {"brand_pattern": "Rivian",
     "endpoint": "https://careers.rivian.com/careers-home/jobs"},
    {"brand_pattern": "ZS Associates",
     "endpoint": "https://jobs.zs.com/jobs"},
    {"brand_pattern": "Intercontinental Exchange", "alt": ["ICE"],
     "endpoint": "https://careers.ice.com/jobs"},
    {"brand_pattern": "Tufts Univ",
     "endpoint": "https://jobs.tufts.edu/jobs"},
    {"brand_pattern": "Yale New Haven",
     "endpoint": "https://jobs.ynhhs.org/jobs"},
    {"brand_pattern": "City National Bank",
     "endpoint": "https://careers.cnb.com/jobs"},
    {"brand_pattern": "Costco",
     "endpoint": "https://careers-costco.icims.com"},  # keep as icims if no custom domain
    {"brand_pattern": "Atlassian",
     "endpoint": "https://globalcareers-atlassian.icims.com"},  # keep checking
    {"brand_pattern": "Brookdale Hospital",
     "endpoint": "https://careers.onebrooklynhealth.org/jobs"},
    {"brand_pattern": "Dish Wireless", "alt": ["EchoStar"],
     "endpoint": "https://jobs.echostar.com/jobs"},
]

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
ts = datetime.now(timezone.utc).isoformat()

print(f"\n[3/3] Migrating companies to Jibe API...")
migrated = 0
skipped = 0

for comp in JIBE_COMPANIES:
    patterns = [comp["brand_pattern"]] + comp.get("alt", [])
    match_all = comp.get("match_all", False)

    all_rows = []
    seen = set()
    for pat in patterns:
        for col in ("brand", "company_name"):
            rows = conn.execute(
                f"SELECT company_name, brand, ats, endpoint FROM companies WHERE UPPER({col}) LIKE ?",
                (f"%{pat.upper()}%",)
            ).fetchall()
            for r in rows:
                if r["company_name"] not in seen:
                    seen.add(r["company_name"])
                    all_rows.append(r)
    if not match_all:
        all_rows = all_rows[:1]

    if not all_rows:
        print(f"  ⚠ {comp['brand_pattern']}: not found in DB")
        continue

    endpoint = comp["endpoint"]
    # Determine if this is a Jibe site (custom domain, not .icims.com)
    is_jibe = ".icims.com" not in endpoint
    new_ats = "jibe" if is_jibe else "icims"

    for row in all_rows:
        cname = row["company_name"]
        brand = row["brand"] or cname

        if row["ats"] == new_ats and row["endpoint"] == endpoint:
            skipped += 1
            continue

        if not DRY_RUN:
            conn.execute(
                "UPDATE companies SET ats=?, endpoint=?, resolve_status='resolved', updated_at=? WHERE company_name=?",
                (new_ats, endpoint, ts, cname)
            )

        print(f"  → {brand[:40]}: {new_ats} → {endpoint[:60]}")
        migrated += 1

if not DRY_RUN:
    conn.commit()
conn.close()

print(f"\n{'='*60}")
print(f"{'DRY RUN ' if DRY_RUN else ''}COMPLETE")
print(f"  Migrated: {migrated}")
print(f"  Skipped:  {skipped} (already correct)")
if not DRY_RUN:
    print(f"\n  Restart collector: sudo systemctl restart jobhunter-collector")
    print(f"  Test: python3 -c \"import collector; print(collector.run())\"")
