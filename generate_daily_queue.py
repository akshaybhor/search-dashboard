#!/usr/bin/env python3
"""
generate_daily_queue.py — Autonomous Daily Application Queue Generator.

Filters all_jobs.json and scores.json for:
1. Target ATS: Greenhouse and Ashby (expandable to Workday).
2. Lookback: Last 48 hours (fresh and unexpired).
3. Quality: AI Fit Score >= 50.

Outputs clean, ready-to-process queue to:
- Daily/daily_queue.json (structured with metadata & ATS grouping)
- Daily/daily_queue.txt  (simple list of URLs for fast piping)
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ALL_JOBS_PATH = os.path.join(SCRIPT_DIR, "all_jobs.json")
SCORES_PATH = os.path.join(SCRIPT_DIR, "scores.json")
DAILY_DIR = os.path.join(SCRIPT_DIR, "Daily")
QUEUE_JSON_PATH = os.path.join(DAILY_DIR, "daily_queue.json")
QUEUE_TXT_PATH = os.path.join(DAILY_DIR, "daily_queue.txt")
WORKDAY_QUEUE_JSON_PATH = os.path.join(DAILY_DIR, "workday_daily_queue.json")
WORKDAY_QUEUE_TXT_PATH = os.path.join(DAILY_DIR, "workday_daily_queue.txt")


def get_effective_direct_url(job: dict) -> tuple[str, str]:
    """
    Returns (direct_url, ats_name) if the job has a valid direct ATS URL
    (Greenhouse, Ashby, Workday, Lever, SmartRecruiters, or Universal).
    Prefers job['direct_url'], then job['url'].
    """
    d_url = (job.get("direct_url") or "").strip()
    u = (job.get("url") or "").strip()
    ats_field = (job.get("ats") or "").strip().lower()

    for candidate in [d_url, u]:
        if not candidate:
            continue
        c_low = candidate.lower()
        if "greenhouse.io" in c_low or "gh_jid=" in c_low:
            return candidate, "Greenhouse"
        if "ashbyhq.com" in c_low or "ashby_jid=" in c_low:
            return candidate, "Ashby"
        if "myworkdayjobs.com" in c_low and "/job/" in c_low:
            return candidate, "Workday"
        if "lever.co" in c_low or "jobs.lever.co" in c_low:
            return candidate, "Lever"
        if "smartrecruiters.com" in c_low:
            return candidate, "SmartRecruiters"
        if "rippling.com" in c_low:
            return candidate, "Rippling"

    if "greenhouse" in ats_field and ("boards.greenhouse.io" in u or "job-boards.greenhouse.io" in u):
        return u, "Greenhouse"
    if "ashby" in ats_field and "jobs.ashbyhq.com" in u:
        return u, "Ashby"
    if "workday" in ats_field and "myworkdayjobs.com" in u.lower() and "/job/" in u.lower():
        return u, "Workday"
    if "lever" in ats_field and "lever.co" in u.lower():
        return u, "Lever"
    if "smartrecruiters" in ats_field and "smartrecruiters.com" in u.lower():
        return u, "SmartRecruiters"
    if "rippling" in ats_field and "rippling.com" in u.lower():
        return u, "Rippling"

    # Direct company career site or generic ATS (Universal form engine)
    for candidate in [d_url, u]:
        if not candidate:
            continue
        c_low = candidate.lower()
        if any(agg in c_low for agg in ["linkedin.com", "indeed.com", "ziprecruiter.com", "glassdoor.com", "dice.com", "monster.com", "simplyhired.com"]):
            continue
        if any(k in c_low for k in ["/job", "/career", "/apply", "bamboohr.com", "breezy.hr", "applytojob.com", "workable.com"]):
            return candidate, "Universal"

    return "", ""


def parse_timestamp(ts_str: str) -> datetime | None:
    """Parses ISO or standard datetime strings into UTC timezone-aware datetime."""
    if not ts_str:
        return None
    s = str(ts_str).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except Exception:
            pass
    try:
        s_clean = s.replace("Z", "+00:00")
        if " " in s_clean and "T" not in s_clean:
            s_clean = s_clean.replace(" ", "T")
        dt = datetime.fromisoformat(s_clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    return None


def is_recent(job: dict, max_hours: int = 48) -> bool:
    """Checks if job was first seen, scraped, or posted within the lookback window."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_hours)

    for field in ("first_seen", "scraped_at"):
        fs = parse_timestamp(job.get(field, ""))
        if fs and fs >= cutoff:
            return True

    dp = parse_timestamp(job.get("date_posted", ""))
    if dp and dp >= (cutoff - timedelta(days=1)):  # 1-day grace period for date_posted resolution
        return True

    return False


