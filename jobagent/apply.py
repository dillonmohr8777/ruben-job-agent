"""Browser step: fill a Greenhouse or Lever application, screenshot it, and (only with submit=True) press Submit.

Browsers:  local      headless Chromium on this machine (free; use for dry runs)
           browserbase  a cloud browser with a persistent context, so logins survive between runs, plus captcha solving

Nothing here decides whether to apply. policy.decide does that. This module only does what it is told,
refuses to touch EEO fields, and says VERIFIED only when the confirmation page actually appeared."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import datetime
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .forms import ANSWER_RULES, EEO, FormPlan, answers, pick_option
from .models import Job

BB_API = "https://api.browserbase.com/v1"
CONFIRM_TEXT = re.compile(r"thank you for applying|thanks for applying|application (has been )?(submitted|received)|we.ve received your application|application submitted", re.I)
CONFIRM_URL = re.compile(r"confirmation|/thanks|submitted", re.I)
ASHBY_SUBMIT_OP = "ApiSubmitSingleApplicationFormAction"   # the GraphQL mutation Ashby's own form sends on Submit


CODE_SENT = re.compile(r"verification code was sent|enter the \d-character code|security code", re.I)


def email_code(company: str, since: float, wait: int = 120) -> str | None:
    """The newest Greenhouse security-code email for this company, read through the composio CLI (Gmail). Never logged."""
    if not shutil.which("composio"):
        return None
    q = json.dumps({"query": "from:greenhouse-mail.io newer_than:1d", "max_results": 5, "include_payload": True, "verbose": True})
    for _ in range(wait // 10):
        time.sleep(10)
        r = subprocess.run(["composio", "execute", "GMAIL_FETCH_EMAILS", "-d", q], capture_output=True, text=True, timeout=60)
        try:
            d = json.loads(r.stdout)
            if d.get("storedInFile"):  # large replies are written to a temp file instead of stdout
                f = Path(d["outputFilePath"])
                d = json.loads(f.read_text())
                f.unlink(missing_ok=True)   # it holds the code
            msgs = (d.get("data") or d)["messages"]
        except Exception:  # noqa: BLE001
            continue
        for m in msgs:
            ts = datetime.fromisoformat(m.get("messageTimestamp", "1970-01-01T00:00:00Z").replace("Z", "+00:00")).timestamp()
            if ts >= since - 60 and company.lower() in (m.get("subject") or "").lower():
                if hit := re.search(r"<h1>\s*([A-Za-z0-9]{8})\s*</h1>", json.dumps(m)):
                    return hit.group(1)
    return None


class ApplyError(RuntimeError):
    pass


@dataclass
class ApplyResult:
    status: str  # DRY_RUN | VERIFIED | FAILED
    shots: list[str] = field(default_factory=list)
    unfilled: list[str] = field(default_factory=list)  # required fields we could not fill
    note: str = ""


# --- browsers -----------------------------------------------------------------


def _bb(method: str, path: str, key: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(BB_API + path, json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"X-BB-API-Key": key, "Content-Type": "application/json"})
    for i in range(5):  # 429: back off (Retry-After, else doubling), then give up
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == 4:
                raise
            time.sleep(float(e.headers.get("Retry-After") or 2 ** (i + 1)))


def create_context(env: dict) -> str:
    """One-time: makes the persistent Browserbase context that keeps your logins between runs."""
    return _bb("POST", "/contexts", env["BROWSERBASE_API_KEY"], {"projectId": env["BROWSERBASE_PROJECT_ID"]})["id"]


@contextmanager
def open_page(mode: str, env: dict, meta: dict | None = None):
    from playwright.sync_api import sync_playwright  # lazy: not needed for fetch/score/tailor

    with sync_playwright() as p:
        if mode == "local":
            b = p.chromium.launch()
            page = b.new_context(viewport={"width": 1280, "height": 1600}).new_page()
            try:
                yield page
            finally:
                b.close()
            return
        key, proj = env.get("BROWSERBASE_API_KEY"), env.get("BROWSERBASE_PROJECT_ID")
        if not key or not proj:
            raise ApplyError("set BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID in .env")
        settings = {"solveCaptchas": False}   # a captcha is a stop sign for a human, never something the agent gets past
        if env.get("BROWSERBASE_CONTEXT_ID"):
            settings["context"] = {"id": env["BROWSERBASE_CONTEXT_ID"], "persist": True}
        sess = _bb("POST", "/sessions", key, {"projectId": proj, "proxies": env.get("BROWSERBASE_PROXIES") == "1", "browserSettings": settings,
                                        "timeout": int(env.get("BROWSERBASE_TIMEOUT", 600)), "userMetadata": meta or {"agent": "job-agent"}})
        try:
            b = p.chromium.connect_over_cdp(sess["connectUrl"])
            ctx = b.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # proxy bandwidth is the plan's tight limit and forms need none of these
            page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font") else r.continue_())
            yield page
        finally:
            try:
                _bb("POST", f"/sessions/{sess['id']}", key, {"projectId": proj, "status": "REQUEST_RELEASE"})
            except Exception:  # noqa: BLE001 - a failed release only costs idle minutes; the session times out
                pass


def ensure_login(page, site: dict, login) -> None:
    """Log in to a site from logins.yaml if the persistent session is not already signed in."""
    page.goto(site["login_url"], wait_until="domcontentloaded")
    if page.query_selector(site["logged_in"]):
        return
    page.fill(site["user"], login.username)
    page.fill(site["pass"], login.password)
    page.click(site["submit"])
    try:
        page.wait_for_selector(site["logged_in"], timeout=20000)
    except Exception:  # noqa: BLE001
        raise ApplyError(f"login to {site['login_url']} not confirmed (2FA or captcha?). Do it once by hand in the same context.") from None


# --- helpers ------------------------------------------------------------------


def _pick(page, name: str, value: str) -> None:
    """Greenhouse's dropdowns are react-select comboboxes."""
    page.click(f"#{name}")
    opts = page.locator("[role=option], .select__option")
    try:
        opts.first.wait_for(timeout=3000)
        texts = [t.strip() for t in opts.all_inner_texts()]
        i = next((i for i, t in enumerate(texts) if t == value), None)
        if i is None:  # 'United States +1' for 'United States'
            i = next((i for i, t in enumerate(texts) if t.lower().startswith(value.lower())), None)
        if i is not None:
            opts.nth(i).click()
            return
    except Exception:  # noqa: BLE001 - fall through to typing
        pass
    page.keyboard.type(value)
    page.keyboard.press("Enter")


