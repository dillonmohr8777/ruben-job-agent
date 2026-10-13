"""Hard filters (a job that fails any of these is dropped, with the reason kept), freshness, dedupe."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from .models import Job, norm

LEVELS = ["intern", "junior", "mid", "senior", "lead", "director", "exec"]


def seniority(title: str) -> tuple[int, bool]:
    """(level index, explicit). explicit=False means no seniority word in the title, so 'mid' is a guess."""
    t = title.lower()
    rules = [
        (6, r"\b(chief|cmo|cto|ceo|coo|vp|svp|evp|vice president)\b"),
        (5, r"\b(director|head of)\b"),
        (4, r"\b(lead|principal|staff|group)\b"),
        (3, r"\b(senior|sr|manager|iii)\b"),
        (1, r"\b(junior|jr|associate|coordinator|assistant|entry)\b"),
        (0, r"\bintern(ship)?\b"),
    ]
    for lvl, rx in rules:
        if re.search(rx, t):
            return lvl, True
    return 2, False


# --- salary -----------------------------------------------------------------

_AMT = r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?([kK])?"
_RANGE = re.compile(_AMT + r"\s*(?:-|–|—|to)\s*" + r"\$?\s?(\d[\d,]*(?:\.\d+)?)\s?([kK])?")


def _val(num: str, k: str | None, after: str) -> float | None:
    v = float(num.replace(",", "")) * (1000 if k else 1)
    if re.search(r"/\s?h(ou)?r|per hour|hourly|an hour", after[:30], re.I) and 10 <= v <= 500:
        return v * 2080
    return v if v >= 20000 else None  # ignore '$5 million budget', '$50 gift card'


def parse_salary(text: str) -> tuple[int | None, int | None]:
    """USD only. Takes the widest range stated (some posts list several location tiers)."""
    los, his = [], []
    for m in _RANGE.finditer(text or ""):
        after = text[m.end():m.end() + 30]
        a, b = _val(m.group(1), m.group(2) or m.group(4), after), _val(m.group(3), m.group(4) or m.group(2), after)
        if a and b:
            los.append(min(a, b))
            his.append(max(a, b))
    return (int(min(los)), int(max(his))) if los else (None, None)


# --- location and visa ------------------------------------------------------

COUNTRY_ALIASES = {
    "united states": r"\b(us|usa|u\.s\.a?|united states|america|north america|namer|amer)\b",
    "canada": r"\bcanada\b", "united kingdom": r"\b(uk|u\.k\.|united kingdom|england|scotland)\b", "ireland": r"\bireland\b",
    "germany": r"\bgermany\b", "france": r"\bfrance\b", "spain": r"\bspain\b", "netherlands": r"\bnetherlands\b",
    "poland": r"\bpoland\b", "portugal": r"\bportugal\b", "italy": r"\bitaly\b", "sweden": r"\bsweden\b",
    "india": r"\bindia\b", "australia": r"\baustralia\b", "new zealand": r"\bnew zealand\b", "singapore": r"\bsingapore\b",
    "japan": r"\bjapan\b", "brazil": r"\bbrazil\b", "mexico": r"\bmexico\b", "argentina": r"\bargentina\b",
    "austria": r"\baustria\b", "belgium": r"\bbelgium\b", "switzerland": r"\bswitzerland\b", "denmark": r"\bdenmark\b", "norway": r"\bnorway\b",
    "finland": r"\bfinland\b", "romania": r"\bromania\b", "czechia": r"\b(czech|czechia)\b", "greece": r"\bgreece\b", "turkey": r"\b(turkey|t[uü]rkiye)\b",
    "israel": r"\bisrael\b", "uae": r"\b(uae|dubai|emirates)\b", "south africa": r"\bsouth africa\b", "nigeria": r"\bnigeria\b", "kenya": r"\bkenya\b",
    "philippines": r"\bphilippines\b", "indonesia": r"\bindonesia\b", "vietnam": r"\bvietnam\b", "korea": r"\bkorea\b", "china": r"\bchina\b",
    "colombia": r"\bcolombia\b", "chile": r"\bchile\b", "peru": r"\bperu\b", "costa rica": r"\bcosta rica\b", "pakistan": r"\bpakistan\b",
    "ukraine": r"\bukraine\b", "emea": r"\b(emea|europe)\b", "apac": r"\b(apac|asia)\b", "latam": r"\b(latam|latin america)\b",
}


# Big non-US hubs by name. A city alone is only believed when it is not followed by a US state code ("Dublin, OH", "London, KY", "Berlin, CT").
CITIES = {
    "germany": "berlin|munich|hamburg|frankfurt|cologne", "united kingdom": "london|manchester|edinburgh|bristol|glasgow",
    "canada": "toronto|vancouver|montreal|ottawa|calgary|waterloo", "ireland": "dublin|cork", "india": "bangalore|bengaluru|mumbai|delhi|hyderabad|pune|chennai|gurgaon|gurugram|noida",
    "australia": "sydney|melbourne|brisbane|perth", "brazil": "sao paulo|rio de janeiro|brasil", "france": "paris|lyon", "spain": "madrid|barcelona",
    "netherlands": "amsterdam|rotterdam", "poland": "warsaw|krakow|wroclaw", "portugal": "lisbon|porto", "israel": "tel aviv|jerusalem", "japan": "tokyo|osaka",
    "mexico": "mexico city|guadalajara|monterrey", "argentina": "buenos aires", "colombia": "bogota|medellin", "philippines": "manila", "singapore": "singapore",
}
US_STATE_CODE = r"(?:A[LKZR]|C[AOT]|DE|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY]|DC)"


def countries_in(text: str) -> set[str]:
    raw = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()   # "São Paulo" -> "Sao Paulo"
    t = raw.lower()
    found = {c for c, rx in COUNTRY_ALIASES.items() if re.search(rx, t)}
    for country, cities in CITIES.items():
        if re.search(rf"(?i:\b(?:{cities})\b)(?!,?\s+{US_STATE_CODE}\b)", raw):   # state codes are matched upper-case only: "Toronto or ..." is not Oregon
            found.add(country)
    return found


def _remote(job: Job) -> bool:
    if job.remote is not None:
        return job.remote
    return bool(re.search(r"\bremote\b", f"{job.location} {job.title} {job.description[:300]}", re.I))


def location_reason(job: Job, cfg: dict) -> str | None:
    remote = _remote(job)
    if cfg.get("remote_only", True) and not remote:
        return "not remote"
    if not remote:
        cities = [c.lower() for c in cfg.get("onsite_cities", [])]
        return None if any(c in job.location.lower() for c in cities) else "onsite outside target cities"
    allowed = {c.lower() for c in cfg.get("allowed_countries", ["United States"])}
    mentioned = countries_in(f"{job.location} {job.title}")
    if mentioned and not (mentioned & allowed):
        return f"remote only in {', '.join(sorted(mentioned))}"
    return None


NEEDS_SPONSOR_BLOCK = re.compile(
    r"(unable|not able|cannot|can't|will not|won't|do not|does not|not)\s+(to\s+)?(offer\s+|provide\s+)?(visa\s+)?sponsor|"
    r"no\s+(visa\s+)?sponsorship|without\s+(the\s+need\s+for\s+)?(visa\s+)?sponsorship", re.I)
CITIZEN_ONLY = re.compile(r"u\.?s\.?\s+citizen(ship)?\s+(is\s+)?required|security clearance (is )?required|\bts/sci\b|active (secret|top secret)", re.I)


def visa_reason(job: Job, cfg: dict) -> str | None:
    d = job.description
    if cfg.get("needs_sponsorship") and NEEDS_SPONSOR_BLOCK.search(d):
        return "no visa sponsorship"
    if CITIZEN_ONLY.search(d) and not cfg.get("us_citizen"):
        return "citizenship or clearance required"
    return None


def excluded(company: str, banned: str) -> bool:
    """'Acme Corp' also bans 'Acme Corp, Inc.' and 'Acme Corp Consulting': the banned name as whole words at the start of the company."""
    c, b = f" {norm(company)} ", norm(banned)
    return bool(b) and c.startswith(f" {b} ")


# --- the filter chain -------------------------------------------------------


def hard_filter(job: Job, profile: dict, now: datetime | None = None) -> str | None:
    """Return the first reason this job is out, or None if it survives."""
    s = profile["search"]
    now = now or datetime.now(timezone.utc)
    if job.posted is None:
        if not s.get("allow_undated", False):
            return "no posted date"
    elif now - job.posted > timedelta(days=s.get("max_age_days", 7)):
        return f"older than {s.get('max_age_days', 7)} days"
    if any(excluded(job.company, c) for c in s.get("exclude_companies", [])):
        return "excluded company"
    title = job.title.lower()
    if bad := next((w for w in s.get("exclude_title_words", []) if re.search(rf"\b{re.escape(w.lower())}\b", title)), None):
        return f"title has '{bad}'"
    lvl, _ = seniority(job.title)
    lo, hi = (s.get("seniority") or {}).get("min", "intern"), (s.get("seniority") or {}).get("max", "exec")
    if not LEVELS.index(lo) <= lvl <= LEVELS.index(hi):
        return f"seniority {LEVELS[lvl]} outside {lo}..{hi}"
    if r := location_reason(job, s.get("locations") or {}):
        return r
    if r := visa_reason(job, s.get("visa") or {}):
        return r
    if job.salary_min is None and job.salary_max is None:
        job.salary_min, job.salary_max = parse_salary(job.description)
    floor = s.get("min_salary")
    if floor and job.salary_max and job.salary_max < floor:
        return f"pay tops out at {job.salary_max} < {floor}"
    if s.get("require_salary") and not job.salary_max:
        return "no pay stated"
    return None


# --- dedupe -----------------------------------------------------------------

_SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology|labs|group|holdings)\b")


def company_key(name: str) -> str:
    return _SUFFIX.sub("", norm(name)).replace(" ", "")


def title_key(title: str) -> str:
    t = re.sub(r"\([^)]*\)", " ", title.lower())
    t = re.sub(r"\s[-–|,]\s*(remote|us|usa|united states|worldwide|north america).*$", "", t)
    return norm(t)


def canon_url(u: str) -> str:
    p = urlsplit(u)
    q = "&".join(sorted(x for x in p.query.split("&") if x and not x.startswith(("utm_", "gh_src", "lever-", "src=", "ref="))))
    return f"{p.netloc.lower()}{p.path.rstrip('/')}" + (f"?{q}" if q else "")


_PREFERRED = {"greenhouse": 0, "lever": 0, "ashby": 0, "workable": 1, "rss": 2, "hn": 3}


def dedupe(jobs: list[Job], known: set[str] | None = None) -> tuple[list[Job], int]:
    """Same URL, or same company and (near-)same title, counts as one job; keep the best source.
    `known` holds ids/canonical URLs already applied to or rejected, so yesterday's jobs don't resurface."""
    known = known or set()
    kept: list[Job] = []
    dropped = 0
    for j in sorted(jobs, key=lambda x: (_PREFERRED.get(x.ats, 9), -len(x.description))):
        ck, tk, cu = company_key(j.company), title_key(j.title), canon_url(j.url)
        if j.id in known or cu in known or any(
            canon_url(k.url) == cu or (company_key(k.company) == ck and SequenceMatcher(None, title_key(k.title), tk).ratio() >= 0.92)
            for k in kept
        ):
            dropped += 1
            continue
        kept.append(j)
    return kept, dropped