def build_daily_queue(min_score: int = 50, max_hours: int = 48) -> dict:
    """Extracts, scores, and categorizes eligible jobs across Greenhouse, Ashby, and Workday."""
    if not os.path.exists(ALL_JOBS_PATH):
        raise FileNotFoundError(f"Missing {ALL_JOBS_PATH}")

    with open(ALL_JOBS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    all_jobs = data.get("jobs", []) if isinstance(data, dict) else data

    scores = {}
    if os.path.exists(SCORES_PATH):
        try:
            with open(SCORES_PATH, "r", encoding="utf-8") as f:
                sc_data = json.load(f)
                scores = sc_data.get("scores", {}) if isinstance(sc_data, dict) else {}
        except Exception as e:
            print(f"⚠️ Warning loading scores.json: {e}")

    greenhouse_jobs = []
    ashby_jobs = []
    workday_jobs = []
    lever_jobs = []
    smartrecruiters_jobs = []
    rippling_jobs = []
    universal_jobs = []
    seen_urls = set()

    for job in all_jobs:
        url = job.get("url", "").strip()
        if not url or url in seen_urls:
            continue

        direct_url, ats = get_effective_direct_url(job)
        if not direct_url or not ats:
            continue

        if not is_recent(job, max_hours=max_hours):
            continue

        score_info = scores.get(url, {}) or scores.get(direct_url, {})
        fit_score = score_info.get("score")
        if fit_score is None:
            fit_score = job.get("fit_score", 0)

        try:
            fit_score = int(fit_score)
        except (ValueError, TypeError):
            fit_score = 0

        if fit_score < min_score:
            continue

        if direct_url in seen_urls:
            continue
        seen_urls.add(direct_url)

        item = {
            "company": job.get("company", "").strip(),
            "title": job.get("title", "").strip(),
            "location": job.get("location", "").strip(),
            "score": fit_score,
            "verdict": score_info.get("verdict", "strong" if fit_score >= 80 else "good"),
            "ats": ats,
            "date_posted": job.get("date_posted", ""),
            "first_seen": job.get("first_seen", "") or job.get("scraped_at", ""),
            "url": direct_url,
            "source_url": url,
        }

        if ats == "Greenhouse":
            greenhouse_jobs.append(item)
        elif ats == "Ashby":
            ashby_jobs.append(item)
        elif ats == "Workday":
            workday_jobs.append(item)
        elif ats == "Lever":
            lever_jobs.append(item)
        elif ats == "SmartRecruiters":
            smartrecruiters_jobs.append(item)
        elif ats == "Rippling":
            rippling_jobs.append(item)
        elif ats == "Universal":
            universal_jobs.append(item)

    greenhouse_jobs.sort(key=lambda x: x["score"], reverse=True)
    ashby_jobs.sort(key=lambda x: x["score"], reverse=True)
    workday_jobs.sort(key=lambda x: x["score"], reverse=True)
    lever_jobs.sort(key=lambda x: x["score"], reverse=True)
    smartrecruiters_jobs.sort(key=lambda x: x["score"], reverse=True)
    rippling_jobs.sort(key=lambda x: x["score"], reverse=True)
    universal_jobs.sort(key=lambda x: x["score"], reverse=True)
    all_eligible = sorted(
        greenhouse_jobs + ashby_jobs + workday_jobs + lever_jobs + smartrecruiters_jobs + rippling_jobs + universal_jobs,
        key=lambda x: x["score"],
        reverse=True,
    )

    now_utc = datetime.now(timezone.utc)
    # Calculate EST offset approx (UTC - 5 / UTC - 4)
    now_est_str = now_utc.astimezone(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d %I:%M %p EDT")

    return {
        "generated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date_est": now_est_str,
        "min_score": min_score,
        "lookback_hours": max_hours,
        "summary": {
            "total": len(all_eligible),
            "greenhouse_count": len(greenhouse_jobs),
            "ashby_count": len(ashby_jobs),
            "workday_count": len(workday_jobs),
            "lever_count": len(lever_jobs),
            "smartrecruiters_count": len(smartrecruiters_jobs),
            "rippling_count": len(rippling_jobs),
            "universal_count": len(universal_jobs),
        },
        "greenhouse": greenhouse_jobs,
        "ashby": ashby_jobs,
        "workday": workday_jobs,
        "lever": lever_jobs,
        "smartrecruiters": smartrecruiters_jobs,
        "rippling": rippling_jobs,
        "universal": universal_jobs,
        "all": all_eligible,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate Daily Application Queue")
    parser.add_argument("--min-score", type=int, default=50, help="Minimum fit score (default: 50)")
    parser.add_argument("--lookback", type=int, default=48, help="Lookback window in hours (default: 48)")
    parser.add_argument("--skip-enrich", action="store_true", help="Skip running resolve_ats enrichment before building queue")
    args = parser.parse_args()

    if not args.skip_enrich:
        try:
            from resolve_ats import enrich_jobs
            enrich_jobs(ALL_JOBS_PATH, max_known=150, max_unknown=5)
        except Exception as e:
            print(f"⚠️ Warning running ATS resolver enrichment: {e}")

    os.makedirs(DAILY_DIR, exist_ok=True)
    queue = build_daily_queue(min_score=args.min_score, max_hours=args.lookback)

    with open(QUEUE_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)

    with open(QUEUE_TXT_PATH, "w", encoding="utf-8") as f:
        for item in queue.get("all", []):
            f.write(f"{item['url']}\n")

    # Also write dedicated Workday queue files
    workday_only = {
        "generated_at": queue["generated_at"],
        "date_est": queue["date_est"],
        "min_score": queue["min_score"],
        "lookback_hours": queue["lookback_hours"],
        "total": len(queue.get("workday", [])),
        "workday": queue.get("workday", []),
    }
    with open(WORKDAY_QUEUE_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(workday_only, f, indent=2)

    with open(WORKDAY_QUEUE_TXT_PATH, "w", encoding="utf-8") as f:
        for item in queue.get("workday", []):
            f.write(f"{item['url']}\n")

    print(f"✅ Daily Queue successfully generated in '{DAILY_DIR}':")
    print(f"   • Total Eligible Roles (Score >={args.min_score}): {queue['summary']['total']}")
    print(f"     - Greenhouse:      {queue['summary']['greenhouse_count']}")
    print(f"     - Ashby:           {queue['summary']['ashby_count']}")
    print(f"     - Workday:         {queue['summary']['workday_count']}")
    print(f"     - Lever:           {queue['summary']['lever_count']}")
    print(f"     - SmartRecruiters: {queue['summary']['smartrecruiters_count']}")
    print(f"     - Rippling:        {queue['summary']['rippling_count']}")
    print(f"     - Universal:       {queue['summary']['universal_count']}")
    print(f"   • Files created: {QUEUE_JSON_PATH}, {QUEUE_TXT_PATH}, {WORKDAY_QUEUE_JSON_PATH}, {WORKDAY_QUEUE_TXT_PATH}")


if __name__ == "__main__":
    main()
