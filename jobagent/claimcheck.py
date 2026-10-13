"""Outside judge for generated text. The writer (a model or a template) never grades itself:
this checks every number, name, tool and credential in a bullet or cover note against the profile.
Anything it cannot find in the profile is flagged, and a flagged draft is never auto-applied.

It is deliberately strict. A false alarm costs you one manual look; a missed invention costs a job."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .models import norm

# Words that are capitalised only because they start a sentence or are plain greeting/closing words.
COMMON = set("""a an and as at by for from i i'm i've i'd i'll in is it its my of on or our so that the their then there these this to we
with you your hi hello hey dear thanks thank best regards sincerely cheers please team hiring manager role position company looking
excited happy glad love would could should can will have has had am are was were be been being not no yes also both each over into
during across about after before led built ran managed grew drove owned launched
one two three four five six things thing most relevant fits fit short call talk through how what need needs welcome work working
experience background skills strong deep hands real results using use used while where when who which why more less than very""".split())
CREDENTIAL = ["certified", "certification", "certificate", "degree", "bachelor", "bachelors", "master", "masters", "mba", "phd", "doctorate",
              "licensed", "license", "clearance", "fluent", "native speaker", "patent", "published", "award", "awarded", "ranked"]
NUM = re.compile(r"\$?(\d[\d,]*(?:\.\d+)?)\s?(%|percent\b|k\b|m\b|x\b|\+)?", re.I)
WORD = re.compile(r"[A-Za-z][A-Za-z0-9&.'+#-]*")


@dataclass
class ClaimReport:
    ok: bool
    flagged: list[tuple[str, str]]  # (text, kind)

    def __str__(self) -> str:
        return "ok" if self.ok else "; ".join(f"{k}: {t}" for t, k in self.flagged)


def corpus(profile: dict, job_allowed: str = "") -> str:
    """Everything the writer is allowed to say, as one lowercase string. Job company/title are allowed
    (naming the company is not a claim about you); job description facts are NOT."""
    return norm(json.dumps({k: v for k, v in profile.items() if k not in ("search", "answers")}, default=str) + " " + job_allowed)


def numkeys(text: str):
    """Yield ((value, unit), shown) per number. $40k and 40,000 are the same value; 40% and 40x are not."""
    for m in NUM.finditer(text):
        v, u = float(m.group(1).replace(",", "")), (m.group(2) or "").lower()
        if u in ("k", "m"):
            v, u = v * (1000 if u == "k" else 1_000_000), ""
        yield (v, "%" if u == "percent" else ("" if u == "+" else u)), m.group().strip()


def check(text: str, profile: dict, job_allowed: str = "") -> ClaimReport:
    allowed = corpus(profile, job_allowed)
    allowed_words = set(allowed.split())
    allowed_nums = {k for k, _ in numkeys(json.dumps(profile, default=str) + " " + job_allowed)}
    flagged: list[tuple[str, str]] = []

    for k, shown in numkeys(text):
        if k not in allowed_nums:
            flagged.append((shown, "number not in profile"))

    for m in WORD.finditer(text):
        w = m.group().strip(".'")
        if not w or not w[0].isupper():
            continue
        if w.lower() in COMMON or all(t in allowed_words for t in norm(w).split()):
            continue
        # A capitalised word that opens a sentence or bullet may just be a verb ("Optimized ...").
        # Mid-sentence capitals are names. Tool-looking words (HubSpot, SEMrush, GA4) are always checked.
        before = text[:m.start()].rstrip(" \t-*\u2022")
        opens = before == "" or before[-1] in ".!?:\n"
        toolish = bool(re.search(r"[A-Z]|\d", w[1:]))
        if opens and not toolish and re.search(r"(ed|ing|ly)$", w.lower()):
            continue
        flagged.append((w, "name or term not in profile"))

    low = norm(text)
    for c in CREDENTIAL:
        if re.search(rf"\b{c}\b", low) and not re.search(rf"\b{c}\b", allowed):
            flagged.append((c, "credential not in profile"))

    seen, uniq = set(), []
    for t, k in flagged:
        if (t.lower(), k) not in seen:
            seen.add((t.lower(), k))
            uniq.append((t, k))
    return ClaimReport(not uniq, uniq)
