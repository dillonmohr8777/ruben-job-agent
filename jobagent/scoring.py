"""Explainable fit score, 0-100, from the profile only. No model call: the same job always scores the same,
and every point can be traced to a line in the breakdown."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .filters import parse_salary, seniority
from .models import Job, norm

WEIGHTS = {"title": 35, "skills": 30, "seniority": 10, "location": 10, "pay": 10, "fresh": 5}
RED_FLAGS = ["commission only", "commission-only", "unpaid", "100% commission", "pay to apply", "wire transfer", "no experience necessary"]


@dataclass
class Score:
    total: int
    parts: dict[str, float]
    skills_hit: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def skill_terms(profile: dict) -> dict[str, list[str]]:
    """{canonical skill: [names to look for]} from profile.skills (strings or {name, aliases})."""
    out = {}
    for s in profile.get("skills", []):
        name, aliases = (s, []) if isinstance(s, str) else (s["name"], s.get("aliases", []))
        out[name] = [name, *aliases]
    return out


def find_skills(text: str, terms: dict[str, list[str]]) -> list[str]:
    return [k for k, names in terms.items() if any(re.search(rf"(?<![a-z0-9]){re.escape(n.lower())}(?![a-z0-9])", text.lower()) for n in names)]


GENERIC = {"manager", "senior", "sr", "lead", "staff", "principal", "specialist", "associate", "ii", "iii", "head", "director", "vp", "chief", "of", "and", "the", "global", "remote"}


def title_match(title: str, targets: list[str]) -> float:
    """Best share of a target title's distinctive words found in the job title (1.0 = all of them).
    'manager' or 'senior' alone never match: 'Product Manager' must not look like 'Growth Marketing Manager'."""
    tw = set(norm(title).split())
    best = 0.0
    for t in targets:
        words = set(norm(t).split())
        key = (words - GENERIC) or words
        if key:
            best = max(best, len(key & tw) / len(key))
    return best


YEARS = re.compile(r"(\d{1,2})\+?\s*(?:-\s*\d{1,2}\s*)?(?:or more\s+)?years?\s+(?:of\s+)?(?:(?:\w+[ ,/&-]+){0,6}?experience|(?:in|as|working|managing|running)\b)", re.I)


def years_of_experience(profile: dict, now: datetime) -> float:
    """From the earliest start date in the profile to now. A stated number would be a claim; this is arithmetic on dates you gave."""
    starts = [str(e.get("start", "")) for e in profile.get("experience", []) if re.match(r"\d{4}", str(e.get("start", "")))]
    if not starts:
        return 99.0  # no dates, no penalty
    y, _, m = min(starts).partition("-")
    return (now.year - int(y)) + (now.month - int(m or 1)) / 12


def years_required(text: str) -> int:
    return max((int(m.group(1)) for m in YEARS.finditer(text) if 2 <= int(m.group(1)) <= 20), default=0)


def score(job: Job, profile: dict, now: datetime | None = None) -> Score:
    s, now = profile["search"], now or datetime.now(timezone.utc)
    notes: list[str] = []
    parts: dict[str, float] = {}

    parts["title"] = WEIGHTS["title"] * title_match(job.title, s.get("target_titles", []))

    terms = skill_terms(profile)
    hit = find_skills(f"{job.title}\n{job.description}", terms)
    parts["skills"] = WEIGHTS["skills"] * min(1.0, len(hit) / max(1, min(len(terms), 8)))

    lvl, explicit = seniority(job.title)
    parts["seniority"] = WEIGHTS["seniority"] * (1.0 if explicit else 0.6)  # hard filter already enforced the range
    if not explicit:
        notes.append("title has no level word; assumed mid")

    remote = job.remote if job.remote is not None else "remote" in f"{job.location} {job.title}".lower()
    parts["location"] = WEIGHTS["location"] * (1.0 if remote else 0.5)

    if job.salary_max is None:
        job.salary_min, job.salary_max = parse_salary(job.description)
    floor = s.get("min_salary")
    if job.salary_max and floor:
        parts["pay"] = WEIGHTS["pay"] * (1.0 if job.salary_max >= floor * 1.1 else 0.7)
    elif job.salary_max:
        parts["pay"] = WEIGHTS["pay"] * 0.7
    else:
        parts["pay"] = WEIGHTS["pay"] * 0.4
        notes.append("no pay stated")

    age = (now - job.posted).total_seconds() / 86400 if job.posted else 99
    parts["fresh"] = WEIGHTS["fresh"] * (1.0 if age <= 2 else 0.6 if age <= 7 else 0.0)

    text = f"{job.title} {job.description}".lower()
    flags = [f for f in RED_FLAGS if f in text]
    penalty = 15 * len(flags)
    if flags:
        notes.append(f"red flags: {', '.join(flags)}")

    need, have = years_required(job.description), years_of_experience(profile, now)
    if need and need >= have + 2:   # "6+ years" for someone with 4 is a stretch the title match cannot see
        penalty += 10
        notes.append(f"asks {need}+ years of experience; the profile's dates show about {have:.0f}")
    total = max(0, round(sum(parts.values()) - penalty))
    return Score(total, {k: round(v, 1) for k, v in parts.items()}, hit, notes)
