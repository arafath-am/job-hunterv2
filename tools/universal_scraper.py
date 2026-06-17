#!/usr/bin/env python3
"""
Universal job scraper for Job Hunter v2.

For companies with career URLs but no supported ATS adapter, this script
tries multiple extraction strategies in priority order:

1. JSON-LD structured data (JobPosting schema)
2. Sitemap.xml parsing for job URLs
3. New ATS platform fingerprinting (BrassRing, Phenom, Jobvite, etc.)
4. Heuristic DOM extraction (repeated link patterns)

Usage:
    python3 tools/universal_scraper.py                    # Probe all unknown
    python3 tools/universal_scraper.py --limit 20         # Test with 20
    python3 tools/universal_scraper.py --extract           # Probe + extract jobs
    python3 tools/universal_scraper.py --dry-run           # No DB writes
"""

import argparse
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser

# ── Config ──────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "jobhunter.db")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
REQUEST_TIMEOUT = 15
FETCH_DELAY = 0.4  # seconds between requests


# ── HTTP helpers ────────────────────────────────────────────────────────

def fetch(url, timeout=REQUEST_TIMEOUT):
    """GET with UA. Returns (status, body, final_url) following redirects."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body, resp.url
    except urllib.error.HTTPError as e:
        return e.code, "", url
    except Exception as e:
        return 0, str(e), url


def fetch_bytes(url, timeout=REQUEST_TIMEOUT):
    """GET returning raw bytes for XML parsing."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.url
    except:
        return 0, b"", url


# ── Strategy 1: JSON-LD Extraction ──────────────────────────────────────

class JsonLdExtractor(HTMLParser):
    """Extracts JSON-LD blocks from HTML."""
    def __init__(self):
        super().__init__()
        self._in_jsonld = False
        self._data_parts = []
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            attr_dict = dict(attrs)
            if attr_dict.get("type") == "application/ld+json":
                self._in_jsonld = True
                self._data_parts = []

    def handle_data(self, data):
        if self._in_jsonld:
            self._data_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            raw = "".join(self._data_parts).strip()
            if raw:
                try:
                    self.blocks.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass


def extract_jsonld_jobs(html, base_url=""):
    """
    Parse JSON-LD from HTML and extract JobPosting entries.
    Returns list of {title, location, url, posted_at, ext_id}.
    """
    parser = JsonLdExtractor()
    try:
        parser.feed(html)
    except:
        return []

    jobs = []
    seen_urls = set()

    def _process_item(item):
        if not isinstance(item, dict):
            return
        item_type = item.get("@type", "")
        # Handle arrays of types
        if isinstance(item_type, list):
            item_type = " ".join(item_type)

        if "JobPosting" in item_type:
            title = item.get("title", "")
            url = item.get("url", "")

            # Location extraction
            location = ""
            loc_data = item.get("jobLocation")
            if isinstance(loc_data, dict):
                addr = loc_data.get("address", {})
                if isinstance(addr, dict):
                    parts = [
                        addr.get("addressLocality", ""),
                        addr.get("addressRegion", ""),
                        addr.get("addressCountry", ""),
                    ]
                    location = ", ".join(p for p in parts if p)
                elif isinstance(addr, str):
                    location = addr
            elif isinstance(loc_data, list) and loc_data:
                # Multiple locations — take first
                first = loc_data[0]
                if isinstance(first, dict):
                    addr = first.get("address", {})
                    if isinstance(addr, dict):
                        parts = [addr.get("addressLocality", ""), addr.get("addressRegion", "")]
                        location = ", ".join(p for p in parts if p)

            posted_at = item.get("datePosted", "")
            identifier = item.get("identifier", {})
            ext_id = ""
            if isinstance(identifier, dict):
                ext_id = str(identifier.get("value", ""))
            elif isinstance(identifier, str):
                ext_id = identifier

            # Generate ext_id from URL if not present
            if not ext_id and url:
                # Try to extract job ID from URL path
                m = re.search(r'/(\d{4,})', url)
                if m:
                    ext_id = m.group(1)

            if title and url and url not in seen_urls:
                seen_urls.add(url)
                jobs.append({
                    "title": title.strip(),
                    "location": location.strip(),
                    "url": url.strip(),
                    "posted_at": posted_at,
                    "ext_id": ext_id or "",
                })

        # Handle @graph arrays
        if "@graph" in item:
            for sub in item["@graph"]:
                _process_item(sub)

        # Handle itemListElement (some sites wrap jobs in lists)
        if "itemListElement" in item:
            for elem in item["itemListElement"]:
                if isinstance(elem, dict):
                    _process_item(elem.get("item", elem))

    for block in parser.blocks:
        if isinstance(block, list):
            for item in block:
                _process_item(item)
        else:
            _process_item(block)

    return jobs


