"""
Bay Area + NYC MLE/DS Job Scraper
Three pipelines (see __main__): LinkedIn guest-endpoint watcher, Indeed via
python-jobspy, and a curated-biotech sweep (direct Greenhouse/Workday probes +
allowlist-filtered LinkedIn). Each writes {basename}.{json,md,html} digests and
accumulates into all_jobs.json for the nightly triage agent and the dashboard.
"""

import http.cookiejar
import itertools
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.request import urlopen, Request, build_opener, HTTPCookieProcessor
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
]

HEADERS = {
    "User-Agent": USER_AGENTS[0],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

LINKEDIN_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

KEYWORDS = [
    # ---- Generic & Popular Software Engineering ----
    "software engineer",
    "software developer",
    "sde",
    "software development engineer",
    "application software engineer",
    "application engineer",
    "enterprise software engineer",
    # ---- Backend & Systems Engineering ----
    "backend engineer",
    "backend developer",
    "back-end engineer",
    "python backend engineer",
    "python developer",
    "systems engineer",
    "systems software engineer",
    "distributed systems engineer",
    "distributed systems",
    "api engineer",
    # ---- C / C++ Specific ----
    "c++ engineer",
    "c++ developer",
    "cpp engineer",
    "c/c++ engineer",
    "high performance c++ developer",
    # ---- Cloud, Infrastructure, Platform & SRE ----
    "cloud engineer",
    "cloud software engineer",
    "platform engineer",
    "sre",
    # ---- Data Engineering & Data Platform ----
    "data engineer",
    "data platform engineer",
    "data infrastructure",
    "etl engineer",
    "etl developer",
    "ml data pipeline engineer",
    # ---- Applied AI & GenAI (Including Beginner / Associate Titles) ----
    "ai engineer",
    "junior ai engineer",
    "associate ai engineer",
    "ai/ml engineer",
    "applied ai engineer",
    "applied ai software engineer",
    "ai solutions developer",
    "generative ai engineer",
    "genai developer",
    "llm engineer",
    "junior llm engineer",
    "rag engineer",
    "conversational ai engineer",
    # ---- Machine Learning, Computer Vision & MLOps ----
    "machine learning engineer",
    "ml engineer",
    "mle",
    "associate machine learning engineer",
    "mlops engineer",
    "associate mlops engineer",
    "computer vision engineer",
]

# Seconds to wait between API probes — keeps us polite
REQUEST_DELAY = 0.5


def polite_sleep(base: float = REQUEST_DELAY, jitter_min: float = 0.8, jitter_max: float = 1.6):
    """Sleep with randomized jitter to emulate organic human request intervals."""
    time.sleep(random.uniform(base * jitter_min, base * jitter_max))

# Biotech digest should only contain reliably fresh roles.
FRESH_JOB_LOOKBACK = timedelta(hours=24)

# Senior-track and executive titles are excluded everywhere: the candidate
# targets early-to-mid IC roles. Covers IC senior tracks (staff/principal/
# distinguished/founding) and management/exec tiers (director/VP/chief/head of).
EXCLUDED_SENIORITY_RE = re.compile(
    r'\b(staff|principal|distinguished|founding|director|vice president|s?vp|chief|head of)\b',
    re.IGNORECASE)

# Clearance and Citizenship dealbreaker keywords (matches explicit requirements)
EXCLUDED_CLEARANCE_RE = re.compile(
    r'\b('
    r'ts/?sci|top\s+secret|secret\s+clearance|security\s+clearance|'
    r'active\s+(?:secret|top\s+secret|ts/?sci|clearance)|'
    r'dod\s+clearance|public\s+trust|'
    r'polygraph|full\s+scope\s+poly|'
    r'clearance\s+required|'
    r'itar|export\s+control|'
    r'u\.?s\.?\s+person|'
    r'u\.?s\.?\s+citizen(?:ship)?\s+(?:only|required)|'
    r'must\s+be\s+(?:a\s+)?u\.?s\.?\s+citizen|'
    r'(?:citizen(?:ship)?\s+(?:or|and)\s+(?:permanent\s+resident|green\s+card))|'
    r'(?:permanent\s+residents?|green\s+card\s+holders?)\s+only'
    r')\b',
    re.IGNORECASE,
)

# Negative Sponsorship keywords (multi-word phrases only; never matches positive sponsorship)
EXCLUDED_NO_SPONSORSHIP_RE = re.compile(
    r'\b('
    r'(?:no|not\s+able\s+to|unable\s+to|cannot|can\s+not|will\s+not|does\s+not|are\s+not)\s+(?:currently\s+)?(?:provide\s+|offer\s+)?(?:visa\s+)?sponsor(?:ship|ing)?|'
    r'without\s+(?:visa\s+)?sponsor(?:ship)?|'
    r'no\s+h-?1b|no\s+opt|no\s+cpt|'
    r'no\s+c2c|c2c\s+not\s+accepted|'
    r'no\s+corp(?:-|\s+)to(?:-|\s+)corp|'
    r'no\s+(?:3rd|third)(?:-|\s+)party|'
    r'w2\s+only|w-?2\s+only'
    r')\b',
    re.IGNORECASE,
)

# Recruiting-platform / aggregator accounts that repost roles which mostly don't
# actually exist (e.g. "Jack & Jill" reposts other companies' jobs on LinkedIn).
# Matched against the parsed company name; add a line to block the next one.
EXCLUDED_COMPANIES = [
    "jack & jill",
    "jack and jill",
    "crewlia", 
    "enhance it",
    "jobright.ai",
    "jobgether",
    # Missed from your screenshot:
    "micro1",       # Spams "AI/ML Engineer, Internal Platforms" globally
    "webbocket",    # Spams intern/junior roles globally from India
    # Notorious Global Spammers & Freelance Platforms:
    "turing",       # Posts thousands of generic remote jobs to collect resumes
    "crossover",    # Same as Turing; infamous for extreme tracking software
    "braintrust",   # A freelance gig platform that clogs up full-time searches
    # "Task / Gig" AI Companies (Not real Engineering roles):
    "outlier",           # Spams "AI Training" and prompt-writing jobs
    "outlier.ai",
    "Steneral Consulting",
    "Anduril Industries",
    "BayOne Solutions",
    "Seneca Resources",
    "OnPoint Insights",
    "The Judge Group",
    "Pyramid Consulting, Inc",
    "Infotree Global Solution.",
    "Infotree Solutions",
    "aditi consulting",
    "TaskVerse",
    "BeaconFire Inc.",
    "Affirm",
    "Haystack",
    "Torentify",
    "Syms Strategic Group, LLC (SSG)",
    "Kforce Inc",
    "Alignerr",
    "Insight Global",
    "dataannotation",    # Spams gig-work prompt-writing jobs
    "data annotation",
    "cybercoders",    # Posts duplicate/evergreen jobs that often don't exist
    "synergisticit",  # Body shop that often asks juniors to pay for training
    "revature",        # Body shop with strict lock-in contracts
    "fonzi ai",  # Aggregators & Talent Networks identified from feed
    "high-priority jobs directory - cyopspath",
    "jobs web3",
    "people in ai",
    "AfterQuery Experts",
    "remotehunter",
    "talenthop",
    "sotalent",
    "wiraa",
    "nhs job mastery with dr. samejo",
    "jobot",     # Massive Recruiting / Spam Agencies
    "robert half",
    "the phoenix group",
    "the phoenix group®",
    "apex systems",
    "randstad digital americas",
    "collabera",
    "teksystems",
    "experis",
    "actalent",
    "tech consulting",
    "talent software services",
    "gbit",
    "global bridge infotech inc",
    "saicon",
    "ktek resourcing",
    "spectraforce",
    "testingxperts",
    "vdart",
    "us tech solutions",
    "tekfortune inc",
    "a-line staffing solutions",
    "mondo",
    "reqroute",
    "system soft technologies",
    "jobsinpython",
    "ai talent insider",
    "jobverse.io",
    "coljob",
    "gamblingcareers.com",
    "joppy",
    "phaxis",
    "o2 technologies,inc",
    "intepros",
    "people prime worldwide",
    "harnham",
    "evolution usa",
    "mastech",
    "trivine technology solutions llc",
    "addison group",
    "engine",
    "acestack",
    "fintal partners",
    "revel it",
    "crossing hurdles",
    "reach velocity",
    "frederick fox",
    "global business ser. 4u",
    "success recruitments",
    "holistic partners, inc",
    "hunter bond",
    "tier4 group",
    "covet it inc",
    "james search group",
    "motion recruitment",
    "myticas consulting",
    "interon it solutions",
    "veriipro",
    "raas infotek",
    "process point technologies corporation",
    "pentangle tech services | p5 group",
    "f1servicecentre",
    "marchon partners",
    "galent",
    "oho group",
    "prodapt",
    "medix technology",
    "grupo tecdata engineering",
    "prestige staffing",
    "aptino, inc.",
    "goliath partners",
    "prosearch",
    "vbeyond corporation",
    "metasys technologies",
    "sid global solutions",
    "chabeztech llc",
    "metrix it solutions inc",
    "bairesdev", # Massive global outsourcing firm
    "contractstaffingrecruiters.com",
    "kgs technology group, inc",
    "largeton group",
    "pro integrate",
    "cullerton group",
    "programmers.io",
    "envision technology solutions",
    "cloudhive",
    "infobahn softworld inc",
    "qbitech",
    "Boeing",
    "wissen technology",
    "stabile search",
    "Collins Aerospace",
    "clevanoo llc",
    "extend information systems inc.",
    "xforia global talent & technology solutions",
    "advanced tech placement",
    "Northrop Grumman",
    "CACI",
    "CACI International Inc",
    "Travelers",
    "consultnet technology services and solutions",
    "findr",
    "Genesys",
    "breed staffing",
    "Raytheon",
    "Lockheed Martin",
    "Amazon",
    "Amazon Web Services (AWS)",
    "NVIDIA",
    "technoidllc",
    "midsysinc",
    "stellarus",
    "L3Harris Technologies",
    "BAE Systems, Inc.",
    "strategic employment partners (sep)",
    "funtonetwork consulting",
    "Booz Allen Hamilton",
    "Blue Origin",
    "Amgen",
    "magnit global",
    "nityo infotech indonesia",
    "expert executive recruiters (eer global)",
    "msbu konsultan",
    "tundra technical solutions",
    "baja tech",
    "issoft georgia",
    "redstream technology",
    "tek leaders inc",
    "stic",
    "gsquared group",
    "vaspire technologies inc.",
    "1st-recruit llc, an e-verified company",
    "marlabs",
    "request technology, llc",
    "synectics inc.",
    "ztek consulting",
    "dfuse technologies, inc.",
    "lts group",
    "smartfox",
    "techneptune consulting inc.",
    "i verve inc",
    "healthasia",
    "mercor",
    "lumenalta",
    "swooped",
    "hired",
    "lensa",
    "yo hr consultancy",
    "great value hiring",
    "talentsync",
    "taskify ai",
    "quik hire staffing",
    "call for referral",
    "north star staffing",
    "get offers",
    "skyhouse agency",
    "bluepeak consulting",
    "starhaven group",
    "avance consulting",
    "Jobgether",
    "talent groups",
    "1872 consulting",
    "4a consulting, llc",
    "7seventy recruiting",
    "ai talent hope",
    "ai talent hunt cloud",
    "ask consulting",
    "alined consulting group",
    "allied resources technical consultants",
    "amaris consulting | part of mantu",
    "analytic recruiting inc.",
    "attractivate consulting solutions",
    "auxo talent",
    "avani tech solutions private limited",
    "bepc inc. - business excellence professional consulting",
    "benchmark it - technology talent",
    "brahma consulting group",
    "bright vision technologies",
    "brilliant infotech inc.",
    "carex consulting group",
    "clarkston consulting",
    "core ai consulting inc",
    "corevia staffing",
    "corellia talent",
    "culturemill recruiting",
    "cypress hcm",
    "dbi staffing",
    "dsm-h consulting",
    "dahl consulting",
    "dale workforce solutions",
    "delta computer consulting",
    "divine talent",
    "emw staffing solutions llc",
    "esg consulting",
    "eliassen group",
    "elite recruiting co",
    "evlo ai",
    "executive recruiting group",
    "fdo consulting",
    "futuretech recruitment",
    "gdi infotech",
    "gtn technical staffing",
    "genpact",
    "goodwin recruiting",
    "gravitas recruitment group (global) ltd",
    "hcm staffing and consulting group",
    "hivaa services llc",
    "horizontal talent",
    "inficare staffing",
    "infotree global solutions",
    "insilico search partners",
    "jeffreym consulting",
    "john burns research and consulting",
    "kbi staffing",
    "kaizen resourcing",
    "kidde global solutions",
    "landshark recruiting",
    "leorose consulting",
    "lucas james talent partners",
    "marketeq talent",
    "mirador search partners",
    "navitas business consulting, inc.",
    "nerd up staffing",
    "newlineinfo corp - it services and it consulting",
    "nexgen tech solutions",
    "nextpath workforce solutions",
    "nityo infotech",
    "omw consulting",
    "op recruiting",
    "paragon alpha - hedge fund talent business",
    "piper companies",
    "professional search group - oc",
    "proven recruiting",
    "raps consulting inc",
    "rachel paul recruiting",
    "ramtec consulting llc",
    "recruiting resources, inc.",
    "recruiting from scratch",
    "red cedar consultancy, llc",
    "redtech recruitment",
    "referment",
    "ringside talent",
    "rivago infotech inc",
    "ronaj recruitment",
    "rōnin consulting",
    "sem3 consulting",
    "srs consulting inc",
    "sabina divosi - headhunter & talent partner",
    "shield consulting solutions, inc.",
    "stellar consulting solutions, llc",
    "synergy business consulting, inc.",
    "think consulting",
    "thought logic consulting",
    "three pillars recruiting",
    "trident consulting",
    "v-soft consulting group, inc.",
    "vforce infotech",
    "veridian tech solutions, inc.",
    "voto consulting llc",
    "wall street consulting services llc",
    "xpertis talent",
    "Accenture Federal Services",
    "Archer",
    "Trinzic",
    "zenotis infotech",
    "tsr consulting"
]
_EXCLUDED_COMPANY_RE = re.compile(
    "|".join(re.escape(c) for c in EXCLUDED_COMPANIES), re.IGNORECASE
)

EXCLUDED_LOCATIONS = [
    # Original list
    "Belgium",
    "United Kingdom",
    "Spain",
    "Brazil",
    "Poland",
    "India",
    "Germany",
    "France",
    "Canada",
    "Australia",
    "Philippines",
    "Mexico",
    "Argentina",
    # Newly identified from dataset
    "Armenia",
    "Austria",
    "Bangladesh",
    "Bosnia and Herzegovina",
    "Bulgaria",
    "Chile",
    "China",
    "Colombia",
    "Costa Rica",
    "Czechia",
    "Denmark",
    "Egypt",
    "Finland",
    "Greece",
    "Hong Kong",
    "Hungary",
    "Indonesia",
    "Ireland",
    "Israel",
    "Italy",
    "Japan",
    "Latvia",
    "Lithuania",
    "Malaysia",
    "Malta",
    "Morocco",
    "Nepal",
    "Netherlands",
    "New Zealand",
    "Nigeria",
    "Pakistan",
    "Peru",
    "Portugal",
    "Qatar",
    "Romania",
    "Saudi Arabia",
    "Serbia",
    "Singapore",
    "Somalia",
    "South Africa",
    "South Korea",
    "Sri Lanka",
    "Sweden",
    "Switzerland",
    "Taiwan",
    "Thailand",
    "Tunisia",
    "Türkiye",
    "Turkey",
    "United Arab Emirates",
    "UAE",
    "Vietnam",
    # Regional broad match strings present in feed
    "Latin America",
    "EMEA",
    "DACH",
    # Foreign offshore tech centers
    "Hyderabad",
    "Bengaluru",
    "Bangalore",
    "Pune",
    "Chennai",
    "Noida",
    "Gurgaon",
    "Mumbai",
    "Delhi",
]
_EXCLUDED_LOCATION_RE = re.compile(
    r'\b(?:' + "|".join(re.escape(c) for c in EXCLUDED_LOCATIONS) + r'|uk|korea)\b|\b-\s*IN\b|---IN', re.IGNORECASE
)

def is_excluded_company(company: str) -> bool:
    return bool(company) and bool(_EXCLUDED_COMPANY_RE.search(company))

def is_excluded_location(location: str) -> bool:
    return bool(location) and bool(_EXCLUDED_LOCATION_RE.search(location))

# Multi-word phrases keep substring semantics; single-word keywords ("mle",
# "devops") are word-bounded so they can't match inside a word ("Hamlet").
_KEYWORD_RE = re.compile(
    "|".join(
        re.escape(k) if " " in k else rf"\b{re.escape(k)}\b"
        for k in KEYWORDS
    ),
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch(url, max_retries: int = 1, extra_headers: dict | None = None):
    for attempt in range(max_retries + 1):
        try:
            # Rotate user-agent slightly to mimic organic client variety
            headers = dict(HEADERS)
            if extra_headers:
                headers.update(extra_headers)
            headers["User-Agent"] = random.choice(USER_AGENTS)
            req = Request(url, headers=headers)
            with urlopen(req, timeout=15) as r:
                return r.read().decode("utf-8", errors="ignore")
        except HTTPError as e:
            if e.code == 429:
                if attempt < max_retries:
                    backoff = random.uniform(3.0, 5.0) * (attempt + 1)
                    print(f"  ⚠️  HTTP 429 (rate-limited) on {url} — backing off for {backoff:.1f}s (retry {attempt + 1}/{max_retries})...")
                    time.sleep(backoff)
                    continue
                else:
                    cooldown = random.uniform(3.0, 5.0)
                    print(f"  ⚠️  Could not fetch {url}: {e} (cooling down {cooldown:.1f}s)")
                    time.sleep(cooldown)
                    return ""
            print(f"  ⚠️  Could not fetch {url}: {e}")
            return ""
        except (URLError, TimeoutError, OSError, ValueError) as e:
            print(f"  ⚠️  Could not fetch {url}: {e}")
            return ""
    return ""


def has_clearance_requirement(text: str) -> bool:
    return bool(text) and bool(EXCLUDED_CLEARANCE_RE.search(text))


def has_no_sponsorship_requirement(text: str) -> bool:
    return bool(text) and bool(EXCLUDED_NO_SPONSORSHIP_RE.search(text))


def is_dealbreaker_role(title: str = "", text: str = "") -> bool:
    return (has_clearance_requirement(title) or has_clearance_requirement(text) or
            has_no_sponsorship_requirement(title) or has_no_sponsorship_requirement(text))


def is_mle_role(title: str) -> bool:
    if EXCLUDED_SENIORITY_RE.search(title):
        return False
    if is_dealbreaker_role(title):
        return False
    return bool(_KEYWORD_RE.search(title))


BAY_AREA_LOCATIONS = [
    "bay area",
    "san francisco", "south san francisco", "daly city",
    "oakland", "berkeley", "alameda", "emeryville", "richmond",
    "palo alto", "mountain view", "menlo park", "sunnyvale",
    "santa clara", "san jose", "cupertino", "los altos", "los gatos",
    "san mateo", "foster city", "redwood city", "san carlos", "brisbane", "millbrae",
    "san bruno", "burlingame", "belmont",
    "fremont", "hayward", "union city", "newark", "milpitas",
    "concord", "walnut creek", "pleasanton", "dublin", "san ramon",
    "danville", "livermore",
    "novato", "san rafael", "mill valley", "sausalito",
    "vacaville",
]


def is_bay_area(location: str) -> bool:
    if not location:
        return False
    loc = location.lower()
    return any(city in loc for city in BAY_AREA_LOCATIONS)


# Non-Bay US biotech hubs, token → state. Tokens are substring-matched like
# BAY_AREA_LOCATIONS. City names with a well-known non-US or wrong-state
# namesake (Cambridge UK/MD, Durham UK, Pasadena TX, Irvine Scotland,
# Queensland, Manhattan KS, Brooklyn OH/MN…) live in _HUB_AMBIGUOUS and only match when the right state
# also appears in the string — so "Cambridge, MA", "Cambridge, Massachusetts"
# and Workday's "Cambridge Crossing - MA - US" all match while "Cambridge,
# UK" never does. "ny office" catches Greenhouse boards that write NYC that
# way (e.g. Flatiron Health); "tarrytown" is Regeneron's Westchester HQ.
# The state values let discover.py emit a real "City, ST" fallback_location.
US_BIOTECH_HUBS = {
    # NYC
    "new york": "NY", "brooklyn": "NY", "manhattan": "NY",
    "queens": "NY", "long island city": "NY", "tarrytown": "NY",
    "ny office": "NY",
    # Boston / Cambridge
    "boston": "MA", "cambridge": "MA", "somerville": "MA",
    "watertown": "MA", "waltham": "MA",
    # SoCal
    "san diego": "CA", "los angeles": "CA", "thousand oaks": "CA",
    "pasadena": "CA", "irvine": "CA",
    # Seattle
    "seattle": "WA", "bothell": "WA",
    # Research Triangle
    "raleigh": "NC", "durham": "NC", "research triangle": "NC",
    "chapel hill": "NC",
    # Top CS Cities
    "austin": "TX", "dallas": "TX",
    "chicago": "IL",
    "atlanta": "GA",
    "washington": "DC",
    "denver": "CO", "boulder": "CO",
    "portland": "OR",
    "salt lake city": "UT",
    "phoenix": "AZ",
    "miami": "FL",
    "philadelphia": "PA",
    "minneapolis": "MN",
    "cincinnati": "OH",
    "dayton": "OH",
    "columbus": "OH",
}

_HUB_AMBIGUOUS = {"cambridge", "queens", "watertown", "pasadena", "irvine",
                  "durham", "brooklyn", "manhattan", "washington"}

_STATE_CONFIRM = {
    "NY": re.compile(r'\b(ny|new york)\b', re.IGNORECASE),
    "MA": re.compile(r'\b(ma|mass|massachusetts)\b', re.IGNORECASE),
    "CA": re.compile(r'\b(ca|calif|california)\b', re.IGNORECASE),
    "WA": re.compile(r'\b(wa|washington)\b', re.IGNORECASE),
    "NC": re.compile(r'\b(nc|north carolina)\b', re.IGNORECASE),
    "TX": re.compile(r'\b(tx|texas)\b', re.IGNORECASE),
    "IL": re.compile(r'\b(il|illinois)\b', re.IGNORECASE),
    "GA": re.compile(r'\b(ga|georgia)\b', re.IGNORECASE),
    "DC": re.compile(r'\b(dc|district of columbia)\b', re.IGNORECASE),
    "CO": re.compile(r'\b(co|colorado)\b', re.IGNORECASE),
    "OR": re.compile(r'\b(or|oregon)\b', re.IGNORECASE),
    "UT": re.compile(r'\b(ut|utah)\b', re.IGNORECASE),
    "AZ": re.compile(r'\b(az|arizona)\b', re.IGNORECASE),
    "FL": re.compile(r'\b(fl|florida)\b', re.IGNORECASE),
    "PA": re.compile(r'\b(pa|pennsylvania)\b', re.IGNORECASE),
    "MN": re.compile(r'\b(mn|minnesota)\b', re.IGNORECASE),
    "OH": re.compile(r'\b(oh|ohio)\b', re.IGNORECASE),
}


def hub_city_match(text: str):
    """Return (token, state) for the first US biotech hub mentioned in text
    (word-boundary matched, state-confirmed for ambiguous city names), else
    None. Boundaries stop containments like "Queensbury" matching "queens"."""
    low = (text or "").lower()
    for tok, state in US_BIOTECH_HUBS.items():
        if not re.search(rf'\b{re.escape(tok)}\b', low):
            continue
        if tok in _HUB_AMBIGUOUS and not _STATE_CONFIRM[state].search(low):
            continue
        return tok, state
    return None


# Bay Area city names with well-known non-CA namesakes (Dublin IE, Brisbane
# AU, Newark NJ/DE, Richmond VA/UK, Concord NH, Union City NJ, Danville VA).
# The confirmed gate requires CA confirmation for these. is_bay_area() keeps
# the looser substring behavior for legacy callers; the gov watcher and the
# default dispatch gate through is_watch_location(), which uses the confirmed
# variant precisely because they see nationwide location strings.
_BAY_AMBIGUOUS = {"dublin", "brisbane", "newark", "richmond", "concord",
                  "union city", "danville"}


def _bay_area_confirmed(location: str) -> bool:
    loc = (location or "").lower()
    for city in BAY_AREA_LOCATIONS:
        if city not in loc:
            continue
        if city in _BAY_AMBIGUOUS and not _STATE_CONFIRM["CA"].search(loc):
            continue
        return True
    return False


# "new york" counted only in city position (or an explicit NYC form) — the
# bare token would otherwise match upstate strings like "Albany, New York"
# on the state name alone.
_NYC_CITY_RE = re.compile(
    r'^\W*new york\b'
    r'|\bnew york\s*,\s*(ny|new york)\b'
    r'|\bnew york city\b'
    r'|\bnew york metro'
    r'|\bnyc\b'
)


def is_nyc(location: str) -> bool:
    """NYC-metro test over the US_BIOTECH_HUBS NY tokens (word-boundary and
    state-confirmed like hub_city_match). Bare "Queens"/"Brooklyn" never
    match; "Queens, NY"/"Brooklyn, NY" do; "Albany, New York" is rejected by
    the city-position guard."""
    low = (location or "").lower()
    for tok, state in US_BIOTECH_HUBS.items():
        if state != "NY" or not re.search(rf'\b{re.escape(tok)}\b', low):
            continue
        if tok in _HUB_AMBIGUOUS and not _STATE_CONFIRM["NY"].search(low):
            continue
        if tok == "new york" and not _NYC_CITY_RE.search(low):
            continue
        return True
    return False


def is_watch_location(location: str) -> bool:
    """Geo gate for the location-scoped watchers: SF Bay Area or NYC metro.
    Uses _bay_area_confirmed, not is_bay_area — its callers see nationwide
    location strings, where bare "newark"/"richmond"/"concord" substrings
    would otherwise pass for out-of-state agencies."""
    return _bay_area_confirmed(location) or is_nyc(location)


# Remote roles count as US only on an affirmative US signal, or when the
# string names no other geography at all — a blocklist of non-US markets
# can't keep up with strings like "Spain - Remote" (live on Amgen's board).
_US_MARKET_RE = re.compile(r'\b(us|usa|u\.s|united states)\b', re.IGNORECASE)
_BARE_REMOTE = {"remote", "fully remote", "remote first", "remote work",
                "remote position", "work from home"}


def is_remote_us(location: str) -> bool:
    loc = (location or "").lower()
    if "remote" not in loc:
        return False
    if _US_MARKET_RE.search(loc):
        return True
    return re.sub(r'[^a-z]+', ' ', loc).strip() in _BARE_REMOTE


def is_target_location(location: str) -> bool:
    """Bay Area + the other major US biotech hubs + US-remote. Used by the
    biotech sweep (and discover.py) only — the location-scoped watchers use
    the tighter is_watch_location (Bay Area + NYC)."""
    if not location:
        return False
    return (
        _bay_area_confirmed(location)
        or hub_city_match(location) is not None
        or is_remote_us(location)
    )


def extract_location(job: dict) -> str:
    loc = job.get("jobLocation", {})
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address", {})
    if isinstance(addr, dict):
        city = addr.get("addressLocality", "")
        state = addr.get("addressRegion", "")
        return f"{city}, {state}".strip(", ")
    return str(addr)


# ---------------------------------------------------------------------------
# Posted-date normalization
# ---------------------------------------------------------------------------
# Every `date_posted` in this repo means ONE thing: the calendar day in
# America/Los_Angeles. The scrapers run on GitHub Actions runners, which are
# UTC, so anything stamped after 17:00 PDT (00:00 UTC) used to land on
# *tomorrow's* date from the dashboard's point of view — 34 roles were dated
# 2026-07-29 on the evening of the 28th. Deriving the day in LOCAL_TZ, and
# clamping bare upstream dates that are still in the future, is the fix.
#
# The tz lookup is guarded because a runner without a system tzdb must degrade
# the dates, not crash the scrape. Guard the CALL, not the import: `import
# ZoneInfo` always succeeds, and guarding it instead would leave LOCAL_TZ
# undefined and blow up at first use.
try:
    LOCAL_TZ = ZoneInfo("America/Los_Angeles")
except (ZoneInfoNotFoundError, KeyError):  # pragma: no cover - needs a broken tzdb
    print("⚠️  tzdb missing — posted dates will fall back to UTC days "
          "(install `tzdata`); expect off-by-one dates after 5pm Pacific")
    LOCAL_TZ = timezone.utc


def local_today() -> date:
    """Today's calendar day in LOCAL_TZ (not the runner's UTC day)."""
    return datetime.now(LOCAL_TZ).date()


def normalize_posted_date(value, *, today: date | None = None) -> str:
    """
    Coerce a posting date to the LOCAL_TZ calendar day, as 'YYYY-MM-DD'.

    Three kinds of input arrive here and each is handled differently:

    - A timestamp with a clock time (Greenhouse `updated_at`, Ashby
      `publishedAt`) is a real instant → convert it to LOCAL_TZ and take the
      day. Naive values are assumed UTC, matching _parse_posted_at.
    - A bare 'YYYY-MM-DD' (LinkedIn's <time datetime>, jobspy) is already
      day-resolution with no clock to convert, so the best we can do is refuse
      to show a day that hasn't happened yet: clamp it to `today`.
    - Anything else ("Posted Today", "Posted 9 Days Ago") passes through
      untouched — the dashboard's jobDateMs() already understands those.

    Never returns a day later than `today`. `today` is a test seam.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if today is None:
        today = local_today()

    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
        try:
            parsed_day = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return raw
        return min(parsed_day, today).strftime("%Y-%m-%d")

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw  # relative string ("Posted Today") or something unparseable
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return min(parsed.astimezone(LOCAL_TZ).date(), today).strftime("%Y-%m-%d")


def _parse_posted_at(value: str, *, now: datetime | None = None) -> datetime | None:
    """
    Parse ATS posting dates into UTC datetimes.

    Some ATS APIs return exact ISO dates/datetimes, while Workday often returns
    relative strings like "Posted Today" or "Posted 3 hours ago".
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    raw = (value or "").strip()
    if not raw:
        return None

    text = re.sub(r'\s+', ' ', raw).strip().lower()
    text = text.removeprefix("posted ").strip()

    if text in {"today", "just posted", "just now"}:
        return now

    relative_m = re.search(
        r'(\d+)\s*(minutes?|mins?|hours?|hrs?)\b(?:\s*ago)?',
        text,
    )
    if relative_m:
        amount = int(relative_m.group(1))
        unit = relative_m.group(2)
        if unit.startswith(("minute", "min")):
            return now - timedelta(minutes=amount)
        return now - timedelta(hours=amount)

    iso_value = raw.replace("Z", "+00:00")
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', iso_value):
            # A bare date is a CALENDAR DAY, and every date_posted in this repo
            # means a LOCAL_TZ day (see normalize_posted_date). Reading it as
            # UTC midnight would make a role stamped with today's Pacific date
            # look up to 7h older than it is — enough to push a 30-minute-old
            # posting past the 24h FRESH_JOB_LOOKBACK and drop it as stale.
            parsed = datetime.strptime(iso_value, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
        else:
            # A naive TIMESTAMP is a real instant, not a calendar day — an ATS
            # emitting one almost certainly means UTC. Do not "fix" this to
            # LOCAL_TZ; that would shift genuine instants by 7 hours.
            parsed = datetime.fromisoformat(iso_value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
        return parsed
    except ValueError:
        return None


def is_recent_posting(job: dict, *, now: datetime | None = None) -> bool:
    posted_at = _parse_posted_at(job.get("date_posted", ""), now=now)
    if posted_at is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return timedelta(0) <= now - posted_at <= FRESH_JOB_LOOKBACK


# ---------------------------------------------------------------------------
# Curated Bay Area biotechs — direct ATS probes (Greenhouse / Workday)
# ---------------------------------------------------------------------------

# Each entry must include: name, ats, fallback_location, and the ATS-specific id
# - greenhouse: "slug" (used in boards-api.greenhouse.io/v1/boards/{slug}/jobs)
# - ashby:      "slug" (used in api.ashbyhq.com/posting-api/job-board/{slug})
# - lever:      "slug" (used in api.lever.co/v0/postings/{slug}?mode=json)
# - workday:    "url"  (full /wday/cxs/{tenant}/{site}/jobs endpoint)
# Curated direct biotech sweep retired per user preference (software / MLE focus).
CURATED_BIOTECHS: list[dict] = []


def probe_curated_greenhouse(entry: dict) -> list:
    time.sleep(REQUEST_DELAY)
    url = f"https://boards-api.greenhouse.io/v1/boards/{entry['slug']}/jobs?content=true"
    raw = fetch(url)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    jobs = []
    for job in data.get("jobs", []):
        title = job.get("title", "")
        if not is_mle_role(title):
            continue
        loc = (job.get("location") or {}).get("name", "") or entry["fallback_location"]
        jobs.append({
            "company": entry["name"],
            "title": title,
            "location": loc,
            "url": job.get("absolute_url", f"https://boards.greenhouse.io/{entry['slug']}"),
            "date_posted": normalize_posted_date(job.get("updated_at")),
            "ats": "Greenhouse",
        })
    return jobs


def probe_curated_ashby(entry: dict) -> list:
    time.sleep(REQUEST_DELAY)
    url = f"https://api.ashbyhq.com/posting-api/job-board/{entry['slug']}"
    raw = fetch(url)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    jobs = []
    for job in data.get("jobs", []):
        title = job.get("title", "")
        if not is_mle_role(title):
            continue
        jobs.append({
            "company": entry["name"],
            "title": title,
            "location": job.get("location") or entry["fallback_location"],
            "url": job.get("jobUrl", f"https://jobs.ashbyhq.com/{entry['slug']}"),
            "date_posted": normalize_posted_date(job.get("publishedAt")),
            "ats": "Ashby",
        })
    return jobs


def probe_curated_lever(entry: dict) -> list:
    time.sleep(REQUEST_DELAY)
    url = f"https://api.lever.co/v0/postings/{entry['slug']}?mode=json"
    raw = fetch(url)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    jobs = []
    for job in data:
        title = job.get("text", "")
        if not is_mle_role(title):
            continue
        created_ms = job.get("createdAt") or 0
        date_posted = (
            datetime.fromtimestamp(created_ms / 1000, tz=LOCAL_TZ).strftime("%Y-%m-%d")
            if created_ms else ""
        )
        jobs.append({
            "company": entry["name"],
            "title": title,
            "location": (job.get("categories") or {}).get("location")
                        or entry["fallback_location"],
            "url": job.get("hostedUrl", f"https://jobs.lever.co/{entry['slug']}"),
            "date_posted": date_posted,
            "ats": "Lever",
        })
    return jobs


WORKDAY_SEARCH_TERMS = [
    "software engineer",
    "backend engineer",
    "C++",
    "cloud engineer",
    "site reliability",
    "data engineer",
    "AI engineer",
    "machine learning",
    "MLOps",
]
# Workday's CXS API caps each response at 20 results; page up to this many
# results per search term (3 pages) so big-pharma tenants aren't truncated
# to the first response.
WORKDAY_MAX_PER_TERM = 60


def _workday_posting_locations(entry: dict, ext_path: str) -> str:
    """Resolve a multi-location Workday posting's real cities from its detail
    JSON (jobPostingInfo.location + additionalLocations). Returns the first
    location that passes the target gate, else all of them joined (which then
    correctly fails the gate), else "" on fetch/parse failure."""
    time.sleep(REQUEST_DELAY)
    raw = fetch(entry["url"].rsplit("/jobs", 1)[0] + ext_path)
    if not raw:
        return ""
    try:
        info = json.loads(raw).get("jobPostingInfo") or {}
    except json.JSONDecodeError:
        return ""
    locs = [info.get("location") or ""]
    locs += [l for l in (info.get("additionalLocations") or []) if isinstance(l, str)]
    locs = [l for l in locs if l]
    for l in locs:
        if is_target_location(l):
            return l
    return "; ".join(locs)


def probe_curated_workday(entry: dict) -> list:
    """
    Workday's /jobs endpoint sometimes 400s on empty searchText, so we hit it
    once per term and dedupe by externalPath.
    """
    domain_m = re.match(r'https://([^/]+)', entry["url"])
    domain = domain_m.group(1) if domain_m else ""
    site_m = re.search(r'/wday/cxs/[^/]+/([^/]+)/jobs', entry["url"])
    site = site_m.group(1) if site_m else ""

    seen: dict[str, dict] = {}
    for term in WORKDAY_SEARCH_TERMS:
        # Big-pharma tenants return hundreds of hits per term; page past the
        # 20-result cap (bounded, so runtime stays sane on the daily sweep).
        offset = 0
        while offset < WORKDAY_MAX_PER_TERM:
            time.sleep(REQUEST_DELAY)
            body = json.dumps({"appliedFacets": {}, "limit": 20,
                               "offset": offset, "searchText": term}).encode()
            try:
                req = Request(
                    entry["url"],
                    data=body,
                    headers={**HEADERS, "Content-Type": "application/json", "Accept": "application/json"},
                )
                with urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8", errors="ignore"))
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                print(f"  ⚠️  Workday {entry['name']} ({term!r}): {e}")
                break

            postings = data.get("jobPostings", [])
            for posting in postings:
                ext_path = posting.get("externalPath", "")
                if ext_path in seen:
                    continue
                title = posting.get("title", "")
                if not is_mle_role(title):
                    continue
                public_url = f"https://{domain}/{site}{ext_path}" if ext_path else entry["url"]
                # Some tenants (e.g. Moderna) omit locationsText and put the
                # location in bulletFields[0] — but on other tenants
                # bulletFields[0] is a requisition id, so only trust it when
                # it contains no digits.
                loc = posting.get("locationsText", "")
                if not loc:
                    bullets = posting.get("bulletFields")
                    first = bullets[0] if isinstance(bullets, list) and bullets else ""
                    if isinstance(first, str) and first and not any(ch.isdigit() for ch in first):
                        loc = first
                loc = loc or entry["fallback_location"]
                # Workday summarizes multi-location roles as "N Locations" —
                # resolve the real cities from the detail endpoint so hub
                # roles aren't relabeled with a fallback the gate rejects
                # (BMS: Princeton) or blindly credited to HQ.
                if re.match(r'^\d+ Locations?$', loc):
                    real = _workday_posting_locations(entry, ext_path) if ext_path else ""
                    loc = real or entry["fallback_location"]
                seen[ext_path] = {
                    "company": entry["name"],
                    "title": title,
                    "location": loc,
                    "url": public_url,
                    "date_posted": posting.get("postedOn") or "",
                    "ats": "Workday",
                }
            offset += 20
            if not postings or offset >= (data.get("total") or 0):
                break
    return list(seen.values())


# ---------------------------------------------------------------------------
# Custom / own-site careers pages — best-effort HTML extraction
# ---------------------------------------------------------------------------
# For startups that post on their own site rather than a supported ATS API.
# Heuristic and best-effort: it CANNOT see JS-rendered job lists (stdlib can't
# run JS), so those companies yield nothing — a known gap, not a bug. Every
# probe fails soft (returns []) so one broken page never kills the run.

_ROBOTS_CACHE: dict = {}
_JOB_HREF_RE = re.compile(r'/job|/careers?/|greenhouse|lever|ashby|workable', re.IGNORECASE)


def _robots_allows(url: str) -> bool:
    """
    Best-effort robots.txt check with a hard timeout. RobotFileParser.read()
    has no timeout and can hang the daily run on a slow host, so we fetch
    robots.txt via the timeout-guarded fetch() and hand it to .parse().
    Fail open (allow) when robots.txt is missing/unreachable — browser posture.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
    except ValueError:
        return True
    if base not in _ROBOTS_CACHE:
        txt = fetch(urllib.parse.urljoin(base, "/robots.txt"))
        rp = None
        if txt:
            rp = urllib.robotparser.RobotFileParser()
            try:
                rp.parse(txt.splitlines())
            except Exception:
                rp = None
        _ROBOTS_CACHE[base] = rp
    rp = _ROBOTS_CACHE[base]
    if rp is None:
        return True
    try:
        return rp.can_fetch(HEADERS["User-Agent"], url)
    except Exception:
        return True


class _CareersHTMLParser(HTMLParser):
    """Collect (anchor_text, href) pairs and heading/list text, skipping
    nav/footer/header/script/style regions."""
    _SKIP = {"nav", "footer", "header", "script", "style"}
    _TEXT_TAGS = {"a", "h1", "h2", "h3", "h4", "li"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._tag_stack: list = []
        self._cur_href = None
        self._cur_text: list = []
        self.links: list = []   # (text, href)
        self.texts: list = []   # non-anchor heading/list text

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag in self._TEXT_TAGS:
            self._tag_stack.append(tag)
            self._cur_text = []
            self._cur_href = dict(attrs).get("href") if tag == "a" else None

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if self._tag_stack and tag == self._tag_stack[-1]:
            text = " ".join("".join(self._cur_text).split())
            if text and self._skip_depth == 0:
                if tag == "a":
                    self.links.append((text, self._cur_href or ""))
                else:
                    self.texts.append(text)
            self._tag_stack.pop()
            self._cur_text = []

    def handle_data(self, data):
        if self._tag_stack:
            self._cur_text.append(data)


def _slugify(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:60]


def probe_curated_custom(entry: dict) -> list:
    """
    Best-effort scrape of a company's own careers page (no supported ATS API).
    Keeps anchor/heading text that looks like a job title AND passes is_mle_role.
    Roles without a dedicated link get a `careers_url#slug(title)` identity so
    they don't collide in _job_identity / all_jobs dedup. Fails soft.
    """
    careers_url = entry.get("careers_url", "")
    if not careers_url:
        return []
    if not _robots_allows(careers_url):
        print(f"  ⚠️  robots.txt disallows {careers_url} — skipping {entry['name']}")
        return []
    time.sleep(REQUEST_DELAY)
    html = fetch(careers_url)
    if not html:
        return []
    parser = _CareersHTMLParser()
    try:
        parser.feed(html)
    except Exception as e:
        print(f"  ⚠️  Custom parse failed for {entry['name']}: {e}")
        return []

    loc = entry.get("fallback_location", "")
    seen_titles: set = set()
    jobs: list = []

    def _add(title: str, url: str):
        title = title.strip()
        key = title.lower()
        if not title or len(title) > 100 or key in seen_titles:
            return
        if not is_mle_role(title):
            return
        seen_titles.add(key)
        jobs.append({
            "company": entry["name"], "title": title, "location": loc,
            "url": url, "date_posted": "", "ats": "Custom",
        })

    # Anchors whose href looks job-like give a real per-role URL.
    for text, href in parser.links:
        if _JOB_HREF_RE.search(href or ""):
            _add(text, urllib.parse.urljoin(careers_url, href) if href else careers_url)
    # Heading/list titles with no dedicated link — synthesize a distinct URL.
    for text in parser.texts:
        _add(text, f"{careers_url}#{_slugify(text)}")
    return jobs



def scrape_curated_biotechs() -> list:
    companies = list(CURATED_BIOTECHS)
    if not companies:
        return []
    print(f"🔬 Scraping {len(companies)} curated biotech leaders...")
    all_jobs: list = []
    for entry in companies:
        if entry["ats"] == "greenhouse":
            jobs = probe_curated_greenhouse(entry)
        elif entry["ats"] == "ashby":
            jobs = probe_curated_ashby(entry)
        elif entry["ats"] == "lever":
            jobs = probe_curated_lever(entry)
        elif entry["ats"] == "workday":
            jobs = probe_curated_workday(entry)
        elif entry["ats"] == "custom":
            jobs = probe_curated_custom(entry)
        else:
            print(f"  ⚠️  Unknown ATS for {entry['name']}: {entry['ats']}")
            continue
        if jobs:
            print(f"  ✅ {entry['name']}: {len(jobs)} role(s)")
            all_jobs.extend(jobs)
    return all_jobs


# ---------------------------------------------------------------------------
# Genentech — custom Phenom ATS, kept as standalone
# ---------------------------------------------------------------------------

def scrape_genentech():
    print("🔍 Scraping Genentech...")
    url = (
        "https://careers.gene.com/us/en/search-results"
        "?keywords=machine+learning+engineer&category=Data+Science+%26+AI%2FML"
    )
    html = fetch(url)
    jobs = []

    matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    for match in matches:
        try:
            data = json.loads(match)
            items = (
                data if isinstance(data, list)
                else data.get("itemListElement", []) if data.get("@type") == "ItemList"
                else [data]
            )
            for item in items:
                job = item.get("item", item)
                title = job.get("title", job.get("name", ""))
                if title and is_mle_role(title):
                    jobs.append({
                        "company": "Genentech",
                        "title": title,
                        "location": extract_location(job),
                        "url": job.get("url", "https://careers.gene.com/us/en/c/data-science-ai-ml-jobs"),
                        "date_posted": job.get("datePosted", ""),
                        "ats": "Phenom",
                    })
        except json.JSONDecodeError:
            continue

    if not jobs:
        title_matches = re.findall(r'data-ph-at-job-title-text="([^"]+)"', html)
        link_matches = re.findall(r'href="(/us/en/job/[^"]+)"', html)
        for i, title in enumerate(title_matches):
            if is_mle_role(title):
                link = link_matches[i] if i < len(link_matches) else ""
                jobs.append({
                    "company": "Genentech",
                    "title": title,
                    "location": "South San Francisco, CA",
                    "url": f"https://careers.gene.com{link}" if link else "https://careers.gene.com/us/en/c/data-science-ai-ml-jobs",
                    "date_posted": "",
                    "ats": "Phenom",
                })

    print(f"  ✅ Found {len(jobs)} MLE role(s) at Genentech")
    return jobs


# ---------------------------------------------------------------------------
# LinkedIn — public guest endpoint, bucketed by recency (broad US-wide net)
# ---------------------------------------------------------------------------

LINKEDIN_SEARCH_TERMS = [
    "software engineer",
    "backend engineer",
    "systems engineer",
    "c++ engineer",
    "cloud engineer",
    "platform engineer",
    "data engineer",
    "etl engineer",
    "ai engineer",
    "machine learning engineer",
    "mlops engineer",
    "computer vision engineer",
]

# We use a 4h lookback because GitHub Actions often throttles hourly crons
LINKEDIN_LOOKBACK_SECONDS = 7200         # 2h
LINKEDIN_BIOTECH_LOOKBACK_SECONDS = 86400 # 24h — biotech is a daily 8pm PT digest

# Guest-endpoint geo scopes as (display name, LinkedIn geoId) pairs.
# geoId 90000070 (NYC metro) verified live against the endpoint 2026-07-21.
# NOTE: To search the other top CS cities on LinkedIn, you must manually find their geoId
# (by searching on linkedin.com/jobs and looking at the geoId parameter in the URL) and add them here.
LINKEDIN_LOCATIONS = [
    # ---- Active: Greater Cincinnati Area + Commutable Hubs + US Remote ----
    ("Cincinnati, OH", "106310628"),                      # Local Greater Cincinnati (Mason, West Chester, NKY)
    ("Dayton, OH", "103326162"),                          # ~50 mins north on I-75 (WPAFB, UDRI, tech)
    ("Columbus, OH", "105838153"),                        # ~1.5h drive on I-71 (JPMorgan Polaris, state tech)
    ("United States", "103644278", "2"),                 # 100% Nationwide US Remote via LinkedIn f_WT=2 filter

    # ---- Previous Nationwide Hubs (Uncomment as desired) ----
    ("San Francisco Bay Area", "90000084"),
    ("New York City Metropolitan Area", "90000070"),
    ("Seattle, WA", "104116203"),
    ("Austin, TX", "90000064"),
    ("Boston, MA", "102380872"),
    ("Los Angeles, CA", "102448103"),
    ("Chicago, IL", "10311267"),
    ("Atlanta, GA", "106224388"),
    ("Washington, DC", "103977389"),
    ("Denver, CO", "103736294"),
    ("Dallas, TX", "104194190"),
    ("San Diego, CA", "103918656"),
    ("Portland, OR", "90000079"),
    ("Raleigh, NC", "100197101"),
    ("Boulder, CO", "102597912"),
    ("Salt Lake City, UT", "90000716"),
    ("Phoenix, AZ", "100219842"),
    ("Miami, FL", "102394087"),
    ("Philadelphia, PA", "104937023"),
    ("Indianapolis, IN", "100871315"),
    ("Minneapolis, MN", "103039849"),
]

# Biotech allowlist used by the LinkedIn-side filter. Broader than CURATED_BIOTECHS
# (which only covers the companies with direct Greenhouse/Ashby/Workday probes) because
# the public LinkedIn endpoint surfaces a wider universe of biotech employers.
# Match is case-insensitive on alphanum-stripped names with bidirectional substring
# matching, so "Genentech" matches "Genentech, Inc." and vice versa. Avoid names
# shorter than ~6 chars to limit incidental substring collisions.
BIOTECH_COMPANY_NAMES = [
    # Direct-scrape biotechs (kept aligned with CURATED_BIOTECHS)
    "10x Genomics", "Twist Bioscience", "Maze Therapeutics", "Freenome",
    "Cytokinetics", "Natera", "Inceptive", "Atomwise", "Profluent",
    "Eikon Therapeutics", "Altos Labs", "Arc Institute", "Caribou Biosciences",
    "Octant Bio", "Gilead Sciences", "Xaira Therapeutics", "Formation Bio",
    "Septerna", "Chai Discovery", "Aralez Bio", "Axiom Bio",
    # Big pharma / biotech with Bay Area / NYC MLE hiring
    "Genentech", "AbbVie", "Amgen", "BioMarin", "Vertex Pharmaceuticals",
    "Bristol Myers Squibb", "Regeneron", "Pfizer",
    # Sequencing / genomics platforms
    "Illumina", "Pacific Biosciences", "PacBio", "Element Biosciences",
    "Ultima Genomics", "Singular Genomics",
    # Clinical genomics / diagnostics
    "GRAIL", "Guardant Health", "Invitae", "Color Health", "Tempus AI",
    "Foundation Medicine", "Veracyte", "Personalis", "Karius",
    "Adaptive Biotechnologies",
    # ML-driven drug discovery
    "Recursion Pharmaceuticals", "Insitro", "Schrodinger", "Schrödinger",
    "Relay Therapeutics", "Generate Biomedicines", "Isomorphic Labs",
    "AbCellera", "Iambic Therapeutics", "Lila Sciences",
    # Cell / gene therapy
    "Sana Biotechnology", "Allogene Therapeutics", "Cellares",
    "Beam Therapeutics", "Editas Medicine", "Intellia Therapeutics",
    "CRISPR Therapeutics",
    # Bay Area biotech & life-sci research
    "Verily Life Sciences", "Calico Life Sciences", "Synthego",
    "Buck Institute", "Chan Zuckerberg Biohub", "Chan Zuckerberg Initiative",
]

BIOTECH_COMPANY_ALLOWLIST = frozenset(
    re.sub(r'[^a-z0-9]', '', n.lower()) for n in BIOTECH_COMPANY_NAMES
)


def _is_biotech_company(name: str) -> bool:
    norm = re.sub(r'[^a-z0-9]', '', (name or "").lower())
    if not norm:
        return False
    return any(b in norm or norm in b for b in BIOTECH_COMPANY_ALLOWLIST)


def _parse_linkedin_cards(html: str) -> tuple[list[dict], int]:
    """Returns (keyword-matched cards, raw card count on the page). The raw
    count lets callers distinguish 'page full of non-matching roles' (keep
    paginating) from 'no results at all' (stop)."""
    import html as html_mod
    cards = re.split(r'<li[^>]*>', html)[1:]
    parsed = []
    raw_count = 0
    for card in cards:
        urn = re.search(r'data-entity-urn="urn:li:jobPosting:(\d+)"', card)
        if not urn:
            continue
        raw_count += 1
        title_m = re.search(r'base-search-card__title[^>]*>\s*([^<]+)', card)
        company_m = re.search(
            r'base-search-card__subtitle[^>]*>.*?<a[^>]*>\s*([^<]+)\s*</a>',
            card, re.DOTALL,
        ) or re.search(r'base-search-card__subtitle[^>]*>\s*([^<]+)', card)
        location_m = re.search(r'job-search-card__location[^>]*>\s*([^<]+)', card)
        time_m = re.search(r'<time[^>]*datetime="([^"]+)"', card)

        title = html_mod.unescape(title_m.group(1).strip()) if title_m else ""
        if not title or not is_mle_role(title):
            continue
        company = (
            html_mod.unescape(re.sub(r'\s+', ' ', company_m.group(1).strip()))
            if company_m else "Unknown"
        )
        if is_excluded_company(company):
            continue
        location = html_mod.unescape(
            (location_m.group(1).strip() if location_m else "")
        ).replace("\n", " ")
        if is_excluded_location(location):
            continue
        parsed.append({
            "id": urn.group(1),
            "company": company,
            "title": title,
            "location": location,
            "date_posted": time_m.group(1) if time_m else "",
        })
    return parsed, raw_count


def _linkedin_search(terms: list[str], lookback_seconds: int) -> tuple[list[dict], int]:
    """
    Per-term, paginated LinkedIn guest-endpoint search. Dedupes by job ID and
    sorts by recency. Used by both the general MLE/DS watcher and the biotech
    allowlist-filtered scrape.

    Returns (jobs, total_raw_cards). total_raw_cards == 0 across every term
    means LinkedIn gave us no data at all — the callers' block guard.
    """
    jobs_by_id: dict[str, dict] = {}
    total_raw_cards = 0
    for loc_item, term in itertools.product(LINKEDIN_LOCATIONS, terms):
        if len(loc_item) >= 3:
            loc_name, geo_id, f_wt = loc_item[0], loc_item[1], loc_item[2]
        else:
            loc_name, geo_id = loc_item[0], loc_item[1]
            f_wt = None

        wt_param = f"&f_WT={f_wt}" if f_wt else ""
        for start in range(0, 75, 25):
            polite_sleep(0.6, 0.8, 1.5)
            url = (
                "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                f"?keywords={urllib.parse.quote(term)}"
                f"&location={urllib.parse.quote(loc_name)}"
                f"&geoId={geo_id}"
                f"&f_TPR=r{lookback_seconds}"
                f"{wt_param}"
                f"&start={start}"
            )
            html = fetch(url, extra_headers=LINKEDIN_HEADERS)
            if not html.strip():
                break
            parsed, raw_count = _parse_linkedin_cards(html)
            total_raw_cards += raw_count
            # Break on a truly empty page, NOT on "no keyword matches" — a page
            # of 25 off-target roles must not end pagination for the term.
            if not raw_count:
                break
            for p in parsed:
                if p["id"] in jobs_by_id:
                    continue
                loc = p["location"]
                is_rem = bool(
                    "remote" in loc.lower()
                    or "remote" in p["title"].lower()
                    or loc.strip().lower() in ("remote", "united states", "")
                )
                jobs_by_id[p["id"]] = {
                    "company": p["company"],
                    "title": p["title"],
                    "location": loc,
                    "url": f"https://www.linkedin.com/jobs/view/{p['id']}/",
                    "date_posted": p["date_posted"],
                    "ats": "LinkedIn",
                    "remote": is_rem,
                }
            # If fewer than 25 raw cards were returned, there is no subsequent page
            if raw_count < 25:
                break

    jobs = list(jobs_by_id.values())
    jobs.sort(key=lambda j: -_iso_to_ts(j.get("date_posted", "")))
    return jobs, total_raw_cards


def scrape_linkedin_recent() -> list:
    print(f"🔎 Scraping LinkedIn (last {LINKEDIN_LOOKBACK_SECONDS // 3600}h)...")
    jobs, raw_cards = _linkedin_search(LINKEDIN_SEARCH_TERMS, LINKEDIN_LOOKBACK_SECONDS)
    # Block guard (mirrors Indeed's): zero raw cards across every term means
    # LinkedIn gave us nothing — rate-limited or blocked, not a quiet hour.
    # Reuse the previous results so we don't clobber the dedupe baseline.
    if raw_cards == 0:
        prev = _load_prev_jobs(os.path.join(SCRIPT_DIR, "linkedin_jobs.json"))
        print(f"  ⛔ LinkedIn returned 0 cards across all terms (likely blocked); "
              f"preserving previous {len(prev)} result(s)")
        return prev
    print(f"  ✅ LinkedIn: {len(jobs)} role(s)")
    # Salary backfill skipped to avoid secondary rate-limiting on individual jobPosting endpoints
    return jobs


def scrape_linkedin_nightly_tech() -> list:
    """
    Last 24h on LinkedIn for Tech & Engineering roles across US hubs & remote.
    Filters out excluded spam staffing agencies and foreign locations.
    """
    print(f"⚡ Scraping LinkedIn tech sweep (last {LINKEDIN_BIOTECH_LOOKBACK_SECONDS // 3600}h)...")
    raw, raw_cards = _linkedin_search(LINKEDIN_SEARCH_TERMS, LINKEDIN_BIOTECH_LOOKBACK_SECONDS)
    if raw_cards == 0:
        print("  ⛔ LinkedIn returned 0 cards across all terms (likely blocked); "
              "skipping LinkedIn for this digest")
        return []
    jobs = [
        j for j in raw
        if not is_excluded_company(j.get("company", ""))
        and not is_excluded_location(j.get("location", ""))
    ]
    print(f"  ✅ Nightly Tech LinkedIn: {len(jobs)} role(s) (from {len(raw)} total)")
    return jobs


# Backward-compat alias
scrape_linkedin_biotech = scrape_linkedin_nightly_tech


# ---------------------------------------------------------------------------
# Indeed — via python-jobspy (Indeed's RSS feeds + Publisher API were both
# deprecated in 2026, and indeed.com sits behind Cloudflare top-tier bot
# protection. JobSpy uses Indeed's mobile-app API internally — no proxies
# required, no documented rate limit.)
# ---------------------------------------------------------------------------

INDEED_LOOKBACK_HOURS = 24  # Indeed posting dates are ~day-resolution, so a 1h window
# returns almost nothing; the hourly watcher's cross-run dedupe trims the overlap.

# jobspy returns the full JD (markdown) for Indeed rows. We keep a trimmed copy
# in indeed_jobs.json (bounded: 24h window) so the nightly triage agent can
# judge Indeed roles from the actual description instead of the title alone.
# _merge_into_all_jobs strips it so the dashboard's master stays lean.
INDEED_JD_MAX_CHARS = 6000

JOBSPY_LOCATIONS = [
    "San Francisco, CA", "New York, NY", "Seattle, WA", "Austin, TX",
    "Boston, MA", "Los Angeles, CA", "Chicago, IL", "Atlanta, GA",
    "Washington, DC", "Denver, CO", "Dallas, TX", "San Diego, CA",
    "Portland, OR", "Raleigh, NC", "Boulder, CO", "Salt Lake City, UT",
    "Phoenix, AZ", "Miami, FL", "Philadelphia, PA", "Minneapolis, MN",
    "Cincinnati, OH"
]


def scrape_indeed_recent() -> list:
    """Indeed MLE/DS roles posted in the last INDEED_LOOKBACK_HOURS, SF Bay Area + NYC."""
    print(f"🟦 Scraping Indeed (last {INDEED_LOOKBACK_HOURS}h)...")
    try:
        from jobspy import scrape_jobs as jobspy_scrape
    except ImportError:
        print("  ⚠️  python-jobspy not installed; skipping Indeed")
        return []

    jobs_by_id: dict[str, dict] = {}
    ok_terms = 0
    errored_terms = 0
    raw_rows = 0
    for location, term in itertools.product(JOBSPY_LOCATIONS, LINKEDIN_SEARCH_TERMS):
        time.sleep(REQUEST_DELAY)  # throttle: back-to-back calls invite blocking on CI IPs
        try:
            # JobSpy Indeed gotcha: hours_old / is_remote / job_type / easy_apply
            # are mutually exclusive — only one may be set, or the time filter
            # silently breaks. Keep hours_old; do not add the others.
            df = jobspy_scrape(
                site_name=["indeed"],
                search_term=term,
                location=location,
                distance=50,
                results_wanted=50,
                hours_old=INDEED_LOOKBACK_HOURS,
                country_indeed="USA",
            )
        except Exception as e:
            errored_terms += 1
            print(f"  ⚠️  Indeed ({term!r} · {location}): {e}")
            continue
        ok_terms += 1
        if df is None or df.empty:
            continue
        raw_rows += len(df)
        df.columns = [c.lower() for c in df.columns]
        df = df.fillna("")
        for _, row in df.iterrows():
            title = str(row.get("title", "") or "")
            if not is_mle_role(title):
                continue
            desc = str(row.get("description", "") or "")
            if is_dealbreaker_role(title, desc):
                continue
            if is_excluded_company(str(row.get("company", "") or "")):
                continue
            url = str(row.get("job_url", "") or "")
            ident = _job_identity(url)
            if ident in jobs_by_id:
                continue
            loc = str(row.get("location", "") or "")
            if not loc:
                city = str(row.get("city", "") or "")
                state = str(row.get("state", "") or "")
                loc = ", ".join(p for p in [city, state] if p)
            jobs_by_id[ident] = {
                "company": str(row.get("company", "") or "Unknown"),
                "title": title,
                "location": loc,
                "url": url,
                "date_posted": str(row.get("date_posted", "") or ""),
                "description": str(row.get("description", "") or "")[:INDEED_JD_MAX_CHARS],
                "salary": format_salary(
                    row.get("min_amount", ""),
                    row.get("max_amount", ""),
                    row.get("interval", ""),
                ),
                "ats": "Indeed",
            }
    jobs = list(jobs_by_id.values())
    print(
        f"  📊 Indeed: {len(LINKEDIN_SEARCH_TERMS)} terms × {len(JOBSPY_LOCATIONS)} metros → "
        f"{ok_terms} ok / {errored_terms} errored · {raw_rows} raw, {len(jobs)} matched"
    )

    # Block guard: zero rows pulled across every term means Indeed gave us no data
    # — a hard block (calls raised) or a soft block (empty frames). This is NOT the
    # same as "rows returned but none matched our keywords" (raw_rows > 0, jobs == []),
    # which is a legitimate empty result. On a no-data run, reuse the previous results
    # so we don't clobber the dedupe baseline (and the dashboard's Indeed column) with
    # an empty file; save_indeed_results() then reports 0 new (all already seen).
    if raw_rows == 0:
        prev = _load_prev_jobs(os.path.join(SCRIPT_DIR, "indeed_jobs.json"))
        print(
            f"  ⛔ Indeed returned 0 rows across all terms (likely blocked); "
            f"preserving previous {len(prev)} result(s)"
        )
        return prev

    return jobs


BOARDS_LOOKBACK_HOURS = 24  # same day-resolution rationale as Indeed
_BOARDS_ATS_LABELS = {"zip_recruiter": "ZipRecruiter", "google": "Google"}


def scrape_boards_recent() -> list:
    """ZipRecruiter + Google Jobs via jobspy — same pipeline shape as Indeed."""
    print(f"🟪 Scraping ZipRecruiter + Google Jobs (last {BOARDS_LOOKBACK_HOURS}h)...")
    try:
        from jobspy import scrape_jobs as jobspy_scrape
    except ImportError:
        print("  ⚠️  python-jobspy not installed; skipping boards")
        return []

    jobs_by_id: dict[str, dict] = {}
    ok_terms = 0
    errored_terms = 0
    raw_rows = 0
    for location, term in itertools.product(JOBSPY_LOCATIONS, LINKEDIN_SEARCH_TERMS):
        time.sleep(REQUEST_DELAY)
        try:
            # Same jobspy gotcha as Indeed: keep hours_old, don't add the other
            # mutually-exclusive filters. Google ignores plain search_term —
            # it needs the full google_search_term query string.
            df = jobspy_scrape(
                site_name=["zip_recruiter", "google"],
                search_term=term,
                google_search_term=(
                    f"{term} jobs near {location} since yesterday"
                ),
                location=location,
                distance=50,
                results_wanted=50,
                hours_old=BOARDS_LOOKBACK_HOURS,
            )
        except Exception as e:
            errored_terms += 1
            print(f"  ⚠️  Boards ({term!r} · {location}): {e}")
            continue
        ok_terms += 1
        if df is None or df.empty:
            continue
        raw_rows += len(df)
        df.columns = [c.lower() for c in df.columns]
        df = df.fillna("")
        for _, row in df.iterrows():
            title = str(row.get("title", "") or "")
            if not is_mle_role(title):
                continue
            desc = str(row.get("description", "") or "")
            if is_dealbreaker_role(title, desc):
                continue
            if is_excluded_company(str(row.get("company", "") or "")):
                continue
            url = str(row.get("job_url", "") or "")
            ident = _job_identity(url)
            if ident in jobs_by_id:
                continue
            loc = str(row.get("location", "") or "")
            if not loc:
                city = str(row.get("city", "") or "")
                state = str(row.get("state", "") or "")
                loc = ", ".join(p for p in [city, state] if p)
            site = str(row.get("site", "") or "").lower()
            jobs_by_id[ident] = {
                "company": str(row.get("company", "") or "Unknown"),
                "title": title,
                "location": loc,
                "url": url,
                "date_posted": str(row.get("date_posted", "") or ""),
                "description": str(row.get("description", "") or "")[:INDEED_JD_MAX_CHARS],
                "salary": format_salary(
                    row.get("min_amount", ""),
                    row.get("max_amount", ""),
                    row.get("interval", ""),
                ),
                "ats": _BOARDS_ATS_LABELS.get(site, "Boards"),
            }
    jobs = list(jobs_by_id.values())
    print(
        f"  📊 Boards: {len(LINKEDIN_SEARCH_TERMS)} terms × {len(JOBSPY_LOCATIONS)} metros → "
        f"{ok_terms} ok / {errored_terms} errored · {raw_rows} raw, {len(jobs)} matched"
    )

    # Same block guard as Indeed: preserve the previous file on a no-data run.
    if raw_rows == 0:
        prev = _load_prev_jobs(os.path.join(SCRIPT_DIR, "boards_jobs.json"))
        print(
            f"  ⛔ Boards returned 0 rows across all terms (likely blocked); "
            f"preserving previous {len(prev)} result(s)"
        )
        return prev

    return jobs


def format_salary(min_amount, max_amount, interval) -> str:
    """
    Display string for jobspy's Indeed pay fields, e.g. "$150k–$190k/yr" or
    "$62.50/hr". Returns "" when neither bound is present.
    """
    def _num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    def _fmt(n):
        if n >= 10000:
            return f"${round(n / 1000)}k"
        if n == int(n):
            return f"${int(n)}"
        return f"${n:.2f}"

    lo, hi = _num(min_amount), _num(max_amount)
    if lo is None and hi is None:
        return ""
    suffix = {"yearly": "/yr", "hourly": "/hr", "monthly": "/mo",
              "weekly": "/wk", "daily": "/day"}.get(str(interval or "").lower(), "")
    if lo is not None and hi is not None and lo != hi:
        return f"{_fmt(lo)}–{_fmt(hi)}{suffix}"
    return f"{_fmt(lo if lo is not None else hi)}{suffix}"


def _iso_to_ts(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def _job_identity(url: str) -> str:
    """
    Stable identity string for a posting URL, used to dedupe across runs.

    LinkedIn → numeric posting ID (LinkedIn appends tracking params that vary
    run-to-run). Indeed → the `jk=` token (Indeed appends `indpubnum` and other
    tracking that varies). Other ATS (Greenhouse, Workday, Phenom) → URL with
    query string and trailing slash stripped.
    """
    if not url:
        return ""
    m = re.search(r'/jobs/view/(\d+)', url)
    if m:
        return f"linkedin:{m.group(1)}"
    m = re.search(r'[?&]jk=([a-zA-Z0-9]+)', url)
    if m:
        return f"indeed:{m.group(1)}"
    return url.split("?")[0].rstrip("/")


def _load_prev_jobs(json_path: str) -> list[dict]:
    """Read the `jobs` list from a previously-saved jobs JSON (empty if missing)."""
    try:
        with open(json_path) as f:
            return json.load(f).get("jobs", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _load_prev_ids(json_path: str) -> set[str]:
    """Read previously-saved jobs JSON and return the set of job identities."""
    ids = set()
    for j in _load_prev_jobs(json_path):
        i = _job_identity(j.get("url", ""))
        if i:
            ids.add(i)
    return ids


ALL_JOBS_PRUNE_DAYS = 14


def _merge_into_all_jobs(new_jobs: list) -> int:
    """
    Maintain all_jobs.json — a cumulative, URL-deduped master of every role the
    scrapers surface, each stamped with first_seen. The per-source JSONs are
    rolling windows that overwrite every run (LinkedIn keeps only ~1h), so this
    master is what the triage agent and the dashboard's Rank tab read to see
    everything from the last ALL_JOBS_PRUNE_DAYS days. Returns count added.
    """
    path = os.path.join(SCRIPT_DIR, "all_jobs.json")
    try:
        with open(path) as f:
            master = json.load(f).get("jobs", [])
    except (FileNotFoundError, json.JSONDecodeError):
        master = []

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    by_url = {j.get("url"): j for j in master if j.get("url")}
    added = 0
    for j in new_jobs:
        if is_excluded_company(j.get("company", "")):
            continue  # backstop: keep blocklisted recruiters out of the master
        url = j.get("url")
        if url and url not in by_url:  # first writer wins on first_seen
            # Drop the JD text: the dashboard fetches this whole file on every
            # load; the triage agent reads descriptions from indeed_jobs.json.
            entry = {k: v for k, v in j.items() if k != "description"}
            entry["first_seen"] = stamp
            by_url[url] = entry
            added += 1

    cutoff = (now - timedelta(days=ALL_JOBS_PRUNE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    purged_excluded_jobs = 0
    kept = []
    for j in by_url.values():
        comp = j.get("company", "")
        if is_excluded_company(comp):
            purged_excluded_jobs += 1
            continue
        if j.get("first_seen", stamp) >= cutoff:
            kept.append(j)

    kept.sort(key=lambda j: j.get("first_seen", ""), reverse=True)

    with open(path, "w") as f:
        # Compact separators: the dashboard downloads this file on every load.
        json.dump({"updated_at": now.strftime("%Y-%m-%d %H:%M UTC"), "jobs": kept},
                  f, separators=(",", ":"))
    print(f"🗂  all_jobs.json: +{added} new, {len(kept)} total (last {ALL_JOBS_PRUNE_DAYS}d)")
    if purged_excluded_jobs > 0:
        print(f"  🧹 all_jobs.json: purged {purged_excluded_jobs} lingering job(s) from excluded companies")

    # Dynamically register companies with direct ATS / career links into companies.json
    try:
        from company_directory import load_company_directory, record_company, save_company_directory, prune_excluded_companies
        comp_dir = load_company_directory()
        comp_changed = False
        for j in new_jobs:
            c_name = j.get("company", "")
            c_url = j.get("url", "")
            if c_url and "linkedin.com" not in c_url and "indeed.com" not in c_url:
                if record_company(c_name, c_url, date_seen=j.get("date_posted"), ats_hint=j.get("ats"), directory_data=comp_dir):
                    comp_changed = True
        pruned_count, _ = prune_excluded_companies(comp_dir)
        if comp_changed or pruned_count > 0:
            save_company_directory(comp_dir)
            print(f"  🏢 companies.json: updated ({comp_dir.get('total_companies', 0)} companies)")
    except Exception as e:
        print(f"  ⚠️ Warning updating company directory: {e}")

    return added


def _normalize_dates(jobs: list) -> None:
    """Rewrite every date_posted to a LOCAL_TZ day, in place. Never raises."""
    today = local_today()
    for j in jobs:
        try:
            j["date_posted"] = normalize_posted_date(j.get("date_posted"), today=today)
        except Exception as e:  # pragma: no cover - defensive
            print(f"  ⚠️  date normalize failed for {j.get('url', '?')} ({e}); left as-is")


def save_jobs_output(jobs: list, *, basename: str, title: str, subtitle: str,
                     accent: str, empty_message: str, window_label: str):
    """
    Save jobs to {basename}.{json,md,html}. Dedupes against the previous JSON at
    the same path so each email surfaces only postings new to this run.
    """
    json_path = os.path.join(SCRIPT_DIR, f"{basename}.json")
    md_path = os.path.join(SCRIPT_DIR, f"{basename}.md")
    html_path = os.path.join(SCRIPT_DIR, f"{basename}.html")

    # Choke-point blocklist filter: the per-scraper guards miss the blocked-run
    # fallbacks (which reload the previous JSON verbatim), so pre-blocklist rows
    # could be re-persisted into the digests the dashboard reads. Filtering here
    # covers every source — including future ones — before anything is written.
    jobs = [
        j for j in jobs
        if not is_excluded_company(j.get("company", ""))
        and not is_dealbreaker_role(j.get("title", ""), j.get("description", ""))
    ]

    # Same choke-point logic for dates: normalizing here covers every source —
    # including future ones — rather than trusting each scraper to get the
    # timezone right. Guarded per-job because this sits on the critical
    # scrape → digest → commit path and one malformed upstream date must not
    # take the whole run down (cf. the _merge_into_all_jobs guard below).
    _normalize_dates(jobs)

    prev_ids = _load_prev_ids(json_path)
    new_jobs = [j for j in jobs if _job_identity(j.get("url", "")) not in prev_ids]

    # Accumulate into the cumulative master. Guarded: a bug here must never
    # break the scrape/commit path that the digests and dashboard depend on.
    try:
        _merge_into_all_jobs(new_jobs)
    except Exception as e:
        print(f"  ⚠️  all_jobs.json accumulator failed (non-fatal): {e}")

    # Push new roles to Pushover (no-op without PUSHOVER_TOKEN/USER env vars).
    try:
        import notify
        notify.notify_new_jobs(new_jobs)
    except Exception as e:
        print(f"  ⚠️  Pushover notify failed (non-fatal): {e}")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    output = {
        "scraped_at": timestamp,
        "total": len(jobs),
        "new_count": len(new_jobs),
        "jobs": jobs,
        "new_jobs": new_jobs,
    }
    with open(json_path, "w") as f:
        json.dump(output, f, indent=2)

    lines = [
        f"# {title}",
        f"*Last updated: {timestamp}*\n",
        f"**{len(new_jobs)} new role(s)** since last run · {len(jobs)} total in {window_label}\n",
    ]
    if not new_jobs:
        lines.append(empty_message)
    else:
        for job in new_jobs:
            lines.append(f"### [{job['title']}]({job['url']}) — {job['company']}")
            lines.append(f"- 📍 **Location:** {job['location'] or 'Not specified'}")
            if job.get("salary"):
                lines.append(f"- 💰 **Salary:** {job['salary']}")
            if job.get("date_posted"):
                lines.append(f"- 🕒 **Posted:** {job['date_posted']}")
            lines.append("")
    with open(md_path, "w") as f:
        f.write("\n".join(lines))

    with open(html_path, "w") as f:
        f.write(_render_jobs_html(
            title=title,
            subtitle=subtitle,
            timestamp=timestamp,
            jobs=new_jobs,
            empty_message=empty_message,
            accent=accent,
        ))
    print(f"📄 Saved {basename}.json/.md/.html ({len(new_jobs)} new of {len(jobs)} total)")


def save_linkedin_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="linkedin_jobs",
        title="🔥 LinkedIn — Engineering / ML / DS Roles (SF Bay Area + NYC)",
        subtitle=f"SF Bay Area + NYC · last {LINKEDIN_LOOKBACK_SECONDS // 3600}h",
        accent="#3b82f6",
        empty_message="No new roles since the last run.",
        window_label=f"last {LINKEDIN_LOOKBACK_SECONDS // 3600}h",
    )


def save_indeed_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="indeed_jobs",
        title="🟦 Indeed — Engineering / ML / DS Roles (SF Bay Area + NYC)",
        subtitle=f"SF Bay Area + NYC · last {INDEED_LOOKBACK_HOURS}h",
        accent="#2557a7",
        empty_message="No new roles since the last run.",
        window_label=f"last {INDEED_LOOKBACK_HOURS}h",
    )


def save_boards_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="boards_jobs",
        title="🟪 ZipRecruiter + Google — Engineering / ML / DS Roles (SF Bay Area + NYC)",
        subtitle=f"SF Bay Area + NYC · last {BOARDS_LOOKBACK_HOURS}h",
        accent="#7c5cff",
        empty_message="No new roles since the last run.",
        window_label=f"last {BOARDS_LOOKBACK_HOURS}h",
    )


def save_nightly_tech_results(jobs: list):
    save_jobs_output(
        jobs,
        basename="jobs",
        title="⚡ Nightly Tech & Direct ATS — Engineering Roles",
        subtitle=f"US Tech Hubs & Remote · last {LINKEDIN_BIOTECH_LOOKBACK_SECONDS // 3600}h",
        accent="#7c5cff",
        empty_message="No new tech roles since the last run.",
        window_label=f"last {LINKEDIN_BIOTECH_LOOKBACK_SECONDS // 3600}h",
    )


save_biotech_linkedin_results = save_nightly_tech_results


def _render_jobs_html(*, title: str, subtitle: str, timestamp: str,
                      jobs: list, empty_message: str, accent: str) -> str:
    import html as html_mod

    if not jobs:
        body = f'<div class="empty">{html_mod.escape(empty_message)}</div>'
    else:
        cards = []
        for j in jobs:
            salary = (
                f'<span class="meta-item">💰 {html_mod.escape(j["salary"])}</span>'
                if j.get("salary") else ""
            )
            posted = (
                f'<span class="meta-item">🕒 Posted {html_mod.escape(j["date_posted"])}</span>'
                if j.get("date_posted") else ""
            )
            ats_tag = (
                f'<span class="ats">{html_mod.escape(j["ats"])}</span>'
                if j.get("ats") else ""
            )
            cards.append(
                f'<div class="job">'
                f'<div class="title"><a href="{html_mod.escape(j["url"])}">'
                f'{html_mod.escape(j["title"])}</a></div>'
                f'<div class="company">{html_mod.escape(j["company"])} {ats_tag}</div>'
                f'<div class="meta">'
                f'<span class="meta-item">📍 {html_mod.escape(j["location"] or "Not specified")}</span>'
                f'{salary}'
                f'{posted}'
                f'</div></div>'
            )
        body = "\n".join(cards)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  max-width: 720px; margin: 24px auto; padding: 0 16px; color: #1a1a1a; background: #fff; line-height: 1.5; }}
h1 {{ font-size: 22px; margin: 0 0 4px 0; }}
.subtitle {{ color: #666; font-size: 14px; margin-bottom: 16px; }}
.summary {{ background: #f4f6fb; padding: 12px 16px; border-left: 4px solid {accent};
  margin: 16px 0; border-radius: 4px; font-size: 14px; }}
.summary strong {{ font-size: 18px; color: {accent}; }}
.job {{ background: #fafafa; border: 1px solid #e8e8e8; border-radius: 8px;
  padding: 14px 18px; margin-bottom: 10px; }}
.title {{ font-size: 16px; font-weight: 600; margin-bottom: 4px; }}
.title a {{ color: #0a66c2; text-decoration: none; }}
.title a:hover {{ text-decoration: underline; }}
.company {{ color: #444; font-weight: 500; margin-bottom: 8px; font-size: 14px; }}
.ats {{ display: inline-block; background: #eaf3fb; color: #0a66c2; font-size: 11px;
  padding: 1px 8px; border-radius: 10px; font-weight: 500; margin-left: 6px; vertical-align: middle; }}
.meta {{ font-size: 13px; color: #666; }}
.meta-item {{ margin-right: 14px; }}
.empty {{ color: #999; font-style: italic; padding: 28px; text-align: center;
  background: #fafafa; border-radius: 8px; border: 1px dashed #ddd; }}
.foot {{ margin-top: 28px; padding-top: 12px; border-top: 1px solid #eee;
  color: #888; font-size: 12px; text-align: center; }}
.foot a {{ color: #0a66c2; }}
</style></head>
<body>
<h1>{title}</h1>
<div class="subtitle">{subtitle}</div>
<div class="summary"><strong>{len(jobs)}</strong> role(s) &nbsp;·&nbsp; scraped {timestamp}</div>
{body}
<div class="foot">Auto-generated by <a href="https://github.com/ernestod1998/Job_Scraper">Job_Scraper</a></div>
</body></html>"""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_results(jobs: list):
    # This path bypasses save_jobs_output, so it needs its own normalization.
    _normalize_dates(jobs)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    output = {"scraped_at": timestamp, "total": len(jobs), "jobs": jobs}
    with open(os.path.join(SCRIPT_DIR, "jobs.json"), "w") as f:
        json.dump(output, f, indent=2)

    lines = [
        "# 🧬 Fresh Biotech MLE Job Listings (SF Bay Area + NYC)",
        f"*Last updated: {timestamp}*\n",
        f"**{len(jobs)} role(s) posted in the last 24 hours**\n",
    ]

    for company in sorted(set(j["company"] for j in jobs)):
        company_jobs = [j for j in jobs if j["company"] == company]
        lines.append(f"## {company} ({len(company_jobs)} role(s))\n")
        for job in company_jobs:
            lines.append(f"### [{job['title']}]({job['url']})")
            lines.append(f"- 📍 **Location:** {job['location'] or 'Not specified'}")
            if job.get("date_posted"):
                lines.append(f"- 📅 **Posted:** {job['date_posted']}")
            lines.append("")

    with open(os.path.join(SCRIPT_DIR, "jobs.md"), "w") as f:
        f.write("\n".join(lines))

    with open(os.path.join(SCRIPT_DIR, "jobs.html"), "w") as f:
        f.write(_render_jobs_html(
            title="🧬 Fresh Biotech MLE Job Listings",
            subtitle="SF Bay Area + NYC · posted in the last 24 hours",
            timestamp=timestamp,
            jobs=jobs,
            empty_message="No biotech roles posted in the last 24 hours.",
            accent="#2ea04f",
        ))

    print(f"\n📄 Saved jobs.json/.md/.html ({len(jobs)} total roles)")


# ===========================================================================
# Salary backfill + extra sources (USAJOBS / GovernmentJobs / CalCareers /
# CalOpps). These reuse the repo's existing keyword gate (is_mle_role) and
# location predicate (is_watch_location — Bay Area + NYC), so they follow
# whatever KEYWORDS / BAY_AREA_LOCATIONS / NY hub tokens the maintainer sets — no domain-specific terms are
# hardcoded here. Heavier per-term sources share GOV_SEARCH_TERMS (a slice of
# the LinkedIn list) to keep request counts sane; widen it if you like.
# ===========================================================================

GOV_SEARCH_TERMS = LINKEDIN_SEARCH_TERMS[:8]


# Set to 0 to skip per-posting salary requests and prevent rate-limiting
LINKEDIN_SALARY_FETCH_CAP = 0


def _linkedin_posting_salary(job_id: str) -> str:
    import html as html_mod
    page = fetch(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}", extra_headers=LINKEDIN_HEADERS)
    if not page:
        return ""
    anchor = re.search(r'compensation__salary', page)
    if not anchor:
        return ""
    window = page[anchor.start():anchor.start() + 400]
    amt = re.search(r'\$[\d][^<]{0,60}', window)
    return re.sub(r'\s+', ' ', html_mod.unescape(amt.group(0))).strip() if amt else ""


def _enrich_linkedin_salaries(jobs: list) -> int:
    """Backfill salary on LinkedIn jobs from their posting pages. Bounded by
    LINKEDIN_SALARY_FETCH_CAP; never raises."""
    filled = fetched = 0
    for job in jobs:
        if fetched >= LINKEDIN_SALARY_FETCH_CAP:
            break
        if job.get("salary") or job.get("ats") != "LinkedIn":
            continue
        m = re.search(r'/jobs/view/(\d+)', job.get("url", ""))
        if not m:
            continue
        polite_sleep(0.6, 0.8, 1.5)
        fetched += 1
        try:
            sal = _linkedin_posting_salary(m.group(1))
        except (URLError, TimeoutError, OSError):
            continue
        if sal:
            job["salary"] = sal
            filled += 1
    if fetched:
        print(f"  💰 LinkedIn salary backfill: {filled}/{fetched} posting(s) had pay")
    return filled


# ---- Shared cookie-jar opener (for ASP.NET session sources) ---------------
def _session_opener():
    jar = http.cookiejar.CookieJar()
    return build_opener(HTTPCookieProcessor(jar))


def _hidden_inputs(html: str) -> dict:
    """All <input type=hidden> name/value pairs (ASP.NET viewstate etc.)."""
    fields = {}
    for tag in re.findall(r'<input\b[^>]*type=["\']hidden["\'][^>]*>', html, re.I):
        n = re.search(r'\bname=["\']([^"\']+)["\']', tag)
        v = re.search(r'\bvalue=["\']([^"\']*)["\']', tag)
        if n:
            fields[n.group(1)] = (v.group(1) if v else "")
    return fields


# ---- USAJOBS — federal jobs (no API key) ----------------------------------
USAJOBS_RESULTS_URL = "https://www.usajobs.gov/Search/Results?hp=public&s=startdate&sd=desc&p=1"
USAJOBS_SEARCH_URL = "https://www.usajobs.gov/Search/ExecuteSearch"
USAJOBS_RESULTS_PER_PAGE = 50


def _usajobs_date(date_display: str) -> str:
    m = re.search(r'(\d{2})/(\d{2})/(\d{4})', date_display or "")
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""


def scrape_usajobs_recent() -> list:
    """Federal roles from usajobs.gov via the public website search (no API key).
    Seeds a session on the Results page, then POSTs each keyword to
    /Search/ExecuteSearch and keeps titles passing is_mle_role(). Returns salary."""
    print("🇺🇸 Scraping USAJOBS (federal jobs)...")
    jobs_by_url: dict[str, dict] = {}
    headers = {
        **HEADERS,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.usajobs.gov",
        "Referer": USAJOBS_RESULTS_URL,
    }
    try:
        opener = _session_opener()
        opener.open(Request(USAJOBS_RESULTS_URL, headers=HEADERS), timeout=25).read()
        for term in GOV_SEARCH_TERMS:
            time.sleep(REQUEST_DELAY)
            body = json.dumps({
                "Keyword": term, "HiringPath": ["public"],
                "SortField": "startdate", "SortDirection": "desc",
                "Page": "1", "ResultsPerPage": USAJOBS_RESULTS_PER_PAGE,
            }).encode()
            try:
                payload = json.loads(opener.open(
                    Request(USAJOBS_SEARCH_URL, data=body, headers=headers),
                    timeout=25).read().decode("utf-8", "ignore"))
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                print(f"  ⚠️  USAJOBS ({term!r}): {e}")
                continue
            for job in payload.get("Jobs", []):
                title = (job.get("Title") or "").strip()
                if not is_mle_role(title):
                    continue
                uri = (job.get("PositionURI") or "").replace(":443", "")
                if not uri and job.get("DocumentID"):
                    uri = f"https://www.usajobs.gov/job/{job['DocumentID']}"
                if not uri or uri in jobs_by_url:
                    continue
                jobs_by_url[uri] = {
                    "company": (job.get("Agency") or job.get("Department") or "Federal Government").strip(),
                    "title": title,
                    "location": (job.get("LocationName") or "").strip(),
                    "url": uri,
                    "date_posted": _usajobs_date(job.get("DateDisplay", "")),
                    "salary": (job.get("SalaryDisplay") or "").strip(),
                    "ats": "USAJOBS",
                }
    except (URLError, TimeoutError, OSError, ValueError) as e:
        print(f"  ⛔ USAJOBS unreachable ({e}); preserving previous results")
        return _load_prev_jobs(os.path.join(SCRIPT_DIR, "usajobs_jobs.json"))
    jobs = list(jobs_by_url.values())
    print(f"  ✅ USAJOBS: {len(jobs)} federal role(s)")
    return jobs if jobs else _load_prev_jobs(os.path.join(SCRIPT_DIR, "usajobs_jobs.json"))


def save_usajobs_results(jobs: list):
    save_jobs_output(
        jobs, basename="usajobs_jobs",
        title="🇺🇸 USAJOBS — Federal Roles",
        subtitle="usajobs.gov · federal agencies",
        accent="#1d4ed8",
        empty_message="No new federal roles since the last run.",
        window_label="current USAJOBS postings",
    )


# ---- GovernmentJobs.com / NEOGOV — state & local government ----------------
GOVERNMENTJOBS_BASE = "https://www.governmentjobs.com"
GOVERNMENTJOBS_DAYS = 21
GOVERNMENTJOBS_PAGES = 2


def scrape_governmentjobs_recent() -> list:
    """State/local-government roles via governmentjobs.com, filtered to the
    repo's watch locations (Bay Area + NYC) with is_watch_location()."""
    print("🏛  Scraping GovernmentJobs/NEOGOV (state & local gov)...")
    import html as html_mod
    item_re = re.compile(r'<li[^>]*class=["\'][^"\']*\bjob-item\b[^"\']*["\'][^>]*>([\s\S]*?)</li>', re.I)
    link_re = re.compile(r'<a[^>]*class=["\'][^"\']*\bjob-details-link\b[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', re.I)
    org_re = re.compile(r'<div[^>]*class=["\'][^"\']*\bjob-organization\b[^"\']*["\'][^>]*>([\s\S]*?)</div>', re.I)
    loc_re = re.compile(r'<span[^>]*class=["\'][^"\']*\bjob-location\b[^"\']*["\'][^>]*>([\s\S]*?)</span>', re.I)

    def _clean(s):
        return re.sub(r'\s+', ' ', html_mod.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()

    jobs_by_url: dict[str, dict] = {}
    raw_items = 0
    for term in GOV_SEARCH_TERMS:
        for page in range(1, GOVERNMENTJOBS_PAGES + 1):
            time.sleep(REQUEST_DELAY)
            url = (f"{GOVERNMENTJOBS_BASE}/jobs?keyword={urllib.parse.quote(term)}"
                   f"&daysposted={GOVERNMENTJOBS_DAYS}&isFiltered=true&page={page}")
            items = item_re.findall(fetch(url))
            raw_items += len(items)
            if not items:
                break
            for it in items:
                lk = link_re.search(it)
                if not lk:
                    continue
                title = _clean(lk.group(2))
                if not is_mle_role(title):
                    continue
                loc_m = loc_re.search(it)
                location = _clean(loc_m.group(1)) if loc_m else ""
                if not is_watch_location(location):
                    continue
                href = re.sub(r'\s+', '', lk.group(1))
                job_url = href if href.startswith("http") else GOVERNMENTJOBS_BASE + "/" + href.lstrip("/")
                if job_url in jobs_by_url:
                    continue
                org_m = org_re.search(it)
                sal_m = re.search(
                    r'\$[\d,]+(?:\.\d{2})?\s*-\s*\$[\d,]+(?:\.\d{2})?'
                    r'\s*(?:Annually|Monthly|Hourly|Biweekly|Bi-Weekly|Weekly|Daily)?',
                    _clean(it), re.I)
                jobs_by_url[job_url] = {
                    "company": _clean(org_m.group(1)) if org_m else "Government Agency",
                    "title": title, "location": location, "url": job_url,
                    "date_posted": "",
                    "salary": sal_m.group(0).strip() if sal_m else "",
                    "ats": "NEOGOV",
                }
    jobs = list(jobs_by_url.values())
    print(f"  ✅ NEOGOV: {len(jobs)} role(s) (from {raw_items} scanned)")
    if not jobs and raw_items == 0:
        return _load_prev_jobs(os.path.join(SCRIPT_DIR, "governmentjobs_jobs.json"))
    return jobs


def save_governmentjobs_results(jobs: list):
    save_jobs_output(
        jobs, basename="governmentjobs_jobs",
        title="🏛 NEOGOV — State & Local Government Roles",
        subtitle="governmentjobs.com",
        accent="#0e7490",
        empty_message="No new state/local-gov roles since the last run.",
        window_label="recent GovernmentJobs postings",
    )


# ---- CalOpps — California local agencies -----------------------------------
CALOPPS_LIST_URL = "https://www.calopps.org/job-search-list"
CALOPPS_MAX_PAGES = 10


def _calopps_company(href: str) -> str:
    m = re.match(r'/?([^/]+)/', href or "")
    return m.group(1).replace('-', ' ').title() if m else "California Agency"


def scrape_calopps_recent() -> list:
    """California local-agency roles from calopps.org (CA-only board)."""
    print("🏛  Scraping CalOpps (California local agencies)...")
    import html as html_mod
    row_re = re.compile(r'<tr[^>]*>([\s\S]*?)</tr>', re.I)
    cell_re = re.compile(r'<td[^>]*>([\s\S]*?)</td>', re.I)
    link_re = re.compile(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', re.I)

    def _clean(s):
        return re.sub(r'\s+', ' ', html_mod.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()

    jobs_by_url: dict[str, dict] = {}
    scanned = 0
    for page in range(CALOPPS_MAX_PAGES):
        time.sleep(REQUEST_DELAY)
        url = CALOPPS_LIST_URL + (f"?page={page}" if page else "")
        rows = [r for r in row_re.findall(fetch(url)) if "views-field-label" in r.lower()]
        if not rows:
            break
        for r in rows:
            cells = cell_re.findall(r)
            if len(cells) < 5:
                continue
            lk = link_re.search(cells[0])
            if not lk:
                continue
            scanned += 1
            title = _clean(lk.group(2))
            if not is_mle_role(title):
                continue
            href = html_mod.unescape(lk.group(1).strip())
            job_url = href if href.startswith("http") else "https://www.calopps.org" + ("" if href.startswith("/") else "/") + href
            if job_url in jobs_by_url:
                continue
            jobs_by_url[job_url] = {
                "company": _calopps_company(href), "title": title,
                "location": _clean(cells[1]) or "California", "url": job_url,
                "date_posted": "", "salary": "", "ats": "CalOpps",
            }
    jobs = list(jobs_by_url.values())
    for job in jobs:  # salary is on the posting page (few matches → cheap)
        time.sleep(REQUEST_DELAY)
        try:
            ph = fetch(job["url"])
        except (URLError, TimeoutError, OSError):
            continue
        sm = re.search(
            r'Salary\s*(\$[\d,]+(?:\.\d{2})?\s*-\s*\$[\d,]+(?:\.\d{2})?'
            r'\s*(?:Monthly|Annually|Hourly|Biweekly|Bi-Weekly|Weekly|Daily)?)',
            re.sub(r'<[^>]+>', ' ', ph), re.I)
        if sm:
            job["salary"] = re.sub(r'\s+', ' ', sm.group(1)).strip()
    print(f"  ✅ CalOpps: {len(jobs)} role(s) (from {scanned} scanned)")
    if not jobs and scanned == 0:
        return _load_prev_jobs(os.path.join(SCRIPT_DIR, "calopps_jobs.json"))
    return jobs


def save_calopps_results(jobs: list):
    save_jobs_output(
        jobs, basename="calopps_jobs",
        title="🏛 CalOpps — California Local-Agency Roles",
        subtitle="calopps.org · CA cities, counties, special districts",
        accent="#15803d",
        empty_message="No new CalOpps roles since the last run.",
        window_label="recent CalOpps postings",
    )


# ---- CalCareers — California state civil service ---------------------------
CALCAREERS_SEARCH_URL = "https://calcareers.ca.gov/CalHRPublic/Search/JobSearchResults.aspx"
CALCAREERS_TIMEOUT = 30
CALCAREERS_CARD_RE = re.compile(
    r'Working Title:\s*</div>\s*<div class="col-xs-6 job-details">\s*<span[^>]*>(.*?)</span>'
    r'[\s\S]*?Job Control:\s*</div>\s*<div class="col-xs-6 job-details">\s*(\d+)\s*</div>'
    r'[\s\S]*?Department:\s*</div>\s*<div class="col-xs-6 job-details">\s*(.*?)\s*</div>'
    r'[\s\S]*?Location:\s*</div>\s*<div class="col-xs-6 job-details">\s*(.*?)\s*</div>'
    r'[\s\S]*?Publish Date:\s*</div>\s*<div class="col-xs-6 job-details">\s*<time[^>]*>\s*([^<]+)\s*</time>'
    r'[\s\S]*?href="(https://www\.calcareers\.ca\.gov/CalHrPublic/Jobs/JobPosting\.aspx\?JobControlId=\d+)"',
    re.I,
)


def _parse_calcareers_results(html: str) -> list[dict]:
    import html as html_mod

    def _clean(s):
        return re.sub(r'\s+', ' ', html_mod.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()

    jobs: list[dict] = []
    for m in CALCAREERS_CARD_RE.finditer(html):
        title, _jc, dept, location, pub_date, url = m.groups()
        date = ""
        dm = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', pub_date or "")
        if dm:
            date = f"{dm.group(3)}-{int(dm.group(1)):02d}-{int(dm.group(2)):02d}"
        card = html[m.start():m.end()]
        sal_m = re.search(r'Salary Range:\s*</div>\s*<div[^>]*>([\s\S]*?)</div>', card, re.I)
        salary = ""
        if sal_m:
            sm = re.search(
                r'\$[\d,]+(?:\.\d{2})?\s*-\s*\$[\d,]+(?:\.\d{2})?(?:\s*(?:per|/)\s*\w+)?',
                _clean(sal_m.group(1)))
            salary = sm.group(0).strip() if sm else ""
        jobs.append({
            "company": _clean(dept) or "State of California",
            "title": _clean(title), "location": _clean(location) or "California",
            "url": _clean(url), "date_posted": date, "salary": salary,
            "ats": "CalCareers",
        })
    return jobs


def _calcareers_payload(hidden: dict, event_target: str, keyword: str) -> dict:
    payload = dict(hidden)
    payload["__EVENTTARGET"] = event_target
    payload["__EVENTARGUMENT"] = ""
    payload["ctl00$cphMainContent$txtKeyword"] = keyword
    payload["ctl00$cphMainContent$hdnInit"] = "true"
    payload.setdefault("ctl00$cphMainContent$chkExactWordMatch", "")
    payload.setdefault("ctl00$hdnShowHeaderPadding", "1")
    payload.setdefault("ctl00$ucSessionTimeoutDialog$tmrCountdown", "1200")
    return payload


def scrape_calcareers_recent() -> list:
    """California state civil-service roles via the ASP.NET search postback.
    Fires the search with __EVENTTARGET=btnSearch + the keyword field, then
    parses the labeled result cards. Guarded — returns previous on any failure."""
    print("🏛  Scraping CalCareers (California state jobs)...")
    headers = {
        **HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": CALCAREERS_SEARCH_URL,
    }
    jobs_by_url: dict[str, dict] = {}
    parsed_total = 0
    reached = False
    for term in GOV_SEARCH_TERMS:
        time.sleep(REQUEST_DELAY)
        try:
            opener = _session_opener()  # fresh session/viewstate per keyword
            seed = opener.open(Request(CALCAREERS_SEARCH_URL, headers=HEADERS),
                               timeout=CALCAREERS_TIMEOUT).read().decode("utf-8", "ignore")
            reached = True
            hidden = _hidden_inputs(seed)
            if not hidden:
                continue
            data = urllib.parse.urlencode(
                _calcareers_payload(hidden, "ctl00$cphMainContent$btnSearch", term)).encode()
            res_html = opener.open(Request(CALCAREERS_SEARCH_URL, data=data, headers=headers),
                                   timeout=CALCAREERS_TIMEOUT).read().decode("utf-8", "ignore")
        except (URLError, TimeoutError, OSError) as e:
            print(f"  ⚠️  CalCareers ({term!r}): {e}")
            continue
        for job in _parse_calcareers_results(res_html):
            parsed_total += 1
            if is_mle_role(job["title"]) and job["url"] not in jobs_by_url:
                jobs_by_url[job["url"]] = job
    jobs = list(jobs_by_url.values())
    print(f"  ✅ CalCareers: {len(jobs)} on-target role(s) (from {parsed_total} parsed)")
    if not jobs and (parsed_total == 0 or not reached):
        return _load_prev_jobs(os.path.join(SCRIPT_DIR, "calcareers_jobs.json"))
    return jobs


def save_calcareers_results(jobs: list):
    save_jobs_output(
        jobs, basename="calcareers_jobs",
        title="🏛 CalCareers — California State Roles",
        subtitle="calcareers.ca.gov · California state civil service",
        accent="#b45309",
        empty_message="No new CalCareers roles since the last run.",
        window_label="current CalCareers postings",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--indeed-only" in sys.argv:
        save_indeed_results(scrape_indeed_recent())
        sys.exit(0)

    if "--boards-only" in sys.argv:
        save_boards_results(scrape_boards_recent())
        sys.exit(0)

    if "--linkedin-only" in sys.argv:
        save_linkedin_results(scrape_linkedin_recent())
        sys.exit(0)

    if "--usajobs-only" in sys.argv:
        save_usajobs_results(scrape_usajobs_recent())
        sys.exit(0)

    if "--governmentjobs-only" in sys.argv:
        save_governmentjobs_results(scrape_governmentjobs_recent())
        sys.exit(0)

    if "--calopps-only" in sys.argv:
        save_calopps_results(scrape_calopps_recent())
        sys.exit(0)

    if "--calcareers-only" in sys.argv:
        save_calcareers_results(scrape_calcareers_recent())
        sys.exit(0)

    if "--nightly-tech" in sys.argv or "--biotech-only" in sys.argv:
        jobs = scrape_linkedin_nightly_tech()
        jobs = [j for j in jobs if is_target_location(j.get("location", ""))]

        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for j in jobs:
            key = (j["company"].strip().lower(), j["title"].strip().lower())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(j)
        print(f"\n⚡ Combined Nightly Tech total: {len(deduped)} unique role(s) "
              f"(from {len(jobs)} across sources)")

        save_nightly_tech_results(deduped)
        sys.exit(0)

    # Legacy default: curated Greenhouse/Workday/Phenom sweep. Returned 0 roles
    # consistently because ATS updated_at dates rarely fall inside the 24h window.
    # CI now uses --biotech-only; this branch is kept for ad-hoc local runs.
    all_jobs = list(scrape_genentech())
    all_jobs.extend(scrape_curated_biotechs())

    before = len(all_jobs)
    all_jobs = [j for j in all_jobs if is_watch_location(j.get("location", ""))]
    print(f"\n📍 Bay Area + NYC filter: {before} → {len(all_jobs)} roles")

    before = len(all_jobs)
    all_jobs = [j for j in all_jobs if is_recent_posting(j)]
    print(f"🕒 Freshness filter (last 24h): {before} → {len(all_jobs)} roles")

    save_results(all_jobs)
