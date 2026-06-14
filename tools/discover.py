#!/usr/bin/env python3
"""
discover.py — Discover ATS platforms for unresolved H-1B sponsor companies.

Uses Serper.dev (Google Search API) to find each company's careers page,
fingerprints which ATS hosts it, and extracts the feed URL/parameters.
Results are saved to jobhunter.db so the existing collector picks them up.

Usage:
    export SERPER_API_KEY="your-key-here"
    python3 discover.py                  # discover all unresolved
    python3 discover.py --limit 50       # test with first 50
    python3 discover.py --dry-run        # search + fingerprint but don't write DB
    python3 discover.py --resume         # skip companies already attempted
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

DB_PATH = "jobhunter.db"
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
SERPER_URL = "https://google.serper.dev/search"

# ── ATS URL fingerprints ─────────────────────────────────────────────
# Each pattern: (regex applied to URL, group indices for params)
# We scan all search-result URLs for these signatures.

ATS_FINGERPRINTS = {
    "workday": [
        # https://mit.wd5.myworkdayjobs.com/MIT → tenant=mit, dc=wd5, site=MIT
        re.compile(r"https?://(\w[\w-]*)\.(\w+)\.myworkdayjobs\.com(?:/en-US)?(?:/(\w[\w-]*))?"),
    ],
    "greenhouse": [
        re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/([\w-]+)"),
    ],
    "lever": [
        re.compile(r"https?://jobs\.lever\.co/([\w-]+)"),
    ],
    "ashby": [
        re.compile(r"https?://jobs\.ashbyhq\.com/([\w-]+)"),
    ],
    "smartrecruiters": [
        re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)"),
    ],
    "workable": [
        re.compile(r"https?://apply\.workable\.com/([\w-]+)"),
    ],
    "icims": [
        re.compile(r"https?://([\w-]+)\.icims\.com"),
        re.compile(r"https?://careers[.-]([\w-]+)\.icims\.com"),
    ],
    "taleo": [
        re.compile(r"https?://([\w-]+)\.taleo\.net"),
    ],
    "successfactors": [
        re.compile(r"https?://([\w-]+)\.successfactors\.\w+"),
    ],
    "pageup": [
        re.compile(r"https?://([\w-]+)\.pageuppeople\.com"),
    ],
}

# Known ATS markers in HTML (fallback when URL doesn't match)
HTML_MARKERS = {
    "workday":        ["myworkdayjobs.com", "workday.com/", "wday/cxs/"],
    "greenhouse":     ["boards.greenhouse.io", "grnh.se"],
    "lever":          ["jobs.lever.co"],
    "ashby":          ["jobs.ashbyhq.com"],
    "smartrecruiters": ["jobs.smartrecruiters.com"],
    "workable":       ["apply.workable.com"],
    "icims":          [".icims.com"],
    "taleo":          [".taleo.net", "oracle.com/careers"],
    "successfactors": [".successfactors."],
    "pageup":         [".pageuppeople.com"],
}

# ── Board-name validation (reused from resolver_patch) ───────────────

VERIFY_STOP = {"the", "of", "and", "inc", "llc", "corp", "co", "ltd",
               "group", "services", "solutions", "international",
               "global", "na", "us", "a", "an", "for"}

def _tokenize(name):
    return {w for w in re.sub(r"[^a-z0-9\s]", " ", name.lower()).split()
            if w and w not in VERIFY_STOP}

def _name_sim(a, b):
    t1, t2 = _tokenize(a), _tokenize(b)
    if not t1 or not t2:
        return 0.0
    return len(t1 & t2) / len(t1 | t2)

def verify_greenhouse(slug, target):
    """Check Greenhouse board name matches target company."""
    try:
        req = Request(f"https://boards-api.greenhouse.io/v1/boards/{slug}",
                      headers={"User-Agent": "JobHunter-Discover/1.0"})
        with urlopen(req, timeout=10) as r:
            board = json.loads(r.read()).get("name", "")
            sim = max(_name_sim(target, board), _name_sim(target.split()[0], board))
            return sim >= 0.25, board
    except Exception:
        return None, None  # Can't verify → skip

def verify_lever(slug, target):
    """Check Lever board has postings."""
    try:
        req = Request(f"https://api.lever.co/v0/postings/{slug}?limit=1",
                      headers={"User-Agent": "JobHunter-Discover/1.0"})
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            return bool(data), slug
    except Exception:
        return None, None

def verify_ashby(slug, target):
    """Check Ashby board name matches target company."""
    try:
        req = Request(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                      headers={"User-Agent": "JobHunter-Discover/1.0"})
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            board = data.get("organizationName") or data.get("title") or ""
            sim = _name_sim(target, board)
            return sim >= 0.20, board
    except Exception:
        return None, None


# ── Serper search ─────────────────────────────────────────────────────

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


# ── Fingerprint URLs ──────────────────────────────────────────────────

def fingerprint_url(url):
    """
    Check a URL against known ATS patterns.
    Returns (ats_type, match_object) or (None, None).
    """
    for ats_type, patterns in ATS_FINGERPRINTS.items():
        for pat in patterns:
            m = pat.search(url)
            if m:
                return ats_type, m
    return None, None


def fingerprint_html(html):
    """Fallback: scan page HTML for ATS markers."""
    html_lower = html.lower()
    for ats_type, markers in HTML_MARKERS.items():
        for marker in markers:
            if marker.lower() in html_lower:
                return ats_type
    return None


# ── Extract feed parameters ───────────────────────────────────────────

def extract_params(ats_type, match, url):
    """
    Given an ATS type and regex match, extract the parameters needed
    to build the feed URL.
    """
    if ats_type == "workday":
        tenant = match.group(1)
        dc = match.group(2)
        site = match.group(3) or tenant
        endpoint = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        return {
            "resolved_token": f"{tenant}/{dc}/{site}",
            "endpoint": endpoint,
        }

    elif ats_type == "greenhouse":
        slug = match.group(1)
        return {
            "resolved_token": slug,
            "endpoint": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        }

    elif ats_type == "lever":
        slug = match.group(1)
        return {
            "resolved_token": slug,
            "endpoint": f"https://api.lever.co/v0/postings/{slug}",
        }

    elif ats_type == "ashby":
        slug = match.group(1)
        return {
            "resolved_token": slug,
            "endpoint": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        }

    elif ats_type == "smartrecruiters":
        slug = match.group(1)
        return {
            "resolved_token": slug,
            "endpoint": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        }

    elif ats_type == "workable":
        slug = match.group(1)
        return {
            "resolved_token": slug,
            "endpoint": f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
        }

    elif ats_type in ("icims", "taleo", "successfactors", "pageup"):
        token = match.group(1)
        return {
            "resolved_token": token,
            "endpoint": url,  # Store the discovered URL; adapter will handle specifics
        }

    return None


# ── Fetch page (for HTML fallback) ────────────────────────────────────

def fetch_page(url, max_bytes=200_000):
    """Fetch a page for HTML fingerprinting. Returns HTML string."""
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; JobHunter/1.0)",
        })
        with urlopen(req, timeout=12) as r:
            return r.read(max_bytes).decode("utf-8", errors="replace")
    except Exception:
        return ""


# ── Workday URL extractor from HTML ───────────────────────────────────

def extract_workday_from_html(html):
    """If a careers page embeds/redirects to Workday, extract the URL."""
    patterns = [
        re.compile(r'https?://(\w[\w-]*)\.(\w+)\.myworkdayjobs\.com(?:/en-US)?(?:/(\w[\w-]*))?'),
    ]
    for pat in patterns:
        m = pat.search(html)
        if m:
            return m
    return None


# ── Main discovery loop ───────────────────────────────────────────────

def discover_company(company_name, brand):
    """
    Discover the ATS platform for one company.
    Returns dict with ats, resolved_token, endpoint, careers_url or None.
    """
    # Search Google for the careers page
    query = f"{brand} careers jobs"
    results = search_google(query, num=8)

    if not results:
        return None

    # Pass 1: scan result URLs for ATS fingerprints
    for r in results:
        url = r.get("link", "")
        ats_type, match = fingerprint_url(url)
        if ats_type:
            params = extract_params(ats_type, match, url)
            if params:
                params["ats"] = ats_type
                params["careers_url"] = url
                return params

    # Pass 2: fetch the top 3 results and scan HTML
    for r in results[:3]:
        url = r.get("link", "")
        if not url:
            continue

        html = fetch_page(url)
        if not html:
            continue

        # Check for Workday embed/redirect first (very common)
        wd_match = extract_workday_from_html(html)
        if wd_match:
            params = extract_params("workday", wd_match, wd_match.group(0))
            if params:
                params["ats"] = "workday"
                params["careers_url"] = url
                return params

        # Generic HTML fingerprint
        ats_type = fingerprint_html(html)
        if ats_type:
            # Found the ATS type but need to extract the actual URL from HTML
            for fp_ats, patterns in ATS_FINGERPRINTS.items():
                if fp_ats != ats_type:
                    continue
                for pat in patterns:
                    m = pat.search(html)
                    if m:
                        params = extract_params(ats_type, m, m.group(0))
                        if params:
                            params["ats"] = ats_type
                            params["careers_url"] = url
                            return params

            # Found ATS type but couldn't extract params — still useful
            return {
                "ats": ats_type,
                "resolved_token": None,
                "endpoint": None,
                "careers_url": url,
            }

    return None


def validate_discovery(ats_type, slug, brand):
    """
    For ATS types with public APIs, validate the board belongs to this company.
    Returns (is_valid, board_name) or (None, None) if can't verify.
    """
    if not slug:
        return None, None

    if ats_type == "greenhouse":
        return verify_greenhouse(slug, brand)
    elif ats_type == "lever":
        return verify_lever(slug, brand)
    elif ats_type == "ashby":
        return verify_ashby(slug, brand)

    # Workday, iCIMS, Taleo etc — can't easily verify, trust the search
    return True, None


# ── Database operations ───────────────────────────────────────────────

def get_unresolved(limit=None, resume=False):
    """Get companies that need discovery."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    sql = """
        SELECT company_name, brand, cap_exempt, sponsor, priority_tier
        FROM companies
        WHERE (resolved_token IS NULL OR resolved_token = '')
    """
    if resume:
        sql += " AND (resolve_status IS NULL OR resolve_status = 'pending')"
    sql += " ORDER BY priority_tier ASC, company_name ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = conn.execute(sql).fetchall()
    conn.close()
    return rows


def save_discovery(company_name, result):
    """Save discovery results to the companies table."""
    conn = sqlite3.connect(DB_PATH)
    ts = datetime.now(timezone.utc).isoformat()

    conn.execute("""
        UPDATE companies
        SET ats = ?,
            resolved_token = ?,
            endpoint = ?,
            careers_url = ?,
            resolve_status = ?,
            updated_at = ?
        WHERE company_name = ?
    """, (
        result.get("ats"),
        result.get("resolved_token"),
        result.get("endpoint"),
        result.get("careers_url"),
        "resolved" if result.get("endpoint") else "identified",
        ts,
        company_name,
    ))
    conn.commit()
    conn.close()


def mark_not_found(company_name):
    """Mark a company as searched but not found."""
    conn = sqlite3.connect(DB_PATH)
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        UPDATE companies
        SET resolve_status = 'not_found', updated_at = ?
        WHERE company_name = ?
    """, (ts, company_name))
    conn.commit()
    conn.close()


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Discover ATS platforms for companies")
    parser.add_argument("--limit", type=int, help="Process only N companies")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--resume", action="store_true", help="Skip already-attempted companies")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between searches (default 1.5)")
    args = parser.parse_args()

    if not SERPER_API_KEY:
        print("ERROR: Set SERPER_API_KEY first:")
        print("  export SERPER_API_KEY='your-key-here'")
        sys.exit(1)

    companies = get_unresolved(limit=args.limit, resume=args.resume)
    print(f"Discovering ATS for {len(companies)} unresolved companies...\n")

    stats = {"resolved": 0, "identified": 0, "not_found": 0, "skipped": 0, "errors": 0}
    ats_counts = {}

    for i, row in enumerate(companies):
        cname = row["company_name"]
        brand = row["brand"] or cname
        tier = row["priority_tier"] or "?"

        print(f"[{i+1}/{len(companies)}] {brand} (tier {tier})")

        try:
            result = discover_company(cname, brand)
        except Exception as e:
            print(f"    ERROR: {e}")
            stats["errors"] += 1
            time.sleep(args.delay)
            continue

        if not result:
            print(f"    → not found")
            if not args.dry_run:
                mark_not_found(cname)
            stats["not_found"] += 1
            time.sleep(args.delay)
            continue

        ats_type = result.get("ats", "unknown")
        slug = result.get("resolved_token")
        endpoint = result.get("endpoint")
        careers_url = result.get("careers_url", "")

        # Validate Greenhouse/Lever/Ashby discoveries
        if ats_type in ("greenhouse", "lever", "ashby") and slug:
            valid, board_name = validate_discovery(ats_type, slug, brand)
            if valid is False:
                print(f"    → {ats_type}/{slug} rejected (board='{board_name}', target='{brand}')")
                # Still save the careers_url and ATS type for manual review
                result["resolved_token"] = None
                result["endpoint"] = None
                if not args.dry_run:
                    save_discovery(cname, result)
                stats["identified"] += 1
                ats_counts[ats_type] = ats_counts.get(ats_type, 0) + 1
                time.sleep(args.delay)
                continue
            elif valid is True and board_name:
                print(f"    → {ats_type}/{slug} verified (board='{board_name}')")

        if endpoint:
            print(f"    → RESOLVED: {ats_type} | {slug or 'n/a'} | {endpoint[:80]}")
            if not args.dry_run:
                save_discovery(cname, result)
            stats["resolved"] += 1
        else:
            print(f"    → identified as {ats_type} (no endpoint yet) | {careers_url[:60]}")
            if not args.dry_run:
                save_discovery(cname, result)
            stats["identified"] += 1

        ats_counts[ats_type] = ats_counts.get(ats_type, 0) + 1
        time.sleep(args.delay)

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"DISCOVERY COMPLETE")
    print(f"{'='*70}")
    print(f"  Resolved (ready to collect): {stats['resolved']}")
    print(f"  Identified (ATS known):      {stats['identified']}")
    print(f"  Not found:                   {stats['not_found']}")
    print(f"  Errors:                      {stats['errors']}")
    print(f"\nATS breakdown:")
    for ats, count in sorted(ats_counts.items(), key=lambda x: -x[1]):
        adapter = "✓ have adapter" if ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "workable") else "⊘ need adapter"
        print(f"  {ats:<20} {count:>4}  ({adapter})")

    # Show how many are now actionable
    if not args.dry_run:
        conn = sqlite3.connect(DB_PATH)
        ready = conn.execute("""
            SELECT COUNT(*) FROM companies
            WHERE endpoint IS NOT NULL AND ats IN ('greenhouse','lever','ashby','smartrecruiters','workable')
        """).fetchone()[0]
        workday = conn.execute("""
            SELECT COUNT(*) FROM companies WHERE ats = 'workday' AND endpoint IS NOT NULL
        """).fetchone()[0]
        conn.close()
        print(f"\nActionable now (existing adapters): {ready}")
        print(f"Actionable after Workday adapter:   {ready + workday}")


if __name__ == "__main__":
    main()
