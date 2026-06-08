"""
resolver.py  (stdlib-only version — NO pip installs needed)
-----------------------------------------------------------
Stage-1 ATS resolution. Reads companies_enriched.csv, probes the easy ATS
job-board APIs with each company's candidate slugs, records hits in a SQLite
DB. Unresolved companies (mostly Workday/Taleo/iCIMS) go to a manual queue.

Uses only Python's standard library — nothing to pip install. Just:
    python resolver.py
Run on any machine with normal internet (your laptop is fine).

Outputs:
    jobhunter.db              (SQLite: companies + seen_jobs tables)
    unresolved_companies.csv  (manual-discovery queue, cap-exempt first)
Re-runnable: already-processed companies are skipped.
"""
import csv
import json
import sqlite3
import time
import random
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

DB = "jobhunter.db"
SRC = "companies_enriched.csv"
WORKERS = 8
TIMEOUT = 8
UA = "Mozilla/5.0 (compatible; jobhunter-resolver/1.0)"

def get_json(url):
    """GET url, return parsed JSON (dict/list) or None on any failure/non-200."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None

def probe_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = get_json(url)
    if isinstance(data, dict) and data.get("jobs"):
        return True, len(data["jobs"]), url
    return False, 0, None

def probe_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = get_json(url)
    if isinstance(data, list) and data:
        return True, len(data), url
    return False, 0, None

def probe_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    data = get_json(url)
    if isinstance(data, dict) and data.get("jobs"):
        return True, len(data["jobs"]), url
    return False, 0, None

def probe_smartrecruiters(slug):
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    data = get_json(url)
    if isinstance(data, dict):
        n = data.get("totalFound", len(data.get("content", [])))
        if n:
            return True, n, url
    return False, 0, None

def probe_workable(slug):
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    data = get_json(url)
    if isinstance(data, dict) and data.get("jobs"):
        return True, len(data["jobs"]), url
    return False, 0, None

ATS_PROBES = [
    ("greenhouse", probe_greenhouse),
    ("lever", probe_lever),
    ("ashby", probe_ashby),
    ("smartrecruiters", probe_smartrecruiters),
    ("workable", probe_workable),
]

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS companies (
        company_name   TEXT PRIMARY KEY,
        brand          TEXT,
        cap_exempt     TEXT,
        sponsor        TEXT,
        priority_tier  TEXT,
        careers_url    TEXT,
        ats            TEXT,
        resolved_token TEXT,
        endpoint       TEXT,
        job_count      INTEGER,
        resolve_status TEXT DEFAULT 'pending',
        updated_at     TEXT
    );
    CREATE TABLE IF NOT EXISTS seen_jobs (
        ats        TEXT,
        company    TEXT,
        job_id     TEXT,
        title      TEXT,
        location   TEXT,
        url        TEXT,
        posted_at  TEXT,
        first_seen TEXT,
        PRIMARY KEY (ats, company, job_id)
    );
    """)
    con.commit()
    return con

def load_companies(con):
    with open(SRC, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        con.execute("""
            INSERT OR IGNORE INTO companies
              (company_name, brand, cap_exempt, sponsor, priority_tier, careers_url, resolve_status)
            VALUES (?,?,?,?,?,?, 'pending')
        """, (r["company_name"], r["brand"], r["cap_exempt"], r["sponsor"],
              r["priority_tier"], r.get("careers_url") or ""))
    con.commit()
    return rows

def resolve_company(row):
    slugs = [x for x in (row.get("candidate_slugs") or "").split("|") if x]
    for slug in slugs:
        for ats_name, probe in ATS_PROBES:
            try:
                ok, n, endpoint = probe(slug)
            except Exception:
                ok = False
            if ok:
                return dict(company=row["company_name"], ats=ats_name,
                            token=slug, endpoint=endpoint, job_count=n,
                            status="resolved")
            time.sleep(0.05 + random.random() * 0.1)
    return dict(company=row["company_name"], ats="", token="", endpoint="",
                job_count=0, status="unresolved")

def save_result(con, res):
    con.execute("""
        UPDATE companies
           SET ats=?, resolved_token=?, endpoint=?, job_count=?,
               resolve_status=?, updated_at=datetime('now')
         WHERE company_name=?
    """, (res["ats"], res["token"], res["endpoint"], res["job_count"],
          res["status"], res["company"]))
    con.commit()

def main():
    con = init_db()
    rows = load_companies(con)
    done = {r[0] for r in con.execute(
        "SELECT company_name FROM companies WHERE resolve_status != 'pending'")}
    todo = [r for r in rows if r["company_name"] not in done]
    print(f"{len(rows)} total, {len(done)} already processed, {len(todo)} to do")

    resolved = unresolved = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(resolve_company, r): r for r in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            save_result(con, res)
            if res["status"] == "resolved":
                resolved += 1
                print(f"[{i}/{len(todo)}] OK  {res['company'][:40]:42s} "
                      f"{res['ats']}/{res['token']} ({res['job_count']} jobs)")
            else:
                unresolved += 1
            if i % 50 == 0:
                print(f"  ... {i}/{len(todo)}  resolved={resolved} unresolved={unresolved}")

    cur = con.execute("""
        SELECT company_name, brand, priority_tier
          FROM companies WHERE resolve_status='unresolved'
       ORDER BY (priority_tier='hot') DESC, company_name
    """)
    with open("unresolved_companies.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company_name", "brand", "priority_tier",
                    "ats (fill: workday/taleo/icims/...)", "tenant", "dc", "site"])
        for r in cur:
            w.writerow([r[0], r[1], r[2], "", "", "", ""])

    print(f"\nDONE. resolved={resolved}  unresolved={unresolved}")
    print("Resolved companies are in jobhunter.db (resolve_status='resolved').")
    print("Manual queue written to unresolved_companies.csv (cap-exempt first).")

if __name__ == "__main__":
    main()
