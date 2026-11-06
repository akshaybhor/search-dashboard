#!/usr/bin/env python3
"""
generate_akshay_cloud_devops_queue.py — Targeted Queue Generator for Akshay Narayan Bhor.

Strategy:
1. Filters all_jobs.json for:
   - Role: Cloud/DevOps (matching cloud, devops, sre, site reliability, platform engineer, infrastructure)
   - Seniority: Senior (matching senior, sr, lead, staff)
   - Freshness: Posted/scraped within the last 24 hours (or configurable lookback)
   - Fit Score: NONE / SKIPPED (applies to ALL matching roles without AI score gatekeeping)
2. Resolves raw LinkedIn links directly to ATS endpoints (Workday, Greenhouse, Ashby, Lever, Universal)
   WITHOUT contacting LinkedIn.
3. Outputs structured queue to Daily/akshay_cloud_queue.json and Daily/akshay_cloud_queue.txt.
"""

import os
import sys
import json
import re
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ensure UTF-8 console output
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(SCRIPT_DIR, "all_jobs.json")):
    ROOT_DIR = SCRIPT_DIR
elif os.path.exists(os.path.join(SCRIPT_DIR, "..", "all_jobs.json")):
    ROOT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
else:
    ROOT_DIR = SCRIPT_DIR
sys.path.insert(0, ROOT_DIR)

from generate_daily_queue import get_effective_direct_url
from resolve_ats import resolve_direct_ats
from company_directory import load_company_directory

ALL_JOBS_PATH = os.path.join(ROOT_DIR, "all_jobs.json")
DAILY_DIR = os.path.join(ROOT_DIR, "Daily")
OUT_JSON_PATH = os.path.join(DAILY_DIR, "akshay_cloud_queue.json")
OUT_TXT_PATH = os.path.join(DAILY_DIR, "akshay_cloud_queue.txt")


def classify_role(title: str) -> str:
    t = (title or "").lower()
    if re.search(r"mechanical|electrical|civil|hvac|hardware|manufacturing|quality", t):
        return "Other"
    if re.search(r"ml infra|ml infrastructure|sre\b|site reliability|cloud|devops|platform engineer|infrastructure", t):
        return "Cloud/DevOps"
    return "Other"


def classify_seniority(title: str) -> str:
    t = (title or "").lower()
    # Explicitly exclude Lead and Staff
    if re.search(r"\blead\b|\bstaff\b|\bprincipal\b|\bdirector\b", t):
        return "Excluded_Seniority"
    if re.search(r"\bsr\.?\b|\bsenior\b", t):
        return "Senior"
    return "Mid"


def job_fresh_ms(j: dict) -> float | None:
    fs = j.get("first_seen") or j.get("scraped_at")
    if fs:
        fs = str(fs).strip()
        if " " in fs and "T" not in fs:
            fs = fs.replace(" ", "T")
        if not fs.endswith("Z") and "+" not in fs:
            fs += "+00:00"
        try:
            dt = datetime.fromisoformat(fs.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp() * 1000
        except Exception:
            pass
    return None


def resolve_single_job(job: dict, comp_dir: dict) -> dict:
    c_name = job.get("company", "").strip()
    title = job.get("title", "").strip()
    url = job.get("url", "").strip()

    # 1. Check if already has direct ATS URL
    existing_direct, existing_ats = get_effective_direct_url(job)
    if existing_direct and existing_ats:
        return {
            "company": c_name,
            "title": title,
            "ats": existing_ats,
            "url": existing_direct,
            "source_url": url,
            "date_posted": job.get("date_posted", ""),
            "status": "direct_available",
        }

    # 2. Query public company ATS APIs (Greenhouse, Ashby, Workday, Lever)
    # ZERO traffic to LinkedIn!
    try:
        direct_url, ats_plat, root_url = resolve_direct_ats(c_name, title, comp_dir=comp_dir)
        if direct_url and ats_plat:
            return {
                "company": c_name,
                "title": title,
                "ats": ats_plat,
                "url": direct_url,
                "source_url": url,
                "date_posted": job.get("date_posted", ""),
                "status": "resolved_via_ats_api",
            }
    except Exception:
        pass

    return {
        "company": c_name,
        "title": title,
        "ats": "Unresolved",
        "url": url,
        "source_url": url,
        "date_posted": job.get("date_posted", ""),
        "status": "unresolved_linkedin",
    }


def generate_akshay_queue(max_hours: int = 24, max_workers: int = 8) -> dict:
    if not os.path.exists(ALL_JOBS_PATH):
        raise FileNotFoundError(f"Missing {ALL_JOBS_PATH}")

    with open(ALL_JOBS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    all_jobs = data.get("jobs", []) if isinstance(data, dict) else data

    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    cutoff_ms = now_ms - (max_hours * 3600 * 1000)

    # 1. Deterministic Filter: Cloud/DevOps + Senior + <24h (NO SCORE FILTER)
    matched = []
    seen_keys = set()
    for j in all_jobs:
        title = j.get("title", "")
        company = j.get("company", "").strip().lower()
        if classify_role(title) == "Cloud/DevOps" and classify_seniority(title) == "Senior":
            t = job_fresh_ms(j)
            if t is not None and t >= cutoff_ms:
                dedupe_key = (company, title.lower()[:30])
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                matched.append(j)

    print(f"🎯 [Akshay Filter] Found {len(matched)} matching Senior Cloud/DevOps jobs posted in last {max_hours}h.")
    print("⚡ Resolving direct ATS endpoints in parallel (Zero LinkedIn traffic)...")

    comp_dir = load_company_directory()
    resolved_queue = []
    unresolved_queue = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(resolve_single_job, job, comp_dir): job for job in matched}
        for future in as_completed(futures):
            res = future.result()
            if res["status"] != "unresolved_linkedin":
                resolved_queue.append(res)
                print(f"  ✅ [{res['ats']}] {res['company']} — {res['title'][:40]} -> {res['url'][:75]}")
            else:
                unresolved_queue.append(res)

    # Group by ATS
    by_ats = {}
    for r in resolved_queue:
        ats = r["ats"]
        by_ats.setdefault(ats, []).append(r)

    # Sort each ATS group by company
    for ats in by_ats:
        by_ats[ats].sort(key=lambda x: x["company"])

    os.makedirs(DAILY_DIR, exist_ok=True)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filter": "Role: Cloud/DevOps | Seniority: Senior | Lookback: 24h | Fit Score: SKIPPED",
        "total_matched": len(matched),
        "total_ready_to_apply": len(resolved_queue),
        "unresolved_count": len(unresolved_queue),
        "by_ats_counts": {ats: len(jobs) for ats, jobs in by_ats.items()},
        "queue": resolved_queue,
        "unresolved_linkedin": unresolved_queue,
    }

    with open(OUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    with open(OUT_TXT_PATH, "w", encoding="utf-8") as f:
        for r in resolved_queue:
            f.write(f"{r['url']}\n")

    print("\n=======================================================")
    print(f"🎉 Akshay Cloud/DevOps Queue Ready: {len(resolved_queue)} direct application URLs")
    for ats, jobs in by_ats.items():
        print(f"   • {ats:<15}: {len(jobs)} jobs")
    print(f"📁 JSON: {OUT_JSON_PATH}")
    print(f"📄 TXT : {OUT_TXT_PATH}")
    print("=======================================================")
    return summary


if __name__ == "__main__":
    generate_akshay_queue(max_hours=24, max_workers=8)
