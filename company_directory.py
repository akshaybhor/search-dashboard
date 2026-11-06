"""
company_directory.py — Dynamic Career Portal & Direct ATS Database Manager.
Extracts clean root career URLs (not job postings), deduplicates companies,
and maintains a permanent companies.json directory for the GUI dashboard.
"""

import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMPANIES_FILE = os.path.join(SCRIPT_DIR, "companies.json")


def extract_career_root_url(url: str, fallback_ats: str = None) -> tuple[str, str]:
    """
    Transforms a specific job posting URL into the company's clean root career portal URL.
    Returns (root_career_url, ats_platform).
    Returns ("", "") if the URL is invalid or a third-party aggregator without company ATS.
    """
    if not url or not isinstance(url, str):
        return "", ""

    url = url.strip()
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    path = parsed.path

    # Filter out LinkedIn job postings unless it's a company page
    if "linkedin.com" in netloc:
        if "/company/" in path:
            # Extract https://www.linkedin.com/company/<slug>/life or /jobs
            m = re.match(r"(/company/[^/]+)", path)
            if m:
                return f"https://www.linkedin.com{m.group(1)}/jobs", "LinkedIn"
        return "", ""

    # Indeed job view pages shouldn't be recorded as company career portals
    if "indeed.com" in netloc:
        return "", ""

    # 1. Workday (e.g., https://genesys.wd1.myworkdayjobs.com/en-US/Genesys/job/...)
    if "myworkdayjobs.com" in netloc:
        # Path format usually: /[lang/]<tenant_or_site>[/job/...]
        # Strip language prefix like /en-US/ or /en/
        clean_path = re.sub(r"^/[a-zA-Z]{2}(-[a-zA-Z]{2})?/", "/", path)
        # Match tenant or career site name before /job or end
        m = re.match(r"^/([^/]+)", clean_path)
        site_name = m.group(1) if m else ""
        if site_name and site_name.lower() not in ("job", "jobs"):
            root = f"https://{netloc}/{site_name}"
        else:
            root = f"https://{netloc}"
        return root, "Workday"

    # 2. Greenhouse (e.g., https://job-boards.greenhouse.io/clearstreet/jobs/123 or https://boards.greenhouse.io/vaxcyte/...)
    if "greenhouse.io" in netloc:
        # Match company slug: /<slug>
        m = re.match(r"^/([^/]+)", path)
        slug = m.group(1) if m else ""
        if slug and slug.lower() not in ("embed", "jobs"):
            root = f"https://{netloc}/{slug}"
        else:
            root = f"https://{netloc}"
        return root, "Greenhouse"

    # 3. Lever (e.g., https://jobs.lever.co/company-slug/uuid)
    if "lever.co" in netloc:
        m = re.match(r"^/([^/]+)", path)
        slug = m.group(1) if m else ""
        root = f"https://jobs.lever.co/{slug}" if slug else f"https://{netloc}"
        return root, "Lever"

    # 4. Ashby (e.g., https://jobs.ashbyhq.com/chaidiscovery/9be875bd...)
    if "ashbyhq.com" in netloc:
        m = re.match(r"^/([^/]+)", path)
        slug = m.group(1) if m else ""
        root = f"https://jobs.ashbyhq.com/{slug}" if slug else f"https://{netloc}"
        return root, "Ashby"

    # 5. iCIMS (e.g., https://careers-blackhawknetwork.icims.com/jobs/26026/job)
    if "icims.com" in netloc:
        return f"https://{netloc}/jobs", "iCIMS"

    # 6. SmartRecruiters (e.g., https://jobs.smartrecruiters.com/Company/1234)
    if "smartrecruiters.com" in netloc:
        m = re.match(r"^/([^/]+)", path)
        slug = m.group(1) if m else ""
        root = f"https://jobs.smartrecruiters.com/{slug}" if slug else f"https://{netloc}"
        return root, "SmartRecruiters"

    # 7. Jobvite (e.g., https://jobs.jobvite.com/company/job/1234)
    if "jobvite.com" in netloc:
        m = re.match(r"^/([^/]+)", path)
        slug = m.group(1) if m else ""
        root = f"https://jobs.jobvite.com/{slug}" if slug else f"https://{netloc}"
        return root, "Jobvite"

    # 8. Rippling (e.g., https://ats.rippling.com/aerospike-inc/jobs/123)
    if "ats.rippling.com" in netloc:
        m = re.match(r"^/([^/]+)", path)
        slug = m.group(1) if m else ""
        root = f"https://ats.rippling.com/{slug}/jobs" if slug else f"https://{netloc}"
        return root, "Rippling"

    # 9. Generic / Custom Career Portals (e.g., careers.bloomberg.com, join.jfrog.com)
    ats_detected = fallback_ats or "Direct"
    if "careers." in netloc or "jobs." in netloc or "join." in netloc:
        # Return base scheme + netloc (e.g. https://careers.bloomberg.com)
        return f"https://{netloc}", ats_detected

    # If the URL contains /careers or /jobs in path:
    m = re.match(r"^(.*?/(?:careers|jobs))", path, re.IGNORECASE)
    if m:
        return f"https://{netloc}{m.group(1)}", ats_detected

    # Default fallback: domain root
    return f"https://{netloc}", ats_detected


