"""Job sources. APIs and feeds first; no browser needed for any of these.

Every fetcher takes a `get(url, data=None)` callable that returns parsed JSON (or text for RSS),
so tests run offline. A board that fails is reported by name, never silently skipped.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .models import Job, html_to_text

UA = "ruben-job-agent/0.1 (personal job search; reads public job-board APIs)"


def http_get(url: str, data: dict | None = None, text: bool = False, timeout: float = 30):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return raw if text else json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503) or attempt == 3:  # rate limited or briefly down: wait and retry, then name the board in the report
                raise
            time.sleep(min(10, float(e.headers.get("Retry-After") or 2 * (attempt + 1))))


def _dt(s) -> datetime | None:
    if s in (None, ""):
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s / 1000 if s > 1e11 else s, tz=timezone.utc)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(str(s))
        except (TypeError, ValueError):
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# --- ATS public boards ------------------------------------------------------


def greenhouse(slug: str, get=http_get) -> list[Job]:
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(Job("greenhouse", j.get("company_name") or slug, j["title"], j["absolute_url"],
                       html_to_text(j.get("content", "")), loc, "remote" in loc.lower() or None,
                       _dt(j.get("first_published") or j.get("updated_at")), board=slug, ats_id=str(j["id"])))
    return out


def lever(slug: str, get=http_get) -> list[Job]:
    out = []
    for j in get(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
        c = j.get("categories") or {}
        loc = c.get("location") or ""
        remote = (j.get("workplaceType") == "remote") or ("remote" in loc.lower()) or None
        desc = j.get("descriptionPlain") or html_to_text(j.get("description", ""))
        lists = " ".join(f"{x.get('text', '')} {html_to_text(x.get('content', ''))}" for x in j.get("lists") or [])
        sr = j.get("salaryRange") or {}
        out.append(Job("lever", slug, j["text"], j.get("hostedUrl", ""), f"{desc}\n{lists}\n{j.get('additionalPlain', '')}",
                       loc, remote, _dt(j.get("createdAt")), sr.get("min"), sr.get("max"), board=slug, ats_id=j["id"],
                       extra={"commitment": c.get("commitment", ""), "apply_url": j.get("applyUrl", "")}))
    return out


def ashby(slug: str, get=http_get) -> list[Job]:
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = (j.get("compensation") or {}).get("compensationTierSummary", "")
        loc = j.get("location", "")
        out.append(Job("ashby", slug, j["title"].strip(), j.get("jobUrl", ""), f"{j.get('descriptionPlain', '')}\nCompensation: {comp}",
                       loc, True if j.get("workplaceType") == "Remote" else (False if j.get("workplaceType") in ("OnSite", "Hybrid") else None),
                       _dt(j.get("publishedAt")), board=slug, ats_id=j["id"], extra={"apply_url": j.get("applyUrl", "")}))
    return out


def workable(slug: str, get=http_get) -> list[Job]:
    body = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
    results, d = [], {}
    for _ in range(40):  # the API returns 10 per page; without paging a 90-job board looked like a 10-job board
        d = get(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", body)
        results += d.get("results", [])
        if not d.get("nextPage") or not d.get("results"):
            break
        body = {**body, "token": d["nextPage"]}
        time.sleep(0.6)  # Workable rate-limits bursts of page requests
    out = []
    for j in results:
        loc = ", ".join(x for x in [(j.get("location") or {}).get("city"), (j.get("location") or {}).get("country")] if x)
        out.append(Job("workable", slug, j["title"], f"https://apply.workable.com/{slug}/j/{j['shortcode']}/", "", loc,
                       True if j.get("remote") else None, _dt(j.get("published")), board=slug, ats_id=j["shortcode"],
                       extra={"detail_url": f"https://apply.workable.com/api/v2/accounts/{slug}/jobs/{j['shortcode']}"}))
    return out


def enrich(job: Job, get=http_get) -> Job:
    """Fetch a description only for jobs that already passed the cheap filters (Workable lists omit it)."""
    url = job.extra.get("detail_url")
    if url and not job.description:
        d = get(url)
        job.description = html_to_text(" ".join(str(d.get(k, "")) for k in ("description", "requirements", "benefits")))
    return job


# --- feeds and HN -----------------------------------------------------------


def rss(url: str, get=http_get) -> list[Job]:
    root = ET.fromstring(get(url, text=True))
    out = []
    for it in root.iter("item"):
        t = (it.findtext("title") or "").strip()
        company, title = "", t
        if m := re.match(r"^(.+?):\s+(.+)$", t):  # "Company: Role" (We Work Remotely)
            company, title = m.groups()
        elif m := re.match(r"^(.+?)\s+at\s+(.+)$", t):  # "Role at Company"
            title, company = m.groups()
        desc = html_to_text(it.findtext("description") or "")
        region = it.findtext("region") or ""
        out.append(Job("rss", company or url.split("/")[2], title, (it.findtext("link") or "").strip(), desc, region,
                       True if "remote" in (t + region + desc[:300]).lower() else None, _dt(it.findtext("pubDate")),
                       ats_id=(it.findtext("guid") or it.findtext("link") or t)))
    return out


def hn_hiring(get=http_get, max_comments: int = 800) -> list[Job]:
    """Latest monthly 'Ask HN: Who is hiring?' thread. Top-level comments are postings; the first line is
    conventionally `Company | Role | Location | REMOTE | ...`."""
    hits = get("https://hn.algolia.com/api/v1/search_by_date?query=Ask%20HN%3A%20Who%20is%20hiring&tags=story,author_whoishiring&hitsPerPage=5")["hits"]
    story = next(h for h in hits if "who is hiring" in h["title"].lower())
    thread = get(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
    out = []
    for c in (thread.get("children") or [])[:max_comments]:
        text = html_to_text(c.get("text") or "")
        if not text:
            continue
        head = text.split("\n", 1)[0]
        parts = [p.strip() for p in head.split("|")]
        if len(parts) < 3:
            continue  # not in the standard format, skip rather than guess
        out.append(Job("hn", parts[0][:80], parts[1][:120], f"https://news.ycombinator.com/item?id={c['id']}", text,
                       next((p for p in parts[2:] if re.search(r"remote|onsite|hybrid|[A-Z]{2}\b|,", p, re.I)), parts[2]),
                       True if "remote" in head.lower() and "onsite" not in head.lower() else None,
                       _dt(c.get("created_at")), ats_id=str(c["id"])))
    return out


# --- run everything ---------------------------------------------------------

FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable, "rss": rss}


def fetch_all(cfg: dict, get=http_get, workers: int = 8) -> tuple[list[Job], list[str]]:
    """cfg is sources.yaml. Returns (jobs, errors). Errors name the board so a dead source is never mistaken for 'no jobs'."""
    tasks = [(kind, arg) for kind, args in cfg.items() if kind in FETCHERS for arg in args or []]
    names = {k.lower(): v for k, v in (cfg.get("names") or {}).items()}  # Lever and Ashby give only the URL slug, not the company name
    if cfg.get("hn"):
        tasks.append(("hn", ""))

    def run(t):
        kind, arg = t
        try:
            got = hn_hiring(get) if kind == "hn" else FETCHERS[kind](arg, get)
            for j in got:
                if j.ats in ("lever", "ashby") and j.board.lower() in names:
                    j.company = names[j.board.lower()]
            return got, None
        except Exception as e:  # noqa: BLE001 - named in the report below
            return [], f"{kind}:{arg or 'thread'} failed: {type(e).__name__}: {str(e)[:100]}"

    jobs: list[Job] = []
    errors: list[str] = []
    with ThreadPoolExecutor(workers) as ex:
        for j, err in ex.map(run, tasks):
            jobs += j
            if err:
                errors.append(err)
    return jobs, errors
