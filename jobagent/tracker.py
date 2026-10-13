"""Follow-up tracker: one CSV (opens in Excel or Google Sheets), a daily digest, and the metric that matters."""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

COLS = ["job_id", "company", "title", "url", "ats", "score", "status", "stage_max", "applied_on", "recruiter_name",
        "recruiter_email", "follow_up_on", "last_update", "notes", "packet", "screenshots"]
STAGES = ["review", "applied", "replied", "screen", "interview", "offer"]  # furthest point reached; rejected/ghosted keep stage_max
TERMINAL = {"rejected", "ghosted", "skipped", "withdrawn"}
STATUSES = STAGES + sorted(TERMINAL)


def load(path: Path) -> list[dict]:
    p = Path(path)
    return list(csv.DictReader(p.open())) if p.exists() else []


def save(path: Path, rows: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def upsert(path: Path, row: dict, today: date | None = None) -> dict:
    today = today or date.today()
    rows = load(path)
    cur = next((r for r in rows if r["job_id"] == row["job_id"]), None)
    if cur is None:
        cur = {c: "" for c in COLS}
        rows.append(cur)
    cur.update({k: str(v) for k, v in row.items() if k in COLS})
    cur["last_update"] = today.isoformat()
    top = cur["stage_max"] or "review"
    cur["stage_max"] = max(top, cur["status"], key=STAGES.index) if cur["status"] in STAGES else top
    save(path, rows)
    return cur


def mark_applied(path: Path, job_id: str, follow_up_days: int, today: date | None = None, shots: str = "") -> dict:
    today = today or date.today()
    return upsert(path, {"job_id": job_id, "status": "applied", "applied_on": today.isoformat(),
                         "follow_up_on": (today + timedelta(days=follow_up_days)).isoformat(), "screenshots": shots}, today)


def set_status(path: Path, job_id: str, status: str, **fields) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if not any(r["job_id"] == job_id for r in load(path)):
        raise KeyError(job_id)
    return upsert(path, {"job_id": job_id, "status": status, **fields})


def metrics(rows: list[dict]) -> dict:
    applied = [r for r in rows if r["applied_on"]]
    reach = lambda r, s: STAGES.index(r["stage_max"] or "review") >= STAGES.index(s)  # noqa: E731
    n = len(applied)
    out = {"applied": n, "replies": sum(reach(r, "replied") for r in applied), "interviews": sum(reach(r, "interview") for r in applied),
           "offers": sum(reach(r, "offer") for r in applied)}
    out["interviews_per_10_applications"] = round(10 * out["interviews"] / n, 2) if n else None
    bands: dict[str, list] = {}
    for r in applied:
        sc = int(float(r["score"] or 0))
        bands.setdefault(">=85" if sc >= 85 else "75-84" if sc >= 75 else "<75", []).append(r)
    out["by_score_band"] = {b: {"applied": len(v), "interviews": sum(reach(x, "interview") for x in v),
                                "per_10": round(10 * sum(reach(x, "interview") for x in v) / len(v), 2)} for b, v in sorted(bands.items())}
    return out


def digest(rows: list[dict], today: date | None = None) -> str:
    today = today or date.today()
    iso = today.isoformat()
    due = [r for r in rows if r["status"] == "applied" and r["follow_up_on"] and r["follow_up_on"] <= iso]
    review = [r for r in rows if r["status"] == "review"]
    replied = [r for r in rows if r["status"] in ("replied", "screen")]
    yday = [r for r in rows if r["applied_on"] == (today - timedelta(days=1)).isoformat()]
    m = metrics(rows)
    line = lambda r: f"  - {r['company']} | {r['title']} | score {r['score']} | {r['url']}"  # noqa: E731
    parts = [f"Job search digest for {iso}", "",
             f"Replies to handle ({len(replied)})", *map(line, replied), "",
             f"Follow-ups due ({len(due)})", *[f"{line(r)} | contact {r['recruiter_name'] or 'unknown'} {r['recruiter_email']}" for r in due], "",
             f"Waiting for your OK ({len(review)})", *[f"{line(r)} | id {r['job_id']} | {r['notes']}" for r in review], "",
             f"Applied yesterday ({len(yday)})", *map(line, yday), "",
             f"Scoreboard: {m['applied']} applied, {m['replies']} replies, {m['interviews']} interviews, {m['offers']} offers. "
             f"Interviews per 10 applications: {m['interviews_per_10_applications']}"]
    return "\n".join(parts)
