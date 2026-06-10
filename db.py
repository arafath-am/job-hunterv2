"""
db.py — SQLite data layer (WAL mode) for the Job Hunter app.

Layers on top of the resolver's jobhunter.db: it already created `companies`
and `seen_jobs`; here we add `jobs`, `users`, `applications`, and `http_cache`.
All schema creation is idempotent (safe to call repeatedly).

WAL mode lets the scheduler write while the web app reads, concurrently.
"""
import os
import sqlite3
import hashlib
import secrets
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = os.environ.get("JOBHUNTER_DB", "jobhunter.db")
RETENTION_DAYS = int(os.environ.get("JOBHUNTER_RETENTION_DAYS", "10"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=30000;")
    con.execute("PRAGMA foreign_keys=ON;")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with get_conn() as con:
        con.executescript("""
        -- companies table may already exist from the resolver; create if not.
        CREATE TABLE IF NOT EXISTS companies (
            company_name TEXT PRIMARY KEY, brand TEXT, cap_exempt TEXT,
            sponsor TEXT, priority_tier TEXT, careers_url TEXT, ats TEXT,
            resolved_token TEXT, endpoint TEXT, job_count INTEGER,
            resolve_status TEXT DEFAULT 'pending', updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ats         TEXT NOT NULL,
            company     TEXT NOT NULL,
            brand       TEXT,
            cap_exempt  TEXT,
            sponsor     TEXT,
            ext_id      TEXT NOT NULL,      -- the ATS's own job id
            title       TEXT,
            location    TEXT,
            department  TEXT,
            url         TEXT,
            posted_at   TEXT,              -- ISO; best-effort from the feed
            first_seen  TEXT,              -- when WE first saw it
            last_seen   TEXT,              -- last run it was still present
            active      INTEGER DEFAULT 1, -- 0 once it disappears from the feed
            UNIQUE(ats, company, ext_id)
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen);
        CREATE INDEX IF NOT EXISTS idx_jobs_company    ON jobs(company);
        CREATE INDEX IF NOT EXISTS idx_jobs_active     ON jobs(active);

        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at    TEXT
        );

        -- application/progress tracking. Stores a SNAPSHOT of the job so the
        -- record survives the 10-day job purge.
        CREATE TABLE IF NOT EXISTS applications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            job_key     TEXT NOT NULL,      -- ats:company:ext_id
            company     TEXT,
            title       TEXT,
            location    TEXT,
            url         TEXT,
            status      TEXT DEFAULT 'interested',  -- interested|applied|interview|offer|rejected
            notes       TEXT DEFAULT '',
            applied_at  TEXT,
            created_at  TEXT,
            updated_at  TEXT,
            UNIQUE(user_id, job_key),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- conditional-request cache for polite polling (ETag / Last-Modified).
        CREATE TABLE IF NOT EXISTS http_cache (
            endpoint      TEXT PRIMARY KEY,
            etag          TEXT,
            last_modified TEXT,
            updated_at    TEXT
        );
        """)


# ----------------------------------------------------------------- users / auth
def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(_hash_password(password, salt), stored)


def create_user(username: str, password: str) -> bool:
    with get_conn() as con:
        try:
            con.execute(
                "INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?)",
                (username.strip().lower(), _hash_password(password), now_iso()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_user(username: str):
    with get_conn() as con:
        return con.execute(
            "SELECT * FROM users WHERE username=?", (username.strip().lower(),)
        ).fetchone()


# ----------------------------------------------------------------- jobs
def upsert_job(con, job: dict):
    """Insert a job if new (returns True), else refresh last_seen (returns False)."""
    row = con.execute(
        "SELECT id FROM jobs WHERE ats=? AND company=? AND ext_id=?",
        (job["ats"], job["company"], job["ext_id"]),
    ).fetchone()
    ts = now_iso()
    if row:
        con.execute(
            "UPDATE jobs SET last_seen=?, active=1, title=?, location=?, url=? WHERE id=?",
            (ts, job.get("title"), job.get("location"), job.get("url"), row["id"]),
        )
        return False
    con.execute(
        """INSERT INTO jobs
           (ats, company, brand, cap_exempt, sponsor, ext_id, title, location,
            department, url, posted_at, first_seen, last_seen, active)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (job["ats"], job["company"], job.get("brand"), job.get("cap_exempt"),
         job.get("sponsor"), job["ext_id"], job.get("title"), job.get("location"),
         job.get("department"), job.get("url"), job.get("posted_at"), ts, ts),
    )
    return True


def mark_missing_inactive(con, ats: str, company: str, seen_ext_ids: set):
    """Any job for this company not in the latest pull is marked closed."""
    rows = con.execute(
        "SELECT ext_id FROM jobs WHERE ats=? AND company=? AND active=1",
        (ats, company),
    ).fetchall()
    for r in rows:
        if r["ext_id"] not in seen_ext_ids:
            con.execute(
                "UPDATE jobs SET active=0 WHERE ats=? AND company=? AND ext_id=?",
                (ats, company, r["ext_id"]),
            )


def query_jobs(keyword="", company="", location="", cap_exempt="", days=10,
               only_active=True, page=1, per_page=50):
    clauses, params = [], []
    if only_active:
        clauses.append("active=1")
    if days:
        clauses.append("first_seen >= datetime('now', ?)")
        params.append(f"-{int(days)} days")
    _kw_relevance = ""
    _kw_relevance_params = []
    if keyword:
        words = keyword.strip().split()
        word_clauses = []
        relevance_parts = []
        for w in words:
            if len(w) <= 3:
                word_clauses.append(
                    "(' '||UPPER(title)||' ' LIKE ? OR ' '||UPPER(department)||' ' LIKE ?)"
                )
                params += [f"% {w.upper()} %", f"% {w.upper()} %"]
                relevance_parts.append(
                    f"(CASE WHEN ' '||UPPER(title)||' ' LIKE ? THEN 1 ELSE 0 END)"
                )
                _kw_relevance_params.append(f"% {w.upper()} %")
            else:
                word_clauses.append("(title LIKE ? OR department LIKE ?)")
                params += [f"%{w}%", f"%{w}%"]
                relevance_parts.append(
                    f"(CASE WHEN UPPER(title) LIKE ? THEN 1 ELSE 0 END)"
                )
                _kw_relevance_params.append(f"%{w.upper()}%")
        clauses.append(f"({' OR '.join(word_clauses)})")
        _kw_relevance = " (" + " + ".join(relevance_parts) + ") DESC, "
    if company:
        clauses.append("(company LIKE ? OR brand LIKE ?)")
        params += [f"%{company}%", f"%{company}%"]
    if location:
        _loc = location.strip().lower()
        _us_aliases = {"united states", "usa", "us", "u.s.", "u.s.a."}
        if _loc in _us_aliases:
            _st = (
                "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID",
                "IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS",
                "MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
                "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV",
                "WI","WY","DC",
            )
            _full = (
                "Alabama","Alaska","Arizona","Arkansas","California","Colorado",
                "Connecticut","Delaware","Florida","Georgia","Hawaii","Idaho",
                "Illinois","Indiana","Iowa","Kansas","Kentucky","Louisiana",
                "Maine","Maryland","Massachusetts","Michigan","Minnesota",
                "Mississippi","Missouri","Montana","Nebraska","Nevada",
                "New Hampshire","New Jersey","New Mexico","New York",
                "North Carolina","North Dakota","Ohio","Oklahoma","Oregon",
                "Pennsylvania","Rhode Island","South Carolina","South Dakota",
                "Tennessee","Texas","Utah","Vermont","Virginia","Washington",
                "West Virginia","Wisconsin","Wyoming",
            )
            # Non-US places that clash with state abbreviations
            _excl_cities = (
                "Bengaluru","Bangalore","Hyderabad","Chennai","Mumbai","Pune",
                "Delhi","Noida","Gurgaon","Gurugram","Kolkata","Ahmedabad",
                "Kochi","Coimbatore","Trivandrum","Chandigarh","Jaipur",
                "Toronto","Vancouver","Montreal","Ottawa","Calgary","Edmonton",
                "Berlin","Munich","Frankfurt","Hamburg","Dublin","London",
                "Manchester","Amsterdam","Paris","Singapore","Tokyo","Seoul",
                "Shanghai","Beijing","Shenzhen","Taipei","Melbourne","Sydney",
                "Sao Paulo","Mexico City","Zurich","Geneva","Stockholm",
            )
            _pats = []
            # State abbreviation at END of string only: "City, CA"
            _pats += [f"%, {s}" for s in _st]
            # State abbreviation before country: "City, CA, United States"
            _pats += [f"%, {s}, United States%" for s in _st]
            # Full state names anywhere
            _pats += [f"%{s}%" for s in _full]
            # Explicit US markers
            _pats += ["%United States%", "%, USA%"]
            _or = " OR ".join(["location LIKE ?" for _ in _pats])
            # Exclude non-US cities
            _not_pats = [f"%{c}%" for c in _excl_cities]
            _not = " AND ".join(["location NOT LIKE ?" for _ in _not_pats])
            clauses.append(f"(({_or}) AND ({_not}))")
            params += _pats + _not_pats
        else:
            clauses.append("location LIKE ?")
            params.append(f"%{location}%")
    if cap_exempt.lower() in ("yes", "no"):
        clauses.append("lower(cap_exempt)=?")
        params.append(cap_exempt.lower())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as con:
        total = con.execute(f"SELECT COUNT(*) FROM jobs{where}", params).fetchone()[0]
        offset = (max(1, page) - 1) * per_page
        sql = f"SELECT * FROM jobs{where} ORDER BY{_kw_relevance} first_seen DESC LIMIT ? OFFSET ?"
        all_params = params + _kw_relevance_params + [per_page, offset]
        jobs = con.execute(sql, all_params).fetchall()
    pages = max(1, (total + per_page - 1) // per_page)
    return {"jobs": jobs, "total": total, "page": max(1, page), "pages": pages, "per_page": per_page}


def job_stats():
    with get_conn() as con:
        total = con.execute("SELECT COUNT(*) c FROM jobs WHERE active=1").fetchone()["c"]
        last24 = con.execute(
            "SELECT COUNT(*) c FROM jobs WHERE active=1 AND first_seen>=datetime('now','-1 day')"
        ).fetchone()["c"]
        companies = con.execute("SELECT COUNT(DISTINCT company) c FROM jobs WHERE active=1").fetchone()["c"]
        return {"total": total, "last24": last24, "companies": companies}


def purge_old_jobs(days: int = RETENTION_DAYS) -> int:
    """Delete jobs older than `days` (by first_seen). Applications are untouched."""
    with get_conn() as con:
        cur = con.execute(
            "DELETE FROM jobs WHERE first_seen < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        return cur.rowcount


# ----------------------------------------------------------------- applications
def track_application(user_id: int, job: dict, status="interested"):
    key = f"{job['ats']}:{job['company']}:{job['ext_id']}"
    ts = now_iso()
    with get_conn() as con:
        con.execute(
            """INSERT INTO applications
               (user_id, job_key, company, title, location, url, status, applied_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id, job_key) DO UPDATE SET
                 status=excluded.status, updated_at=excluded.updated_at""",
            (user_id, key, job.get("company"), job.get("title"), job.get("location"),
             job.get("url"), status, ts if status == "applied" else None, ts, ts),
        )


def update_application(user_id: int, job_key: str, status=None, notes=None):
    sets, params = ["updated_at=?"], [now_iso()]
    if status is not None:
        sets.append("status=?"); params.append(status)
        if status == "applied":
            sets.append("applied_at=COALESCE(applied_at, ?)"); params.append(now_iso())
    if notes is not None:
        sets.append("notes=?"); params.append(notes)
    params += [user_id, job_key]
    with get_conn() as con:
        con.execute(f"UPDATE applications SET {', '.join(sets)} WHERE user_id=? AND job_key=?", params)


def list_applications(user_id: int):
    with get_conn() as con:
        return con.execute(
            "SELECT * FROM applications WHERE user_id=? ORDER BY updated_at DESC", (user_id,)
        ).fetchall()


def tracked_keys(user_id: int) -> set:
    with get_conn() as con:
        return {r["job_key"] for r in con.execute(
            "SELECT job_key FROM applications WHERE user_id=?", (user_id,))}


# ----------------------------------------------------------------- http cache
def get_cache(endpoint: str):
    with get_conn() as con:
        return con.execute("SELECT etag, last_modified FROM http_cache WHERE endpoint=?",
                           (endpoint,)).fetchone()


def set_cache(endpoint: str, etag: str | None, last_modified: str | None):
    with get_conn() as con:
        con.execute(
            """INSERT INTO http_cache(endpoint, etag, last_modified, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(endpoint) DO UPDATE SET
                 etag=excluded.etag, last_modified=excluded.last_modified,
                 updated_at=excluded.updated_at""",
            (endpoint, etag, last_modified, now_iso()),
        )


def resolved_companies():
    with get_conn() as con:
        return con.execute(
            """SELECT company_name, brand, cap_exempt, sponsor, ats,
                      resolved_token, endpoint
                 FROM companies WHERE resolve_status='resolved' AND endpoint!=''"""
        ).fetchall()


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH} (retention={RETENTION_DAYS} days)")
