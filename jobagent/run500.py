"""The 500-role run: triage every row of a part-time/contract jobs.csv into a route, then apply down the list by CSV score.

No fit-score gate (policy.volume500.yaml sets the floor to 0), no scoring model and no per-job model: notes come from the
fixed templates in tailor.py and the deterministic claim check. Hard filters kept: companies already applied to or cold-emailed,
Momentum/clients/Align, one application per company per 90 days. Every row ends in exactly one status, appended to results.jsonl."""

from __future__ import annotations

import csv
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import filters, forms, ledger, sources
from . import pipeline as pl
from .models import Job
from .policy import Decision, decide
from .scoring import Score
from .tailor import template_packet
from .volume import ats_from_url

WK_VIEW = re.compile(r"jobs\.workable\.com/view/(\w+)/")
LOGIN_GATED = {"indeed": "Indeed Apply needs a logged-in Indeed account", "indeed-site": "Indeed Apply needs a logged-in Indeed account",
               "linkedin": "LinkedIn Easy Apply needs a logged-in LinkedIn account",
               "remotive": "Remotive's apply flow needs a Remotive account", "jobicy": "Jobicy's apply form is behind a Jobicy account",
               "wwr-all": "We Work Remotely shows the apply link only to registered users",
               "fractionaljobs": "fractionaljobs.io applications go through its own member form"}


def read_rows(path: Path) -> list[dict]:
    rows = [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(open(path, newline=""))]
    for r in rows:
        r["score"] = int(float(r.get("score") or 0))
    return sorted(rows, key=lambda r: -r["score"])   # stable: working down by score


def excluded_names(excl_csv: Path, profile: dict) -> tuple[set[str], list[str]]:
    keys = {filters.company_key(r["company"]) for r in csv.DictReader(open(excl_csv, newline=""))} if excl_csv.exists() else set()
    return keys, profile["search"].get("exclude_companies", [])


def to_job(r: dict, harvest: dict, extra_routes: dict) -> tuple[Job, str | None]:
    """(job, why it cannot be auto-applied or None). The job's ats is the real form type once an outbound link is followed."""
    url, src = r["URL"], r["source"]
    j = Job("csv", r["company"], r["title"], url, " | ".join(r.get(k, "") for k in ("type", "hours", "pay", "why it fits") if r.get(k)), r.get("location", ""), None, None)
    if hit := ats_from_url(url):
        j.ats, j.board, j.ats_id = hit
        return j, None
    if m := WK_VIEW.search(url):
        try:
            d = sources.http_get(f"https://jobs.workable.com/api/v1/jobs/{m.group(1)}")
            j.ats, j.ats_id = "workable", d["id"]
            return j, None
        except Exception as e:  # noqa: BLE001
            return j, f"workable job lookup failed: {type(e).__name__}"
    if src == "himalayas":
        h = harvest.get(url) or {}
        link = h.get("applicationLink") or ""
        if link.startswith("http") and "himalayas.app" not in link:
            if hit := ats_from_url(link):
                j.ats, j.board, j.ats_id = hit
                return j, None
            return j, f"Himalayas links out to {link[:80]}, which is not a form the agent has an adapter for"
        if not h:
            return j, "Himalayas page was not read (see harvest log)"
        return j, "Himalayas-native apply: no employer link or email on the posting; the Apply button goes to a Himalayas talent signup (login required)"
    if src in extra_routes:
        return j, extra_routes[src]
    return j, LOGIN_GATED.get(src, f"{src}: no adapter")


