#!/usr/bin/env python3
"""
tools/endpoint_checker.py — Endpoint drift detection.

Probes every resolved company's endpoint with a lightweight request
to verify it still responds and returns jobs. Flags drift (redirects,
errors, zero jobs) for review.

Usage:
    python3 tools/endpoint_checker.py              # check all resolved
    python3 tools/endpoint_checker.py --ats workday  # check one ATS type
    python3 tools/endpoint_checker.py --limit 50   # check N companies

Designed to run weekly via scheduler or manually.
"""
import os
import sys
import json
import time
import argparse
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import db

try:
    import monitoring
except ImportError:
    monitoring = None

UA = "Mozilla/5.0 (compatible; jobhunter-healthcheck/1.0)"
TIMEOUT = 15
DELAY = 1.0


def check_endpoint(company_name: str, ats: str, endpoint: str,
                    resolved_token: str = None) -> dict:
    """Probe one endpoint. Returns check result dict."""
    result = {
        "company_name": company_name,
        "endpoint": endpoint,
        "http_status": None,
        "response_ok": False,
        "jobs_found": 0,
        "drift_detected": False,
        "notes": "",
    }

    if not endpoint:
        result["notes"] = "no endpoint"
        return result

    try:
        if ats == "workday":
            result = _check_workday(result, endpoint, resolved_token)
        elif ats == "jibe":
            result = _check_jibe(result, endpoint)
        elif ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "workable"):
            result = _check_json_api(result, endpoint)
        elif ats in ("sitemap", "heuristic", "jsonld"):
            result = _check_url_alive(result, endpoint)
        else:
            result = _check_url_alive(result, endpoint)
    except requests.Timeout:
        result["notes"] = "timeout"
        result["drift_detected"] = True
    except requests.ConnectionError:
        result["notes"] = "connection error"
        result["drift_detected"] = True
    except Exception as e:
        result["notes"] = f"error: {str(e)[:100]}"

    return result


def _check_workday(result, endpoint, token_str):
    """POST to Workday CXS endpoint, check for job results."""
    body = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
    resp = requests.post(endpoint, json=body, headers={"User-Agent": UA}, timeout=TIMEOUT)
    result["http_status"] = resp.status_code
    if resp.status_code == 200:
        data = resp.json()
        total = data.get("total", 0)
        result["jobs_found"] = total
        result["response_ok"] = total > 0
        if total == 0:
            result["drift_detected"] = True
            result["notes"] = "0 jobs returned"
    else:
        result["drift_detected"] = True
        result["notes"] = f"HTTP {resp.status_code}"
    return result


def _check_jibe(result, endpoint):
    """GET Jibe /api/jobs endpoint."""
    # endpoint is like https://domain/api/jobs — add limit param
    url = endpoint.rstrip("/")
    if "?" not in url:
        url += "?limit=1"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    result["http_status"] = resp.status_code
    if resp.status_code == 200:
        data = resp.json()
        count = data.get("count", 0)
        result["jobs_found"] = count
        result["response_ok"] = count > 0
        if count == 0:
            result["drift_detected"] = True
            result["notes"] = "0 jobs in response"
    else:
        result["drift_detected"] = True
        result["notes"] = f"HTTP {resp.status_code}"
    return result


def _check_json_api(result, endpoint):
    """GET a JSON API endpoint, check it returns data."""
    resp = requests.get(endpoint, headers={"User-Agent": UA}, timeout=TIMEOUT)
    result["http_status"] = resp.status_code
    if resp.status_code == 200:
        try:
            data = resp.json()
            # Try to estimate job count from response
            if isinstance(data, list):
                result["jobs_found"] = len(data)
            elif isinstance(data, dict):
                # Common patterns
                for key in ("jobs", "postings", "results", "data", "content"):
                    if key in data and isinstance(data[key], list):
                        result["jobs_found"] = len(data[key])
                        break
                else:
                    result["jobs_found"] = 1  # got valid JSON at least
            result["response_ok"] = result["jobs_found"] > 0
            if result["jobs_found"] == 0:
                result["drift_detected"] = True
                result["notes"] = "valid JSON but 0 jobs"
        except (ValueError, KeyError):
            result["drift_detected"] = True
            result["notes"] = "invalid JSON response"
    elif resp.status_code in (301, 302, 308):
        result["drift_detected"] = True
        result["notes"] = f"redirect to {resp.headers.get('Location', '?')[:80]}"
    else:
        result["drift_detected"] = True
        result["notes"] = f"HTTP {resp.status_code}"
    return result


