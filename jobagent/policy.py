"""The gate between 'a good job' and 'the agent presses submit'. auto / ask / skip, always with a reason.

Auto needs EVERY condition. Anything else becomes `ask` (you approve it with `approve <id>`) or `skip`.
There is no setting that lets a draft with an unverified claim, a legal question it cannot answer from
your profile, or a score under the bar go out on its own."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import ledger
from .filters import company_key
from .forms import FormPlan
from .models import Job
from .scoring import Score
from .tailor import Packet


@dataclass
class Decision:
    verdict: str  # auto | ask | skip | deny
    reason: str


def decide(job: Job, sc: Score, packet: Packet, plan: FormPlan, cfg: dict, ledger_path: Path,
           stop_file: Path | None = None, now: datetime | None = None) -> Decision:
    now = now or datetime.now().astimezone()
    if stop_file and Path(stop_file).exists():
        return Decision("deny", "STOP file present")
    if sc.total < cfg["ask_min_score"]:
        return Decision("skip", f"score {sc.total} < {cfg['ask_min_score']}")
    if job.ats not in cfg["auto_ats"]:
        return Decision("ask", f"{job.ats} forms are not auto-applied; review and apply by hand")
    if sc.total < cfg["auto_min_score"]:
        return Decision("ask", f"score {sc.total} < auto bar {cfg['auto_min_score']}")
    if not packet.claims.ok:
        return Decision("ask", f"claim check failed: {packet.claims}")
    if plan.blockers:
        return Decision("ask", f"needs your answer: {'; '.join(plan.blockers[:3])}")
    used = ledger.attempts_today(ledger_path)
    if used >= cfg["daily_cap"]:
        return Decision("ask", f"daily cap reached ({used}/{cfg['daily_cap']})")
    last = ledger.last_attempt_for(ledger_path, company_key(job.company))
    if last and now - last < timedelta(days=cfg["company_cooldown_days"]):
        return Decision("ask", f"already applied to {job.company} on {last:%Y-%m-%d}")
    return Decision("auto", f"score {sc.total}, claims verified, no open questions, {used + 1}/{cfg['daily_cap']} today")
