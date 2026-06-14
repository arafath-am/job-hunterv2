#!/usr/bin/env python3
"""
deploy_ui_refresh.py — Deploys the UI refresh + sort/company counts backend.
Run from ~/job-hunterv2
"""
import shutil, os

print("=== UI Refresh Deploy ===\n")

# ── 1. Patch db.py — add sort + company_job_counts ──
print("[1/3] Patching db.py...")
with open('db.py', 'r') as f: code = f.read()
changed = False

# 1a. Add sort parameter
if 'sort="newest"' not in code:
    code = code.replace(
        'only_active=True, page=1, per_page=50, focus=""):',
        'only_active=True, page=1, per_page=50, focus="", sort="newest"):'
    )
    changed = True
    print("  ✓ sort parameter added")
else:
    print("  ✓ sort parameter already present")

# 1b. Replace ORDER BY with sort logic
if 'sort == "company"' not in code:
    code = code.replace(
        '        sql = f"SELECT * FROM jobs{where} ORDER BY{_kw_relevance} first_seen DESC LIMIT ? OFFSET ?"',
        '''        if sort == "company":
            _order = f"ORDER BY{_kw_relevance} COALESCE(brand, company) ASC, first_seen DESC"
        elif sort == "openings":
            _order = f"ORDER BY{_kw_relevance} (SELECT COUNT(*) FROM jobs j2 WHERE j2.active=1 AND j2.company=jobs.company) DESC, first_seen DESC"
        else:
            _order = f"ORDER BY{_kw_relevance} first_seen DESC"
        sql = f"SELECT * FROM jobs{where} {_order} LIMIT ? OFFSET ?"'''
    )
    changed = True
    print("  ✓ sort ORDER BY logic added")
else:
    print("  ✓ sort ORDER BY already present")

# 1c. Add company_job_counts function
if 'def company_job_counts' not in code:
    code = code.replace(
        'def purge_old_jobs',
        '''def company_job_counts():
    """Return {company_name: job_count} for active jobs."""
    with get_conn() as con:
        rows = con.execute(
            "SELECT company, COUNT(*) as cnt FROM jobs WHERE active=1 GROUP BY company"
        ).fetchall()
        return {r["company"]: r["cnt"] for r in rows}


def purge_old_jobs'''
    )
    changed = True
    print("  ✓ company_job_counts function added")
else:
    print("  ✓ company_job_counts already present")

if changed:
    with open('db.py', 'w') as f: f.write(code)

# ── 2. Patch app.py — add sort + co_counts ──
print("\n[2/3] Patching app.py...")
with open('app.py', 'r') as f: code = f.read()
changed = False

if 'sort: str' not in code:
    code = code.replace(
        'page: int = 1, focus: str = ""):',
        'page: int = 1, focus: str = "", sort: str = "newest"):'
    )
    changed = True
    print("  ✓ sort parameter added to route")
else:
    print("  ✓ sort parameter already in route")

if 'focus=focus, sort=sort' not in code:
    code = code.replace(
        'cap_exempt=cap_exempt, days=days, page=page, focus=focus)',
        'cap_exempt=cap_exempt, days=days, page=page, focus=focus, sort=sort)'
    )
    changed = True
    print("  ✓ sort passed to query_jobs")
else:
    print("  ✓ sort already passed to query_jobs")

if 'co_counts' not in code:
    code = code.replace(
        '    tracked = db.tracked_keys(user["id"])\n    stats = db.job_stats()',
        '    tracked = db.tracked_keys(user["id"])\n    stats = db.job_stats()\n    co_counts = db.company_job_counts()'
    )
    code = code.replace(
        '"stats": stats, "retention": RETENTION, "statuses": STATUSES,',
        '"stats": stats, "co_counts": co_counts, "retention": RETENTION, "statuses": STATUSES,'
    )
    changed = True
    print("  ✓ co_counts added")
else:
    print("  ✓ co_counts already present")

if '"focus": focus},' in code and '"sort": sort' not in code:
    code = code.replace(
        '"cap_exempt": cap_exempt, "days": days, "focus": focus},',
        '"cap_exempt": cap_exempt, "days": days, "focus": focus, "sort": sort},'
    )
    changed = True
    print("  ✓ sort added to filters dict")
else:
    print("  ✓ sort already in filters dict")

if changed:
    with open('app.py', 'w') as f: f.write(code)

# ── 3. Replace template + CSS ──
print("\n[3/3] Replacing template and CSS...")

# Backup originals
for src, bak in [('templates/jobs.html', 'templates/jobs_old.html'),
                 ('static/style.css', 'static/style_old.css')]:
    if os.path.exists(src) and not os.path.exists(bak):
        shutil.copy2(src, bak)
        print(f"  ✓ backed up {src} → {bak}")

# Copy new files
shutil.copy2('templates/jobs_new.html', 'templates/jobs.html')
print("  ✓ templates/jobs.html replaced")

shutil.copy2('static/style_new.css', 'static/style.css')
print("  ✓ static/style.css replaced")

print("\n=== Done! ===")
print("  Restart web: sudo systemctl restart jobhunter-web")
print("  Rollback:    cp templates/jobs_old.html templates/jobs.html")
print("               cp static/style_old.css static/style.css")