# ── Strategy 2: Sitemap Parsing ─────────────────────────────────────────

def discover_sitemap_jobs(base_url):
    """
    Check sitemap.xml for job-related URLs.
    Returns list of {url} or empty list.
    """
    parsed = urllib.parse.urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # Try common sitemap locations
    sitemap_urls = [
        f"{base}/sitemap.xml",
        f"{base}/sitemap_index.xml",
        f"{base}/sitemap-jobs.xml",
        f"{base}/jobs/sitemap.xml",
        f"{base}/careers/sitemap.xml",
    ]

    job_urls = []
    job_patterns = re.compile(
        r'/(job|position|opening|career|posting|vacancy|requisition|opportunity)s?/',
        re.IGNORECASE
    )

    for sitemap_url in sitemap_urls:
        status, body, _ = fetch_bytes(sitemap_url)
        if status != 200 or not body:
            continue

        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            continue

        # Handle namespace
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        # Check if this is a sitemap index
        sitemaps = root.findall(f".//{ns}sitemap/{ns}loc")
        if sitemaps:
            # It's an index — look for job-related child sitemaps
            for sm in sitemaps:
                sm_url = sm.text.strip() if sm.text else ""
                if sm_url and job_patterns.search(sm_url):
                    # Fetch this child sitemap
                    s2, b2, _ = fetch_bytes(sm_url)
                    if s2 == 200 and b2:
                        try:
                            r2 = ET.fromstring(b2)
                            ns2 = ""
                            if r2.tag.startswith("{"):
                                ns2 = r2.tag.split("}")[0] + "}"
                            for loc in r2.findall(f".//{ns2}url/{ns2}loc"):
                                if loc.text:
                                    job_urls.append(loc.text.strip())
                        except ET.ParseError:
                            pass
            if job_urls:
                break

        # Regular sitemap — filter for job URLs
        for loc in root.findall(f".//{ns}url/{ns}loc"):
            url_text = loc.text.strip() if loc.text else ""
            if url_text and job_patterns.search(url_text):
                job_urls.append(url_text)

        if job_urls:
            break

    return job_urls


# ── Strategy 3: New ATS Fingerprinting ──────────────────────────────────

# Additional ATS platforms not covered by the main resolver
NEW_ATS_PATTERNS = [
    # (url_pattern, ats_name, can_scrape_with_existing_adapters)
    (r'brassring\.com', 'brassring', False),
    (r'\.silkroad\.com', 'silkroad', False),
    (r'phenom\.com|phenompeople\.com', 'phenom', False),
    (r'jobvite\.com', 'jobvite', False),
    (r'applicantpro\.com', 'applicantpro', False),
    (r'avature\.net', 'avature', False),
    (r'peopleadmin\.com', 'peopleadmin', False),
    (r'catsone\.com', 'catsone', False),
    (r'ultipro\.com|ukg\.net', 'ultipro', False),
    (r'paylocity\.com', 'paylocity', False),
    (r'paycomonline\.net', 'paycom', False),
    (r'adp\.com/recruit|recruiting\.adp\.com', 'adp', False),
    (r'myworkday\.com', 'workday', True),  # alternate Workday domain
    (r'selectminds\.com|referrals\.selectminds', 'selectminds', False),
    (r'dayforce\.com|ceridian\.com', 'dayforce', False),
    (r'bamboohr\.com', 'bamboohr', False),
    (r'jazz\.co|applytojob\.com', 'jazzhr', False),
    (r'recruitee\.com', 'recruitee', False),
    (r'pinpointhq\.com', 'pinpoint', False),
]


def fingerprint_new_ats(url, html=""):
    """
    Check URL and HTML content for new ATS patterns.
    Returns (ats_name, can_use_existing) or (None, False).
    """
    for pattern, ats_name, can_use in NEW_ATS_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return ats_name, can_use

    # Check HTML content for ATS signatures
    if html:
        html_lower = html[:50000].lower()  # check first 50KB
        html_signatures = [
            ('phenom', r'phenom-careers|phenom\.com|powered by phenom'),
            ('jobvite', r'jobvite\.com|app\.jobvite'),
            ('avature', r'avature\.net'),
            ('bamboohr', r'bamboohr\.com'),
            ('jazzhr', r'applytojob\.com|jazz\.co'),
            ('recruitee', r'recruitee\.com'),
            ('breezy', r'breezy\.hr'),
            ('lever', r'jobs\.lever\.co|lever-jobs-container'),
            ('greenhouse', r'boards\.greenhouse\.io|greenhouse-job-board'),
            ('workday', r'myworkdayjobs\.com|workday'),
        ]
        for ats_name, pattern in html_signatures:
            if re.search(pattern, html_lower):
                return ats_name, ats_name in ('lever', 'greenhouse', 'workday')

    return None, False


