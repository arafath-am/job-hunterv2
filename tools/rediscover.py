#!/usr/bin/env python3
"""
rediscover.py — Targeted re-discovery for companies that have an ATS type
identified but no working endpoint.

Unlike discover.py (which targets all unresolved companies), this focuses
specifically on 'identified' companies — we already know their ATS platform
but need to find the actual tenant URL / feed endpoint.

Uses Serper.dev (Google Search API) with ATS-specific search queries.

Usage:
    export SERPER_API_KEY="your-key-here"
    python3 tools/rediscover.py                    # discover all identified
    python3 tools/rediscover.py --ats icims        # only iCIMS companies
    python3 tools/rediscover.py --ats workday      # only Workday companies
    python3 tools/rediscover.py --ats pageup       # only PageUp companies
    python3 tools/rediscover.py --limit 10         # first 10 only
    python3 tools/rediscover.py --dry-run          # preview, don't write DB
"""

import sqlite3
import json
import re
import time
import os
import sys
import argparse
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone

DB_PATH = os.environ.get("JOBHUNTER_DB", "jobhunter.db")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
SERPER_URL = "https://google.serper.dev/search"

# ── ATS-specific search strategies ──────────────────────────────────

ATS_SEARCH_TEMPLATES = {
    "icims": [
        '"{brand}" careers site:icims.com',
        '"{brand}" jobs icims.com',
        '"{brand}" careers jobs apply',
    ],
    "workday": [
        '"{brand}" careers site:myworkdayjobs.com',
        '"{brand}" jobs myworkdayjobs.com',
        '"{brand}" careers jobs apply workday',
    ],
    "pageup": [
        '"{brand}" careers site:pageuppeople.com',
        '"{brand}" jobs pageuppeople.com',
        '"{brand}" careers jobs apply',
    ],
    "successfactors": [
        '"{brand}" careers site:successfactors.com',
        '"{brand}" jobs successfactors',
        '"{brand}" careers jobs apply',
    ],
    "greenhouse": [
        '"{brand}" careers site:greenhouse.io',
        '"{brand}" jobs greenhouse.io',
    ],
    "smartrecruiters": [
        '"{brand}" careers site:smartrecruiters.com',
        '"{brand}" jobs smartrecruiters.com',
    ],
    "ashby": [
        '"{brand}" careers site:ashbyhq.com',
        '"{brand}" jobs ashbyhq.com',
    ],
}

# ── URL patterns for endpoint extraction ─────────────────────────────

ENDPOINT_PATTERNS = {
    "workday": [
        # https://mit.wd5.myworkdayjobs.com/MIT → tenant=mit, dc=wd5, site=MIT
        re.compile(r"https?://(\w[\w-]*)\.(\w+)\.myworkdayjobs\.com(?:/en-US)?(?:/(\w[\w-]*))?"),
    ],
    "icims": [
        # https://careers-amd.icims.com  OR  https://globalcareers-atlassian.icims.com
        re.compile(r"(https?://[\w-]+\.icims\.com)"),
    ],
    "pageup": [
        # https://careers.pageuppeople.com/884/cwuat/en-us/listing/
        re.compile(r"(https?://[\w.]*pageuppeople\.com/\d+/\w+/[\w-]+/[\w-]+(?:/listing)?)"),
        # https://careersmanager.pageuppeople.com/997/cw/en-us/listing
        re.compile(r"(https?://careersmanager\.pageuppeople\.com/\d+/\w+/[\w-]+/[\w-]+(?:/listing)?)"),
    ],
    "greenhouse": [
        re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/([\w-]+)"),
    ],
    "smartrecruiters": [
        re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)"),
    ],
    "ashby": [
        re.compile(r"https?://jobs\.ashbyhq\.com/([\w-]+)"),
    ],
    "successfactors": [
        re.compile(r"(https?://[\w-]+\.successfactors\.\w+)"),
    ],
}


