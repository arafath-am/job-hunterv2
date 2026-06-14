#!/usr/bin/env python3
"""
resolve_identified.py — Resolve companies with manually researched ATS endpoints.

Incorporates URLs found via web research (Session 5) for iCIMS custom-domain
React SPAs, Workday tenants, PageUp tenants, and Ashby boards.

Also corrects ATS misidentifications and demotes companies with proprietary
or unsupported ATS platforms.

Run from the project root:
    python3 migrations/resolve_identified.py              # apply all
    python3 migrations/resolve_identified.py --dry-run    # preview only
"""

import sqlite3
import sys
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get("JOBHUNTER_DB", "jobhunter.db")

# ═══════════════════════════════════════════════════════════════════════
# RESOLUTIONS — companies with confirmed working career page URLs
# ═══════════════════════════════════════════════════════════════════════
# brand_pattern: matched case-insensitively against brand column
# match_all: if True, resolve ALL matching entries (e.g., Qualcomm ×3)

RESOLUTIONS = [

    # ── iCIMS (custom domain React SPAs) ─────────────────────────────
    # These use modern iCIMS Talent Cloud with custom domains.
    # The playwright scraper already handles non-.icims.com URLs
    # via the custom domain branch (is_react_spa = True).
    {
        "brand_pattern": "Advanced Micro Devices",
        "alt_patterns": ["AMD"],
        "ats": "icims",
        "resolved_token": "careers-amd",
        "endpoint": "https://careers.amd.com/careers-home/jobs",
        "careers_url": "https://careers.amd.com/careers-home/jobs",
    },
    {
        "brand_pattern": "Auburn Univ",
        "ats": "icims",
        "resolved_token": "jobs-auburn",
        "endpoint": "https://jobs.auburn.edu/auburn-careers-home/jobs",
        "careers_url": "https://jobs.auburn.edu/auburn-careers-home/jobs",
    },
    {
        "brand_pattern": "Brookdale Hospital",
        "ats": "icims",
        "resolved_token": "onebrooklynhealth",
        "endpoint": "https://careers.onebrooklynhealth.org/jobs",
        "careers_url": "https://careers.onebrooklynhealth.org/jobs",
    },
    {
        "brand_pattern": "City National Bank",
        "ats": "icims",
        "resolved_token": "careers-cnb",
        "endpoint": "https://careers.cnb.com/jobs",
        "careers_url": "https://careers.cnb.com/jobs",
    },
    {
        "brand_pattern": "Costco",
        "ats": "icims",
        "resolved_token": "careers-costco",
        "endpoint": "https://careers-costco.icims.com",
        "careers_url": "https://www.costco.com/jobs.html",
    },
    {
        "brand_pattern": "Dish",
        "alt_patterns": ["EchoStar"],
        "ats": "icims",
        "resolved_token": "jobs-echostar",
        "endpoint": "https://jobs.echostar.com/jobs",
        "careers_url": "https://www.echostar.com/careers",
    },
    {
        "brand_pattern": "DocuSign",
        "alt_patterns": ["Docusign"],
        "ats": "icims",
        "resolved_token": "careers-docusign",
        "endpoint": "https://careers.docusign.com/careers-home/jobs",
        "careers_url": "https://careers.docusign.com/careers-home/jobs",
    },
    {
        "brand_pattern": "Icahn School",
        "alt_patterns": ["Mount Sinai"],
        "match_all": True,
        "ats": "icims",
        "resolved_token": "careers-mountsinai",
        "endpoint": "https://careers.mountsinai.org/jobs",
        "careers_url": "https://careers.mountsinai.org/jobs",
        "note": "Mount Sinai entities share careers.mountsinai.org",
    },
    {
        "brand_pattern": "Intercontinental Exchange",
        "alt_patterns": ["ICE"],
        "ats": "icims",
        "resolved_token": "careers-ice",
        "endpoint": "https://careers.ice.com/jobs",
        "careers_url": "https://careers.ice.com/jobs",
    },
    {
        "brand_pattern": "Atlassian",
        "ats": "icims",
        "resolved_token": "globalcareers-atlassian",
        "endpoint": "https://globalcareers-atlassian.icims.com",
        "careers_url": "https://www.atlassian.com/company/careers/all-jobs",
    },

    # ── Ashby ────────────────────────────────────────────────────────
    {
        "brand_pattern": "Resolve AI",
        "ats": "ashby",
        "resolved_token": "Resolve AI",
        "endpoint": "https://api.ashbyhq.com/posting-api/job-board/Resolve AI",
        "careers_url": "https://jobs.ashbyhq.com/Resolve%20AI",
    },

    # ── Workday ──────────────────────────────────────────────────────
    {
        "brand_pattern": "Cornell",
        "ats": "workday",
        "resolved_token": "cornell/wd1/CornellCareerPage",
        "endpoint": "https://cornell.wd1.myworkdayjobs.com/wday/cxs/cornell/CornellCareerPage/jobs",
        "careers_url": "https://cornell.wd1.myworkdayjobs.com/CornellCareerPage",
    },
    {
        "brand_pattern": "Qualcomm",
        "match_all": True,
        "ats": "workday",
        "resolved_token": "qualcomm/wd12/External",
        "endpoint": "https://qualcomm.wd12.myworkdayjobs.com/wday/cxs/qualcomm/External/jobs",
        "careers_url": "https://qualcomm.wd12.myworkdayjobs.com/External",
        "note": "3 Qualcomm entities (Atheros/Innovation Center/Technologies) share one Workday tenant",
    },

    # ── PageUp ───────────────────────────────────────────────────────
    {
        "brand_pattern": "Columbia Univ",
        "ats": "pageup",
        "resolved_token": "884",
        "endpoint": "https://careers.pageuppeople.com/884/cwuat/en-us/listing/",
        "careers_url": "https://careers.columbia.edu",
    },
    {
        "brand_pattern": "Michigan State",
        "ats": "pageup",
        "resolved_token": "782",
        "endpoint": "https://careers.msu.edu/jobs/search",
        "careers_url": "https://careers.msu.edu/jobs/search",
        "note": "Custom domain PageUp (careers.msu.edu)",
    },
    {
        "brand_pattern": "University of Florida",
        "ats": "pageup",
        "resolved_token": "674",
        "endpoint": "https://careers.pageuppeople.com/674/cw/en-us/listing/",
        "careers_url": "https://jobs.ufl.edu",
    },
]

