# ruben-job-agent

A job-search agent built on one idea: **fewer, better applications, and prove every one.**

Mass-apply tools send 200 applications and you find out later what went wrong. This does the opposite:
it reads public job boards, throws away everything that is not a real fit, writes your material using only
facts you gave it, has a separate checker catch anything invented, applies on its own only when the form
is simple and everything checks out, and hands you the rest with a ready packet. Every submitted
application gets a screenshot and a ledger line. The number it tracks is **interviews per 10 applications**,
not how many it sent.

Nothing in this repo is personal. Your details live in `profile.yaml`, which is git-ignored.

## What it does, in order

1. **Finds jobs** from the sources that have clean data: Greenhouse, Lever, Ashby and Workable public job
   boards, RSS feeds, and the monthly "Ask HN: Who is hiring?" thread. No browser needed for this.
2. **Drops bad fits** (hard filters): older than 7 days, not remote or the wrong country, pay below your
   floor, wrong seniority, visa or clearance problems, banned words or companies. Every drop is counted by reason.
3. **Removes duplicates**, including the same job posted on two boards.
4. **Scores fit 0 to 100** against your profile. No model involved, so the same job always scores the same and
   every point can be explained (title match, skills, level, location, pay, freshness).
5. **Writes tailored bullets and a short cover note** from your own profile only.
6. **Checks the writing with a separate judge** (`claimcheck`): every number, tool name, employer and credential
   must exist in your profile. Anything else is flagged and the job is never auto-applied.
7. **Reads the application form before opening a browser** and works out what it can truthfully answer.
8. **Applies through a policy gate** (below). Dropdown and text answers come from your profile.
9. **Tracks everything** in a CSV, prints a daily digest, and reports your real conversion numbers.

## Setup in 15 minutes

```bash
cd ruben-job-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium        # only needed for the browser steps
cp profile.example.yaml profile.yaml          # then edit it: this is the important step
cp .env.example .env                          # only needed for --live and model tailoring
```

1. **Fill in `profile.yaml`.** Real employers, real bullets, real numbers. The agent can only say what is written
   here, so a thin profile means thin applications. Tag each bullet with the skills it proves. Put your resume PDF
   next to it and set `resume_file`.
2. **Pick your boards in `sources.yaml`.** Take the slug from a company's jobs URL
   (`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`). Twenty to fifty smaller
   companies in your field beats the famous ten.
3. **Try it without risk:** `python -m jobagent run`. It plans only: no browser, no applications, nothing recorded.
   Read `out/<today>/report.md` and the packets in `out/<today>/packets/`.
4. **Watch a form get filled, still without submitting:** `python -m jobagent run --fill`. A local browser fills
   every eligible form and takes a screenshot. It never presses Submit.
5. **Go live when you trust it:** add Browserbase keys to `.env` (see costs), then `python -m jobagent run --live`.

## The gate: when it applies by itself, and when it asks you

It applies on its own only if **all** of these are true (settings in `policy.yaml`):

- the form is Greenhouse or Lever (simple, standard forms; everything else is "ask")
- fit score is at least 80
- the claim check found nothing outside your profile
- every required question can be answered from `profile.answers`
- you are under today's cap (default 8) and have not applied to that company in 90 days
- there is no `STOP` file in the repo (create one to halt everything instantly)

Everything else becomes a packet in your review list. After reading it: `python -m jobagent approve <id>`.

**Legal and EEO questions:** gender, race, veteran, disability and similar are never filled, whatever your profile says.
Consents (privacy policies and so on) are only ticked if you wrote an answer for `privacy_ack`. A required
question it cannot answer sends the job to you instead of guessing.

**"Submitted" means proven.** A run is recorded as `VERIFIED` only when the employer's confirmation page appears.
No confirmation means `FAILED`, with a screenshot, and it still counts against the daily cap.

## Logins and persistent sessions

Browserbase keeps a persistent browser context, so a site you sign in to once stays signed in.
Credentials come from your password manager at the moment they are needed (Bitwarden `bw` or 1Password `op`),
live only in memory, and are never printed, logged or written to a file. Copy `logins.example.yaml` to
`logins.yaml`, then `python -m jobagent login <site>`. Greenhouse and Lever need no login at all.
Google and LinkedIn block cloud browsers; keep those on your own computer.

## Every day

```bash
python -m jobagent run --live      # find, filter, apply to the few that qualify, queue the rest
python -m jobagent digest          # replies to handle, follow-ups due, items waiting for your OK
python -m jobagent track <id> interview --name "Pat Recruiter" --email pat@co.com --follow-up 2026-11-02
python -m jobagent metrics         # interviews per 10 applications, split by score band
```

Statuses: `review, applied, replied, screen, interview, offer, rejected, ghosted, skipped, withdrawn`.
The tracker is `data/applications.csv` (opens in Excel; import it into Google Sheets if you want a shared view).
Follow-ups default to 7 days after applying. The ledger is `data/ledger.jsonl`, append-only, one line per attempt.

**What the metric tells you:** `metrics` splits interviews per 10 applications by score band. If 85+ jobs convert
and the 70s do not, raise `auto_min_score`. If nothing converts, the problem is the profile or the target
titles, not the volume. Change those before sending more.

## What it costs

- **Fetching, filtering, scoring, form-reading:** free. Public APIs, no account.
- **Browserbase** (cloud browser with persistent logins and captcha solving): paid monthly plan, around $20 a month
  at the time of writing. Check browserbase.com/pricing. You only need it for `--live` and `login`; `run` and
  `run --fill` use free local Chromium.
- **Model API** (optional, for rewording): without a key the agent reuses your bullets word for word, which is
  free and cannot invent anything. With a key (`LLM_API_KEY`, any OpenAI-compatible API such as OpenRouter) a
  tailored application costs roughly a cent or two (an estimate; it depends on the model). A reworded draft that
  fails the claim check is rejected and the word-for-word version is used instead.
- **Bitwarden:** free tier works. 1Password is supported too.

## Honest limits

- **Indeed and LinkedIn are not automated.** They need logins and captchas and restrict automation. Use the
  review list for those.
- Only Greenhouse and Lever are auto-submitted. Ashby, Workable, HN and RSS jobs become packets for you.
- The claim check is deliberately strict. It checks numbers, capitalised names and tools, and credential words.
  It cannot judge whether a true fact is framed well; read the cover note before you approve anything.
- Location and visa rules are keyword rules, not legal advice. Pay is parsed in USD only; jobs that state no pay
  are kept unless you set `require_salary: true`.
- Greenhouse's city autocomplete and employer-specific custom questions are the usual reasons a form falls back to
  "ask". That is by design: an unfilled required field is never guessed.
- Browser submission was proven only up to the Submit button (see test notes): fills, uploads and the empty-field
  check ran against real public forms, but no real application has been sent by this code. Do your first live
  run with a low cap (`daily_cap: 1`) and watch the screenshot.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Covers scoring, filters, dedupe, salary parsing, the claim check, the apply policy, form planning, the tracker
metric, the password vault wrapper, every source parser, an offline end-to-end run, and the empty-field check in a
real browser.

## Files

`jobagent/sources.py` boards and feeds | `filters.py` hard filters and dedupe | `scoring.py` fit score |
`tailor.py` bullets and note | `claimcheck.py` the outside judge | `forms.py` form planning |
`policy.py` the gate | `apply.py` browser step | `ledger.py`, `tracker.py` records |
`vault.py` password manager | `pipeline.py`, `cli.py` glue
