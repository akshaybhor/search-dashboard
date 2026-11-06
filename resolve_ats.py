"""
resolve_ats.py — Automated Direct ATS Link Resolver.
Given a job posting (e.g. from LinkedIn or Indeed), checks if the employer uses
Workday, Greenhouse, Ashby, or Lever. Resolves the direct ATS application link
for that specific role, attaches it to all_jobs.json, and registers the company's
root career portal into companies.json.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from company_directory import (
    load_company_directory,
    record_company,
    save_company_directory,
    normalize_company_key,
)
try:
    from scrape_jobs import is_excluded_company
except ImportError:
    def is_excluded_company(name: str) -> bool:
        return False

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
ALIASES_FILE = os.path.join(SCRIPT_DIR, "company_aliases.json")


def load_company_aliases() -> dict:
    if os.path.exists(ALIASES_FILE):
        try:
            with open(ALIASES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def slugify_company(company_name: str) -> list[str]:
    """Generates candidate slugs for a company name."""
    if not company_name:
        return []
    clean = company_name.lower().strip()
    clean = re.sub(r"[\.,'\"\(\)]", "", clean)
    clean = re.sub(r"\b(inc|llc|corp|corporation|ltd|co|technologies|holdings|plc)\b", "", clean).strip()

    slugs = []

    # 0. Check explicit aliases dictionary
    aliases = load_company_aliases()
    norm_key = normalize_company_key(company_name)
    if norm_key in aliases and aliases[norm_key] not in slugs:
        slugs.append(aliases[norm_key])
    if clean in aliases and aliases[clean] not in slugs:
        slugs.append(aliases[clean])

    # 1. pure alphanumeric without spaces
    s1 = re.sub(r"\s+", "", clean)
    if s1 and s1 not in slugs:
        slugs.append(s1)
    # 2. hyphenated
    s2 = re.sub(r"\s+", "-", clean)
    if s2 and s2 not in slugs:
        slugs.append(s2)
    # 3. underscored
    s3 = re.sub(r"\s+", "_", clean)
    if s3 and s3 not in slugs:
        slugs.append(s3)
    # 4. primary brand token (e.g. 'Oscar Health' -> 'oscar', 'Liftoff Mobile' -> 'liftoff')
    parts = clean.split()
    if len(parts) > 1 and len(parts[0]) >= 3 and parts[0] not in slugs:
        slugs.append(parts[0])
    # 5. AI suffix (e.g. 'Scale' -> 'scaleai')
    if s1 and not s1.endswith("ai") and f"{s1}ai" not in slugs:
        slugs.append(f"{s1}ai")
    # 6. Inc suffix (e.g. 'Aerospike' -> 'aerospike-inc')
    if s1 and f"{s1}-inc" not in slugs:
        slugs.append(f"{s1}-inc")
    return slugs


def title_similarity(t1: str, t2: str) -> float:
    """Computes simple word-overlap Jaccard score between two titles."""
    w1 = set(re.findall(r"\w+", (t1 or "").lower()))
    w2 = set(re.findall(r"\w+", (t2 or "").lower()))
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / len(w1 | w2)


def check_workday_board(career_url: str, job_title: str) -> tuple[str, str]:
    """
    Queries a company's Workday CXS API for a specific job title.
    Returns (direct_job_url, matched_title).
    """
    if not career_url or not job_title:
        return "", ""

    parsed = urllib.parse.urlparse(career_url)
    netloc = parsed.netloc.lower()
    path = parsed.path.strip("/")
    parts = path.split("/")
    site = parts[0] if parts and parts[0] else "careers"
    tenant = netloc.split(".")[0]
    cxs_url = f"https://{netloc}/wday/cxs/{tenant}/{site}/jobs"
    base_url = f"https://{netloc}/{site}"

    # Clean punctuation and generic seniority prefixes to formulate effective search text
    clean_term = re.sub(r"[\(\)\[\],\-–—/]", " ", job_title)
    words = [w for w in clean_term.split() if w.lower() not in ("senior", "sr", "lead", "principal", "staff", "ii", "iii", "iv")]
    search_str = " ".join(words[:5]) if words else job_title

    body = json.dumps({"appliedFacets": {}, "limit": 10, "offset": 0, "searchText": search_str}).encode("utf-8")
    req = urllib.request.Request(
        cxs_url,
        data=body,
        headers={**HEADERS, "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            data = json.loads(res.read().decode("utf-8"))
    except Exception:
        return "", ""

    postings = data.get("jobPostings", [])
    best_match = None
    best_score = 0.0

    for p in postings:
        p_title = p.get("title", "")
        score = title_similarity(job_title, p_title)
        if score > best_score:
            best_score = score
            best_match = p

    if best_match and (best_score >= 0.30 or (len(postings) == 1 and best_score >= 0.25)):
        ext_path = best_match.get("externalPath", "")
        return f"{base_url}{ext_path}", best_match.get("title", "")

    return "", ""


def check_greenhouse_board(slug: str) -> list[dict]:
    """Queries Greenhouse public API for a company slug."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=3) as res:
            data = json.loads(res.read().decode("utf-8"))
            return data.get("jobs", [])
    except Exception:
        return []