US_STATES = dict(a.split(":") for a in (
    "AL:Alabama AK:Alaska AZ:Arizona AR:Arkansas CA:California CO:Colorado CT:Connecticut DE:Delaware FL:Florida GA:Georgia HI:Hawaii ID:Idaho "
    "IL:Illinois IN:Indiana IA:Iowa KS:Kansas KY:Kentucky LA:Louisiana ME:Maine MD:Maryland MA:Massachusetts MI:Michigan MN:Minnesota "
    "MS:Mississippi MO:Missouri MT:Montana NE:Nebraska NV:Nevada NH:New_Hampshire NJ:New_Jersey NM:New_Mexico NY:New_York NC:North_Carolina "
    "ND:North_Dakota OH:Ohio OK:Oklahoma OR:Oregon PA:Pennsylvania RI:Rhode_Island SC:South_Carolina SD:South_Dakota TN:Tennessee TX:Texas "
    "UT:Utah VT:Vermont VA:Virginia WA:Washington WV:West_Virginia WI:Wisconsin WY:Wyoming DC:District_of_Columbia").split())


def _pick_location(page, text: str) -> None:
    """Greenhouse's city field is a places autocomplete. Type the city, then click a suggestion that starts with the
    city and names the right state. If nothing matches exactly, leave it empty: it is reported, never guessed."""
    el = page.query_selector("#candidate-location")
    if not el:
        return
    city, _, state = [x.strip() for x in (text + ",,").split(",")][:3]
    state_full = US_STATES.get(state.upper(), state).replace("_", " ").lower()
    el.click()
    page.keyboard.type(city, delay=60)
    try:
        page.wait_for_selector(".select__option", timeout=6000)
        for opt in page.query_selector_all(".select__option"):
            t = opt.inner_text().strip().lower()
            if t.startswith(city.lower() + ",") and (not state_full or state_full in t):
                opt.click()
                return
    except Exception:  # noqa: BLE001
        pass
    page.keyboard.press("Escape")