def search_google(query, num=10):
    """Search via Serper.dev. Returns list of {title, link, snippet}."""
    if not SERPER_API_KEY:
        print("ERROR: Set SERPER_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)

    payload = json.dumps({"q": query, "num": num}).encode()
    req = Request(SERPER_URL, data=payload, headers={
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json",
    })
    try:
        with urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            return data.get("organic", [])
    except Exception as e:
        print(f"    Search error: {e}")
        return []


def extract_endpoint(ats, url, html=None):
    """Extract a working endpoint URL from a search result URL (or page HTML)."""
    patterns = ENDPOINT_PATTERNS.get(ats, [])

    for pat in patterns:
        m = pat.search(url)
        if m:
            return _build_endpoint(ats, m, url)

    # Also scan HTML if provided
    if html:
        for pat in patterns:
            m = pat.search(html)
            if m:
                return _build_endpoint(ats, m, m.group(0))

    return None


def _build_endpoint(ats, match, url):
    """Build the feed endpoint from a regex match."""
    if ats == "workday":
        tenant = match.group(1)
        dc = match.group(2)
        site = match.group(3) or tenant
        endpoint = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        return {
            "resolved_token": f"{tenant}/{dc}/{site}",
            "endpoint": endpoint,
            "careers_url": f"https://{tenant}.{dc}.myworkdayjobs.com/{site}",
        }

    elif ats == "icims":
        base = match.group(1)
        # Strip path — just need the host
        base = re.match(r"(https?://[\w.-]+\.icims\.com)", base).group(1)
        token = re.search(r"https?://([\w-]+)\.icims\.com", base).group(1)
        return {
            "resolved_token": token,
            "endpoint": base,
            "careers_url": base,
        }

    elif ats == "pageup":
        page_url = match.group(1)
        # Ensure it ends with /listing/ for the scraper
        if not page_url.rstrip("/").endswith("listing"):
            page_url = page_url.rstrip("/") + "/listing/"
        # Extract tenant ID
        tid = re.search(r"/(\d+)/", page_url)
        token = tid.group(1) if tid else page_url
        return {
            "resolved_token": token,
            "endpoint": page_url,
            "careers_url": page_url,
        }

    elif ats == "greenhouse":
        slug = match.group(1)
        return {
            "resolved_token": slug,
            "endpoint": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            "careers_url": f"https://boards.greenhouse.io/{slug}",
        }

    elif ats == "smartrecruiters":
        slug = match.group(1)
        return {
            "resolved_token": slug,
            "endpoint": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            "careers_url": f"https://jobs.smartrecruiters.com/{slug}",
        }

    elif ats == "ashby":
        slug = match.group(1)
        return {
            "resolved_token": slug,
            "endpoint": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            "careers_url": f"https://jobs.ashbyhq.com/{slug}",
        }

    return None


def fetch_page(url, max_bytes=200_000):
    """Fetch a page for HTML scanning."""
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; JobHunter/1.0)",
        })
        with urlopen(req, timeout=12) as r:
            return r.read(max_bytes).decode("utf-8", errors="replace")
    except Exception:
        return ""


def discover_one(brand, ats):
    """Search for a company's ATS endpoint. Returns dict or None."""
    templates = ATS_SEARCH_TEMPLATES.get(ats, ['"{brand}" careers jobs apply'])

    for template in templates:
        query = template.format(brand=brand)
        results = search_google(query, num=8)

        if not results:
            continue

        # Scan result URLs for endpoint patterns
        for r in results:
            url = r.get("link", "")
            ep = extract_endpoint(ats, url)
            if ep:
                return ep

        # Fallback: fetch top 2 results and scan HTML
        for r in results[:2]:
            url = r.get("link", "")
            if not url:
                continue
            html = fetch_page(url)
            if html:
                ep = extract_endpoint(ats, url, html)
                if ep:
                    return ep

        # If first template found results but no endpoint, try next template
        time.sleep(0.8)

    return None


