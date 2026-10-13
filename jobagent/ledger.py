"""Append-only JSONL ledger. One line per attempt; lines are never edited or deleted."""

from __future__ import annotations

import fcntl
import json
from datetime import datetime
from pathlib import Path

ATTEMPTED = ("VERIFIED", "FAILED")  # statuses that count against the daily cap (a failed try still touched the employer)


def append(path: Path, rec: dict) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), **rec}
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(rec, default=str) + "\n")
    return rec


def read(path: Path) -> list[dict]:
    p = Path(path)
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def attempts_today(path: Path, today: str | None = None) -> int:
    today = today or datetime.now().astimezone().strftime("%Y-%m-%d")
    return sum(1 for r in read(path) if r.get("action") == "apply" and r.get("status") in ATTEMPTED and r["ts"].startswith(today))


def last_attempt_for(path: Path, company_key: str) -> datetime | None:
    from .filters import company_key as ck
    ts = [datetime.fromisoformat(r["ts"]) for r in read(path)
          if r.get("action") == "apply" and r.get("status") in ATTEMPTED and ck(r.get("company", "")) == company_key]
    return max(ts) if ts else None