# ═══════════════════════════════════════════════════════════════════════
# ATS CORRECTIONS — companies that were identified as the wrong ATS
# ═══════════════════════════════════════════════════════════════════════
# These were tagged "icims" but the actual career site uses a different
# ATS platform for which we have no adapter.

ATS_CORRECTIONS = [
    {
        "brand_pattern": "Brooklyn Hospital",
        "new_ats": "adp",
        "new_status": "identified",
        "note": "Uses ADP Workforce (myjobs.adp.com) — no adapter",
        "careers_url": "https://myjobs.adp.com/tbhcareers/cx",
    },
    {
        "brand_pattern": "Insurance Services Office",
        "alt_patterns": ["Verisk"],
        "new_ats": "oracle_hcm",
        "new_status": "identified",
        "note": "Uses Oracle HCM Cloud — no adapter",
        "careers_url": "https://fa-ewmy-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs",
    },
]

# ═══════════════════════════════════════════════════════════════════════
# DEMOTIONS — companies with proprietary or unsupported ATS
# ═══════════════════════════════════════════════════════════════════════

DEMOTIONS = [
    {
        "brand_pattern": "Meta Platforms",
        "note": "Uses proprietary ATS at metacareers.com — no standard adapter possible",
    },
    {
        "brand_pattern": "Alliance for Sustainable Energy",
        "note": "NREL — career page not found, possibly internal/custom system",
    },
]


# ═══════════════════════════════════════════════════════════════════════
# Implementation
# ═══════════════════════════════════════════════════════════════════════

def find_companies(conn, brand_pattern, alt_patterns=None, match_all=False):
    """Find company_name PK(s) matching a brand pattern."""
    patterns = [brand_pattern] + (alt_patterns or [])

    all_rows = []
    seen = set()
    for pat in patterns:
        for col in ("brand", "company_name"):
            rows = conn.execute(
                f"SELECT company_name, brand, ats, resolve_status, endpoint "
                f"FROM companies WHERE UPPER({col}) LIKE ?",
                (f"%{pat.upper()}%",)
            ).fetchall()
            for r in rows:
                if r["company_name"] not in seen:
                    seen.add(r["company_name"])
                    all_rows.append(r)

    if match_all:
        return all_rows
    return all_rows[:1]


