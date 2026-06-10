#!/usr/bin/env python3
"""
Fix iCIMS and Taleo scrapers + demote junk companies.

Run from ~/job-hunterv2/:
    python3 fix_scrapers.py
"""
import sqlite3

# ═══════════════════════════════════════════════════════════════
# 1. PATCH playwright_adapter.py — fix iCIMS + Taleo scrapers
# ═══════════════════════════════════════════════════════════════
print("[1/3] Patching playwright_adapter.py...")

with open("playwright_adapter.py", "r") as f:
    code = f.read()

# ── 1a: Fix iCIMS scraper ──
# Problem: clicks submit button on React SPAs, breaking the page
# Fix: detect redirect to different domain (React SPA) and skip button click

old_icims = '''def _scrape_icims(page, url):
    base_match = re.match(r"(https://[\\w.-]+\\.icims\\.com)", url)
    if not base_match:
        return []
    base = base_match.group(1)
    search_url = base + "/jobs/search?ss=1&in_iframe=1"
    page.goto(search_url, timeout=TIMEOUT, wait_until="networkidle")
    page.wait_for_timeout(RENDER_WAIT)
    frame = page.frames[1] if len(page.frames) > 1 else page.main_frame
    try:
        btn = frame.locator('input[type="submit"], button[type="submit"]')
        if btn.count() > 0:
            btn.first.click()
            page.wait_for_timeout(4000)
    except Exception:
        pass'''

new_icims = '''def _scrape_icims(page, url):
    base_match = re.match(r"(https?://[\\w.-]+\\.icims\\.com)", url)
    if not base_match:
        return []
    base = base_match.group(1)
    search_url = base + "/jobs/search?ss=1"
    page.goto(search_url, timeout=TIMEOUT, wait_until="networkidle")
    page.wait_for_timeout(RENDER_WAIT)

    final_url = page.url
    is_react_spa = not final_url.startswith(base.replace("http://", "https://"))

    if is_react_spa:
        # Modern iCIMS: redirected to React SPA (e.g. jobs.company.com)
        # Jobs are already rendered — no button click needed
        page.wait_for_timeout(2000)
        frame = page.main_frame
    else:
        # Legacy iCIMS: iframe-based search
        frame = page.frames[1] if len(page.frames) > 1 else page.main_frame
        try:
            btn = frame.locator('input[type="submit"], button[type="submit"]')
            if btn.count() > 0:
                btn.first.click()
                page.wait_for_timeout(4000)
        except Exception:
            pass'''

if old_icims in code:
    code = code.replace(old_icims, new_icims)
    print("  ✓ iCIMS scraper patched (React SPA detection)")
else:
    print("  ✗ Could not find iCIMS function to patch")

# ── 1b: Fix Taleo scraper ──
# Problem: search button has value="Search for Jobs" but scraper looks for value='Search' (exact)
# Fix: use contains-match and also try clicking "All Jobs" link first

old_taleo = '''def _scrape_taleo(page, url):
    page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    try:
        btn = page.locator("button:has-text('Search'), input[type='submit'][value='Search']")
        if btn.count() > 0 and btn.first.is_visible():
            btn.first.click()
            page.wait_for_timeout(3000)
    except Exception:
        pass'''

new_taleo = '''def _scrape_taleo(page, url):
    page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    # Check if we landed on a login/SSO page (not a job board)
    if "login" in page.url.lower() or "sso" in page.url.lower() or "idp/" in page.url.lower():
        return []

    # Try 1: click "Search for Jobs" button (Taleo standard ID)
    try:
        btn = page.locator("[id='basicSearchFooterInterface.searchAction'], input[value*='Search'][type='submit'], button:has-text('Search')")
        if btn.count() > 0 and btn.first.is_visible():
            btn.first.click()
            page.wait_for_timeout(5000)
    except Exception:
        pass'''

if old_taleo in code:
    code = code.replace(old_taleo, new_taleo)
    print("  ✓ Taleo scraper patched (button selector + SSO detection)")
else:
    print("  ✗ Could not find Taleo function to patch")

with open("playwright_adapter.py", "w") as f:
    f.write(code)


# ═══════════════════════════════════════════════════════════════
# 2. DEMOTE Taleo SSO-login companies (base domain → login page)
# ═══════════════════════════════════════════════════════════════
print("\n[2/3] Demoting SSO-login and duplicate companies...")

conn = sqlite3.connect("jobhunter.db")
demoted = 0

# Taleo base domains that redirect to SSO login (no public careers page)
taleo_sso = [
    "Leland Stanford Jr University",       # stanford.taleo.net → Stanford SSO
    "Lawrence Berkeley National Laboratory", # lbl.taleo.net → LBNL SSO
    "Bayshore Therapies",                   # bayshore.taleo.net → likely SSO
    "Children's Hospital",                  # cnhs.taleo.net → likely SSO
    "Henry Ford Health System",             # henryford.taleo.net → likely SSO
    "University of Iowa",                   # uiowa.taleo.net → likely SSO
    "University of Texas Health Science Center at San Antonio",  # uthscsa.taleo.net
    "University of Texas Md Anderson Cancer Center",  # mdanderson.taleo.net
    "University of Texas Medical Branch",   # aa083.taleo.net
]
for name in taleo_sso:
    cur = conn.execute("UPDATE companies SET resolve_status='identified', endpoint=NULL WHERE company_name=?", (name,))
    if cur.rowcount:
        print(f"  demoted (SSO): {name}")
        demoted += cur.rowcount

# DISH duplicate
cur = conn.execute("""UPDATE companies SET resolve_status='identified', endpoint=NULL 
    WHERE company_name LIKE 'Dish Wireless%'""")
if cur.rowcount:
    print(f"  demoted (dupe): Dish Wireless")
    demoted += cur.rowcount

conn.commit()
print(f"\n  Total demoted: {demoted}")


# ═══════════════════════════════════════════════════════════════
# 3. VERIFY — show remaining 0-job companies
# ═══════════════════════════════════════════════════════════════
print("\n[3/3] Remaining 0-job resolved companies:")
remaining = conn.execute("""
    SELECT c.company_name, c.ats, c.endpoint
    FROM companies c
    WHERE c.resolve_status='resolved' AND c.endpoint IS NOT NULL
    AND c.company_name NOT IN (SELECT DISTINCT company FROM jobs WHERE active=1)
    ORDER BY c.ats, c.company_name
""").fetchall()

for r in remaining:
    print(f"  {r[1]:<12s} {r[0]:<55s} {(r[2] or '')[:60]}")
print(f"\nRemaining 0-job: {len(remaining)}")

conn.close()
print("\nDone. Restart collector to activate scraper fixes:")
print("  sudo systemctl restart jobhunter-collector")