# ── Strategy 4: Heuristic DOM Extraction ────────────────────────────────

class LinkExtractor(HTMLParser):
    """Extract all links from HTML for heuristic job detection."""
    def __init__(self):
        super().__init__()
        self.links = []  # (href, text)
        self._in_a = False
        self._current_href = ""
        self._current_text_parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attr_dict = dict(attrs)
            href = attr_dict.get("href", "")
            if href:
                self._in_a = True
                self._current_href = href
                self._current_text_parts = []

    def handle_data(self, data):
        if self._in_a:
            self._current_text_parts.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            self._in_a = False
            text = " ".join(self._current_text_parts).strip()
            if text and self._current_href:
                self.links.append((self._current_href, text))


def heuristic_extract_jobs(html, base_url):
    """
    Find job listing links by looking for repeated URL patterns
    that look like job detail pages.
    Returns list of {title, url, ext_id}.
    """
    parser = LinkExtractor()
    try:
        parser.feed(html)
    except:
        return []

    # Filter links that look like job pages
    job_link_patterns = re.compile(
        r'/(job|position|opening|career|posting|vacancy|requisition|opportunity|role)s?/'
        r'|/job[_-]?detail|/job[_-]?view|/posting/|/requisition/'
        r'|[?&](id|jobId|requisitionId|positionId)=',
        re.IGNORECASE
    )

    parsed_base = urllib.parse.urlparse(base_url)
    base_domain = parsed_base.netloc

    candidates = []
    seen = set()

    for href, text in parser.links:
        # Make absolute URL
        if href.startswith("/"):
            href = f"{parsed_base.scheme}://{base_domain}{href}"
        elif not href.startswith("http"):
            continue

        # Must be same domain or known career subdomain
        href_domain = urllib.parse.urlparse(href).netloc
        if base_domain not in href_domain and href_domain not in base_domain:
            continue

        # Skip non-job links
        if any(skip in href.lower() for skip in [
            'login', 'signup', 'register', 'privacy', 'terms', 'faq',
            'about', 'contact', 'blog', '.pdf', '.png', '.jpg',
            'linkedin.com', 'facebook.com', 'twitter.com',
            '#', 'javascript:', 'mailto:'
        ]):
            continue

        # Check if it matches job URL patterns
        if job_link_patterns.search(href):
            # Filter out navigation/menu text
            if len(text) > 3 and len(text) < 200 and text.lower() not in (
                'apply', 'apply now', 'view all', 'see all', 'more',
                'next', 'previous', 'back', 'home', 'search',
                'view all jobs', 'see all jobs', 'browse all',
            ):
                if href not in seen:
                    seen.add(href)
                    # Try to extract job ID
                    ext_id = ""
                    m = re.search(r'/(\d{4,})', href)
                    if m:
                        ext_id = m.group(1)
                    else:
                        m = re.search(r'[?&](?:id|jobId|requisitionId)=(\w+)', href)
                        if m:
                            ext_id = m.group(1)

                    candidates.append({
                        "title": text,
                        "url": href,
                        "ext_id": ext_id,
                        "location": "",  # can't reliably get from link text alone
                    })

    return candidates


# ── Orchestrator ────────────────────────────────────────────────────────

