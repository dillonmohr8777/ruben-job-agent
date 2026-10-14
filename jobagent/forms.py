"""Work out, before any browser opens, what an application form asks and what we can truthfully answer.

Rules that do not bend:
  - EEO / demographic / disability / veteran questions are never filled. Left at their default (usually 'decline' or blank).
  - A required question we cannot answer from profile.answers becomes a blocker, which sends the job to `ask`.
  - Nothing is guessed. 'Unknown' is a valid result."""

from __future__ import annotations

import json
import re
import urllib.error
from dataclasses import dataclass, field

from .models import html_to_text

EEO = re.compile(r"gender|race|ethnic|hispanic|veteran|disabilit|sexual orientation|transgender|pronoun|protected|self[- ]identif|demographic", re.I)

# (label regex, key in profile.answers). First match wins; order matters.
ANSWER_RULES = [
    (r"authori[sz]ed|authori[sz]ation|legally|eligible to work|right to work", "work_authorized"),
    (r"(located|reside|live|based) in the (u\.?s\.?|united states)|reside in (the )?u\.?s\b", "us_resident"),
    (r"which (u\.?s\.? )?state|what state|^\W*state\b|state ?/ ?province|state of residence", "state"),
    (r"(most recent|current).{0,20}employer|current company", "current_employer"),
    (r"(most recent|current).{0,20}(job )?title", "current_title"),
    (r"sponsor", "requires_sponsorship"),
    (r"^\W*phone", "phone"),
    (r"linkedin", "linkedin"),
    (r"website|portfolio|github|personal site", "website"),
    (r"how did you (first )?(hear|find|learn)|where did you (first )?(hear|find|learn)|referral source", "how_did_you_hear"),
    (r"time ?zone", "timezone"),
    (r"salary|compensation|pay expectation", "salary_expectation"),
    (r"notice|start date|when can you start|availab", "start_availability"),
    (r"previously (worked|employed)|ever (been )?(employed|worked)|worked at or consulted|current or former|former employee", "previously_worked_here"),
    (r"employment agreement|post-employment|non-?compete|restrictions with your", "post_employment_restrictions"),
    (r"preferred last name|legal last name", "last_name"),
    (r"preferred (first )?name|name you.d prefer|go by", "preferred_name"),
    (r"country.*(reside|live|located|based)|where do you (live|reside)", "country"),
    (r"city|where are you (based|located)|current location", "location"),
    (r"privacy (policy|notice)|acknowledge", "privacy_ack"),   # reading the employer's applicant privacy notice is part of applying
    (r"consent|agree", "consent"),   # SMS, marketing, recording consents are yours to give: never in answers, so the agent asks
]
STANDARD_KEYS = {"first_name": "first_name", "last_name": "last_name", "email": "email", "phone": "phone"}


@dataclass
class Fill:
    name: str  # field name/id in the form
    label: str
    kind: str  # text | select | file
    value: str
    required: bool = False


@dataclass
class FormPlan:
    fills: list[Fill] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)  # required questions we cannot answer
    eeo_skipped: int = 0
    left_blank: list[str] = field(default_factory=list)  # optional questions we have no answer for


def answers(profile: dict) -> dict[str, str]:
    c, a = profile.get("contact", {}), dict(profile.get("answers") or {})
    first, _, last = (c.get("full_name") or profile.get("name", "")).partition(" ")
    base = {"first_name": c.get("first_name") or first, "last_name": c.get("last_name") or last, "email": c.get("email", ""),
            "phone": c.get("phone", ""), "linkedin": c.get("linkedin", ""), "website": c.get("website", ""),
            "location": c.get("location", ""), "country": c.get("country", ""), "preferred_name": c.get("first_name") or first,
            "current_employer": (profile.get("experience") or [{}])[0].get("employer", ""),   # straight from the profile, newest first
            "current_title": (profile.get("experience") or [{}])[0].get("title", "")}
    out: dict[str, list[str]] = {}
    for k, v in {**base, **a}.items():
        vals = [str(x).strip() for x in (v if isinstance(v, list) else [v]) if x not in (None, "") and str(x).strip()]
        if vals:
            out[k] = vals
    return out  # every answer is a list of acceptable phrasings, best first


def pick_option(wanted: list[str], options: list[str]) -> str | None:
    """First acceptable phrasing that matches an option exactly, by prefix, or as the only option containing it."""
    for w in wanted:
        lw = w.lower()
        for test in (lambda o: o.lower() == lw, lambda o: o.lower().startswith(lw)):
            hits = [o for o in options if test(o)]
            if hits:
                return hits[0]
        hits = [o for o in options if lw in o.lower()]
        if len(hits) == 1:
            return hits[0]
    return None


def plan_basic(profile: dict) -> FormPlan:
    """Standard contact fields only. Used where the ATS exposes no question list (Lever, Ashby, Workable);
    the browser step then reports any required field it could not fill."""
    a = answers(profile)
    p = FormPlan()
    for k in STANDARD_KEYS:
        if a.get(k):
            p.fills.append(Fill(k, k.replace("_", " ").title(), "text", a[k][0], True))
        else:
            p.blockers.append(f"profile is missing contact.{k}")
    return p


def fetch_greenhouse_questions(board: str, job_id: str, get) -> dict:
    return get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true")


def plan_greenhouse(detail: dict, profile: dict) -> FormPlan:
    a = answers(profile)
    plan = FormPlan()
    plan.eeo_skipped = sum(len(s.get("questions", [])) for s in (detail.get("compliance") or [])) + len(detail.get("demographic_questions") or [])
    for q in detail.get("questions", []):
        label, required, fields = html_to_text(q.get("label", "")), bool(q.get("required")), q.get("fields", [])
        if not fields:
            continue
        f = fields[0]
        if f["name"] in ("resume", "cover_letter"):
            continue  # files are uploaded by the browser step
        if f["name"] in ("resume_text", "cover_letter_text"):
            continue
        if EEO.search(label):
            plan.eeo_skipped += 1
            continue
        key = f["name"] if f["name"] in STANDARD_KEYS else next((k for rx, k in ANSWER_RULES if re.search(rx, label, re.I)), None)
        vals = a.get(key or "", [])
        kind = "select" if "select" in f["type"] else "text"
        val = ""
        if vals and kind == "select":
            val = pick_option(vals, [v["label"] for v in f.get("values", [])])
            if not val:
                (plan.blockers if required else plan.left_blank).append(f"{label[:60]!r}: no option matches your answer {vals[0]!r}")
                continue
        elif vals:
            val = vals[0]
        if val:
            plan.fills.append(Fill(f["name"], label, kind, val, required))
        elif required:
            plan.blockers.append(f"required: {label[:80]!r} (add an answer to profile.answers)")
        else:
            plan.left_blank.append(label[:80])
    return plan
