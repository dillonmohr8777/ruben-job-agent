"""python -m jobagent <command>. Safe by default: `run` plans only; --fill opens a browser but never submits; --live submits."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import apply as ap
from . import claimcheck, ledger, llm, tracker
from .filters import canon_url  # noqa: F401
from .forms import FormPlan, Fill
from .models import Job
from .pipeline import Paths, run

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> dict:
    env = dict(os.environ)
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.split("#")[0].strip())
    return env


def _yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text())


def main(argv: list[str] | None = None) -> int:
    ap_ = argparse.ArgumentParser(prog="jobagent")
    ap_.add_argument("--home", default=str(ROOT), help="where data/ (ledger, tracker) and out/ live; default: this repo")
    sub = ap_.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="find, score, tailor, check, and decide. Plans only unless --fill or --live.")
    r.add_argument("--profile", default="profile.yaml")
    r.add_argument("--sources", default="sources.yaml")
    r.add_argument("--policy", default="policy.yaml")
    r.add_argument("--fill", action="store_true", help="open a browser and fill forms for auto-eligible jobs, screenshot, never submit")
    r.add_argument("--live", action="store_true", help="submit auto-eligible applications (browserbase unless --browser local)")
    r.add_argument("--browser", choices=["local", "browserbase"])
    r.add_argument("--limit", type=int, default=25)
    r.add_argument("--only", help="comma-separated job ids: redo just these (a model call per job is the slow, paid part)")
    a = sub.add_parser("approve", help="submit one job from the review queue after you read its packet")
    a.add_argument("job_id")
    a.add_argument("--browser", choices=["local", "browserbase"], default="browserbase")
    a.add_argument("--dry", action="store_true", help="fill and screenshot only")
    a.add_argument("--allow-empty", action="append", default=[], metavar="LABEL",
                   help="a required-looking question you have decided to leave blank (repeatable), e.g. an SMS consent")
    f = sub.add_parser("fill", help="open one queued job's real form in a local browser, fill it, screenshot it, and stop. Never submits.")
    f.add_argument("job_id")
    f.add_argument("--browser", choices=["local", "browserbase"], default="local")
    t = sub.add_parser("track", help="update a tracked job: track <id> <status> [--name --email --follow-up --note]")
    t.add_argument("job_id")
    t.add_argument("status", choices=tracker.STATUSES)
    t.add_argument("--name", dest="recruiter_name")
    t.add_argument("--email", dest="recruiter_email")
    t.add_argument("--follow-up", dest="follow_up_on")
    t.add_argument("--note", dest="notes")
    sub.add_parser("digest", help="today's follow-ups, replies, and items waiting for you")
    sub.add_parser("metrics", help="interviews per 10 applications, by score band")
    lg = sub.add_parser("login", help="sign in to a site from logins.yaml using your password manager; the session persists in the context")
    lg.add_argument("site")
    sub.add_parser("browser-context", help="one-time: create the persistent Browserbase context for logins")
    v = sub.add_parser("volume", help="volume mode: Jev scoring, batched Haiku drafts, parallel browsers. Submits unless --dry.")
    v.add_argument("--profile", default="profile.yaml")
    v.add_argument("--policy", default="policy.volume.yaml")
    v.add_argument("--sources", help="a sources.yaml to fetch live")
    v.add_argument("--jobs-csv", action="append", default=[], help="a search's jobs.csv (repeatable)")
    v.add_argument("--packets", nargs="*", default=[], help="submit these already-prepared packet .json files instead of searching")
    v.add_argument("--browsers", type=int, default=10, help="parallel sessions; the plan allows 25, leave the rest for other work")
    v.add_argument("--gap", type=float, default=5.0, help="seconds between application starts on the same ATS")
    v.add_argument("--until", default="01:25", help="local HH:MM after which nothing new starts")
    v.add_argument("--browser", choices=["local", "browserbase"], default="browserbase")
    v.add_argument("--dry", action="store_true", help="fill and screenshot, never submit")
    v.add_argument("--max", type=int, help="submit at most this many in this run (a first batch), still under the daily cap")
    v.add_argument("--no-model", action="store_true", help="verbatim profile bullets only")
    args = ap_.parse_args(argv)

    env, paths = load_env(), Paths(Path(args.home))
    if args.cmd == "run":
        prof = ROOT / args.profile
        if not prof.exists():
            print(f"{args.profile} not found. Copy profile.example.yaml to profile.yaml and fill it in.", file=sys.stderr)
            return 2
        profile = _yaml(prof)
        for w in claimcheck.profile_warnings(profile):
            print("profile warning:", w, file=sys.stderr)
        mode = "live" if args.live else "fill" if args.fill else "plan"
        browser = args.browser or ("browserbase" if args.live else "local")
        rep = run(profile, _yaml(ROOT / args.sources), _yaml(ROOT / args.policy), paths, mode=mode, browser=browser, limit=args.limit,
                  llm=llm.from_env(env), judge=llm.from_env(env, 'JUDGE_MODEL') if env.get('JUDGE_MODEL') else None,
                  only=set(args.only.split(',')) if args.only else None, env=env)
        print((paths.out / "report.md").read_text())
        return 0
    if args.cmd == "approve":
        return approve(args, paths, env)
    if args.cmd == "fill":
        return fill(args, paths, env)
    if args.cmd == "track":
        kw = {k: v for k, v in vars(args).items() if k in ("recruiter_name", "recruiter_email", "follow_up_on", "notes") and v}
        print(tracker.set_status(paths.tracker, args.job_id, args.status, **kw))
        return 0
    if args.cmd == "digest":
        text = tracker.digest(tracker.load(paths.tracker))
        (paths.root / "out").mkdir(exist_ok=True)
        (paths.root / "out" / f"digest-{date.today()}.md").write_text(text)
        print(text)
        return 0
    if args.cmd == "metrics":
        print(json.dumps(tracker.metrics(tracker.load(paths.tracker)), indent=1))
        return 0
    if args.cmd == "login":
        from . import vault
        site = _yaml(ROOT / "logins.yaml")[args.site]
        creds = vault.get_login(env.get("VAULT", "bitwarden"), site["vault_ref"])
        with ap.open_page("browserbase", env) as page:
            ap.ensure_login(page, site, creds)
        print(f"signed in to {args.site}; the session is saved in your Browserbase context")
        return 0
    if args.cmd == "volume":
        return volume_cmd(args, paths, env)
    if args.cmd == "browser-context":
        print("BROWSERBASE_CONTEXT_ID=" + ap.create_context(env))
        return 0
    return 1


def latest_packet(paths: Paths, job_id: str) -> Path | None:
    found = sorted((paths.root / "out").glob(f"*/packets/{job_id}.json"))
    return found[-1] if found else None


def fill(args, paths: Paths, env: dict) -> int:
    """Dry run for any packet from any run: Greenhouse, Lever or Ashby. Reads out/<day>/packets/<id>.json, so it works for
    jobs the gate sent to `ask` too. It cannot submit: apply_job is called with submit=False and nothing is recorded."""
    pk = latest_packet(paths, args.job_id)
    if not pk:
        print("no packet with that id under out/*/packets; run `run` first", file=sys.stderr)
        return 2
    d = json.loads(pk.read_text())
    job = Job(**{**d["job"], "posted": None})
    plan = FormPlan(fills=[Fill(**f) for f in d["plan"]["fills"]], blockers=d["plan"]["blockers"])
    profile = _yaml(ROOT / "profile.yaml")
    with ap.open_page(args.browser, env) as page:
        res = ap.apply_job(page, job, profile, plan, d["packet"]["cover_note"], ROOT / profile.get("resume_file", "resume.pdf"),
                           pk.parent.parent / "shots", submit=False)
    print(json.dumps({"job": f"{job.title} at {job.company}", "status": res.status, "screenshots": res.shots, "required_fields_still_empty": res.unfilled}, indent=1))
    return 0


def approve(args, paths: Paths, env: dict) -> int:
    row = next((x for x in tracker.load(paths.tracker) if x["job_id"] == args.job_id), None)
    if not row or not row["packet"] or not Path(row["packet"]).exists():
        print("no saved packet for that id; run `digest` to see ids waiting for you", file=sys.stderr)
        return 2
    d = json.loads(Path(row["packet"]).read_text())
    j = d["job"]
    job = Job(**{**j, "posted": None})
    plan = FormPlan(fills=[Fill(**f) for f in d["plan"]["fills"]], blockers=d["plan"]["blockers"])
    profile = _yaml(ROOT / "profile.yaml")
    with ap.open_page(args.browser, env) as page:
        res = ap.apply_job(page, job, profile, plan, d["packet"]["cover_note"], ROOT / profile.get("resume_file", "resume.pdf"),
                           paths.out / "shots", submit=not args.dry, allow_empty=tuple(args.allow_empty))
    ledger.append(paths.ledger, {"action": "apply", "id": job.id, "company": job.company, "title": job.title, "url": job.url, "ats": job.ats,
                                 "status": res.status, "decision": "human-approved", "note": res.note, "shots": res.shots})
    if res.status == "VERIFIED":
        tracker.mark_applied(paths.tracker, job.id, 7, shots=";".join(res.shots))
    print(res.status, res.note)
    return 0 if res.status in ("VERIFIED", "DRY_RUN") else 1


def _until(hhmm: str, now: datetime | None = None) -> datetime | None:
    """Next local HH:MM, or None inside the 01:30-09:30 quiet window (nothing may start then)."""
    now = now or datetime.now().astimezone()
    if 90 <= now.hour * 60 + now.minute < 570:
        return None
    h, m = map(int, hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def volume_cmd(args, paths: Paths, env: dict) -> int:
    from types import SimpleNamespace

    from . import pipeline as pl
    from . import sources, volume
    from .policy import Decision
    from .scoring import Score
    deadline = _until(args.until)
    if deadline is None:
        print("quiet hours (01:30-09:30): nothing starts now", file=sys.stderr)
        return 2
    profile, pol = _yaml(ROOT / args.profile), _yaml(ROOT / args.policy)
    if args.packets:  # already scored, tailored and claim-checked: just submit them, same browsers, same proof
        items = []
        for p in args.packets:
            d = json.loads(Path(p).read_text())
            items.append(pl.Item(Job(**{**d["job"], "posted": None}), Score(d["score"]["total"], d["score"]["parts"]),
                                 SimpleNamespace(cover_note=d["packet"]["cover_note"]),
                                 FormPlan(fills=[Fill(**f) for f in d["plan"]["fills"]], blockers=d["plan"]["blockers"]),
                                 Decision("auto", "prepared packet")))
        print(volume.apply_many(items, profile, ROOT / profile.get("resume_file", "resume.pdf"), paths, env, browsers=args.browsers,
                          gap=args.gap, deadline=deadline, submit=not args.dry, browser=args.browser))
        for it in items:
            print(it.result.status, it.job.company, "|", it.job.title, "|", it.result.note[:160])
        return 0
    jobs, errors = sources.fetch_all(_yaml(ROOT / args.sources)) if args.sources else ([], [])
    for c in args.jobs_csv:
        jobs += volume.from_csv(Path(c))
    rep, bench = volume.run(jobs, profile, pol, paths, env, browsers=args.browsers, gap=args.gap, deadline=deadline, submit=not args.dry,
                            model=not args.no_model, judge=llm.from_env(env, "JUDGE_MODEL") if env.get("JUDGE_MODEL") else None,
                            browser=args.browser, errors=errors, max_submits=args.max)
    (paths.out / "bench.json").write_text(json.dumps(bench, indent=1))
    print((paths.out / "report.md").read_text())
    print(json.dumps(bench, indent=1))
    return 0