JS_UNFILLED = """() => [...document.querySelectorAll('input,textarea,select')].filter(e => {
  const req = e.required || e.getAttribute('aria-required') === 'true';
  if (!req || e.type === 'hidden' || e.type === 'checkbox' && e.checked) return false;
  const box = e.closest('[class*="select__control"]');  // react-select keeps its value in a styled div, not the input
  if (box && box.querySelector('[class*="single-value"],[class*="multi-value"]')) return false;
  if (e.type === 'file') return e.files.length === 0;
  if (e.type === 'radio') return ![...document.querySelectorAll('input[type=radio]')].some(r => r.name === e.name && r.checked);   // a radio's value is never empty; its group is what must be answered
  return !(e.value || '').trim();
}).map(e => {
  const q = e.type === 'radio' && ((e.closest('.application-field') || {}).parentElement || e.closest('fieldset') || e.closest('.application-question') || e.closest('li'));   // name the question, not the option ("Yes")
  if (q) return ((q.querySelector('.application-label, legend') || q).innerText.split('\\n')[0] + '').trim().slice(0, 90);
  const l = (e.id && document.querySelector(`label[for="${e.id}"]`)) || e.closest('label') || e.closest('fieldset,.field,.application-question,li,div');
  return ((e.getAttribute('aria-label') || (l && l.innerText) || e.name || e.id || e.type) + '').trim().slice(0, 90);
})"""


def unfilled_required(page) -> list[str]:
    return sorted({x for x in page.evaluate(JS_UNFILLED) if x})


def _shot(page, shots: Path, tag: str) -> str:
    shots.mkdir(parents=True, exist_ok=True)
    f = shots / f"{tag}.png"
    page.screenshot(path=str(f), full_page=True)
    return str(f)


# --- the two supported forms ----------------------------------------------------


def _fill_greenhouse(page, plan: FormPlan, profile: dict, resume: Path, note_file: Path | None) -> None:
    for f in plan.fills:
        if f.kind == "select":
            _pick(page, f.name, f.value)
        else:
            page.fill(f"#{f.name}", f.value)
    c = profile.get("contact", {})
    if c.get("country") and page.query_selector("#country"):
        _pick(page, "country", c["country"])
    if c.get("location"):
        _pick_location(page, c["location"])
    page.set_input_files("input#resume", str(resume))
    if note_file and page.query_selector("input#cover_letter"):
        page.set_input_files("input#cover_letter", str(note_file))
    for name in (resume.name, note_file.name if note_file else None):  # wait for the uploads to finish, so the screenshot proves them
        if name:
            try:
                page.get_by_text(name).first.wait_for(timeout=15000)
            except Exception:  # noqa: BLE001
                pass


LEVER_FIELDS = {"name": "name", "email": "email", "phone": "phone", "org": "org", "location": "location",
                "linkedin": "urls[LinkedIn]", "website": "urls[Portfolio]"}