def normalize_company_key(company_name: str) -> str:
    """Creates a deterministic, clean deduplication key for a company."""
    if not company_name:
        return ""
    # Strip common business entity suffixes for robust deduplication
    name = company_name.lower().strip()
    name = re.sub(r"[\.,'\"\(\)]", "", name)
    name = re.sub(r"\b(inc|llc|corp|corporation|ltd|co|group|technologies|holdings|plc)\b", "", name).strip()
    # Normalize whitespaces to single dash
    return re.sub(r"\s+", "-", name)


def load_company_directory(filepath: str = COMPANIES_FILE) -> dict:
    """Loads existing companies.json or returns empty template."""
    if not os.path.exists(filepath):
        return {"updated_at": "", "total_companies": 0, "companies": {}}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "companies" not in data or not isinstance(data["companies"], dict):
                data = {"updated_at": "", "total_companies": 0, "companies": {}}
            return data
    except Exception as e:
        print(f"⚠️ Warning loading {filepath}: {e}")
        return {"updated_at": "", "total_companies": 0, "companies": {}}


def save_company_directory(data: dict, filepath: str = COMPANIES_FILE) -> None:
    """Saves the companies.json directory safely."""
    data["total_companies"] = len(data.get("companies", {}))
    data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Atomic write via temp file
    temp_file = f"{filepath}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    if os.path.exists(filepath):
        os.replace(temp_file, filepath)
    else:
        os.rename(temp_file, filepath)


def record_company(
    company_name: str,
    job_url: str,
    date_seen: str = None,
    ats_hint: str = None,
    directory_data: dict = None,
) -> bool:
    """
    Registers or updates a company career portal. Deduplicates deterministically.
    Returns True if added or updated.
    """
    if not company_name or not company_name.strip() or company_name.strip().lower() == "unknown":
        return False

    root_url, detected_ats = extract_career_root_url(job_url, fallback_ats=ats_hint)
    if not root_url:
        return False

    company_key = normalize_company_key(company_name)
    if not company_key:
        return False

    # Block any company listed in scrape_jobs.EXCLUDED_COMPANIES from ever being registered
    try:
        from scrape_jobs import is_excluded_company
        if is_excluded_company(company_name) or is_excluded_company(company_key.replace("-", " ")):
            return False
    except ImportError:
        pass

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_date = date_seen if (date_seen and re.match(r"^\d{4}-\d{2}-\d{2}", str(date_seen))) else today_iso
    now_date = now_date[:10]

    should_save = False
    if directory_data is None:
        directory_data = load_company_directory()
        should_save = True

    companies = directory_data.setdefault("companies", {})
    existing = companies.get(company_key)

    changed = False
    if existing:
        existing["total_jobs_seen"] = existing.get("total_jobs_seen", 1) + 1
        existing["last_seen"] = max(existing.get("last_seen", now_date), now_date)
        # Upgrade URL if existing was just generic domain root and new is a specific ATS
        if detected_ats in ("Workday", "Greenhouse", "Lever", "Ashby", "iCIMS") and existing.get("ats") not in (
            "Workday", "Greenhouse", "Lever", "Ashby", "iCIMS"
        ):
            existing["career_url"] = root_url
            existing["ats"] = detected_ats
            changed = True
    else:
        companies[company_key] = {
            "name": company_name.strip(),
            "career_url": root_url,
            "ats": detected_ats,
            "first_seen": now_date,
            "last_seen": now_date,
            "total_jobs_seen": 1,
        }
        changed = True

    if should_save and changed:
        save_company_directory(directory_data)

    return True



