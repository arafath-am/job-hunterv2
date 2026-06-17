#!/usr/bin/env python3
"""
Auto-resolver for Job Hunter v2.

Takes all not_found + identified companies, searches for their careers page
via Serper.dev, fingerprints the ATS from URL patterns, extracts endpoint
parameters, validates with API probes, and writes results to the DB.

Usage:
    python3 tools/auto_resolve.py                   # Run all unresolved
    python3 tools/auto_resolve.py --status not_found # Only not_found
    python3 tools/auto_resolve.py --status identified # Only identified
    python3 tools/auto_resolve.py --limit 10         # First 10 only (test)
    python3 tools/auto_resolve.py --dry-run           # Don't write to DB
"""

import argparse
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "jobhunter.db")
SERPER_KEY = os.environ.get("SERPER_API_KEY", "")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
REQUEST_TIMEOUT = 12
SERPER_DELAY = 0.5   # seconds between Serper calls
PROBE_DELAY = 0.3    # seconds between validation probes

# ── ATS URL patterns ───────────────────────────────────────────────────
# Each pattern: (regex on URL, ats_type, token_extractor_function)

def _extract_greenhouse(url):
    """boards.greenhouse.io/{slug} or job-boards.greenhouse.io/{slug}"""
    m = re.search(r'(?:boards|job-boards)\.greenhouse\.io/([a-zA-Z0-9_-]+)', url)
    return m.group(1) if m else None

def _extract_lever(url):
    m = re.search(r'jobs\.lever\.co/([a-zA-Z0-9_-]+)', url)
    return m.group(1) if m else None

def _extract_ashby(url):
    m = re.search(r'jobs\.ashbyhq\.com/([a-zA-Z0-9._-]+)', url)
    return m.group(1) if m else None

def _extract_smartrecruiters(url):
    m = re.search(r'jobs\.smartrecruiters\.com/([a-zA-Z0-9._-]+)', url)
    if m:
        slug = m.group(1)
        # Skip if it's a specific job posting URL segment
        if slug.lower() not in ('ni', 'oneclick', 'posting'):
            return slug
    return None

def _extract_workable(url):
    m = re.search(r'([a-zA-Z0-9_-]+)\.workable\.com', url)
    if m:
        sub = m.group(1)
        if sub.lower() not in ('www', 'apply', 'help', 'support', 'blog'):
            return sub
    return None

def _extract_workday(url):
    """
    Extracts tenant, datacenter, site from Workday URLs.
    Pattern: {tenant}.{dc}.myworkdayjobs.com/.../{site}
    Returns dict with tenant, dc, site or None.
    """
    m = re.search(
        r'([a-zA-Z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com(?:/[^/]*/)?([a-zA-Z0-9_-]+)',
        url
    )
    if m:
        return {"tenant": m.group(1), "dc": m.group(2), "site": m.group(3)}
    # Also match the /en-US/ or /en/ path variant
    m2 = re.search(
        r'([a-zA-Z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:en-US/|en/)?([a-zA-Z0-9_-]+)',
        url
    )
    if m2:
        return {"tenant": m2.group(1), "dc": m2.group(2), "site": m2.group(3)}
    return None

def _extract_icims(url):
    """*.icims.com career portal"""
    m = re.search(r'(https?://[a-zA-Z0-9_-]+\.icims\.com)', url)
    return m.group(1) if m else None

def _extract_taleo(url):
    """taleo.net or oraclecloud.com/hcmUI"""
    if 'taleo.net' in url or 'oraclecloud.com/hcmUI' in url:
        # Return the base URL up to the domain
        parsed = urllib.parse.urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    return None

def _extract_pageup(url):
    m = re.search(r'(https?://[a-zA-Z0-9._-]+\.pageuppeople\.com)', url)
    return m.group(1) if m else None

def _extract_successfactors(url):
    if 'successfactors' in url.lower() or 'sap.com/career' in url.lower():
        parsed = urllib.parse.urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


# Order matters — check more specific patterns first
ATS_PATTERNS = [
    (r'(?:boards|job-boards)\.greenhouse\.io/', 'greenhouse', _extract_greenhouse),
    (r'jobs\.lever\.co/', 'lever', _extract_lever),
    (r'jobs\.ashbyhq\.com/', 'ashby', _extract_ashby),
    (r'jobs\.smartrecruiters\.com/', 'smartrecruiters', _extract_smartrecruiters),
    (r'[a-zA-Z0-9_-]+\.workable\.com', 'workable', _extract_workable),
    (r'\.wd\d+\.myworkdayjobs\.com', 'workday', _extract_workday),
    (r'[a-zA-Z0-9_-]+\.icims\.com', 'icims', _extract_icims),
    (r'taleo\.net|oraclecloud\.com/hcmUI', 'taleo', _extract_taleo),
    (r'pageuppeople\.com', 'pageup', _extract_pageup),
    (r'successfactors|sap\.com/career', 'successfactors', _extract_successfactors),
]