JS_RADIO_GROUPS = """() => {
  const g = {};
  for (const r of document.querySelectorAll('input[type=radio]')) (g[r.name] = g[r.name] || []).push(r);
  return Object.entries(g).map(([name, rs]) => {
    const box = (rs[0].closest('.application-field') || {}).parentElement || rs[0].closest('fieldset') || rs[0].closest('.application-question') || rs[0].closest('li');
    const q = (box.querySelector('.application-label, legend') || box).innerText.split('\\n')[0].trim();
    return {name, q, opts: rs.map(r => ((r.labels && r.labels[0]) || r.parentElement).innerText.trim())};
  });
}"""


def _answer_radio_groups(page, profile: dict) -> None:
    """Yes/No and choice questions (work authorization, sponsorship, how did you hear). Answered only when a profile answer matches an option."""
    a = answers(profile)
    for g in page.evaluate(JS_RADIO_GROUPS):
        if EEO.search(g["q"]):
            continue
        key = next((k for rx, k in ANSWER_RULES if re.search(rx, g["q"], re.I)), None)
        pick = pick_option(a.get(key or "", []), g["opts"])
        if pick is not None:
            page.locator(f'input[type=radio][name="{g["name"]}"]').nth(g["opts"].index(pick)).evaluate("r => ((r.labels && r.labels[0]) || r).click()")


def _fill_lever(page, plan: FormPlan, profile: dict, resume: Path, note: str) -> None:
    by = {f.name: f.value for f in plan.fills}
    full = " ".join(x for x in (by.get("first_name"), by.get("last_name")) if x)
    vals = {"name": full, "email": by.get("email"), "phone": by.get("phone"),
            "org": ((profile.get("experience") or [{}])[0]).get("employer"),   # most recent employer, straight from the profile
            "location": (profile.get("contact") or {}).get("location"),
            "linkedin": (profile.get("contact") or {}).get("linkedin"), "website": (profile.get("contact") or {}).get("website")}
    for key, name in LEVER_FIELDS.items():
        el = page.query_selector(f'[name="{name}"]')
        if not el or not vals.get(key):
            continue
        if key == "location":  # autocomplete: only a clicked suggestion counts
            el.click()
            page.keyboard.type(vals[key], delay=40)
            city = vals[key].split(",")[0].strip().lower()
            try:
                page.wait_for_selector(".dropdown-location", timeout=5000)
                for opt in page.query_selector_all(".dropdown-location"):
                    if opt.inner_text().strip().lower().startswith(city):
                        opt.click()
                        break
            except Exception:  # noqa: BLE001
                pass
        else:
            el.fill(vals[key])
    page.set_input_files('input[name="resume"]', str(resume))
    if el := page.query_selector('textarea[name="comments"]'):
        el.fill(note)
    _answer_radio_groups(page, profile)


JS_ASHBY_TEXT = """([label, value]) => {
  const rx = new RegExp(label, 'i');
  for (const e of document.querySelectorAll('input[type=text],input[type=tel],input[type=url],textarea')) {
    if (e.value) continue;
    const l = (e.id && document.querySelector(`label[for="${e.id}"]`)) || e.closest('label') || e.closest('div[class*="field"],fieldset,div');
    if (l && rx.test(l.innerText.split('\\n')[0])) {
      const set = Object.getOwnPropertyDescriptor(e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value').set;
      set.call(e, value); e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true}));
      return true;
    }
  }
  return false;
}"""


def _fill_ashby(page, plan: FormPlan, profile: dict, resume: Path, note: str) -> None:
    """Ashby forms are plain React inputs with stable ids for name, email and resume; everything else is a custom question,
    answered only when its label is one we have a profile fact for. The rest is reported as unfilled, never guessed."""
    by = {f.name: f.value for f in plan.fills}
    c = profile.get("contact", {})
    page.fill("#_systemfield_name", " ".join(x for x in (by.get("first_name"), by.get("last_name")) if x))
    page.fill("#_systemfield_email", by.get("email", ""))
    page.set_input_files("#_systemfield_resume", str(resume))
    for label, value in (("linkedin", c.get("linkedin")), ("phone", c.get("phone")), ("website|portfolio", c.get("website")),
                         ("cover letter|additional information|anything else", note)):
        if value:
            page.evaluate(JS_ASHBY_TEXT, [label, value])
    _ashby_yes_no_and_location(page, profile)
    try:
        page.get_by_text(resume.name).first.wait_for(timeout=15000)  # the uploaded file name appearing is the proof it attached
    except Exception:  # noqa: BLE001
        pass


