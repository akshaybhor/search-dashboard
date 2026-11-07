#!/usr/bin/env python3
"""
audit_workday_portals.py — Automated Workday Career Portal Auditor & Directory Builder.

Audits candidate Workday URLs against live Workday CXS APIs (/wday/cxs/{tenant}/{site}/jobs).
Validates:
  1. Live HTTP 200 response from Workday's edge.
  2. JSON schema with valid jobPostings array.
  3. Active job count (total > 0).
  4. Authentic job title sampling.

Only 100% verified portals are registered into companies.json.
Can be rerun anytime in the future to audit and add more Workday portals.
"""

import os
import sys
import json
import re
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from company_directory import (
    load_company_directory,
    save_company_directory,
    record_company,
    normalize_company_key,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Curated list of candidate enterprise, tech, fintech, and defense Workday portals
CANDIDATE_WORKDAY_COMPANIES = [
    # Top Tech, Cloud & Semis
    ("Salesforce", "salesforce.wd12.myworkdayjobs.com", "External_Career_Site"),
    ("Nvidia", "nvidia.wd5.myworkdayjobs.com", "NVIDIAExternalCareerSite"),
    ("Mastercard", "mastercard.wd1.myworkdayjobs.com", "CorporateCareers"),
    ("Target", "target.wd5.myworkdayjobs.com", "targetcareers"),
    ("U.S. Bank", "usbank.wd1.myworkdayjobs.com", "US_Bank_Careers"),
    ("Adobe", "adobe.wd5.myworkdayjobs.com", "external_experienced"),
    ("Cisco", "cisco.wd5.myworkdayjobs.com", "Cisco_Careers"),
    ("PayPal", "paypal.wd1.myworkdayjobs.com", "jobs"),
    ("CrowdStrike", "crowdstrike.wd5.myworkdayjobs.com", "crowdstrikecareers"),
    ("HP", "hp.wd5.myworkdayjobs.com", "ExternalCareerSite"),
    ("Hewlett Packard Enterprise", "hpe.wd5.myworkdayjobs.com", "CareersGlobal"),
    ("Broadcom", "broadcom.wd1.myworkdayjobs.com", "External_Career"),
    ("Autodesk", "autodesk.wd1.myworkdayjobs.com", "Ext"),
    ("Arm", "arm.wd3.myworkdayjobs.com", "Arm_Careers"),
    ("BlackLine", "blackline.wd108.myworkdayjobs.com", "BlackLineCareers"),
    ("SAIC", "saic.wd5.myworkdayjobs.com", "saic_careers"),
    ("Capital One", "capitalone.wd12.myworkdayjobs.com", "Capital_One"),
    ("KLA", "kla.wd1.myworkdayjobs.com", "Search"),
    ("eBay", "ebay.wd5.myworkdayjobs.com", "apply"),
    ("Motorola Solutions", "motorolasolutions.wd5.myworkdayjobs.com", "Careers"),
    ("Truist", "truist.wd1.myworkdayjobs.com", "Careers"),
    ("Workday", "workday.wd5.myworkdayjobs.com", "Workday"),
    ("Intuit", "intuit.wd5.myworkdayjobs.com", "Early_Career"),
    ("Dell", "dell.wd1.myworkdayjobs.com", "External"),
    ("Micron Technology", "micron.wd1.myworkdayjobs.com", "External"),
    ("Western Digital", "westerndigital.wd1.myworkdayjobs.com", "WDC_Careers"),
    ("Synopsys", "synopsys.wd1.myworkdayjobs.com", "Synopsys_CareersSite"),
    ("Cadence Design Systems", "cadence.wd1.myworkdayjobs.com", "External_Careers"),
    ("Palo Alto Networks", "paloaltonetworks.wd1.myworkdayjobs.com", "PANW_Careers"),
    ("Fortinet", "fortinet.wd1.myworkdayjobs.com", "Fortinet_Careers"),
    ("Zscaler", "zscaler.wd5.myworkdayjobs.com", "Zscaler_Careers"),
    ("Splunk", "splunk.wd1.myworkdayjobs.com", "splunk_careers"),
    ("Nutanix", "nutanix.wd1.myworkdayjobs.com", "NutanixCareers"),
    ("Teradata", "teradata.wd1.myworkdayjobs.com", "TeradataCareers"),
    ("NetApp", "netapp.wd1.myworkdayjobs.com", "Careers"),
    ("Juniper Networks", "juniper.wd1.myworkdayjobs.com", "JuniperCareers"),
    ("Qualcomm", "qualcomm.wd5.myworkdayjobs.com", "External"),
    ("VMware", "vmware.wd1.myworkdayjobs.com", "VMware"),
    ("Analog Devices", "analogdevices.wd1.myworkdayjobs.com", "External"),
    ("CoStar Group", "costar.wd1.myworkdayjobs.com", "CoStarCareers"),
    ("Illumina", "illumina.wd1.myworkdayjobs.com", "illumina-careers"),

    # Financial Services & Banking
    ("Fifth Third Bank", "fifththird.wd5.myworkdayjobs.com", "53careers"),
    ("KeyBank", "keybank.wd5.myworkdayjobs.com", "KeyBank_Careers"),
    ("Citizens Bank", "citizensbank.wd1.myworkdayjobs.com", "CitizensBankCareers"),
    ("Northern Trust", "northerntrust.wd1.myworkdayjobs.com", "NorthernTrustCareers"),
    ("Ally Financial", "ally.wd1.myworkdayjobs.com", "Ally_Careers"),
    ("Fiserv", "fiserv.wd5.myworkdayjobs.com", "EXT"),
    ("FIS", "fis.wd5.myworkdayjobs.com", "External"),
    ("Discover", "discover.wd5.myworkdayjobs.com", "DiscoverCareers"),
    ("American Express", "aexp.wd5.myworkdayjobs.com", "AxpCareers"),
    ("Fannie Mae", "fanniemae.wd1.myworkdayjobs.com", "FannieMaeCareers"),
    ("Freddie Mac", "freddiemac.wd1.myworkdayjobs.com", "FreddieMacCareers"),
    ("Santander", "santander.wd1.myworkdayjobs.com", "SantanderCareers"),
    ("Charles Schwab", "schwab.wd1.myworkdayjobs.com", "Schwab_Careers"),
    ("BNY Mellon", "bnymellon.wd1.myworkdayjobs.com", "BNYMellonCareers"),
    ("State Street", "statestreet.wd1.myworkdayjobs.com", "StateStreetCareers"),
    ("Moody's", "moodys.wd5.myworkdayjobs.com", "Moodys_Careers"),
    ("Morningstar", "morningstar.wd5.myworkdayjobs.com", "Morningstar"),
    ("Nasdaq", "nasdaq.wd1.myworkdayjobs.com", "US_External_Career_Site"),
    ("S&P Global", "spglobal.wd5.myworkdayjobs.com", "SPGlobal_Careers"),

    # Aerospace, Defense & Federal
    ("Booz Allen Hamilton", "bah.wd1.myworkdayjobs.com", "BAH_Jobs"),
    ("Leidos", "leidos.wd5.myworkdayjobs.com", "External"),
    ("RTX (Raytheon)", "rtx.wd1.myworkdayjobs.com", "RTX_Careers"),
    ("Northrop Grumman", "northropgrumman.wd1.myworkdayjobs.com", "Northrop_Grumman_External_Workday_Career_Site"),
    ("Lockheed Martin", "lockheedmartin.wd1.myworkdayjobs.com", "Lockheed_Martin_Careers"),
    ("General Dynamics", "gd.wd1.myworkdayjobs.com", "GDCareers"),
    ("CACI", "caci.wd1.myworkdayjobs.com", "CACI_Careers"),
    ("Jacobs", "jacobs.wd1.myworkdayjobs.com", "Jacobs_Careers"),
    ("L3Harris", "l3harris.wd1.myworkdayjobs.com", "L3Harris_Careers"),
    ("BAE Systems", "baesystems.wd1.myworkdayjobs.com", "BAESystemsCareers"),
    ("GE Aerospace", "geaerospace.wd5.myworkdayjobs.com", "GE_Aerospace"),

    # Healthcare, Life Sciences & Pharma
    ("CVS Health", "cvshealth.wd1.myworkdayjobs.com", "CVS_Health_Careers"),
    ("Humana", "humana.wd5.myworkdayjobs.com", "Humana_External_Career_Site"),
    ("Bristol Myers Squibb", "bristolmyerssquibb.wd5.myworkdayjobs.com", "BMS"),
    ("Abbott", "abbott.wd5.myworkdayjobs.com", "abbottcareers"),
    ("Medtronic", "medtronic.wd1.myworkdayjobs.com", "Medtronic_Careers"),
    ("Pfizer", "pfizer.wd1.myworkdayjobs.com", "PfizerCareers"),
    ("Johnson & Johnson", "jnj.wd1.myworkdayjobs.com", "JNJCareers"),
    ("Centene", "centene.wd1.myworkdayjobs.com", "Centene_Careers"),
    ("Elevance Health", "elevancehealth.wd1.myworkdayjobs.com", "Elevance_Health"),
    ("Becton Dickinson", "bd.wd1.myworkdayjobs.com", "BD_External"),
    ("Boston Scientific", "bostonscientific.wd1.myworkdayjobs.com", "BSC"),
    ("Thermo Fisher Scientific", "thermofisher.wd1.myworkdayjobs.com", "ThermoFisher"),
    ("Baxter", "baxter.wd1.myworkdayjobs.com", "baxter"),
    ("Cardinal Health", "cardinalhealth.wd1.myworkdayjobs.com", "EXT"),

    # Retail, Logistics & Manufacturing
    ("Walmart", "walmart.wd5.myworkdayjobs.com", "WalmartExternal"),
    ("Nike", "nike.wd1.myworkdayjobs.com", "Nike_Careers"),
    ("Lowe's", "lowes.wd1.myworkdayjobs.com", "LowesCareers"),
    ("Home Depot", "homedepot.wd1.myworkdayjobs.com", "HomeDepotCareers"),
    ("Best Buy", "bestbuy.wd1.myworkdayjobs.com", "BestBuyCareers"),
    ("General Motors", "generalmotors.wd5.myworkdayjobs.com", "Careers_GM"),
    ("Ford", "ford.wd1.myworkdayjobs.com", "FordCareers"),
    ("Caterpillar", "caterpillar.wd5.myworkdayjobs.com", "CaterpillarCareers"),
    ("Deere & Company", "deere.wd1.myworkdayjobs.com", "Deere_Careers"),
    ("FedEx", "fedex.wd1.myworkdayjobs.com", "FXG_Careers"),
    ("UPS", "ups.wd5.myworkdayjobs.com", "UPS_External_Site"),
    ("Eaton", "eaton.wd5.myworkdayjobs.com", "Eaton_Careers"),
    ("Honeywell", "honeywell.wd1.myworkdayjobs.com", "Honeywell_Careers"),
    ("Siemens", "siemens.wd3.myworkdayjobs.com", "Siemens_Careers"),
    ("Schneider Electric", "schneiderelectric.wd3.myworkdayjobs.com", "SchneiderElectric"),
    ("Emerson", "emerson.wd5.myworkdayjobs.com", "Emerson_Careers"),
    ("Rockwell Automation", "rockwellautomation.wd1.myworkdayjobs.com", "Careers"),
]


def test_workday_endpoint(netloc: str, tenant: str, site: str) -> tuple[bool, int, str]:
    """
    Directly queries a Workday CXS API endpoint.
    Returns (is_valid, total_job_count, sample_job_title).
    """
    cxs_url = f"https://{netloc}/wday/cxs/{tenant}/{site}/jobs"
    payload = json.dumps({"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}).encode("utf-8")
    req = urllib.request.Request(cxs_url, data=payload, headers=HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=4.0) as res:
            if res.status == 200:
                data = json.loads(res.read().decode("utf-8"))
                total = data.get("total", 0)
                postings = data.get("jobPostings", [])
                if total > 0 and postings:
                    sample_title = postings[0].get("title", "")
                    return True, total, sample_title
    except Exception:
        pass
    return False, 0, ""


def audit_single_company(name: str, candidate_netloc: str, candidate_site: str) -> dict:
    """
    Audits a company by testing its primary candidate URL, with automatic cluster
    and site variations fallback if the primary path is slightly different.
    """
    tenant = candidate_netloc.split(".")[0]

    # 1. Primary check
    is_valid, total, sample = test_workday_endpoint(candidate_netloc, tenant, candidate_site)
    if is_valid:
        return {
            "name": name,
            "netloc": candidate_netloc,
            "site": candidate_site,
            "career_url": f"https://{candidate_netloc}/{candidate_site}",
            "total_jobs": total,
            "sample_title": sample,
            "status": "APPROVED",
        }

    # 2. Smart fallback variation check (common cluster & site paths)
    clusters = ["wd5", "wd1", "wd12", "wd3", "wd108"]
    sites = [candidate_site, f"{tenant}_Careers", f"{tenant}careers", "Careers", "External", "External_Careers", "jobs", "Search", tenant]
    seen_combos = set([(candidate_netloc, candidate_site)])

    for cl in clusters:
        netloc = f"{tenant}.{cl}.myworkdayjobs.com"
        for s in sites:
            if (netloc, s) in seen_combos:
                continue
            seen_combos.add((netloc, s))
            is_valid, total, sample = test_workday_endpoint(netloc, tenant, s)
            if is_valid:
                return {
                    "name": name,
                    "netloc": netloc,
                    "site": s,
                    "career_url": f"https://{netloc}/{s}",
                    "total_jobs": total,
                    "sample_title": sample,
                    "status": "APPROVED",
                }

    return {
        "name": name,
        "netloc": candidate_netloc,
        "site": candidate_site,
        "career_url": f"https://{candidate_netloc}/{candidate_site}",
        "total_jobs": 0,
        "sample_title": "",
        "status": "REJECTED",
    }


def run_audit(dry_run: bool = False, max_workers: int = 12) -> dict:
    """Audits the candidate Workday catalog in parallel and records approved portals."""
    print("======================================================================")
    print("🔍 WORKDAY PORTAL AUDITOR & DIRECTORY BUILDER")
    print(f"   Target Candidates : {len(CANDIDATE_WORKDAY_COMPANIES)} companies")
    print(f"   Parallel Workers  : {max_workers}")
    print(f"   Mode              : {'DRY RUN (Audit only)' if dry_run else 'ACTIVE (Save to companies.json)'}")
    print("======================================================================")

    comp_dir = load_company_directory()
    existing_companies = comp_dir.get("companies", {})
    existing_keys = set(existing_companies.keys())

    approved = []
    rejected = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(audit_single_company, name, netloc, site): name
            for name, netloc, site in CANDIDATE_WORKDAY_COMPANIES
        }
        for future in as_completed(futures):
            res = future.result()
            if res["status"] == "APPROVED":
                approved.append(res)
                print(f"  ✅ [APPROVED] {res['name']:<25} -> {res['career_url']} ({res['total_jobs']} jobs | e.g. '{res['sample_title'][:32]}')")
            else:
                rejected.append(res)
                print(f"  ❌ [REJECTED] {res['name']:<25} (No active CXS API response)")

    print("\n======================================================================")
    print(f"📊 Audit Results: {len(approved)} Approved | {len(rejected)} Rejected")
    print("======================================================================")

    if not dry_run and approved:
        added_count = 0
        updated_count = 0
        for item in approved:
            c_name = item["name"]
            c_url = item["career_url"]
            k = normalize_company_key(c_name)
            is_new = k not in existing_keys
            record_company(
                company_name=c_name,
                job_url=c_url,
                ats_hint="Workday",
                directory_data=comp_dir,
            )
            # Update total jobs seen
            if k in comp_dir.get("companies", {}):
                comp_dir["companies"][k]["total_jobs_seen"] = item["total_jobs"]
            if is_new:
                added_count += 1
            else:
                updated_count += 1

        save_company_directory(comp_dir)
        total_wd = sum(1 for v in comp_dir.get("companies", {}).values() if v.get("ats") == "Workday")
        print(f"🎉 Successfully saved to companies.json!")
        print(f"   • Newly Added Workday Portals : {added_count}")
        print(f"   • Verified Existing Portals   : {updated_count}")
        print(f"   • Total Workday Portals in DB : {total_wd}")
        print(f"   • Total All Companies in DB   : {comp_dir.get('total_companies')}")

    return {"approved": approved, "rejected": rejected}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Audit and build Workday Tenant Knowledge Base")
    parser.add_argument("--dry-run", action="store_true", help="Perform audit without modifying companies.json")
    parser.add_argument("--workers", type=int, default=12, help="Number of concurrent worker threads (default: 12)")
    args = parser.parse_args()

    run_audit(dry_run=args.dry_run, max_workers=args.workers)