def get_identified_companies(ats_filter=None, limit=None):
    """Get companies with identified ATS but no working endpoint."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    sql = """
        SELECT company_name, brand, ats, cap_exempt, sponsor, resolve_status,
               endpoint, resolved_token
        FROM companies
        WHERE resolve_status = 'identified'
          AND (endpoint IS NULL OR endpoint = '')
    """
    params = []
    if ats_filter:
        sql += " AND ats = ?"
        params.append(ats_filter)
    sql += " ORDER BY ats, company_name"
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def save_resolution(company_name, ats, result):
    """Save discovered endpoint to the companies table."""
    conn = sqlite3.connect(DB_PATH)
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        UPDATE companies
        SET ats = ?,
            resolved_token = ?,
            endpoint = ?,
            careers_url = ?,
            resolve_status = 'resolved',
            updated_at = ?
        WHERE company_name = ?
    """, (
        ats,
        result.get("resolved_token"),
        result.get("endpoint"),
        result.get("careers_url", ""),
        ts,
        company_name,
    ))
    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Re-discover endpoints for identified companies")
    parser.add_argument("--ats", help="Filter by ATS type (icims, workday, pageup, etc.)")
    parser.add_argument("--limit", type=int, help="Process only N companies")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between searches (default 1.5)")
    args = parser.parse_args()

    if not SERPER_API_KEY:
        print("ERROR: Set SERPER_API_KEY first:")
        print("  export SERPER_API_KEY='your-key-here'")
        sys.exit(1)

    companies = get_identified_companies(ats_filter=args.ats, limit=args.limit)
    print(f"Re-discovering endpoints for {len(companies)} identified companies")
    if args.ats:
        print(f"  Filtered to ATS: {args.ats}")

    # Show ATS breakdown
    ats_counts = {}
    for c in companies:
        ats_counts[c["ats"]] = ats_counts.get(c["ats"], 0) + 1
    for ats, count in sorted(ats_counts.items(), key=lambda x: -x[1]):
        print(f"  {ats:<18} {count:>3} companies")
    print()

    stats = {"resolved": 0, "not_found": 0, "errors": 0, "api_calls": 0}

    for i, row in enumerate(companies):
        cname = row["company_name"]
        brand = row["brand"] or cname
        ats = row["ats"]

        print(f"[{i+1}/{len(companies)}] {brand} ({ats})")

        try:
            result = discover_one(brand, ats)
            stats["api_calls"] += len(ATS_SEARCH_TEMPLATES.get(ats, [""]))
        except Exception as e:
            print(f"    ERROR: {e}")
            stats["errors"] += 1
            time.sleep(args.delay)
            continue

        if not result:
            print(f"    → not found")
            stats["not_found"] += 1
            time.sleep(args.delay)
            continue

        endpoint = result.get("endpoint", "")
        token = result.get("resolved_token", "")
        print(f"    → FOUND: {ats} | {token} | {endpoint[:75]}")

        if not args.dry_run:
            save_resolution(cname, ats, result)

        stats["resolved"] += 1
        time.sleep(args.delay)

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"RE-DISCOVERY COMPLETE")
    print(f"{'='*60}")
    print(f"  Resolved:   {stats['resolved']}")
    print(f"  Not found:  {stats['not_found']}")
    print(f"  Errors:     {stats['errors']}")
    print(f"  API calls:  ~{stats['api_calls']} (Serper queries used)")

    if stats["resolved"] > 0 and not args.dry_run:
        print(f"\n  Restart collector to begin collecting:")
        print(f"    sudo systemctl restart jobhunter-collector")

    remaining = stats["not_found"] + stats["errors"]
    if remaining > 0:
        print(f"\n  {remaining} companies still need endpoints.")
        print(f"  Options: manual lookup, or try different search queries.")


if __name__ == "__main__":
    main()