def _check_url_alive(result, endpoint):
    """Simple HEAD/GET check for non-API endpoints."""
    # For sitemap/heuristic, just check the careers URL is alive
    resp = requests.get(endpoint, headers={"User-Agent": UA}, timeout=TIMEOUT,
                        allow_redirects=True)
    result["http_status"] = resp.status_code
    result["response_ok"] = resp.status_code == 200
    if resp.status_code != 200:
        result["drift_detected"] = True
        result["notes"] = f"HTTP {resp.status_code}"
    # Check for significant redirect
    if resp.url and urlparse(resp.url).netloc != urlparse(endpoint).netloc:
        result["drift_detected"] = True
        result["notes"] = f"redirected to {urlparse(resp.url).netloc}"
    return result


def store_check(check: dict):
    """Write check result to endpoint_checks table."""
    with db.get_conn() as con:
        con.execute("""
            INSERT INTO endpoint_checks
                (company_name, checked_at, endpoint, http_status,
                 response_ok, jobs_found, drift_detected, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            check["company_name"],
            datetime.now(timezone.utc).isoformat(),
            check.get("endpoint", ""),
            check.get("http_status"),
            1 if check.get("response_ok") else 0,
            check.get("jobs_found", 0),
            1 if check.get("drift_detected") else 0,
            check.get("notes", ""),
        ))


def main():
    parser = argparse.ArgumentParser(description="Endpoint drift checker")
    parser.add_argument("--ats", help="Check only this ATS type")
    parser.add_argument("--limit", type=int, help="Max companies to check")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    args = parser.parse_args()

    db.init_db()
    if monitoring:
        monitoring.init_monitoring()

    # Get resolved companies
    with db.get_conn() as con:
        query = """
            SELECT company_name, brand, ats, endpoint, resolved_token
            FROM companies
            WHERE resolve_status='resolved' AND endpoint IS NOT NULL AND endpoint != ''
        """
        params = []
        if args.ats:
            query += " AND ats=?"
            params.append(args.ats)
        if args.limit:
            query += " LIMIT ?"
            params.append(args.limit)
        companies = [dict(r) for r in con.execute(query, params).fetchall()]

    total = len(companies)
    print(f"Checking {total} endpoints...\n")

    drifted = []
    ok_count = 0
    fail_count = 0

    for i, c in enumerate(companies, 1):
        name = c["company_name"]
        brand = c.get("brand", name)
        ats = c["ats"]
        endpoint = c["endpoint"]

        print(f"[{i}/{total}] {brand} ({ats})", end=" ")

        check = check_endpoint(name, ats, endpoint, c.get("resolved_token"))

        if check["drift_detected"]:
            print(f"⚠️  DRIFT: {check['notes']}")
            drifted.append(check)
            fail_count += 1
        elif check["response_ok"]:
            print(f"✅ {check['jobs_found']} jobs")
            ok_count += 1
        else:
            print(f"⚠️  {check['notes']}")
            fail_count += 1

        if not args.dry_run:
            store_check(check)

        time.sleep(DELAY)

    # Summary
    print(f"\n{'='*60}")
    print(f"DONE: {ok_count} healthy, {fail_count} issues, {len(drifted)} drifted")
    print(f"{'='*60}")

    if drifted:
        print(f"\nDrifted endpoints:")
        for d in drifted:
            print(f"  ⚠️  {d['company_name']:45} {d['notes']}")

        # Discord alert for drift
        if monitoring and len(drifted) > 0:
            drift_names = [d["company_name"] for d in drifted[:10]]
            msg = (
                f"🔍 **Endpoint check complete**: {ok_count} OK, {len(drifted)} drifted\n"
                f"Drifted: {', '.join(drift_names)}"
                + (f" +{len(drifted)-10} more" if len(drifted) > 10 else "")
            )
            monitoring._alert(msg)


if __name__ == "__main__":
    main()