JS_ASHBY_UNFILLED = """() => [...document.querySelectorAll('[class*="_fieldEntry_"]')].filter(e => {
  const lab = e.querySelector('label');
  if (!lab || !/_required_/.test(lab.className)) return false;
  if (e.querySelector('.ashby-application-form-input-yesno')) return !e.querySelector('[aria-pressed=true]');   // a 'No' leaves the hidden checkbox unchecked
  const tx = e.querySelector('input[type=tel],input[type=text],input[type=email],textarea');
  if (tx && tx.value.trim()) return false;   // Ashby's Phone carries optional SMS/WhatsApp consent radios; the number is the answer
  const boxes = [...e.querySelectorAll('input[type=checkbox],input[type=radio]')];
  if (boxes.length) return !boxes.some(b => b.checked);
  const f = e.querySelector('input[type=file]');
  if (f) return f.files.length === 0 && !/\\.(pdf|docx?|txt)/i.test(e.innerText);
  const t = e.querySelector('input,textarea');
  return !(t && t.value.trim());
}).map(e => e.querySelector('label').innerText.trim().slice(0, 90))"""


def _ashby_yes_no_and_location(page, profile: dict) -> None:
    """Yes/No buttons answered only from profile.answers (work authorization etc.); EEO never; the city autocomplete only by clicking a real suggestion."""
    a = answers(profile)
    entries = page.locator('[class*="_fieldEntry_"]')
    for i in range(entries.count()):
        e = entries.nth(i)
        label = e.locator("label").first.inner_text()
        if EEO.search(label):
            continue
        yes, no = e.get_by_role("button", name="Yes", exact=True), e.get_by_role("button", name="No", exact=True)
        key = next((k for rx, k in ANSWER_RULES if re.search(rx, label, re.I)), None)
        wanted = a.get(key or "") or [""]
        want = wanted[0]
        radios = e.locator("input[type=radio]")
        if radios.count() and wanted[0]:
            opts = [radios.nth(j).evaluate("r => ((r.labels && r.labels[0]) || r.parentElement).innerText.trim()") for j in range(radios.count())]
            if (pick := pick_option(wanted, opts)) is not None:   # only an option that matches a profile answer; consents and the rest stay unanswered
                radios.nth(opts.index(pick)).evaluate("r => ((r.labels && r.labels[0]) || r).click()")
            continue
        if yes.count() and no.count() and want in ("Yes", "No"):
            (yes if want == "Yes" else no).first.click()
        elif (box := e.locator("input[type=text],input[type=tel]")).count() and want and key in ("phone", "how_did_you_hear", "linkedin", "website") and not box.first.input_value():
            box.first.fill(want)
        elif re.fullmatch(r"\s*location\s*\*?\s*", label, re.I) and (loc := (profile.get("contact") or {}).get("location")):
            box = e.get_by_role("combobox")
            box.click()
            page.keyboard.type(loc.split(",")[0], delay=50)
            try:
                opt = page.get_by_role("option").filter(has_text=re.compile(rf"^{re.escape(loc.split(',')[0])}", re.I)).first
                opt.wait_for(timeout=6000)
                opt.click()
            except Exception:  # noqa: BLE001 - leave it empty and let the unfilled check report it
                page.keyboard.press("Escape")


