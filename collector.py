import re
from datetime import datetime, timedelta, timezone
"""
collector.py — pulls current postings from each resolved company's feed,
normalizes them, and diffs them into the jobs table.

Safety built in (per the rate-limit plan):
  * per-HOST rate limiting (the only thing ATS platforms actually throttle)
  * parallel ACROSS hosts, gentle WITHIN a host
  * 429/503 exponential backoff, honoring Retry-After
  * conditional requests (ETag / If-Modified-Since) -> cheap 304s
  * defensive parsing: never crash on a missing field

Requires: requests   (pip install requests)
Runs on the cloud VM, not a locked-down sandbox.
"""
import time
import threading
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

import db

UA = "Mozilla/5.0 (compatible; jobhunter/1.0; +https://example.com/jobhunter)"
PER_HOST_MIN_INTERVAL = 0.30   # seconds between requests to the SAME host (~3/s)
MAX_WORKERS = 12               # parallelism across hosts
MAX_RETRIES = 3
REQUEST_TIMEOUT = 12


# --------------------------------------------------------- per-host rate limiter
class HostRateLimiter:
    """Ensures requests to any single host are spaced >= min_interval apart."""
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._next_ok = defaultdict(float)
        self._locks = defaultdict(threading.Lock)

    def wait(self, host: str):
        with self._locks[host]:
            now = time.monotonic()
            wait_for = self._next_ok[host] - now
            if wait_for > 0:
                time.sleep(wait_for)
            self._next_ok[host] = time.monotonic() + self.min_interval


_limiter = HostRateLimiter(PER_HOST_MIN_INTERVAL)
_host_counts = defaultdict(int)   # for logging "we're nowhere near trouble"
_host_counts_lock = threading.Lock()


def _fetch(url: str, use_cache=True):
    """GET with rate limiting, conditional headers, and backoff.
    Returns (status, json_or_None). status 304 => unchanged, skip."""
    host = urlparse(url).netloc
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if use_cache:
        c = db.get_cache(url)
        if c:
            if c["etag"]:
                headers["If-None-Match"] = c["etag"]
            if c["last_modified"]:
                headers["If-Modified-Since"] = c["last_modified"]

    for attempt in range(MAX_RETRIES + 1):
        _limiter.wait(host)
        with _host_counts_lock:
            _host_counts[host] += 1
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            time.sleep((2 ** attempt) + random.random())
            continue

        if r.status_code == 304:
            return 304, None
        if r.status_code in (429, 503):
            retry_after = r.headers.get("Retry-After")
            delay = float(retry_after) if (retry_after or "").isdigit() else (2 ** attempt) + random.random()
            time.sleep(min(delay, 60))
            continue
        if r.status_code == 200:
            if use_cache:
                db.set_cache(url, r.headers.get("ETag"), r.headers.get("Last-Modified"))
            try:
                return 200, r.json()
            except ValueError:
                return 200, None
        return r.status_code, None
    return None, None




def _post_json(url: str, payload: dict):
    """POST JSON with rate limiting and backoff. For Workday CXS API."""
    host = urlparse(url).netloc
    headers = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
    for attempt in range(MAX_RETRIES + 1):
        _limiter.wait(host)
        with _host_counts_lock:
            _host_counts[host] += 1
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            time.sleep((2 ** attempt) + random.random())
            continue
        if r.status_code in (429, 503):
            retry_after = r.headers.get("Retry-After")
            delay = float(retry_after) if (retry_after or "").isdigit() else (2 ** attempt) + random.random()
            time.sleep(min(delay, 60))
            continue
        if r.status_code == 200:
            try:
                return 200, r.json()
            except ValueError:
                return 200, None
        return r.status_code, None
    return None, None


# --------------------------------------------------------- adapters (parse -> normalized dicts)
# Each returns a list of {ext_id, title, location, department, url, posted_at}.
def _g(d, *keys, default=""):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def parse_greenhouse(data):
    out = []
    for j in (data or {}).get("jobs", []):
        loc = j.get("location") or {}
        out.append({
            "ext_id": str(j.get("id")),
            "title": j.get("title", ""),
            "location": loc.get("name", "") if isinstance(loc, dict) else str(loc),
            "department": "",
            "url": j.get("absolute_url", ""),
            "posted_at": _g(j, "first_published", "updated_at"),
        })
    return out


def parse_lever(data):
    out = []
    for j in (data or []):
        cats = j.get("categories") or {}
        ts = j.get("createdAt")
        posted = ""
        if isinstance(ts, (int, float)):
            posted = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts / 1000))
        out.append({
            "ext_id": str(j.get("id")),
            "title": j.get("text", ""),
            "location": cats.get("location", ""),
            "department": cats.get("team", ""),
            "url": j.get("hostedUrl", ""),
            "posted_at": posted,
        })
    return out


def parse_ashby(data):
    out = []
    for j in (data or {}).get("jobs", []):
        out.append({
            "ext_id": str(_g(j, "id", "jobId")),
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "department": _g(j, "department", "team"),
            "url": _g(j, "jobUrl", "applyUrl"),
            "posted_at": _g(j, "publishedAt", "publishedDate"),
        })
    return out


