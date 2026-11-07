#!/usr/bin/env python3
"""
discover_candidate_portals.py — Autonomous ATS Portal Discovery Engine.

Harvests uncataloged hiring employers from all_jobs.json, prioritizes them by job
volume, tests them against candidate ATS APIs (Greenhouse, Ashby, Lever, Rippling,
and Workday clusters), and automatically registers genuine career portals into
companies.json.

Includes a negative cache (probed_companies_cache.json) to ensure uncataloged
or non-standard companies are only tested once every 30 days, avoiding redundant
requests or workflow slowdowns.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse

# Ensure UTF-8 console output
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ALL_JOBS_FILE = os.path.join(SCRIPT_DIR, "all_jobs.json")
COMPANIES_FILE = os.path.join(SCRIPT_DIR, "companies.json")
CACHE_FILE = os.path.join(SCRIPT_DIR, "probed_companies_cache.json")
ALIASES_FILE = os.path.join(SCRIPT_DIR, "company_aliases.json")

from company_directory import (
    load_company_directory,
    save_company_directory,
    record_company,
    normalize_company_key,
    prune_excluded_companies,
)

try:
    from scrape_jobs import is_excluded_company
except ImportError:
    def is_excluded_company(name: str) -> bool:
        return False

from resolve_ats import (
    slugify_company,
    check_greenhouse_board,
    check_ashby_board,
    check_lever_board,
    check_rippling_board,
    title_similarity,
)
from audit_workday_portals import test_workday_endpoint

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

NEGATIVE_CACHE_DAYS = 30


def load_probe_cache() -> dict:
    """Loads the negative/probed history cache."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_probe_cache(cache: dict):
    """Persists the probe history cache."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"[Warning] Failed to save probe cache: {e}", flush=True)


def harvest_candidates(max_days: int = 14) -> list[dict]:
    """
    Harvests uncataloged companies from all_jobs.json seen within max_days.
    Returns list of dicts: {'company': name, 'key': comp_key, 'job_count': count, 'sample_titles': [...]}
    sorted by job_count descending.
    """
    if not os.path.exists(ALL_JOBS_FILE):
        print(f"[Warning] all_jobs.json not found at {ALL_JOBS_FILE}", flush=True)
        return []

    with open(ALL_JOBS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    jobs = data.get("jobs", [])
    comp_dir = load_company_directory()
    prune_excluded_companies(comp_dir)
    existing_companies = comp_dir.get("companies", {})
    probe_cache = load_probe_cache()

    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=max_days)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    counts: dict[str, dict] = {}

    for j in jobs:
        c_name = (j.get("company") or "").strip()
        if not c_name or c_name.lower() in ("unknown", "various", "confidential"):
            continue

        c_key = normalize_company_key(c_name)
        if not c_key or is_excluded_company(c_name) or is_excluded_company(c_key.replace("-", " ")):
            continue

        # Skip if already known in directory
        if c_key in existing_companies:
            continue

        # Check negative cache recency
        cache_entry = probe_cache.get(c_key)
        if cache_entry:
            probed_at = cache_entry.get("probed_at", "")
            if probed_at:
                try:
                    p_dt = datetime.strptime(probed_at[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - p_dt).days < NEGATIVE_CACHE_DAYS:
                        continue
                except Exception:
                    pass

        # Check job recency
        j_date = str(j.get("date_posted") or j.get("scraped_at") or "")[:10]
        if j_date and j_date < cutoff_date:
            continue

        if c_key not in counts:
            counts[c_key] = {
                "company": c_name,
                "key": c_key,
                "job_count": 0,
                "sample_titles": [],
            }
        counts[c_key]["job_count"] += 1
        t = (j.get("title") or "").strip()
        if t and len(counts[c_key]["sample_titles"]) < 3 and t not in counts[c_key]["sample_titles"]:
            counts[c_key]["sample_titles"].append(t)

    sorted_candidates = sorted(counts.values(), key=lambda x: x["job_count"], reverse=True)
    return sorted_candidates


def probe_company_portals(company_name: str, job_count: int = 1) -> tuple[str, str, int]:
    """
    Attempts to discover genuine ATS career portal for a company.
    Checks fast ATS platforms first, then Workday clusters if job_count >= 2.
    Returns (ats_platform, root_career_url, total_jobs).
    """
    slugs = slugify_company(company_name)
    if not slugs:
        return "", "", 0

    # Expand slugs with common corporate suffixes
    expanded_slugs = list(slugs)
    clean_base = re.sub(r"[^a-zA-Z0-9]", "", company_name.lower())
    for sfx in ("usa", "careers", "-careers", "jobs"):
        cand = f"{clean_base}{sfx}"
        if cand not in expanded_slugs and len(cand) <= 30:
            expanded_slugs.append(cand)

    # 1. Fast ATS probes: Greenhouse, Ashby, Lever, Rippling
    t_clean = re.sub(r"[^a-zA-Z0-9]", "", company_name.lower())

    for slug in expanded_slugs[:5]:
        # Greenhouse with board name verification
        try:
            gh_info_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}"
            req = urllib.request.Request(gh_info_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=3.0) as res:
                b_data = json.loads(res.read().decode("utf-8"))
                b_name = b_data.get("name", "").strip()
                b_clean = re.sub(r"[^a-zA-Z0-9]", "", b_name.lower())
                s_clean = re.sub(r"[^a-zA-Z0-9]", "", slug.lower())
                if t_clean in b_clean or b_clean in t_clean or s_clean in b_clean or title_similarity(company_name, b_name) >= 0.35:
                    gh_jobs = check_greenhouse_board(slug)
                    if gh_jobs and len(gh_jobs) > 0:
                        return "Greenhouse", f"https://job-boards.greenhouse.io/{slug}", len(gh_jobs)
        except Exception:
            pass

        # Ashby
        ashby_jobs = check_ashby_board(slug)
        if ashby_jobs and len(ashby_jobs) > 0:
            return "Ashby", f"https://jobs.ashbyhq.com/{slug}", len(ashby_jobs)

        # Lever
        lever_jobs = check_lever_board(slug)
        if lever_jobs and len(lever_jobs) > 0:
            return "Lever", f"https://jobs.lever.co/{slug}", len(lever_jobs)

        # Rippling
        rip_jobs = check_rippling_board(slug)
        if rip_jobs and len(rip_jobs) > 0:
            return "Rippling", f"https://ats.rippling.com/{slug}/jobs", len(rip_jobs)

    # 2. Workday Probes (Only for companies with >= 2 tech jobs or enterprise footprint)
    if job_count >= 2:
        clusters = ["wd1", "wd5", "wd12", "wd3", "wd108"]
        candidate_tenants = expanded_slugs[:2]
        
        for tenant in candidate_tenants:
            workday_sites = [
                "External",
                "Careers",
                "External_Careers",
                "jobs",
                "Search",
            ]
            for s in expanded_slugs[:3]:
                if s not in workday_sites:
                    workday_sites.append(s)
                c_site = f"{s}_Careers"
                if c_site not in workday_sites:
                    workday_sites.append(c_site)

            for cl in clusters:
                netloc = f"{tenant}.{cl}.myworkdayjobs.com"
                for site in workday_sites:
                    is_valid, total, sample = test_workday_endpoint(netloc, tenant, site)
                    if is_valid and total > 0:
                        return "Workday", f"https://{netloc}/{site}", total

    return "", "", 0


def run_discovery(limit: int = 30, max_workers: int = 8) -> dict:
    """Runs candidate portal discovery on top uncataloged employers."""
    print("======================================================================")
    print("🔍 AUTONOMOUS ATS PORTAL DISCOVERY ENGINE")
    print(f"   Batch Limit      : {limit} companies")
    print(f"   Parallel Workers : {max_workers}")
    print("======================================================================")

    candidates = harvest_candidates(max_days=14)
    print(f"[*] Harvested {len(candidates)} uncataloged candidate companies from active jobs.")

    batch = candidates[:limit]
    if not batch:
        print("[OK] No eligible candidates to probe. Everything up-to-date.")
        return {"discovered": 0, "probed": 0}

    print(f"[*] Probing top {len(batch)} candidates (Job count range: {batch[-1]['job_count']} to {batch[0]['job_count']} jobs):")

    comp_dir = load_company_directory()
    probe_cache = load_probe_cache()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    discovered = 0
    results = []

    def _worker(cand):
        name = cand["company"]
        c_key = cand["key"]
        cnt = cand["job_count"]
        ats, root_url, total = probe_company_portals(name, job_count=cnt)
        return cand, ats, root_url, total

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_worker, c): c for c in batch}
        for future in as_completed(futures):
            cand, ats, root_url, total = future.result()
            c_name = cand["company"]
            c_key = cand["key"]
            cnt = cand["job_count"]

            if ats and root_url:
                discovered += 1
                record_company(c_name, root_url, ats_hint=ats, directory_data=comp_dir)
                probe_cache[c_key] = {
                    "name": c_name,
                    "status": "found",
                    "ats": ats,
                    "career_url": root_url,
                    "probed_at": today,
                    "total_jobs": total,
                }
                print(f"  ✅ [FOUND {ats:<10}] {c_name:<30} -> {root_url} ({total} active jobs)", flush=True)
            else:
                probe_cache[c_key] = {
                    "name": c_name,
                    "status": "not_found",
                    "probed_at": today,
                    "jobs_seen": cnt,
                }
                print(f"  ❌ [NOT FOUND]     {c_name:<30} ({cnt} jobs in queue - cached for 30d)", flush=True)

    # Save updates
    if discovered > 0:
        prune_excluded_companies(comp_dir)
        save_company_directory(comp_dir)
        print(f"\n🎉 Successfully added {discovered} new company portals to companies.json!")

    save_probe_cache(probe_cache)
    print(f"[OK] Probe cache updated ({len(probe_cache)} total cached entries).")
    return {"discovered": discovered, "probed": len(batch)}


def main():
    parser = argparse.ArgumentParser(description="Autonomous ATS Portal Discovery")
    parser.add_argument("--limit", type=int, default=30, help="Max candidates to probe this run")
    parser.add_argument("--workers", type=int, default=8, help="Parallel worker threads")
    args = parser.parse_args()

    run_discovery(limit=args.limit, max_workers=args.workers)


if __name__ == "__main__":
    main()
