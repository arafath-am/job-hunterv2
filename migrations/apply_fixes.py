#!/usr/bin/env python3
"""
Apply performance fixes to Job Hunter v2:
  1. Database indexes for query performance
  2. Pagination in db.query_jobs()
  3. Dashboard route accepts page param
  4. Template shows pagination controls
  5. Pagination CSS

Run from ~/job-hunterv2/:
    python3 apply_fixes.py
"""
import sqlite3
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)) if os.path.dirname(__file__) else ".")

errors = []

def patch_file(path, replacements, label):
    """Apply a list of (old, new) replacements to a file."""
    with open(path, "r") as f:
        content = f.read()
    for old, new in replacements:
        if old not in content:
            errors.append(f"  ✗ {label}: could not find match for replacement")
            print(f"  ✗ {label}: pattern not found — skipping this replacement")
            print(f"    Expected (first 80 chars): {old[:80]}...")
            return False
        content = content.replace(old, new, 1)
    with open(path, "w") as f:
        f.write(content)
    print(f"  ✓ {label}")
    return True


# ═══════════════════════════════════════════════════════════════
# 1. DATABASE INDEXES
# ═══════════════════════════════════════════════════════════════
print("\n[1/5] Adding database indexes...")
try:
    conn = sqlite3.connect("jobhunter.db")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_active_seen ON jobs(active, first_seen DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_active_company ON jobs(active, company)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_active_location ON jobs(active, location)")
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()
    print("  ✓ Composite indexes created + ANALYZE")
except Exception as e:
    errors.append(f"  ✗ DB indexes: {e}")
    print(f"  ✗ DB indexes: {e}")


# ═══════════════════════════════════════════════════════════════
# 2. PATCH db.py — pagination in query_jobs
# ═══════════════════════════════════════════════════════════════
print("\n[2/5] Patching db.py (pagination)...")

DB_PATCHES = [
    # 2a: function signature
    (
        'def query_jobs(keyword="", company="", location="", cap_exempt="", days=10,\n'
        '               only_active=True, limit=2000):',
        'def query_jobs(keyword="", company="", location="", cap_exempt="", days=10,\n'
        '               only_active=True, page=1, per_page=50):',
    ),
    # 2b: replace the tail (SQL + return) with pagination logic
    (
        '    sql = f"SELECT * FROM jobs{where} ORDER BY{_kw_relevance} first_seen DESC LIMIT ?"\n'
        '    params += _kw_relevance_params\n'
        '    params.append(limit)\n'
        '    with get_conn() as con:\n'
        '        return con.execute(sql, params).fetchall()',

        '    with get_conn() as con:\n'
        '        total = con.execute(f"SELECT COUNT(*) FROM jobs{where}", params).fetchone()[0]\n'
        '        offset = (max(1, page) - 1) * per_page\n'
        '        sql = f"SELECT * FROM jobs{where} ORDER BY{_kw_relevance} first_seen DESC LIMIT ? OFFSET ?"\n'
        '        all_params = params + _kw_relevance_params + [per_page, offset]\n'
        '        jobs = con.execute(sql, all_params).fetchall()\n'
        '    pages = max(1, (total + per_page - 1) // per_page)\n'
        '    return {"jobs": jobs, "total": total, "page": max(1, page), "pages": pages, "per_page": per_page}',
    ),
]
patch_file("db.py", DB_PATCHES, "db.py query_jobs")


# ═══════════════════════════════════════════════════════════════
# 3. PATCH app.py — dashboard route accepts page
# ═══════════════════════════════════════════════════════════════
print("\n[3/5] Patching app.py (dashboard pagination)...")

APP_PATCHES = [
    # 3a: add page param to route signature
    (
        'location: str = "", cap_exempt: str = "", days: int = RETENTION):',
        'location: str = "", cap_exempt: str = "", days: int = RETENTION, page: int = 1):',
    ),
    # 3b: query call returns result dict
    (
        'jobs = db.query_jobs(keyword=keyword, company=company, location=location,\n'
        '                         cap_exempt=cap_exempt, days=days)',
        'result = db.query_jobs(keyword=keyword, company=company, location=location,\n'
        '                           cap_exempt=cap_exempt, days=days, page=page)',
    ),
    # 3c: pass result instead of jobs to template
    (
        '"user": user, "jobs": jobs, "tracked": tracked,',
        '"user": user, "result": result, "tracked": tracked,',
    ),
]
patch_file("app.py", APP_PATCHES, "app.py dashboard")


# ═══════════════════════════════════════════════════════════════
# 4. PATCH templates/jobs.html — pagination controls
# ═══════════════════════════════════════════════════════════════
print("\n[4/5] Patching templates/jobs.html (pagination UI)...")

PAGINATION_HTML = """
  <div class="pager">
    <span class="pager-info">{{ result.total }} result{{ 's' if result.total != 1 else '' }} · page {{ result.page }} of {{ result.pages }}</span>
    <div class="pager-nav">
      {% if result.page > 1 %}
      <a href="/?keyword={{ filters.keyword|urlencode }}&company={{ filters.company|urlencode }}&location={{ filters.location|urlencode }}&cap_exempt={{ filters.cap_exempt }}&days={{ filters.days }}&page={{ result.page - 1 }}" class="btn">&#8592; Prev</a>
      {% endif %}
      {% if result.page < result.pages %}
      <a href="/?keyword={{ filters.keyword|urlencode }}&company={{ filters.company|urlencode }}&location={{ filters.location|urlencode }}&cap_exempt={{ filters.cap_exempt }}&days={{ filters.days }}&page={{ result.page + 1 }}" class="btn">Next &#8594;</a>
      {% endif %}
    </div>
  </div>
"""

HTML_PATCHES = [
    # 4a: change jobs → result.jobs in conditional
    ('{% if jobs %}', '{% if result.jobs %}'),
    # 4b: change jobs → result.jobs in loop
    ('{% for j in jobs %}', '{% for j in result.jobs %}'),
    # 4c: insert pagination between jobs list and else block
    (
        '  {% else %}\n  <div class="empty">',
        PAGINATION_HTML + '  {% else %}\n  <div class="empty">',
    ),
]
patch_file("templates/jobs.html", HTML_PATCHES, "jobs.html pagination")


# ═══════════════════════════════════════════════════════════════
# 5. APPEND pagination CSS to static/style.css
# ═══════════════════════════════════════════════════════════════
print("\n[5/5] Adding pagination CSS...")

PAGER_CSS = """
/* ── pagination ── */
.pager{display:flex;justify-content:space-between;align-items:center;padding:.8rem 0;margin-top:.5rem;border-top:1px solid rgba(255,255,255,.08)}
.pager-info{color:#888;font-size:.85rem;letter-spacing:.01em}
.pager-nav{display:flex;gap:.5rem}
.pager .btn{padding:.35rem .8rem;font-size:.82rem;text-decoration:none}
"""

try:
    with open("static/style.css", "r") as f:
        css = f.read()
    if ".pager" not in css:
        with open("static/style.css", "a") as f:
            f.write(PAGER_CSS)
        print("  ✓ Pagination CSS appended")
    else:
        print("  · Pagination CSS already present — skipped")
except Exception as e:
    errors.append(f"  ✗ CSS: {e}")
    print(f"  ✗ CSS: {e}")


# ═══════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════
print("\n" + "═" * 50)
if errors:
    print(f"Done with {len(errors)} error(s):")
    for e in errors:
        print(e)
    print("\nReview the errors above before restarting.")
else:
    print("All patches applied cleanly.")
    print("Restart services to activate.")