# ── HTTP helpers ────────────────────────────────────────────────────────

def _get(url, timeout=REQUEST_TIMEOUT):
    """Simple GET with UA header. Returns (status, body_str) or (0, error)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, str(e)

def _post_json(url, body, timeout=REQUEST_TIMEOUT):
    """POST JSON. Returns (status, parsed_json_or_None)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": UA, "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


# ── Serper.dev search ───────────────────────────────────────────────────

def serper_search(query, num_results=5):
    """Search via Serper.dev. Returns list of {title, link, snippet}."""
    api_key = os.environ.get("SERPER_API_KEY", "") or SERPER_KEY
    if not api_key:
        print("  ⚠ SERPER_API_KEY not set")
        return []
    body = json.dumps({"q": query, "num": num_results}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=body,
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("organic", [])
    except Exception as e:
        print(f"  ⚠ Serper error: {e}")
        return []


# ── ATS fingerprinting from search results ──────────────────────────────

def fingerprint_urls(results):
    """
    Given Serper search results, check each URL against ATS patterns.
    Returns list of (ats_type, token_or_info, url) matches.
    """
    matches = []
    seen_ats = set()
    for r in results:
        url = r.get("link", "")
        for pattern, ats_type, extractor in ATS_PATTERNS:
            if re.search(pattern, url, re.IGNORECASE):
                token = extractor(url)
                if token and ats_type not in seen_ats:
                    matches.append((ats_type, token, url))
                    seen_ats.add(ats_type)
                break  # first pattern match wins per URL
    return matches


# ── Jibe detection on custom career domains ─────────────────────────────

def probe_jibe(career_url):
    """
    Given a career page URL, try /api/jobs to detect Jibe/iCIMS Talent Cloud.
    Returns (job_count, api_base_url) or (0, None).
    """
    parsed = urllib.parse.urlparse(career_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    api_url = f"{base}/api/jobs?limit=1"
    status, body = _get(api_url)
    if status == 200 and body:
        try:
            data = json.loads(body)
            count = data.get("count", 0)
            if count > 0:
                return count, base
        except (json.JSONDecodeError, KeyError):
            pass
    return 0, None


# ── Validation probes ───────────────────────────────────────────────────

def validate_greenhouse(token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false"
    status, body = _get(url)
    if status == 200:
        try:
            data = json.loads(body)
            count = len(data.get("jobs", []))
            return count, f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        except:
            pass
    return 0, None

def validate_lever(token):
    url = f"https://api.lever.co/v0/postings/{token}?limit=1&mode=json"
    status, body = _get(url)
    if status == 200:
        try:
            data = json.loads(body)
            count = len(data) if isinstance(data, list) else 0
            return count, f"https://api.lever.co/v0/postings/{token}"
        except:
            pass
    return 0, None

def validate_ashby(token):
    url = "https://jobs.ashbyhq.com/api/non-user-graphql"
    body = {
        "operationName": "ApiJobBoardWithTeams",
        "variables": {"organizationHostedJobsPageName": token},
        "query": "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) { jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) { teams { id name } jobPostings { id title } } }"
    }
    status, data = _post_json(url, body)
    if status == 200 and data:
        try:
            postings = data["data"]["jobBoard"]["jobPostings"]
            return len(postings), f"ashby:{token}"
        except:
            pass
    return 0, None

def validate_smartrecruiters(token):
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=1"
    status, body = _get(url)
    if status == 200:
        try:
            data = json.loads(body)
            count = data.get("totalFound", 0)
            return count, f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
        except:
            pass
    return 0, None

def validate_workable(token):
    url = f"https://apply.workable.com/api/v2/accounts/{token}/jobs"
    status, data = _post_json(url, {"query": "", "location": [], "department": [], "worktype": [], "remote": []})
    if status == 200 and data:
        try:
            count = data.get("total", 0)
            return count, f"workable:{token}"
        except:
            pass
    return 0, None

def validate_workday(info):
    """info is dict with tenant, dc, site."""
    tenant, dc, site = info["tenant"], info["dc"], info["site"]
    url = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    body = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
    status, data = _post_json(url, body)
    if status == 200 and data:
        try:
            count = data.get("total", 0)
            endpoint = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
            return count, endpoint
        except:
            pass
    return 0, None

def validate_jibe(base_url):
    url = f"{base_url}/api/jobs?limit=1"
    status, body = _get(url)
    if status == 200:
        try:
            data = json.loads(body)
            return data.get("count", 0), f"{base_url}/api/jobs"
        except:
            pass
    return 0, None

VALIDATORS = {
    "greenhouse": lambda token: validate_greenhouse(token),
    "lever": lambda token: validate_lever(token),
    "ashby": lambda token: validate_ashby(token),
    "smartrecruiters": lambda token: validate_smartrecruiters(token),
    "workable": lambda token: validate_workable(token),
    "workday": lambda info: validate_workday(info),
    # icims, taleo, pageup — no easy API probe, mark as identified
}


# ── Career page Jibe probing ───────────────────────────────────────────

def try_jibe_from_search(results):
    """
    For results that link to custom career domains (not a known ATS URL),
    try probing for Jibe /api/jobs endpoint.
    """
    for r in results:
        url = r.get("link", "")
        # Skip known ATS domains
        skip_domains = [
            'greenhouse.io', 'lever.co', 'ashbyhq.com', 'smartrecruiters.com',
            'workable.com', 'myworkdayjobs.com', 'icims.com', 'taleo.net',
            'oraclecloud.com', 'pageuppeople.com', 'successfactors',
            'linkedin.com', 'indeed.com', 'glassdoor.com', 'ziprecruiter.com',
            'google.com', 'facebook.com', 'twitter.com', 'wikipedia.org',
            'youtube.com', 'yelp.com', 'bbb.org'
        ]
        if any(d in url.lower() for d in skip_domains):
            continue

        # Check if URL looks like a careers page
        if any(kw in url.lower() for kw in ['career', 'jobs', 'hiring', 'employment', 'work-with-us', 'join']):
            count, base = probe_jibe(url)
            if count > 0:
                return count, base, url
    return 0, None, None


# ── Name cleaning ───────────────────────────────────────────────────────

def clean_company_name(name):
    """
    Best-effort cleanup for damaged/legal company names before searching.
    E.g. "Univ of Wi Systemstout" → "University of Wisconsin Stout"
    """
    # Remove common legal suffixes
    cleaned = re.sub(
        r'\s*,?\s*(Inc\.?|LLC|LP|L\.?P\.?|Corp\.?|Corporation|Ltd\.?|Limited|Co\.?|'
        r'P\.?C\.?|P\.?A\.?|P\.?L\.?L\.?C\.?|LLP|Associates|Group|Holdings?|'
        r'International|Intl|Services|Solutions|Technologies|Technology|Tech|'
        r'Systems|Enterprises?|Medical Center|Medical Group|Health System)\s*$',
        '', name, flags=re.IGNORECASE
    ).strip()

    # Common abbreviation expansions for better search
    replacements = {
        r'\bUniv\b': 'University',
        r'\bHosp\b': 'Hospital',
        r'\bMed\b': 'Medical',
        r'\bCtr\b': 'Center',
        r'\bNatl\b': 'National',
        r'\bSvcs?\b': 'Services',
        r'\bMgmt\b': 'Management',
        r'\bAssoc\b': 'Associates',
    }
    for pat, repl in replacements.items():
        cleaned = re.sub(pat, repl, cleaned, flags=re.IGNORECASE)

    return cleaned if cleaned else name


# ── Main resolve logic ──────────────────────────────────────────────────

def resolve_company(company_name, brand, current_ats=None, current_status=None):
    """
    Attempt to resolve a single company.
    Returns dict with results or None if nothing found.
    """
    search_name = brand if brand and brand != company_name else clean_company_name(company_name)
    query = f'"{search_name}" careers jobs'

    print(f"  🔍 Searching: {query}")
    results = serper_search(query, num_results=8)
    time.sleep(SERPER_DELAY)

    if not results:
        print(f"  ❌ No search results")
        return None

    # Phase 1: Check URLs for known ATS patterns
    matches = fingerprint_urls(results)
    if matches:
        ats_type, token, match_url = matches[0]  # take first/best match
        print(f"  🎯 URL match: {ats_type} → {token}")

        # Validate if we have a validator
        if ats_type in VALIDATORS:
            print(f"  🔬 Validating {ats_type} endpoint...")
            time.sleep(PROBE_DELAY)
            job_count, endpoint = VALIDATORS[ats_type](token)
            if job_count > 0:
                print(f"  ✅ Validated: {job_count} jobs at {endpoint}")
                return {
                    "ats": ats_type,
                    "resolved_token": token if isinstance(token, str) else json.dumps(token),
                    "endpoint": endpoint,
                    "careers_url": match_url,
                    "job_count": job_count,
                    "resolve_status": "resolved",
                }
            else:
                print(f"  ⚠ Validation failed (0 jobs), marking identified")
                return {
                    "ats": ats_type,
                    "resolved_token": token if isinstance(token, str) else json.dumps(token),
                    "endpoint": endpoint or "",
                    "careers_url": match_url,
                    "job_count": 0,
                    "resolve_status": "identified",
                }
        else:
            # No validator (icims, taleo, pageup, successfactors) → identified
            print(f"  📋 No API validator for {ats_type}, marking identified")
            endpoint_str = token if isinstance(token, str) else json.dumps(token)
            return {
                "ats": ats_type,
                "resolved_token": endpoint_str,
                "endpoint": endpoint_str,
                "careers_url": match_url,
                "job_count": 0,
                "resolve_status": "identified",
            }

    # Phase 2: No ATS URL match — try Jibe probe on career-looking pages
    print(f"  🌐 No ATS URL match, probing for Jibe API...")
    count, jibe_base, career_url = try_jibe_from_search(results)
    if count > 0:
        print(f"  ✅ Jibe detected: {count} jobs at {jibe_base}")
        return {
            "ats": "jibe",
            "resolved_token": jibe_base,
            "endpoint": f"{jibe_base}/api/jobs",
            "careers_url": career_url,
            "job_count": count,
            "resolve_status": "resolved",
        }

    # Phase 3: Store careers URL even if we can't fingerprint ATS
    # Find first non-aggregator career-looking URL
    for r in results:
        url = r.get("link", "")
        skip = ['linkedin.com', 'indeed.com', 'glassdoor.com', 'ziprecruiter.com',
                'google.com', 'facebook.com', 'twitter.com', 'wikipedia.org',
                'youtube.com', 'yelp.com', 'bbb.org', 'crunchbase.com']
        if not any(d in url.lower() for d in skip):
            if any(kw in url.lower() for kw in ['career', 'jobs', 'hiring', 'employment', 'join', 'work']):
                print(f"  📎 Found careers page but unknown ATS: {url}")
                return {
                    "ats": None,
                    "resolved_token": None,
                    "endpoint": None,
                    "careers_url": url,
                    "job_count": 0,
                    "resolve_status": "identified",
                }

    print(f"  ❌ No careers page found")
    return None


# ── DB operations ───────────────────────────────────────────────────────

def get_unresolved_companies(db_path, status_filter=None, limit=None):
    """Get companies that need resolution."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if status_filter:
        q = "SELECT company_name, brand, ats, resolve_status FROM companies WHERE resolve_status = ? ORDER BY company_name"
        rows = conn.execute(q, (status_filter,)).fetchall()
    else:
        q = "SELECT company_name, brand, ats, resolve_status FROM companies WHERE resolve_status IN ('not_found', 'identified') ORDER BY resolve_status, company_name"
        rows = conn.execute(q).fetchall()
    conn.close()
    if limit:
        rows = rows[:limit]
    return [dict(r) for r in rows]


def update_company(db_path, company_name, result, dry_run=False):
    """Write resolution result to DB."""
    if dry_run:
        print(f"  [DRY RUN] Would update {company_name}: {result['ats']} → {result['resolve_status']}")
        return
    conn = sqlite3.connect(db_path)
    fields = []
    values = []
    if result.get("ats"):
        fields.append("ats = ?")
        values.append(result["ats"])
    if result.get("resolved_token"):
        fields.append("resolved_token = ?")
        values.append(result["resolved_token"])
    if result.get("endpoint"):
        fields.append("endpoint = ?")
        values.append(result["endpoint"])
    if result.get("careers_url"):
        fields.append("careers_url = ?")
        values.append(result["careers_url"])
    if result.get("job_count") is not None:
        fields.append("job_count = ?")
        values.append(result["job_count"])
    fields.append("resolve_status = ?")
    values.append(result["resolve_status"])
    fields.append("updated_at = ?")
    values.append(datetime.now(timezone.utc).isoformat())

    values.append(company_name)
    sql = f"UPDATE companies SET {', '.join(fields)} WHERE company_name = ?"
    conn.execute(sql, values)
    conn.commit()
    conn.close()


# ── Report ──────────────────────────────────────────────────────────────

def print_report(stats):
    """Print summary report."""
    print("\n" + "=" * 70)
    print("AUTO-RESOLVE REPORT")
    print("=" * 70)
    print(f"Total processed:    {stats['total']}")
    print(f"Newly resolved:     {stats['resolved']}  ✅")
    print(f"Newly identified:   {stats['identified']}  📋")
    print(f"Still not found:    {stats['still_not_found']}  ❌")
    print(f"Errors:             {stats['errors']}  ⚠")
    print(f"Skipped (already):  {stats['skipped']}  ⏭")
    print()

    if stats['resolved_details']:
        print("── Newly Resolved ──")
        for d in stats['resolved_details']:
            print(f"  {d['company']:45} {d['ats']:18} {d['job_count']:>6} jobs")
        print()

    if stats['identified_details']:
        print("── Newly Identified ──")
        for d in stats['identified_details']:
            print(f"  {d['company']:45} {d['ats'] or 'unknown':18} {d.get('careers_url', '')[:50]}")
        print()

    # ATS breakdown
    ats_counts = {}
    for d in stats['resolved_details'] + stats['identified_details']:
        ats = d.get('ats') or 'unknown'
        ats_counts[ats] = ats_counts.get(ats, 0) + 1
    if ats_counts:
        print("── ATS Breakdown ──")
        for ats, count in sorted(ats_counts.items(), key=lambda x: -x[1]):
            print(f"  {ats:20} {count:>4}")
    print("=" * 70)


# ── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Auto-resolve unresolved companies")
    parser.add_argument("--status", choices=["not_found", "identified"], help="Filter by status")
    parser.add_argument("--limit", type=int, help="Max companies to process")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--db", default=DB_PATH, help="Path to jobhunter.db")
    args = parser.parse_args()

    serper_key = SERPER_KEY
    if not serper_key:
        # Try loading from .env file
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("SERPER_API_KEY="):
                        serper_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        os.environ["SERPER_API_KEY"] = serper_key
                        break

    if not serper_key:
        print("❌ SERPER_API_KEY not found in environment or .env file")
        return

    companies = get_unresolved_companies(args.db, args.status, args.limit)
    total = len(companies)
    print(f"\n🚀 Auto-resolve starting: {total} companies to process")
    if args.dry_run:
        print("🔒 DRY RUN — no DB writes")
    print()

    stats = {
        "total": total,
        "resolved": 0,
        "identified": 0,
        "still_not_found": 0,
        "errors": 0,
        "skipped": 0,
        "resolved_details": [],
        "identified_details": [],
    }

    for i, company in enumerate(companies, 1):
        name = company["company_name"]
        brand = company.get("brand")
        current_ats = company.get("ats")
        current_status = company.get("resolve_status")

        print(f"\n[{i}/{total}] {brand or name} (status: {current_status}, ats: {current_ats or 'none'})")

        # Skip if already resolved with a working ATS and it's identified (may have endpoint)
        if current_status == "identified" and current_ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "workday", "jibe"):
            # Re-validate the existing endpoint
            pass  # let it try to re-resolve

        try:
            result = resolve_company(name, brand, current_ats, current_status)

            if result is None:
                stats["still_not_found"] += 1
                continue

            if result["resolve_status"] == "resolved":
                stats["resolved"] += 1
                stats["resolved_details"].append({
                    "company": brand or name,
                    "ats": result["ats"],
                    "job_count": result.get("job_count", 0),
                })
            elif result["resolve_status"] == "identified":
                stats["identified"] += 1
                stats["identified_details"].append({
                    "company": brand or name,
                    "ats": result.get("ats"),
                    "careers_url": result.get("careers_url", ""),
                })

            update_company(args.db, name, result, dry_run=args.dry_run)

        except KeyboardInterrupt:
            print("\n\n⛔ Interrupted by user")
            break
        except Exception as e:
            print(f"  ⚠ Error: {e}")
            stats["errors"] += 1

    print_report(stats)

    # Save report to file
    report_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auto_resolve_report.json")
    if not args.dry_run:
        with open(report_path, "w") as f:
            json.dump({
                "run_at": datetime.now(timezone.utc).isoformat(),
                "stats": {k: v for k, v in stats.items() if not k.endswith("_details")},
                "resolved": stats["resolved_details"],
                "identified": stats["identified_details"],
            }, f, indent=2)
        print(f"\n📄 Report saved to {report_path}")


if __name__ == "__main__":
    main()