def main():
    dry_run = "--dry-run" in sys.argv

    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ts = datetime.now(timezone.utc).isoformat()

    print(f"{'DRY RUN — ' if dry_run else ''}Resolving identified companies...\n")

    resolved_count = 0
    corrected_count = 0
    demoted_count = 0
    skipped_count = 0

    # ── Apply resolutions ────────────────────────────────────────────
    print("─── RESOLUTIONS ───")
    for res in RESOLUTIONS:
        pattern = res["brand_pattern"]
        alt = res.get("alt_patterns")
        match_all = res.get("match_all", False)
        matches = find_companies(conn, pattern, alt, match_all)

        if not matches:
            print(f"  ⚠ {pattern}: no matching company in DB")
            continue

        for row in matches:
            cname = row["company_name"]
            brand = row["brand"] or cname
            current_status = row["resolve_status"]
            current_endpoint = row["endpoint"]

            if current_status == "resolved" and current_endpoint:
                print(f"  ✓ {brand[:45]}: already resolved")
                skipped_count += 1
                continue

            if not dry_run:
                conn.execute("""
                    UPDATE companies
                    SET ats = ?, resolved_token = ?, endpoint = ?,
                        careers_url = ?, resolve_status = 'resolved',
                        updated_at = ?
                    WHERE company_name = ?
                """, (
                    res["ats"], res["resolved_token"], res["endpoint"],
                    res.get("careers_url", ""), ts, cname,
                ))

            note = res.get("note", "")
            note_str = f"  ({note})" if note else ""
            print(f"  → {brand[:45]}: {res['ats']} → {res['endpoint'][:60]}{note_str}")
            resolved_count += 1

    # ── Apply ATS corrections ────────────────────────────────────────
    print("\n─── ATS CORRECTIONS ───")
    for cor in ATS_CORRECTIONS:
        pattern = cor["brand_pattern"]
        alt = cor.get("alt_patterns")
        matches = find_companies(conn, pattern, alt, match_all=True)

        if not matches:
            print(f"  ⚠ {pattern}: no matching company in DB")
            continue

        for row in matches:
            cname = row["company_name"]
            brand = row["brand"] or cname

            if not dry_run:
                conn.execute("""
                    UPDATE companies
                    SET ats = ?, resolve_status = ?, careers_url = ?,
                        endpoint = NULL, resolved_token = NULL, updated_at = ?
                    WHERE company_name = ?
                """, (
                    cor["new_ats"], cor["new_status"],
                    cor.get("careers_url", ""), ts, cname,
                ))

            print(f"  ✎ {brand[:45]}: {row['ats']} → {cor['new_ats']} ({cor['note']})")
            corrected_count += 1

    # ── Apply demotions ──────────────────────────────────────────────
    print("\n─── DEMOTIONS ───")
    for dem in DEMOTIONS:
        pattern = dem["brand_pattern"]
        matches = find_companies(conn, pattern, match_all=True)

        if not matches:
            print(f"  ⚠ {pattern}: no matching company in DB")
            continue

        for row in matches:
            cname = row["company_name"]
            brand = row["brand"] or cname

            if row["resolve_status"] == "not_found":
                print(f"  ✓ {brand[:45]}: already not_found")
                skipped_count += 1
                continue

            if not dry_run:
                conn.execute("""
                    UPDATE companies
                    SET resolve_status = 'not_found', updated_at = ?
                    WHERE company_name = ?
                """, (ts, cname))

            print(f"  ✗ {brand[:45]}: demoted — {dem['note']}")
            demoted_count += 1

    if not dry_run:
        conn.commit()
    conn.close()

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"{'DRY RUN ' if dry_run else ''}COMPLETE")
    print(f"  Resolved:   {resolved_count} companies (will start collecting)")
    print(f"  Corrected:  {corrected_count} (wrong ATS fixed)")
    print(f"  Demoted:    {demoted_count} (proprietary/unsupported)")
    print(f"  Skipped:    {skipped_count} (already resolved)")
    if dry_run:
        print(f"\n  Run without --dry-run to apply.")
    else:
        print(f"\n  Restart collector to begin collecting:")
        print(f"    sudo systemctl restart jobhunter-collector")


if __name__ == "__main__":
    main()