def prune_excluded_companies(directory_data: dict = None, filepath: str = COMPANIES_FILE) -> tuple[int, list[str]]:
    """
    Scans companies.json (or passed directory_data) and deletes any company
    matching EXCLUDED_COMPANIES in scrape_jobs.py.
    Returns (evicted_count, evicted_names).
    """
    try:
        from scrape_jobs import is_excluded_company
    except ImportError:
        return 0, []

    data = directory_data if directory_data is not None else load_company_directory(filepath)
    companies = data.get("companies", {})
    evicted_keys = []
    evicted_names = []

    for key, c_info in list(companies.items()):
        name = c_info.get("name", "")
        # Check display name, dictionary key, and key with dashes replaced with spaces
        if is_excluded_company(name) or is_excluded_company(key) or is_excluded_company(key.replace("-", " ")):
            evicted_keys.append(key)
            evicted_names.append(name or key)

    for k in evicted_keys:
        del companies[k]

    if evicted_keys:
        save_company_directory(data, filepath)
        sample_names = ", ".join(evicted_names[:8]) + ("..." if len(evicted_names) > 8 else "")
        print(f"🧹 [Company Directory Purge] Evicted {len(evicted_keys)} excluded company(s) from {os.path.basename(filepath)}: {sample_names}")

    return len(evicted_keys), evicted_names


def seed_from_all_jobs(all_jobs_path: str = None) -> int:
    """Scans all_jobs.json for any direct ATS jobs and seeds companies.json."""
    if not all_jobs_path:
        all_jobs_path = os.path.join(SCRIPT_DIR, "all_jobs.json")

    if not os.path.exists(all_jobs_path):
        print(f"❌ {all_jobs_path} does not exist.")
        return 0

    with open(all_jobs_path, "r", encoding="utf-8") as f:
        all_data = json.load(f)

    jobs = all_data.get("jobs", [])
    data = load_company_directory()
    added_count = 0

    for j in jobs:
        url = j.get("url", "")
        company = j.get("company", "")
        ats = j.get("ats", "")
        date_p = j.get("date_posted") or j.get("first_seen", "")[:10]

        # Only process if it has a non-LinkedIn direct ATS url or ATS is known
        if url and "linkedin.com" not in url:
            if record_company(company, url, date_seen=date_p, ats_hint=ats, directory_data=data):
                added_count += 1

    prune_excluded_companies(data)
    save_company_directory(data)
    print(f"[OK] Populated Company Directory: {len(data['companies'])} companies registered.")
    return len(data["companies"])


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Company Directory Manager")
    parser.add_argument("--prune", action="store_true", help="Prune any excluded companies from companies.json")
    parser.add_argument("--seed", action="store_true", help="Seed companies from all_jobs.json")
    args = parser.parse_args()

    if args.prune:
        prune_excluded_companies()
    elif args.seed:
        seed_from_all_jobs()
    else:
        prune_excluded_companies()
        seed_from_all_jobs()