def parse_smartrecruiters(data):
    out = []
    for j in (data or {}).get("content", []):
        loc = j.get("location") or {}
        city = loc.get("city", "") if isinstance(loc, dict) else ""
        country = loc.get("country", "") if isinstance(loc, dict) else ""
        out.append({
            "ext_id": str(j.get("id")),
            "title": j.get("name", ""),
            "location": ", ".join([p for p in (city, country) if p]),
            "department": "",
            "url": _g(j, "applyUrl", "ref",
                      default=f"https://jobs.smartrecruiters.com/{j.get('company',{}).get('identifier','')}/{j.get('id','')}"),
            "posted_at": _g(j, "releasedDate", "createdOn"),
        })
    return out


def parse_workable(data):
    out = []
    for j in (data or {}).get("jobs", []):
        out.append({
            "ext_id": str(_g(j, "shortcode", "id")),
            "title": j.get("title", ""),
            "location": _g(j, "location", "city"),
            "department": j.get("department", ""),
            "url": j.get("url", j.get("application_url", "")),
            "posted_at": _g(j, "published_on", "created_at"),
        })
    return out



def _parse_workday_date(text):
    """Convert 'Posted 3 Days Ago' or 'Posted Today' to ISO date."""
    if not text:
        return ""
    t = text.lower().strip()
    now = datetime.now(timezone.utc)
    if "today" in t or "just posted" in t:
        return now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if "yesterday" in t:
        return (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return (now - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return (now - timedelta(days=int(m.group(1)) * 30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


def parse_workday(data, base_url=""):
    """Parse Workday CXS jobs response."""
    out = []
    for j in (data or {}).get("jobPostings", []):
        ext_path = j.get("externalPath", "")
        # ext_id = last path segment (usually Title_JR-ID)
        ext_id = ext_path.rsplit("/", 1)[-1] if ext_path else str(hash(j.get("title", "")))
        job_url = base_url + ext_path if ext_path else ""
        out.append({
            "ext_id": ext_id,
            "title": j.get("title", ""),
            "location": j.get("locationsText", ""),
            "department": "",
            "url": job_url,
            "posted_at": _parse_workday_date(j.get("postedOn", "")),
        })
    return out


def collect_workday(c) -> dict:
    """Fetch all pages from a Workday CXS endpoint (POST with pagination)."""
    company, endpoint = c["company_name"], c["endpoint"]
    # Derive base URL for job links
    # endpoint: https://mit.wd5.myworkdayjobs.com/wday/cxs/mit/MIT/jobs
    # base:     https://mit.wd5.myworkdayjobs.com/MIT
    m = re.match(r"(https?://[\w.-]+)/wday/cxs/[\w-]+/([\w-]+)/jobs", endpoint)
    base_url = f"{m.group(1)}/{m.group(2)}" if m else ""

    all_postings = []
    offset = 0
    PAGE = 20
    MAX_JOBS = 500

    while offset < MAX_JOBS:
        payload = {"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": ""}
        status, data = _post_json(endpoint, payload)
        if status != 200 or not data:
            if offset == 0:
                return {"company": company, "status": f"err:{status}", "new": 0}
            break
        page = parse_workday(data, base_url)
        if not page:
            break
        all_postings.extend(page)
        total = data.get("total", 0)
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
            job = {"ats": "workday", "company": company, "brand": c["brand"],
                   "cap_exempt": c["cap_exempt"], "sponsor": c["sponsor"], **p}
            if db.upsert_job(con, job):
                new_count += 1
        db.mark_missing_inactive(con, "workday", company, seen_ids)
    return {"company": company, "status": "ok", "new": new_count, "total": len(all_postings)}


PARSERS = {
    "greenhouse": parse_greenhouse,
    "lever": parse_lever,
    "ashby": parse_ashby,
    "smartrecruiters": parse_smartrecruiters,
    "workable": parse_workable,
}


# --------------------------------------------------------- one company


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
            "department": str(j.get("department", "") or ""),
            "url": job_url,
            "posted_at": j.get("posted_date", ""),
        })
    return jobs


def collect_jibe(c) -> dict:
    """Fetch all jobs from a Jibe (iCIMS Talent Cloud) JSON API."""
    company, endpoint = c["company_name"], c["endpoint"]
    m = re.match(r"(https?://[\w.-]+)", endpoint)
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


def collect_company(c) -> dict:
    """Fetch + diff one company. Returns a small summary dict."""
    ats, company, endpoint = c["ats"], c["company_name"], c["endpoint"]
    if ats == "workday":
        return collect_workday(c)
    if ats == "jibe":
        return collect_jibe(c)
    if ats in ("icims", "taleo", "pageup"):
        return {"company": company, "status": "skip:playwright", "new": 0}
    parser = PARSERS.get(ats)
    if not parser or not endpoint:
        return {"company": company, "status": "skip", "new": 0}

    status, data = _fetch(endpoint)
    if status == 304:
        return {"company": company, "status": "unchanged", "new": 0}
    if status != 200 or data is None:
        return {"company": company, "status": f"err:{status}", "new": 0}

    postings = parser(data)
    new_count = 0
    seen_ids = set()
    with db.get_conn() as con:
        for p in postings:
            if not p.get("ext_id"):
                continue
            seen_ids.add(p["ext_id"])
            job = {
                "ats": ats, "company": company, "brand": c["brand"],
                "cap_exempt": c["cap_exempt"], "sponsor": c["sponsor"], **p,
            }
            if db.upsert_job(con, job):
                new_count += 1
        db.mark_missing_inactive(con, ats, company, seen_ids)
    return {"company": company, "status": "ok", "new": new_count, "total": len(postings)}


# --------------------------------------------------------- run
def run() -> dict:
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
    return summary


if __name__ == "__main__":
    run()


def run_playwright() -> dict:
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
    return summary
