"""
playwright_adapter.py — Browser-based scraper for iCIMS, Taleo, PageUp.
"""

import re
from playwright.sync_api import sync_playwright


TIMEOUT = 25000
RENDER_WAIT = 4000


def _scrape_pageup(page, url):
    page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(RENDER_WAIT)
    for _ in range(10):
        try:
            btn = page.locator("a:has-text('Show More'), button:has-text('Show More')")
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(1500)
            else:
                break
        except Exception:
            break

    return page.evaluate("""() => {
        const results = [];
        const seen = new Set();
        document.querySelectorAll('a[href*="/job/"]').forEach(a => {
            const href = a.href || '';
            const match = href.match(/\\/job\\/(\\d+)/);
            if (!match) return;
            const jobId = match[1];
            if (seen.has(jobId)) return;
            seen.add(jobId);
            const title = a.textContent.trim();
            if (!title || title.length < 3 || title.includes('Send me jobs')) return;
            let location = '', posted_at = '';
            const container = a.closest('tr, li, div, article');
            if (container) {
                const lines = container.innerText.split('\\n').map(s => s.trim()).filter(s => s.length > 0 && s !== title);
                for (const line of lines) {
                    if (line.match(/^\\d{1,2}\\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)/i)) {
                        posted_at = line;
                    } else if (!location && line.length < 100 && !line.includes('Send me') && !line.includes('email')) {
                        location = line;
                    }
                }
            }
            results.push({ext_id: jobId, title, location, url: href, posted_at, department: ''});
        });
        return results;
    }""")


def _scrape_icims(page, url):
    base_match = re.match(r"(https?://[\w.-]+\.icims\.com)", url)
    if not base_match:
        return []
    base = base_match.group(1)
    search_url = base + "/jobs/search?ss=1"
    page.goto(search_url, timeout=TIMEOUT, wait_until="networkidle")
    page.wait_for_timeout(RENDER_WAIT)

    final_url = page.url
    is_react_spa = not final_url.startswith(base.replace("http://", "https://"))

    if is_react_spa:
        # Modern iCIMS Talent Cloud (Angular SPA on a different domain)
        page.wait_for_timeout(3000)
        return page.evaluate("""() => {
            const results = [];
            const seen = new Set();
            const titleLinks = document.querySelectorAll('a.job-title-link, a[itemprop="url"]');
            titleLinks.forEach(a => {
                const href = a.href || '';
                const m = href.match(/\\/jobs\\/(\\d+)/);
                if (!m) return;
                const jobId = m[1];
                if (seen.has(jobId)) return;
                seen.add(jobId);
                const titleSpan = a.querySelector('span[itemprop="title"]');
                let title = titleSpan ? titleSpan.textContent.trim() : a.textContent.trim();
                if (!title || title === 'Apply now' || title === 'Read More' || title.match(/\\d+ result/)) return;
                // Walk up to the job card container
                let card = a;
                for (let i = 0; i < 8; i++) { card = card.parentElement; if (!card) break; }
                let location = '';
                if (card) {
                    const text = card.innerText || '';
                    const locMatch = text.match(/Location\\n([^\\n]+?)\\n([^\\n]+?)\\n/);
                    if (locMatch) {
                        location = locMatch[1].trim();
                        if (locMatch[2] && !locMatch[2].match(/^(Category|Apply|Req)/)) {
                            location += ', ' + locMatch[2].trim();
                        }
                    }
                }
                let jobUrl = href.split('?')[0];
                results.push({ext_id: jobId, title: title, location: location, url: jobUrl, posted_at: '', department: ''});
            });
            // Fallback if no .job-title-link found
            if (results.length === 0) {
                document.querySelectorAll('a').forEach(a => {
                    const href = a.href || '';
                    const m = href.match(/\\/jobs\\/(\\d+)/);
                    if (!m) return;
                    const jobId = m[1];
                    if (seen.has(jobId)) return;
                    seen.add(jobId);
                    let title = a.textContent.trim();
                    if (!title || title.length < 3 || title === 'Apply now' || title === 'Read More') return;
                    results.push({ext_id: jobId, title: title, location: '', url: href.split('?')[0], posted_at: '', department: ''});
                });
            }
            return results;
        }""")

    # Legacy iCIMS: iframe-based search
    frame = page.frames[1] if len(page.frames) > 1 else page.main_frame
    try:
        btn = frame.locator('input[type="submit"], button[type="submit"]')
        if btn.count() > 0:
            btn.first.click()
            page.wait_for_timeout(4000)
    except Exception:
        pass

    jobs = frame.evaluate("""(base) => {
        const results = [];
        const seen = new Set();
        document.querySelectorAll('a').forEach(a => {
            const href = a.href || '';
            const match = href.match(/\\/jobs\\/(\\d+)/);
            if (!match) return;
            const jobId = match[1];
            if (seen.has(jobId)) return;
            seen.add(jobId);
            let title = '';
            const row = a.closest('tr, li, div.row, div[class*="list"]');
            if (row) {
                const titleEl = row.querySelector('.iCIMS_Anchor, [class*="title"] a, h2, h3, h4');
                if (titleEl) title = titleEl.textContent.trim();
            }
            if (!title) title = a.textContent.trim();
            title = title.split('\\n').map(s => s.trim()).filter(s => s && s !== 'Title' && s !== 'Location' && s !== 'Date' && s.length > 2)[0] || '';
            if (!title || title.includes('Skip') || title.includes('Search') || title.includes('Welcome')) return;
            let location = '';
            if (row) {
                const lines = row.innerText.split('\\n').map(s => s.trim()).filter(s => s.length > 1 && s !== title);
                for (const line of lines) {
                    if (line.match(/[A-Z]{2}-[A-Z]{2}-/) || line.match(/,\\s+[A-Z]{2}/) || line.match(/Remote/i) || line.match(/^US-/)) {
                        location = line.replace(/^US-/, '').replace(/-/g, ', ');
                        break;
                    }
                }
            }
            let jobUrl = href.split('?')[0];
            if (!jobUrl.startsWith('http')) jobUrl = base + jobUrl;
            results.push({ext_id: jobId, title: title, location: location, url: jobUrl, posted_at: '', department: ''});
        });
        return results;
    }""", base)
    return jobs