def decide_all(rows: list[dict], profile: dict, pol: dict, paths: pl.Paths, harvest: dict, excl_csv: Path, extra: dict, now: datetime):
    """Returns (items ready to act on, skipped/queued results). Each result: (row, status, reason)."""
    ex_keys, ex_names = excluded_names(excl_csv, profile)
    seen: set[str] = set()
    items: list[pl.Item] = []
    results: list[tuple[dict, str, str]] = []
    rep = pl.Report()
    for r in rows:
        key = filters.company_key(r["company"])
        if any(filters.excluded(r["company"], n) for n in ex_names) or key in ex_keys:
            results.append((r, "SKIPPED", "already applied or cold-emailed, or a Momentum/client/Align/own-employer company"))
            continue
        last = ledger.last_attempt_for(paths.ledger, key)
        if last and now - last < timedelta(days=pol["company_cooldown_days"]):
            results.append((r, "SKIPPED", f"already applied to {r['company']} on {last:%Y-%m-%d} (90-day rule)"))
            continue
        if key in seen:
            results.append((r, "SKIPPED", f"one application per company: a higher-ranked {r['company']} role is already in this run"))
            continue
        seen.add(key)
        job, why = to_job(r, harvest, extra)
        packet = template_packet(job, profile)
        sc = Score(r["score"], {"csv_score": r["score"]}, [])
        plan = pl._form_plan(job, profile, sources.http_get, rep) if job.ats in ("greenhouse", "workable") else forms.plan_basic(profile)
        if why:
            dec = Decision("ask", why)
        else:
            dec = decide(job, sc, packet, plan, pol, paths.ledger, paths.stop, now.astimezone())
        items.append(pl.Item(job, sc, packet, plan, dec))
    return items, results, rep


def main() -> None:  # pragma: no cover - thin wrapper, exercised by the real run
    print("use: python -m jobagent run500 (see cli.py)", file=sys.stderr)


def load_harvest(path: Path) -> dict:
    return {r["url"]: r for r in (json.loads(x) for x in open(path))} if path.exists() else {}


def classify(it: pl.Item) -> tuple[str, str]:
    """(bucket, reason) for an item before any browser opens. Buckets: auto | fill (captcha form, stop at the final page) | queue."""
    j, d = it.job, it.decision
    if d.verdict == "auto":
        return "auto", d.reason
    if j.ats in ("greenhouse", "lever", "ashby", "workable"):   # a real form: fill all we may answer, screenshot, queue the rest
        return "fill", d.reason
    return "queue", d.reason


def status_of(it: pl.Item, bucket: str) -> tuple[str, str]:
    r = it.result
    if r is None:
        return "QUEUED", it.decision.reason
    shots = ", ".join(r.shots[-1:])
    if r.status == "VERIFIED":
        return "SUBMITTED-VERIFIED", "employer confirmation page/state"
    if r.status == "BLOCKED":
        return "QUEUED", f"captcha at Submit, nothing sent. Form is filled in the packet; submit by hand. {shots}"
    if r.status == "DRY_RUN" and bucket == "fill" and not r.unfilled and it.job.ats != "greenhouse":
        return "QUEUED", f"{it.job.ats} shows a human check (captcha) on Submit, so the agent stops at the final page. Filled and screenshotted: {shots}"
    if r.status == "DRY_RUN":
        return "QUEUED", f"{r.note} (rest of the form filled; {shots})"
    if r.status == "NOT_SENT":
        return "QUEUED", f"page did not load or fill, nothing sent: {r.note}"
    if r.status == "NOT_ATTEMPTED":
        return "QUEUED", f"not attempted: {r.note}"
    return "SUBMITTED-UNCONFIRMED", f"{r.status}: {r.note}. Submit was pressed but no employer confirmation appeared; check Gmail before reapplying"


# --- email-apply: the posting itself says "email your resume to ..." ------------------------------------------------------------------------
def _html_note(note: str) -> str:
    import html as _h
    out, bullets = [], []
    for line in note.split("\n"):
        if line.startswith("- "):
            bullets.append(f"<li>{_h.escape(line[2:])}</li>")
            continue
        if bullets:
            out.append("<ul>" + "".join(bullets) + "</ul>")
            bullets = []
        if line.strip():
            out.append(f"<p>{_h.escape(line)}</p>")
    return "".join(out) + ("<ul>" + "".join(bullets) + "</ul>" if bullets else "")


def send_email_apply(job: Job, to: str, packet, resume: Path, signature_html: str, dry: bool = False) -> dict:
    """Send the application from Dillon's Gmail through the composio CLI (is_html, his exact signature, resume attached) and read it back from Sent."""
    import subprocess
    body = _html_note(packet.cover_note.rsplit("\n\n", 1)[0] + "\n\nThanks,") + signature_html   # the signature block carries the name
    data = {"recipient_email": to, "subject": f"Application: {job.title}", "body": body, "is_html": True}
    cmd = ["composio", "execute", "GMAIL_SEND_EMAIL", "-d", json.dumps(data), "--file", str(resume)] + (["--dry-run"] if dry else [])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return {"rc": r.returncode, "out": r.stdout[-600:], "err": r.stderr[-300:]}