def probe_company(company_name, brand, careers_url):
    """
    Try all strategies against a single company's career URL.
    Returns dict with strategy results.
    """
    result = {
        "company": company_name,
        "brand": brand,
        "careers_url": careers_url,
        "strategy": None,
        "ats_detected": None,
        "job_count": 0,
        "jobs": [],
        "notes": "",
    }

    if not careers_url:
        result["notes"] = "no career URL"
        return result

    # Normalize URL
    if not careers_url.startswith("http"):
        careers_url = "https://" + careers_url

    parsed = urllib.parse.urlparse(careers_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # ── Strategy 1: JSON-LD ──
    print(f"    [1/4] JSON-LD...", end=" ", flush=True)
    status, html, final_url = fetch(careers_url)
    if status == 200 and html:
        jobs = extract_jsonld_jobs(html, base)
        if jobs:
            print(f"✅ {len(jobs)} jobs")
            result["strategy"] = "jsonld"
            result["job_count"] = len(jobs)
            result["jobs"] = jobs
            return result
        else:
            print("none", end="")
    else:
        print(f"HTTP {status}", end="")
        html = ""

    # ── Strategy 3: New ATS fingerprint (check before sitemap — faster) ──
    print(f" → [2/4] ATS fingerprint...", end=" ", flush=True)
    ats, can_use = fingerprint_new_ats(careers_url, html)
    if ats:
        print(f"🔖 {ats}" + (" (supported)" if can_use else ""))
        result["ats_detected"] = ats
        result["notes"] = f"uses {ats}" + (" — could use existing adapter" if can_use else " — new adapter needed")
        # Don't return yet — still try sitemap and heuristic for job counts

    else:
        print("none", end="")

    # ── Strategy 2: Sitemap ──
    print(f" → [3/4] Sitemap...", end=" ", flush=True)
    time.sleep(FETCH_DELAY)
    sitemap_jobs = discover_sitemap_jobs(careers_url)
    if sitemap_jobs:
        print(f"✅ {len(sitemap_jobs)} URLs")
        result["strategy"] = "sitemap"
        result["job_count"] = len(sitemap_jobs)
        result["jobs"] = [{"url": u, "title": "", "ext_id": "", "location": ""} for u in sitemap_jobs[:500]]
        return result
    else:
        print("none", end="")

    # ── Strategy 4: Heuristic DOM ──
    print(f" → [4/4] Heuristic DOM...", end=" ", flush=True)
    if html:
        heuristic_jobs = heuristic_extract_jobs(html, careers_url)
        if heuristic_jobs:
            print(f"✅ {len(heuristic_jobs)} links")
            result["strategy"] = "heuristic"
            result["job_count"] = len(heuristic_jobs)
            result["jobs"] = heuristic_jobs
            return result
        else:
            print("none")
    else:
        print("skipped (no HTML)")

    if not result["strategy"] and result["ats_detected"]:
        result["strategy"] = "ats_only"

    return result


# ── DB operations ───────────────────────────────────────────────────────

def get_unknown_companies(db_path, limit=None):
    """Get identified companies with unknown ATS but with career URLs."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Get companies that have career URLs but no working ATS adapter
    q = """
        SELECT company_name, brand, careers_url, ats, cap_exempt, sponsor
        FROM companies
        WHERE resolve_status = 'identified'
          AND careers_url IS NOT NULL
          AND careers_url != ''
          AND (ats IS NULL OR ats NOT IN ('greenhouse','lever','ashby','smartrecruiters','workable','workday','jibe'))
        ORDER BY cap_exempt DESC, company_name
    """
    rows = conn.execute(q).fetchall()
    conn.close()
    if limit:
        rows = rows[:limit]
    return [dict(r) for r in rows]


def store_jobs(db_path, company_name, brand, cap_exempt, sponsor, jobs, strategy, dry_run=False):
    """Store extracted jobs in the jobs table."""
    if dry_run or not jobs:
        return 0

    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0

    for job in jobs:
        ext_id = job.get("ext_id") or job.get("url", "")[-80:]  # fallback to URL fragment
        try:
            conn.execute("""
                INSERT INTO jobs (ats, company, brand, cap_exempt, sponsor, ext_id, title, location, url, posted_at, first_seen, last_seen, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(ats, company, ext_id) DO UPDATE SET last_seen=?, active=1
            """, (
                strategy, company_name, brand, cap_exempt, sponsor,
                ext_id, job.get("title", ""), job.get("location", ""),
                job.get("url", ""), job.get("posted_at", ""),
                now, now, now
            ))
            inserted += 1
        except sqlite3.IntegrityError:
            pass
        except Exception as e:
            print(f"    DB error: {e}")

    conn.commit()
    conn.close()
    return inserted


def update_company_status(db_path, company_name, strategy, ats_detected, job_count, dry_run=False):
    """Update company resolve status based on probe results."""
    if dry_run:
        return

    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat()

    if strategy in ("jsonld", "sitemap", "heuristic") and job_count > 0:
        # Mark as resolved with the extraction strategy as the "ats" type
        conn.execute("""
            UPDATE companies SET resolve_status='resolved', ats=?, job_count=?, updated_at=?
            WHERE company_name=?
        """, (strategy, job_count, now, company_name))
    elif ats_detected:
        # Update the ATS field for future adapter development
        conn.execute("""
            UPDATE companies SET ats=?, updated_at=?
            WHERE company_name=?
        """, (ats_detected, now, company_name))

    conn.commit()
    conn.close()


# ── Report ──────────────────────────────────────────────────────────────

def print_report(results):
    """Print summary report."""
    total = len(results)
    by_strategy = {}
    by_ats = {}
    total_jobs = 0

    for r in results:
        s = r["strategy"] or "none"
        by_strategy[s] = by_strategy.get(s, 0) + 1
        if r["ats_detected"]:
            by_ats[r["ats_detected"]] = by_ats.get(r["ats_detected"], 0) + 1
        total_jobs += r["job_count"]

    print("\n" + "=" * 70)
    print("UNIVERSAL SCRAPER REPORT")
    print("=" * 70)
    print(f"Total probed:     {total}")
    print(f"Total jobs found: {total_jobs}")
    print()

    print("── By Extraction Strategy ──")
    for s, count in sorted(by_strategy.items(), key=lambda x: -x[1]):
        emoji = {"jsonld": "📋", "sitemap": "🗺️", "heuristic": "🔍", "ats_only": "🔖", "none": "❌"}.get(s, "?")
        print(f"  {emoji} {s:20} {count:>4} companies")
    print()

    if by_ats:
        print("── New ATS Platforms Detected ──")
        for ats, count in sorted(by_ats.items(), key=lambda x: -x[1]):
            print(f"  🔖 {ats:20} {count:>4} companies")
        print()

    # Top companies by job count
    with_jobs = [r for r in results if r["job_count"] > 0]
    if with_jobs:
        with_jobs.sort(key=lambda x: -x["job_count"])
        print("── Top Companies by Job Count ──")
        for r in with_jobs[:25]:
            print(f"  {r['brand'] or r['company']:40} {r['strategy']:10} {r['job_count']:>6} jobs")
    print()

    # Companies with no results
    no_result = [r for r in results if not r["strategy"]]
    if no_result:
        print(f"── No Strategy Found ({len(no_result)} companies) ──")
        for r in no_result[:20]:
            url = r.get("careers_url", "")[:50]
            print(f"  {r['brand'] or r['company']:40} {url}")
        if len(no_result) > 20:
            print(f"  ... and {len(no_result) - 20} more")

    print("=" * 70)


# ── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Universal job scraper")
    parser.add_argument("--limit", type=int, help="Max companies to probe")
    parser.add_argument("--extract", action="store_true", help="Store extracted jobs in DB")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--db", default=DB_PATH, help="Path to jobhunter.db")
    args = parser.parse_args()

    companies = get_unknown_companies(args.db, args.limit)
    total = len(companies)
    print(f"\n🌐 Universal scraper: {total} companies to probe")
    if args.dry_run:
        print("🔒 DRY RUN — no DB writes")
    if args.extract:
        print("📥 EXTRACT MODE — will store jobs in DB")
    print()

    results = []

    for i, company in enumerate(companies, 1):
        name = company["company_name"]
        brand = company.get("brand") or name
        url = company.get("careers_url", "")

        print(f"[{i}/{total}] {brand}")

        try:
            result = probe_company(name, brand, url)
            results.append(result)

            strategy = result["strategy"]
            job_count = result["job_count"]

            if strategy and job_count > 0:
                # Update company status
                update_company_status(
                    args.db, name, strategy,
                    result["ats_detected"], job_count,
                    dry_run=args.dry_run
                )

                # Store jobs if --extract flag
                if args.extract and result["jobs"]:
                    stored = store_jobs(
                        args.db, name, brand,
                        company.get("cap_exempt", 0),
                        company.get("sponsor", 0),
                        result["jobs"], strategy,
                        dry_run=args.dry_run
                    )
                    print(f"  💾 Stored {stored} jobs")

            time.sleep(FETCH_DELAY)

        except KeyboardInterrupt:
            print("\n\n⛔ Interrupted")
            break
        except Exception as e:
            print(f"  ⚠ Error: {e}")
            results.append({
                "company": name, "brand": brand, "careers_url": url,
                "strategy": None, "ats_detected": None,
                "job_count": 0, "jobs": [], "notes": str(e),
            })

    print_report(results)

    # Save detailed report
    report_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "universal_scraper_report.json")
    if not args.dry_run:
        report_data = []
        for r in results:
            report_data.append({
                "company": r["company"],
                "brand": r["brand"],
                "careers_url": r["careers_url"],
                "strategy": r["strategy"],
                "ats_detected": r["ats_detected"],
                "job_count": r["job_count"],
                "notes": r["notes"],
            })
        with open(report_path, "w") as f:
            json.dump({
                "run_at": datetime.now(timezone.utc).isoformat(),
                "total": len(results),
                "results": report_data,
            }, f, indent=2)
        print(f"\n📄 Report saved to {report_path}")


if __name__ == "__main__":
    main()