def _scrape_taleo(page, url):
    page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    # Check if we landed on a login/SSO page (not a job board)
    if "login" in page.url.lower() or "sso" in page.url.lower() or "idp/" in page.url.lower():
        return []

    # Try to click "Search for Jobs" button (classic Taleo UI)
    try:
        btn = page.locator("[id='basicSearchFooterInterface.searchAction'], input[value*='Search'][type='submit'], button:has-text('Search')")
        if btn.count() > 0 and btn.first.is_visible():
            btn.first.click()
            page.wait_for_timeout(5000)
    except Exception:
        pass

    # Extraction JS — handles alphanumeric job IDs + both table and div layouts
    _extract_js = """() => {
        const results = [];
        const seen = new Set();
        document.querySelectorAll('a').forEach(a => {
            const href = a.href || '';
            if (!href.includes('jobdetail') && !href.includes('requisition') && !href.includes('Job_')) return;
            const title = a.textContent.trim();
            if (!title || title.length < 5) return;
            let jobId = '';
            const m = href.match(/job=([A-Za-z0-9_]+)/) || href.match(/Job_([A-Za-z0-9_]+)/) || href.match(/requisitionId=([A-Za-z0-9_]+)/);
            if (m) jobId = m[1];
            else return;
            if (seen.has(jobId)) return;
            seen.add(jobId);

            let location = '';
            // Table-based layout (classic Taleo)
            const row = a.closest('tr');
            if (row) {
                const cells = row.querySelectorAll('td');
                for (let i = 0; i < cells.length; i++) {
                    const t = cells[i].textContent.trim();
                    if (t && t !== title && t.length < 60) {
                        if (!location) location = t;
                    }
                }
            }
            // Div-based layout (newer Taleo) — look for location in sibling/parent spans
            if (!location) {
                const parent = a.closest('div[class*="job"], li, article') || a.parentElement;
                if (parent) {
                    const spans = parent.querySelectorAll('span, div');
                    for (const s of spans) {
                        const t = s.textContent.trim();
                        if (t && t !== title && t.length > 3 && t.length < 60 && /[A-Z]{2}/.test(t)) {
                            location = t; break;
                        }
                    }
                }
            }
            results.push({ext_id: jobId, title, location, url: href, posted_at: '', department: ''});
        });
        return results;
    }"""

    # Check main frame first, then child frames (some Taleo sites load via AJAX iframe)
    results = page.evaluate(_extract_js)
    if not results:
        for frame in page.frames[1:]:
            try:
                results = frame.evaluate(_extract_js)
                if results:
                    break
            except Exception:
                pass
    return results


SCRAPERS = {"pageup": _scrape_pageup, "icims": _scrape_icims, "taleo": _scrape_taleo}


def scrape_batch(companies):
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        for c in companies:
            cname = c["company_name"]
            scraper = SCRAPERS.get(c["ats"])
            if not scraper or not c["endpoint"]:
                results[cname] = {"status": "skip", "jobs": []}
                continue
            try:
                ctx = browser.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36", viewport={"width": 1280, "height": 720})
                pg = ctx.new_page()
                jobs = scraper(pg, c["endpoint"])
                pg.close()
                ctx.close()
                results[cname] = {"status": "ok" if jobs else "empty", "jobs": jobs}
                print(f"  [playwright] {cname}: {len(jobs)} jobs")
            except Exception as e:
                results[cname] = {"status": f"err:{e}", "jobs": []}
                print(f"  [playwright] {cname}: ERROR {e}")
        browser.close()
    return results


def scrape_one(endpoint, ats_type):
    scraper = SCRAPERS.get(ats_type)
    if not scraper:
        return []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36", viewport={"width": 1280, "height": 720})
        pg = ctx.new_page()
        try:
            jobs = scraper(pg, endpoint)
        except Exception as e:
            print(f"Error: {e}")
            jobs = []
        pg.close()
        ctx.close()
        browser.close()
        return jobs
