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
experience background skills strong deep hands real results using use used while where when who which why more less than very
ran took wrote cut set got made grew drove held sent spent taught rose fixed january february march april may june july august september october november december""".split())
CREDENTIAL = ["certified", "certification", "certificate", "degree", "bachelor", "bachelors", "master", "masters", "mba", "phd", "doctorate",
              "licensed", "license", "clearance", "fluent", "native speaker", "patent", "published", "award", "awarded", "ranked"]
NUM = re.compile(r"\$?(\d[\d,]*(?:\.\d+)?)\s?(%|percent\b|k\b|m\b|x\b|\+)?", re.I)
WORD = re.compile(r"[A-Za-z][A-Za-z0-9&.'+#-]*")


# Inventions that carry no digit and no capital letter, so the number and name checks cannot see them.
QUANT = re.compile(r"\b(doubled|tripled|quadrupled|triple|double|decades?|millions?|billions?|dozens?|hundreds|thousands|"
                   r"team of (?:\d+|\w+ (?:people|engineers|designers|analysts|marketers|reports))|(?:led|lead|managed|manage|built|ran|run) a team|direct reports?|c-suite|years of experience)\b", re.I)
BIG_NUMBER_WORDS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
                    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
                    "hundred": 100, "thousand": 1000}
# Tools people are tempted to add. Lower-case mentions ("built dashboards in tableau") slip past the capital-letter check.
TOOLS = ("tableau looker marketo salesforce pardot eloqua braze iterable klaviyo segment amplitude mixpanel snowflake bigquery dbt zapier make.com "
         "n8n airtable asana jira shopify wordpress webflow squarespace semrush ahrefs moz screaming frog hubspot marketo outreach salesloft apollo zoominfo "
         "linkedin tiktok pinterest snapchat sql python javascript react figma canva photoshop illustrator").split()
TOOL_BRANDS = {"google", "meta", "linkedin", "microsoft", "bing", "tiktok", "hubspot", "adobe", "canva", "figma", "claude", "chatgpt", "perplexity", "gemini", "copilot", "semrush", "ahrefs"}
STOP = set("a an and the of to in on for with at by from as is are was were be been i my our we you your it its this that these those or but if so than then "
           "also not no yes can could would should will have has had do did does done into over under across per about after before during while where when who which "
           "built set led ran made worked work using including plus like more most other each every own".split())


CLAUSE = re.compile(r",|;|:|\band\b|\bbut\b|\bwhile\b|\bwhich\b|\bso\b|\bthen\b|\bwhere\b|\bwith\b", re.I)


def _stem(w: str) -> str:
    return w[:5]


def _content(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z][a-z0-9'&+-]*", norm(text).replace("'", "")) if w not in STOP and len(w) > 2]


def fact_lines(profile: dict) -> list[tuple[str, str]]:
    """(employer or '', text) for every line the writer may rely on."""
    out = [("", str(profile.get("summary", "")))]
    for e in profile.get("experience", []):
        out += [(e.get("employer", ""), f"{e.get('title', '')} {e.get('employer', '')} {b['text']}") for b in e.get("bullets", [])]
    out += [("", f"{x.get('school', '')} {x.get('degree', '')}") for x in profile.get("education", [])]
    out += [("", str(c)) for c in profile.get("certifications", [])]
    return out


SMALL_NUMBER_WORDS = {"four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, **BIG_NUMBER_WORDS}


def _digits(text: str) -> str:
    """'four leads' -> '4 leads', and drop emails, links and phone numbers whose digits are not claims."""
    text = re.sub(r"\S+@\S+|https?://\S+|\+?(?:\d{3}[-. ])?\d{3}[-. ]\d{4}", " ", text)
    return re.sub(r"\b(" + "|".join(SMALL_NUMBER_WORDS) + r")\b", lambda m: str(SMALL_NUMBER_WORDS[m.group().lower()]), text, flags=re.I)


def number_in_context(text: str, profile: dict) -> list[tuple[str, str]]:
    """Each number must sit next to the same words it sits next to in ONE profile line. A bag of allowed numbers lets
    '25+ accounts' pass because the profile has a '25-prompt scorecard', and '91% organic growth' pass because a different result was 91%."""
    lines = [(emp, _digits(line)) for emp, line in fact_lines(profile)]
    flagged = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", _digits(text)):
        employers = [emp for emp, _ in lines if emp and re.search(rf"\b{re.escape(emp)}\b", sent, re.I)]
        for m in NUM.finditer(sent):
            key, shown = next(numkeys(m.group()))
            if (m.start() and sent[m.start() - 1].isalpha()) or (m.end() < len(sent) and sent[m.end() - 1].isdigit() and sent[m.end()].isalpha()
                                           and not re.match(r"(st|nd|rd|th)\b", sent[m.end():])):
                continue  # part of a name: GA4, H1s, Globex3, B2B
            if 1900 <= key[0] <= 2100 and not key[1]:
                continue  # a year
            near = _content(CLAUSE.split(sent[:m.start()])[-1])[-4:] + _content(CLAUSE.split(sent[m.end():])[0])[:4]   # up to four content words either side, same clause
            if not near:
                continue
            ok = False
            for emp, line in lines:
                if employers and emp not in employers:
                    continue
                if key not in {k for k, _ in numkeys(line)}:
                    continue
                have = set(_content(line))
                if sum(1 for w in near if w in have) >= min(2, len(near)):   # reworded is fine; the same number next to different words is not
                    ok = True
                    break
            if not ok:
                flagged.append((f"{shown} (in: ...{sent[max(0, m.start() - 30):m.end() + 30].strip()}...)", "number not tied to the profile line it came from"))
    return flagged


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

    for m in re.finditer(r"\b(" + "|".join(BIG_NUMBER_WORDS) + r")\b", text, re.I):   # "twelve campaigns": spelled-out numbers are numbers too
        if (BIG_NUMBER_WORDS[m.group().lower()], "") not in allowed_nums and not re.search(rf"\b{m.group().lower()}\b", allowed):
            flagged.append((m.group(), "number not in profile"))
    for m in QUANT.finditer(text):
        if not re.search(rf"\b{re.escape(m.group().lower())}\b", allowed):
            flagged.append((m.group(), "size or seniority claim not in profile"))
    for t in TOOLS:
        if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text.lower()) and not re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", allowed):
            flagged.append((t, "name or term not in profile"))
    employer_names = [e.get("employer", "") for e in profile.get("experience", [])]
    bullets = norm(" ".join(b["text"] for e in profile.get("experience", []) for b in e.get("bullets", [])))
    for m in re.finditer(r"\b[Aa]t ((?:[A-Z][\w&'-]*(?:\.\w+)*)(?:\s+(?:[A-Z][\w&'-]*(?:\.\w+)*|&|\d+))*)", text):   # "At Google I ran..." when Google is only a tool in the profile
        words = m.group(1).split()
        while words and words[-1] in ("I", "My", "We", "Our", "The"):
            words.pop()
        name = " ".join(words)
        known = any(norm(name) == norm(e) or norm(e).startswith(norm(name) + " ") for e in employer_names if e)   # "Acme" for "Acme 360"
        client = norm(name) in bullets and norm(name) not in TOOL_BRANDS     # a client named in a bullet (Bridge Software Development) is fine
        if name and not known and not client and norm(name) not in norm(job_allowed):
            flagged.append((name, "employer not in profile"))
    for m in re.finditer(r"\b(more than|over|in excess of|upwards of|nearly|almost|at least|up to)\s+\$?\d[\d,]*", text, re.I):   # "more than 10" is not "10+"
        if norm(m.group()) not in allowed:
            flagged.append((m.group(), "qualifier changes a number in the profile"))
    flagged += number_in_context(text, profile)

    for m in WORD.finditer(text):
        w = m.group().strip(".'")
        if not w or not w[0].isupper():
            continue
        if w.lower() in COMMON or all(t in allowed_words for t in norm(w).split()):
            continue
        if "-" in w and all(p.lower() in COMMON or p.lower() in allowed_words or not (p[:1].isupper() or re.search(r"\d", p)) for p in w.split("-")):
            continue  # "AI-enabled": only the capitalised or digit-bearing parts need to be in the profile
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


def profile_warnings(profile: dict) -> list[str]:
    """Catch profile typos that silently change facts. In YAML an unquoted ' #' starts a comment, so
    `text: ... multiple #1 Google rankings` loses everything after 'multiple' and nothing complains."""
    out = []
    for e in profile.get("experience", []):
        for b in e.get("bullets", []):
            t = str(b.get("text", "")).strip()
            if t and t[-1] not in ".!?)\"'":
                out.append(f"{e.get('employer')}: bullet looks cut off (unquoted '#'? stray ':'?): ...{t[-50:]!r}")
    return out


JUDGE_SYSTEM = ("You audit a job-application cover note for invented facts. PROFILE is the ONLY source of truth about the candidate. "
                "List EVERY factual claim the note makes about the candidate (numbers, employers, titles, clients, tools, outcomes, credentials, scope, habits, skills, "
                "team size, seniority). For each, quote the exact PROFILE text that supports it, or null. verdict: supported | stretched (true but stronger, wider or "
                "differently attributed than the profile) | unsupported (no profile text says it, including preferences, motives and habits like 'I enjoy fixing X' or 'I care about clean data', "
                "and results or methods the profile does not state). "
                "Not claims, ignore them: the candidate's name, greetings and sign-offs, saying they are applying for the role, mentioning an attached resume or portfolio link, offers to talk or to send things, "
                "interest in the company, and anything about the job or company. Reply JSON only: "
                '{"claims":[{"claim":"...","support":"exact profile text or null","verdict":"supported|stretched|unsupported"}]}')


def judge_with_model(text: str, profile: dict, judge) -> tuple[list[str], list[str]]:
    """Second opinion from a DIFFERENT model than the writer (set JUDGE_MODEL). Returns (unsupported claims, stretched claims).
    The regex checker cannot see plain-prose inventions ('I think in funnels'); a model reading the profile can.
    The caller rejects a draft with either kind of finding."""
    import json as _json
    from .llm import parse_json
    facts = {k: v for k, v in profile.items() if k in ("summary", "skills", "experience", "education", "certifications")}
    d = parse_json(judge(JUDGE_SYSTEM, "PROFILE:\n" + _json.dumps(facts, default=str) + "\n\nNOTE:\n" + text))
    claims = d.get("claims", [])
    return [c["claim"] for c in claims if c.get("verdict") == "unsupported"], [c["claim"] for c in claims if c.get("verdict") == "stretched"]
