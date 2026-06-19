# Job Hunter v2 — Environment Setup

## Fresh VM / venv rebuild steps

1. Create venv:
   python3 -m venv venv

2. Install Python packages:
   ./venv/bin/pip install -r requirements.txt

3. Install Playwright's browser binary (NOT covered by pip install alone):
   ./venv/bin/playwright install chromium
   ./venv/bin/playwright install-deps chromium

   Skipping step 3 causes a silent failure: collector.run_playwright()
   catches the ImportError/missing-browser error internally and logs
   "0/N companies succeeded" with no crash, no loud error, and nothing
   in `collection_runs`. This happened in production on 2026-06-19 —
   the Playwright collector had been silently failing all day with zero
   visibility until manually diagnosed.

4. Verify:
   ./venv/bin/python -c "
   from playwright.sync_api import sync_playwright
   with sync_playwright() as p:
       b = p.chromium.launch()
       print('Chromium OK')
       b.close()
   "
