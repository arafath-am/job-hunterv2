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


PARSERS = {
    "greenhouse": parse_greenhouse,
    "lever": parse_lever,
    "ashby": parse_ashby,
    "smartrecruiters": parse_smartrecruiters,
    "workable": parse_workable,
}


# --------------------------------------------------------- one company
def collect_company(c) -> dict:
    """Fetch + diff one company. Returns a small summary dict."""
    ats, company, endpoint = c["ats"], c["company_name"], c["endpoint"]
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
