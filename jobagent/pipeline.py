"""fetch -> hard filters -> dedupe -> score -> tailor -> claim check -> form plan -> policy -> (fill | apply) -> record."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import apply as ap
from . import filters, forms, ledger, sources, tracker
from .llm import LLM
from .models import Job
from .policy import Decision, decide
from .scoring import Score, score, title_match
from .tailor import Packet, tailor


PROJECT = Path(__file__).resolve().parent.parent


@dataclass
class Paths:
    root: Path
    day: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))

    @property
    def ledger(self): return self.root / "data" / "ledger.jsonl"
    @property
    def tracker(self): return self.root / "data" / "applications.csv"
    @property
    def out(self): return self.root / "out" / self.day
    @property
    def stop(self): return self.root / "STOP"


@dataclass
class Item:
    job: Job
    score: Score
    packet: Packet
    plan: forms.FormPlan
    decision: Decision
    result: ap.ApplyResult | None = None


@dataclass
class Report:
    fetched: int = 0
    errors: list[str] = field(default_factory=list)
    dropped: Counter = field(default_factory=Counter)
    deduped: int = 0
    scored: int = 0
    items: list[Item] = field(default_factory=list)


def _jobs_to_enrich(jobs: list[Job], profile: dict, now: datetime) -> list[Job]:
    targets = profile["search"].get("target_titles", [])
    days = profile["search"].get("max_age_days", 7)
    return [j for j in jobs if j.extra.get("detail_url") and j.posted and (now - j.posted).days <= days and title_match(j.title, targets) >= 0.5]


def run(profile: dict, src_cfg: dict, pol: dict, paths: Paths, *, mode: str = "plan", browser: str = "local", limit: int = 25,
        get=sources.http_get, llm: LLM | None = None, judge: LLM | None = None, only: set[str] | None = None, now: datetime | None = None, env: dict | None = None) -> Report:
    """mode: plan (no browser, no tracker) | fill (browser fills forms, never submits) | live (submits auto-approved jobs)."""
    now, env, rep = now or datetime.now(timezone.utc), env or {}, Report()
    jobs, rep.errors = sources.fetch_all(src_cfg, get)
    rep.fetched = len(jobs)

    for j in _jobs_to_enrich(jobs, profile, now):
        try:
            sources.enrich(j, get)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"enrich {j.ats}:{j.board}/{j.ats_id} failed: {type(e).__name__}")

    survivors = []
    s = profile["search"]
    for j in jobs:
        if reason := filters.hard_filter(j, profile, now):
            rep.dropped[reason.split(" at ")[0] if reason.startswith("pay tops") else reason] += 1
        elif title_match(j.title, s.get("target_titles", [])) < s.get("min_title_match", 0.6):
            # "Operations Manager" is half of "Marketing Operations Manager"; without this gate Product Managers and Data Scientists scored in
            rep.dropped["title is not close to a target role"] += 1
        else:
            survivors.append(j)

    known = {r["job_id"] for r in tracker.load(paths.tracker)} | {filters.canon_url(r["url"]) for r in tracker.load(paths.tracker)}
    survivors, rep.deduped = filters.dedupe(survivors, known)

    scored = sorted(((score(j, profile, now), j) for j in survivors), key=lambda x: -x[0].total)
    rep.scored = len(scored)
    shortlist = [(s, j) for s, j in scored if s.total >= pol["ask_min_score"] and (not only or j.id in only)][:limit]

    resume = PROJECT / profile.get("resume_file", "resume.pdf")  # relative to the repo; an absolute path also works
    page_cm = None
    try:
        if mode in ("fill", "live") and any(j.ats in pol["auto_ats"] for _, j in shortlist):
            page_cm = ap.open_page(browser, env)
            page = page_cm.__enter__()
        for sc, j in shortlist:
            packet = tailor(j, profile, sc, llm, judge)
            plan = _form_plan(j, profile, get, rep)
            dec = decide(j, sc, packet, plan, pol, paths.ledger, paths.stop)
            item = Item(j, sc, packet, plan, dec)
            rep.items.append(item)
            _write_packet(paths, item)
            if dec.verdict == "auto" and mode in ("fill", "live"):
                item.result = _apply(page, item, profile, resume, paths, submit=(mode == "live"))
            _record(paths, item, mode)
    finally:
        if page_cm:
            page_cm.__exit__(None, None, None)
    _write_report(paths, rep, mode)
    return rep


def _form_plan(j: Job, profile: dict, get, rep: Report) -> forms.FormPlan:
    if j.ats == "greenhouse":
        try:
            return forms.plan_greenhouse(forms.fetch_greenhouse_questions(j.board, j.ats_id, get), profile)
        except Exception as e:  # noqa: BLE001 - unknown form means we cannot promise it is answerable
            rep.errors.append(f"form questions for {j.company}/{j.title} failed: {type(e).__name__}")
            p = forms.plan_basic(profile)
            p.blockers.append("could not read the form's questions")
            return p
    if j.ats == "workable":
        try:
            return forms.plan_workable(forms.fetch_workable_form(j.ats_id, get), profile)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"workable form for {j.company}/{j.title} failed: {type(e).__name__}")
            p = forms.plan_basic(profile)
            p.blockers.append("could not read the form's questions")
            return p
    return forms.plan_basic(profile)


def _apply(page, item: Item, profile: dict, resume: Path, paths: Paths, submit: bool) -> ap.ApplyResult:
    try:
        return ap.apply_job(page, item.job, profile, item.plan, item.packet.cover_note, resume, paths.out / "shots", submit)
    except Exception as e:  # noqa: BLE001
        return ap.ApplyResult("FAILED", note=f"{type(e).__name__}: {str(e)[:200]}")


def _record(paths: Paths, it: Item, mode: str) -> None:
    if mode == "plan":
        return  # a plan run writes reports and packets under out/ only: no ledger, no tracker
    j, r = it.job, it.result
    status = r.status if r else {"auto": "DRY_RUN", "ask": "QUEUED", "skip": "SKIPPED", "deny": "DENIED"}[it.decision.verdict]
    ledger.append(paths.ledger, {"action": "apply", "id": j.id, "company": j.company, "title": j.title, "url": j.url, "ats": j.ats,
                                 "score": it.score.total, "status": status, "decision": it.decision.verdict, "reason": it.decision.reason,
                                 "mode": mode, "note": r.note if r else "", "shots": r.shots if r else []})
    if mode == "live" and it.decision.verdict in ("auto", "ask"):
        row = {"job_id": j.id, "company": j.company, "title": j.title, "url": j.url, "ats": j.ats, "score": it.score.total,
               "packet": str(paths.out / "packets" / f"{j.id}.json"), "notes": it.decision.reason}
        if r and r.status == "VERIFIED":
            tracker.upsert(paths.tracker, {**row, "status": "review"})
            tracker.mark_applied(paths.tracker, j.id, 7, shots=";".join(r.shots))
        else:
            tracker.upsert(paths.tracker, {**row, "status": "review", "notes": (r.note if r and r.note else it.decision.reason)})


def _write_packet(paths: Paths, it: Item) -> None:
    d = paths.out / "packets"
    d.mkdir(parents=True, exist_ok=True)
    j, s, p = it.job, it.score, it.packet
    (d / f"{j.id}.json").write_text(json.dumps({"job": asdict(j), "score": asdict(s), "packet": {**asdict(p), "claims": str(p.claims)},
                                                "plan": asdict(it.plan), "decision": asdict(it.decision)}, default=str, indent=1))
    (d / f"{j.id}.md").write_text(
        f"# {j.title} at {j.company}\n\n{j.url}\n\nScore {s.total}: {s.parts}\nSkills matched: {', '.join(s.skills_hit) or 'none'}\n"
        f"Decision: **{it.decision.verdict}**, {it.decision.reason}\nWriter: {p.mode}. Claim check: {p.claims}\n\n## Resume bullets\n"
        + "\n".join(f"- {b}" for b in p.bullets) + f"\n\n## Cover note\n{p.cover_note}\n\n## Form\n"
        f"Will fill: {', '.join(f.label[:40] for f in it.plan.fills) or 'n/a'}\nBlockers: {'; '.join(it.plan.blockers) or 'none'}\n"
        f"Left blank: {len(it.plan.left_blank)}. EEO questions left untouched: {it.plan.eeo_skipped}\n")


def _write_report(paths: Paths, rep: Report, mode: str) -> None:
    paths.out.mkdir(parents=True, exist_ok=True)
    c = Counter(i.decision.verdict for i in rep.items)
    lines = [f"# Run {paths.day} (mode: {mode})", "",
             f"Fetched {rep.fetched} | dropped by hard filters {sum(rep.dropped.values())} | duplicates {rep.deduped} | scored {rep.scored} | "
             f"shortlisted {len(rep.items)} -> auto {c['auto']}, ask {c['ask']}, skip {c['skip']}, deny {c['deny']}", "",
             "## Why jobs were dropped", *[f"- {n} x {r}" for r, n in rep.dropped.most_common(12)], "",
             "## Source errors (a dead board is not an empty board)", *([f"- {e}" for e in rep.errors] or ["- none"]), "",
             "## Shortlist", "", "| score | company | role | writer | claims | decision | why |", "|---|---|---|---|---|---|---|"]
    for i in rep.items:
        lines.append(f"| {i.score.total} | {i.job.company} | [{i.job.title}]({i.job.url}) | {i.packet.mode} | {'ok' if i.packet.claims.ok else 'FLAGGED'} | "
                     f"{i.decision.verdict}{' / ' + i.result.status if i.result else ''} | {i.decision.reason}{' / ' + i.result.note if i.result and i.result.note else ''} |")
    (paths.out / "report.md").write_text("\n".join(lines) + "\n")