def ashby_verdict(status: int, body: dict) -> tuple[bool, str]:
    """Judge Ashby's submit response. Ashby's own page treats `applicationFormResult.__typename == "FormSubmitSuccess"` as the
    moment the application exists (it only then shows its thank-you screen), so that is the signal here, not any page text."""
    if status != 200:
        return False, f"submit returned HTTP {status}"
    if body.get("errors"):
        return False, "submit error: " + "; ".join(str(e.get("message", e))[:120] for e in body["errors"][:3])
    act = (body.get("data") or {}).get("submitApplicationFormAction") or {}
    res = act.get("applicationFormResult") or {}
    if res.get("__typename") == "FormSubmitSuccess":
        return True, ""
    blocked = ((act.get("messages") or {}).get("blockMessageForCandidateHtml") or "").strip()
    errs = [e.get("message", "") for e in (res.get("errorMessages") or []) + (res.get("formErrors") or []) if isinstance(e, dict)]
    return False, f"Ashby did not accept it: {res.get('__typename') or 'no result'} {blocked[:160]} {'; '.join(errs)[:160]}".strip()


JS_CAPTCHA_SHOWN = """() => [...document.querySelectorAll('iframe')].some(f => /recaptcha|hcaptcha|captcha/i.test((f.src || '') + (f.title || ''))
  && /challenge|bframe|hcaptcha-challenge/i.test((f.src || '') + (f.title || '')) && f.getBoundingClientRect().height > 100)"""


def _submit_ashby(page) -> tuple[bool, str]:
    """Press Submit Application (not the other type=submit buttons on the form, such as Upload file or Yes/No) and read Ashby's reply."""
    btn = page.get_by_role("button", name=re.compile(r"^\s*submit application", re.I))
    try:
        with page.expect_response(lambda r: ASHBY_SUBMIT_OP in r.url, timeout=60000) as info:
            btn.first.click()
        r = info.value
    except Exception as e:  # noqa: BLE001 - playwright TimeoutError: nothing was sent
        if page.evaluate(JS_CAPTCHA_SHOWN):   # a human-check challenge is a stop sign; the agent never solves or works around it
            return False, "NOT SUBMITTED: a captcha challenge appeared after Submit and was not completed; nothing was sent"
        if re.search(r"thank you for applying", page.inner_text("body"), re.I):   # Ashby's own success banner; its reply came after the wait
            return True, ""
        return False, f"Submit sent nothing within 60s (the form's own validation probably blocked it): {type(e).__name__}"
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return False, f"submit reply was not JSON (HTTP {r.status})"
    return ashby_verdict(r.status, body)