def check_ashby_board(slug: str) -> list[dict]:
    """Queries Ashby public posting API for a company slug."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=3) as res:
            data = json.loads(res.read().decode("utf-8"))
            return data.get("jobs", [])
    except Exception:
        return []


def check_lever_board(slug: str) -> list[dict]:
    """Queries Lever public API for a company slug."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=3) as res:
            data = json.loads(res.read().decode("utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        return []


def check_rippling_board(slug: str) -> list[dict]:
    """Queries Rippling public job board for a company slug."""
    url = f"https://ats.rippling.com/{slug}/jobs"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=4) as res:
            text = res.read().decode("utf-8", errors="ignore")
            props = re.findall(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text)
            if not props:
                return []
            data = json.loads(props[0])
            queries = data.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
            for q in queries:
                q_key = q.get("queryKey", [])
                if isinstance(q_key, list) and len(q_key) >= 2 and q_key[0] == "board":
                    items = q.get("state", {}).get("data", {}).get("items", [])
                    if items:
                        formatted = []
                        for it in items:
                            uuid = it.get("uuid") or it.get("id")
                            if uuid:
                                formatted.append({
                                    "title": it.get("name") or it.get("title", ""),
                                    "jobUrl": f"https://ats.rippling.com/{slug}/jobs/{uuid}",
                                })
                        return formatted
    except Exception:
        pass
    return []


def resolve_direct_ats(company_name: str, job_title: str, comp_dir: dict = None) -> tuple[str, str, str]:
    """
    Attempts to discover direct ATS application link for a company and title.
    Checks Workday, Greenhouse, Ashby, and Lever.
    Returns (direct_url, ats_platform, root_career_url).
    """
    if not company_name or is_excluded_company(company_name):
        return "", "", ""

    if comp_dir is None:
        comp_dir = load_company_directory()
    existing_companies = comp_dir.get("companies", {})
    comp_key = normalize_company_key(company_name)

    # 1. First priority: if the employer is already in companies.json, query its known ATS
    if comp_key in existing_companies:
        rec = existing_companies[comp_key]
        ats = rec.get("ats", "")
        career_url = rec.get("career_url", "")

        if ats == "Workday" and career_url:
            direct_url, _ = check_workday_board(career_url, job_title)
            return direct_url, "Workday", career_url

        elif ats == "Greenhouse" and career_url:
            slug = career_url.rstrip("/").split("/")[-1]
            gh_jobs = check_greenhouse_board(slug)
            if gh_jobs:
                best = max(gh_jobs, key=lambda j: title_similarity(job_title, j.get("title", "")), default=None)
                if best and title_similarity(job_title, best.get("title", "")) >= 0.35:
                    return best.get("absolute_url", ""), "Greenhouse", career_url
            return "", "Greenhouse", career_url

        elif ats == "Ashby" and career_url:
            slug = career_url.rstrip("/").split("/")[-1]
            ashby_jobs = check_ashby_board(slug)
            if ashby_jobs:
                best = max(ashby_jobs, key=lambda j: title_similarity(job_title, j.get("title", "")), default=None)
                if best and title_similarity(job_title, best.get("title", "")) >= 0.35:
                    return best.get("jobUrl", ""), "Ashby", career_url
            return "", "Ashby", career_url

        elif ats == "Lever" and career_url:
            slug = career_url.rstrip("/").split("/")[-1]
            lever_jobs = check_lever_board(slug)
            if lever_jobs:
                best = max(lever_jobs, key=lambda j: title_similarity(job_title, j.get("text", "")), default=None)
                if best and title_similarity(job_title, best.get("text", "")) >= 0.35:
                    return best.get("hostedUrl", ""), "Lever", career_url
            return "", "Lever", career_url

        elif ats == "Rippling" and career_url:
            parts = [p for p in career_url.strip("/").split("/") if p and p != "jobs"]
            slug = parts[-1] if parts else ""
            rip_jobs = check_rippling_board(slug)
            if rip_jobs:
                best = max(rip_jobs, key=lambda j: title_similarity(job_title, j.get("title", "")), default=None)
                if best and title_similarity(job_title, best.get("title", "")) >= 0.35:
                    return best.get("jobUrl", ""), "Rippling", career_url
            return "", "Rippling", career_url

    # 2. Second priority: candidate slug probe across Greenhouse, Ashby, Lever, Rippling
    slugs = slugify_company(company_name)
    if not slugs:
        return "", "", ""

    def _maybe_record(root_u: str, ats_name: str):
        if root_u and ats_name and comp_key not in existing_companies:
            record_company(company_name, root_u, ats_hint=ats_name, directory_data=comp_dir)
            save_company_directory(comp_dir)

    for slug in slugs:
        # Try Greenhouse
        gh_jobs = check_greenhouse_board(slug)
        if gh_jobs:
            best_match = None
            best_score = 0.0
            for j in gh_jobs:
                score = title_similarity(job_title, j.get("title", ""))
                if score > best_score:
                    best_score = score
                    best_match = j
            root = f"https://job-boards.greenhouse.io/{slug}"
            _maybe_record(root, "Greenhouse")
            if best_match and best_score >= 0.35:
                return (
                    best_match.get("absolute_url", ""),
                    "Greenhouse",
                    root,
                )
            elif gh_jobs:
                return "", "Greenhouse", root

        # Try Ashby
        ashby_jobs = check_ashby_board(slug)
        if ashby_jobs:
            best_match = None
            best_score = 0.0
            for j in ashby_jobs:
                score = title_similarity(job_title, j.get("title", ""))
                if score > best_score:
                    best_score = score
                    best_match = j
            root = f"https://jobs.ashbyhq.com/{slug}"
            _maybe_record(root, "Ashby")
            if best_match and best_score >= 0.35:
                return (
                    best_match.get("jobUrl", ""),
                    "Ashby",
                    root,
                )
            elif ashby_jobs:
                return "", "Ashby", root

        # Try Lever
        lever_jobs = check_lever_board(slug)
        if lever_jobs:
            best_match = None
            best_score = 0.0
            for j in lever_jobs:
                score = title_similarity(job_title, j.get("text", ""))
                if score > best_score:
                    best_score = score
                    best_match = j
            root = f"https://jobs.lever.co/{slug}"
            _maybe_record(root, "Lever")
            if best_match and best_score >= 0.35:
                return (
                    best_match.get("hostedUrl", ""),
                    "Lever",
                    root,
                )
            elif lever_jobs:
                return "", "Lever", root

        # Try Rippling
        rip_jobs = check_rippling_board(slug)
        if rip_jobs:
            best_match = None
            best_score = 0.0
            for j in rip_jobs:
                score = title_similarity(job_title, j.get("title", ""))
                if score > best_score:
                    best_score = score
                    best_match = j
            root = f"https://ats.rippling.com/{slug}/jobs"
            _maybe_record(root, "Rippling")
            if best_match and best_score >= 0.35:
                return (
                    best_match.get("jobUrl", ""),
                    "Rippling",
                    root,
                )
            elif rip_jobs:
                return "", "Rippling", root

    return "", "", ""


def enrich_jobs(all_jobs_path: str = None, max_known: int = 150, max_unknown: int = 5) -> int:
    """
    Enriches all_jobs.json with direct ATS application URLs (Workday, Greenhouse, Ashby, Lever).
    Prioritizes recent jobs from known ATS employers (NVIDIA, Salesforce, Adobe, Capital One, etc.)
    for instant linking, and remembers probed portals so old unmatched jobs never block new scrapes.
    """
    if not all_jobs_path:
        all_jobs_path = os.path.join(SCRIPT_DIR, "all_jobs.json")
    if not os.path.exists(all_jobs_path):
        print(f"File not found: {all_jobs_path}", flush=True)
        return 0

    with open(all_jobs_path, "r", encoding="utf-8") as f:
        all_data = json.load(f)

    jobs = all_data.get("jobs", [])
    comp_dir = load_company_directory()
    from company_directory import prune_excluded_companies
    prune_excluded_companies(comp_dir)
    existing_companies = comp_dir.get("companies", {})

    print(f"[ATS Resolver] Scanning jobs for direct ATS linking (Workday, Greenhouse, Ashby, Lever)...", flush=True)

    # Separate candidates: known companies (fast direct match) vs unknown companies (slug probes)
    known_candidates = []
    unknown_candidates = []
    seen_probes = set()
    jobs_enriched = 0

    for j in jobs:
        c_name = j.get("company", "").strip()
        title = j.get("title", "").strip()
        url = j.get("url", "").strip()
        existing_direct = j.get("direct_url", "").strip()

        if not c_name or c_name.lower() == "unknown" or is_excluded_company(c_name):
            continue

        # Fast-path: if primary url is already a direct ATS URL, populate direct_url & ats without API calls
        if not existing_direct and url:
            u_low = url.lower()
            detected = ""
            if "myworkdayjobs.com" in u_low and "/job/" in u_low:
                detected = "Workday"
            elif "greenhouse.io" in u_low or "gh_jid=" in u_low:
                detected = "Greenhouse"
            elif "ashbyhq.com" in u_low or "ashby_jid=" in u_low:
                detected = "Ashby"
            elif "lever.co" in u_low:
                detected = "Lever"
            if detected:
                j["direct_url"] = url
                j["ats"] = detected
                jobs_enriched += 1
                continue

        if existing_direct:
            continue

        comp_key = normalize_company_key(c_name)
        if comp_key in existing_companies:
            comp_entry = existing_companies[comp_key]
            ats_plat = comp_entry.get("ats", "")
            portal_url = comp_entry.get("career_url", "")
            if ats_plat in ("Workday", "Greenhouse", "Lever", "Ashby") and portal_url:
                # Skip if already probed against this exact portal URL
                if j.get("ats_probed_portal") == portal_url:
                    continue
                probe_key = (comp_key, title.lower())
                if probe_key in seen_probes:
                    continue
                seen_probes.add(probe_key)
                known_candidates.append(j)
        else:
            if j.get("ats_probed_unknown"):
                continue
            probe_key = (comp_key, title.lower())
            if probe_key in seen_probes:
                continue
            seen_probes.add(probe_key)
            unknown_candidates.append(j)

    # Sort candidates so newest jobs (is_recent=True, then scraped_at/date_posted descending) are always probed first
    def _recency_key(job_item):
        return (
            1 if job_item.get("is_recent") else 0,
            str(job_item.get("scraped_at") or job_item.get("date_posted") or ""),
        )

    known_candidates.sort(key=_recency_key, reverse=True)
    unknown_candidates.sort(key=_recency_key, reverse=True)

    print(f"[ATS Resolver] Found {len(known_candidates)} unprobed roles from known ATS employers, {len(unknown_candidates)} from uncataloged companies.", flush=True)

    companies_resolved = 0
    probed = 0

    # Phase 1: Fast direct linking for known employers
    for j in known_candidates[:max_known]:
        c_name = j.get("company", "").strip()
        title = j.get("title", "").strip()
        existing_direct = j.get("direct_url", "")
        comp_key = normalize_company_key(c_name)
        portal_url = existing_companies.get(comp_key, {}).get("career_url", "")

        direct_url, ats_plat, root_url = resolve_direct_ats(c_name, title, comp_dir=comp_dir)
        j["ats_probed_portal"] = portal_url
        probed += 1

        if direct_url and direct_url != existing_direct:
            j["direct_url"] = direct_url
            j["ats"] = ats_plat
            jobs_enriched += 1
            print(f"  🔗 [Linked {ats_plat}] {c_name} — {title[:40]} -> {direct_url[:75]}...", flush=True)
        time.sleep(0.1)

    # Immediate flush of Phase 1 matches & probed markers so no progress is ever delayed
    if jobs_enriched > 0 or probed > 0:
        with open(all_jobs_path, "w", encoding="utf-8") as f:
            json.dump(all_data, f, separators=(",", ":"))
        print(f"[OK] Phase 1 saved: {jobs_enriched} direct ATS links ({probed} probed) in all_jobs.json", flush=True)

    # Phase 2: Discovery for uncataloged companies (quick probe of top candidates)
    if unknown_candidates and max_unknown > 0:
        for j in unknown_candidates[:max_unknown]:
            c_name = j.get("company", "").strip()
            title = j.get("title", "").strip()
            comp_key = normalize_company_key(c_name)

            direct_url, ats_plat, root_url = resolve_direct_ats(c_name, title, comp_dir=comp_dir)
            j["ats_probed_unknown"] = True
            probed += 1

            if direct_url:
                j["direct_url"] = direct_url
                j["ats"] = ats_plat
                jobs_enriched += 1
                print(f"  🔗 [Discovered & Linked {ats_plat}] {c_name} -> {direct_url[:75]}...", flush=True)

            if root_url and ats_plat and comp_key not in existing_companies:
                record_company(
                    c_name,
                    root_url,
                    date_seen=j.get("date_posted"),
                    ats_hint=ats_plat,
                    directory_data=comp_dir,
                )
                companies_resolved += 1
                print(f"  🏢 [Discovered Portal] {c_name} -> {ats_plat} ({root_url})", flush=True)
            time.sleep(0.15)

    if jobs_enriched > 0 or probed > 0:
        with open(all_jobs_path, "w", encoding="utf-8") as f:
            json.dump(all_data, f, separators=(",", ":"))

    if companies_resolved > 0:
        save_company_directory(comp_dir)
        print(f"[OK] Saved {companies_resolved} newly discovered company portal(s) to companies.json", flush=True)

    print(f"[ATS Resolver] Done. Probed {probed} role(s), linked {jobs_enriched} direct URL(s).", flush=True)
    return jobs_enriched


def test_resolver():
    """Runs a quick live verification test on Workday, Greenhouse, and Ashby."""
    test_cases = [
        ("NVIDIA", "Machine Learning Engineer, GeForce G-Assist"),
        ("Salesforce", "GovCloud Software Engineer"),
        ("Adobe", "Software Development Engineer"),
        ("CrowdStrike", "Sr. Software Engineer - Mac Sensor (Hybrid)"),
    ]
    print("Testing direct ATS resolver across verified Workday portals:")
    for company, title in test_cases:
        direct_url, ats, root = resolve_direct_ats(company, title)
        print(f"  {company:<12} | {title:<40} -> {ats:<10} | {direct_url}")


if __name__ == "__main__":
    if "--enrich" in sys.argv:
        enrich_jobs()
    else:
        test_resolver()
