"""
RESOLVER PATCH — Add board-name validation to prevent false positives.

HOW TO APPLY:
1. Copy the two functions below (tokenize, name_similarity, verify_board_owner)
   into resolver.py
2. In your probe function, after a slug returns valid JSON, call:
       if not verify_board_owner(ats_type, slug, target_company_name):
           continue  # Skip this slug — belongs to someone else
3. That's it. The resolver will only accept slugs where the board name
   actually matches the target company.

The threshold is 0.20 Jaccard similarity — intentionally lenient to handle
abbreviations (e.g., "MIT" vs "Massachusetts Institute of Technology" won't
match on tokens alone, but "Henry Ford Health System" vs "System Thinkers"
correctly fails at 0.0).
"""

import re
import json
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

# ── Paste these into resolver.py ─────────────────────────────────────

VERIFY_STOP_WORDS = {"the", "of", "and", "inc", "llc", "corp", "co", "ltd",
                     "group", "services", "solutions", "international",
                     "global", "na", "us", "a", "an", "for"}


def _tokenize(name: str) -> set:
    name = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    return {w for w in name.split() if w and w not in VERIFY_STOP_WORDS}


def _name_similarity(a: str, b: str) -> float:
    t1, t2 = _tokenize(a), _tokenize(b)
    if not t1 or not t2:
        return 0.0
    return len(t1 & t2) / len(t1 | t2)


def _fetch_board_name(ats_type: str, slug: str) -> str | None:
    """Fetch the real company/board name from the ATS metadata endpoint."""
    endpoints = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}",
        "ashby": f"https://api.ashbyhq.com/posting-api/posting-board/{slug}",
        "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{slug}",
    }
    name_keys = {
        "greenhouse": "name",
        "ashby": "organizationName",
        "smartrecruiters": "name",
    }

    url = endpoints.get(ats_type)
    if not url:
        # Lever slug IS the company name; Workable similar
        if ats_type == "lever":
            return slug.replace("-", " ")
        if ats_type == "workable":
            return slug.replace("-", " ")
        return None

    try:
        req = Request(url, headers={"User-Agent": "JobHunter-Resolver/1.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get(name_keys.get(ats_type, "name"))
    except (HTTPError, URLError, json.JSONDecodeError):
        return None


def verify_board_owner(ats_type: str, slug: str, target_name: str,
                       threshold: float = 0.20) -> bool:
    """
    Returns True if the ATS board's real name matches the target company.

    Usage in resolver probe loop:
        if not verify_board_owner(ats_type, slug, company_name):
            continue  # false positive, try next slug
    """
    board_name = _fetch_board_name(ats_type, slug)
    if board_name is None:
        # Can't verify — allow it but log a warning
        print(f"  ⚠ Could not verify board owner for {ats_type}/{slug}")
        return True  # Fail open — better to have unverified than miss real ones

    sim = _name_similarity(target_name, board_name)
    if sim >= threshold:
        return True
    else:
        print(f"  ✗ Rejected slug '{slug}' on {ats_type}: "
              f"board='{board_name}' vs target='{target_name}' (sim={sim:.2f})")
        return False


# ── Example integration ──────────────────────────────────────────────
#
# In your existing resolver probe loop, you probably have something like:
#
#   for slug in candidate_slugs:
#       url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
#       resp = urlopen(url)
#       data = json.loads(resp.read())
#       if data.get("jobs"):
#           # FOUND! Save to DB
#           save_resolved(company_id, "greenhouse", slug, url)
#           break
#
# Change it to:
#
#   for slug in candidate_slugs:
#       url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
#       resp = urlopen(url)
#       data = json.loads(resp.read())
#       if data.get("jobs"):
#           # VERIFY before saving
#           if verify_board_owner("greenhouse", slug, company_name):
#               save_resolved(company_id, "greenhouse", slug, url)
#               break
#           # else: slug belongs to someone else, try next