def apply_job(page, job: Job, profile: dict, plan: FormPlan, cover_note: str, resume: Path, shots: Path, submit: bool = False,
              allow_empty: tuple[str, ...] = ()) -> ApplyResult:
    """submit=False fills and screenshots only. submit=True also presses Submit and requires the employer's confirmation.
    allow_empty: labels a human has said may stay blank (for example an SMS-consent question). Anything else empty blocks the submit."""
    if job.ats not in ("greenhouse", "lever", "ashby"):
        raise ApplyError(f"{job.ats} is not an auto-apply form")
    resume = Path(resume)
    if not resume.exists():
        raise ApplyError(f"resume not found: {resume}")
    shots.mkdir(parents=True, exist_ok=True)
    note_dir = shots / job.id  # the employer sees this filename, so keep it human
    note_dir.mkdir(parents=True, exist_ok=True)
    note_file = note_dir / f"{(profile.get('name') or 'Applicant').replace(' ', '-')}-cover-note.txt"
    note_file.write_text(cover_note)
    # Greenhouse boards on a company's own domain (stripe.com/jobs/...) all have this hosted form too.
    url = {# the embed form is the same hosted form, and it also works for boards whose job page lives on the company's own site (acme.com)
           "greenhouse": f"https://job-boards.greenhouse.io/embed/job_app?for={job.board}&token={job.ats_id}",
           "lever": f"https://jobs.lever.co/{job.board}/{job.ats_id}/apply",
           "ashby": f"https://jobs.ashbyhq.com/{job.board}/{job.ats_id}/application"}[job.ats]
    try:  # nothing has reached the employer yet: a failure here is NOT_SENT, which never starts the 90-day cooldown
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)   # Lever pages with live analytics never go idle
        except Exception:  # noqa: BLE001
            pass
        if job.ats == "greenhouse":
            _fill_greenhouse(page, plan, profile, resume, note_file)
        elif job.ats == "lever":
            _fill_lever(page, plan, profile, resume, cover_note)
        else:
            _fill_ashby(page, plan, profile, resume, cover_note)
    except Exception as e:  # noqa: BLE001
        return ApplyResult("NOT_SENT", note=f"{type(e).__name__}: {str(e)[:160]}")
    out = ApplyResult("DRY_RUN", [_shot(page, shots, f"{job.id}-filled")])
    out.unfilled = sorted(set(page.evaluate(JS_ASHBY_UNFILLED))) if job.ats == "ashby" else unfilled_required(page)
    blocking = [x for x in out.unfilled if not any(a.lower() in x.lower() for a in allow_empty if a.strip())]
    if blocking or (out.unfilled and not submit):
        out.note = "required fields still empty: " + "; ".join(out.unfilled[:5])
        if any(EEO.search(x) for x in out.unfilled):
            out.note += " (includes legal/EEO fields; those are never auto-filled)"
        return out
    if not submit:
        return out
    if job.ats == "ashby":
        ok, why = _submit_ashby(page)
        out.status, out.note = ("VERIFIED", "") if ok else ("BLOCKED" if why.startswith("NOT SUBMITTED") else "FAILED", why)
        out.shots.append(_shot(page, shots, f"{job.id}-after"))
        return out
    try:  # Lever parses the resume first and its Submit is not clickable until then
        page.wait_for_function("() => !/analyzing resume/i.test(document.body.innerText)", timeout=30000)
    except Exception:  # noqa: BLE001
        pass
    submit_btn = page.get_by_role("button", name=re.compile(r"^\s*submit( application)?\s*$", re.I)).first   # not Lever's hidden #hcaptchaSubmitBtn
    sent_at = time.time()
    submit_btn.click()
    try:
        page.wait_for_function(
            "([t,u,c]) => new RegExp(t,'i').test(document.body.innerText) || new RegExp(u,'i').test(location.href) || new RegExp(c,'i').test(document.body.innerText)",
            arg=[CONFIRM_TEXT.pattern, CONFIRM_URL.pattern, CODE_SENT.pattern], timeout=25000)
        if CODE_SENT.search(page.inner_text("body")) and not CONFIRM_TEXT.search(page.inner_text("body")):
            code = email_code(job.company, sent_at)   # Greenhouse emails the applicant an 8-character code; it proves the address, it is not a captcha
            if not code:
                raise ApplyError("security code email not found")
            page.locator("input[id^=security-input], input[name*=security]").first.click()
            page.keyboard.type(code)
            submit_btn.click()
            page.wait_for_function("([t,u]) => new RegExp(t,'i').test(document.body.innerText) || new RegExp(u,'i').test(location.href)",
                                   arg=[CONFIRM_TEXT.pattern, CONFIRM_URL.pattern], timeout=25000)
        out.status = "VERIFIED"  # the employer's own confirmation page, not our click, is the proof
    except Exception as e:  # noqa: BLE001
        captcha = re.search(r"complete the recaptcha", page.inner_text("body"), re.I) or page.evaluate(JS_CAPTCHA_SHOWN)
        if not captcha and isinstance(e, ApplyError):
            out.status, out.note = "FAILED", str(e)
            out.shots.append(_shot(page, shots, f"{job.id}-after"))
            return out
        out.status, out.note = ("BLOCKED", "NOT SUBMITTED: the form asked for a captcha; nothing was sent") if captcha else ("FAILED", "no confirmation page after submit")
    out.shots.append(_shot(page, shots, f"{job.id}-after"))
    return out
