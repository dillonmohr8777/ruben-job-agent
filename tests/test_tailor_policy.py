import json
from datetime import timedelta

from conftest import NOW, make_job
from jobagent import ledger, scoring, tailor
from jobagent.forms import FormPlan
from jobagent.policy import decide


def _packet(profile, llm=None, job=None):
    job = job or make_job()
    sc = scoring.score(job, profile, NOW)
    return job, sc, tailor.tailor(job, profile, sc, llm)


def test_extractive_uses_only_profile_text_and_passes_check(profile):
    _, _, p = _packet(profile)
    assert p.mode == "extractive" and p.claims.ok and len(p.bullets) == 5
    allbul = [b["text"] for e in profile["experience"] for b in e["bullets"]]
    assert all(b in allbul for b in p.bullets)
    assert "Acme" in p.cover_note and profile["name"] in p.cover_note


def test_model_draft_used_when_clean(profile):
    good = json.dumps({"bullets": ["Managed $85,000 a month in Google Ads and Meta Ads for B2B SaaS."] * 5,
                       "cover_note": "Hi Acme team, I run paid media. Managed $85,000 a month in Google Ads. Alex Sample"})
    _, _, p = _packet(profile, llm=lambda s, u: good)
    assert p.mode == "model" and p.claims.ok


def test_model_inventing_claims_is_rejected_and_falls_back(profile):
    calls = []
    bad = json.dumps({"bullets": ["Grew pipeline 300% using Marketo."] * 5, "cover_note": "Hi Acme team, I am a certified expert. Alex Sample"})

    def llm(s, u):
        calls.append(u)
        return bad

    _, _, p = _packet(profile, llm=llm)
    assert p.mode == "model-rejected-fell-back" and p.claims.ok and len(calls) == 2
    assert "Marketo" not in "\n".join(p.bullets) and "claims not in PROFILE" in calls[1]   # second try was told what to remove


def test_unparseable_model_reply_falls_back(profile):
    _, _, p = _packet(profile, llm=lambda s, u: "sure! here you go")
    assert p.mode == "model-rejected-fell-back" and p.claims.ok


# --- policy ---------------------------------------------------------------------


def _decide(profile, pol, tmp_path, job=None, plan=None, packet=None, stop=None, **ov):
    job, sc, p = _packet(profile, job=job)
    pol = {**pol, **ov}
    return decide(job, sc, packet or p, plan or FormPlan(), pol, tmp_path / "l.jsonl", stop, NOW)


def test_auto_when_everything_holds(profile, pol, tmp_path):
    d = _decide(profile, pol, tmp_path)
    assert d.verdict == "auto", d.reason


def test_each_gate_downgrades(profile, pol, tmp_path):
    assert _decide(profile, pol, tmp_path, job=make_job(ats="ashby")).verdict == "ask"
    assert _decide(profile, pol, tmp_path, plan=FormPlan(blockers=["required: x"])).verdict == "ask"
    assert _decide(profile, pol, tmp_path, auto_min_score=99).verdict == "ask"
    assert _decide(profile, pol, tmp_path, ask_min_score=99).verdict == "skip"
    _, sc, p = _packet(profile)
    p.claims.ok, p.claims.flagged = False, [("Marketo", "name or term not in profile")]
    assert _decide(profile, pol, tmp_path, packet=p).verdict == "ask"


def test_stop_file_denies(profile, pol, tmp_path):
    stop = tmp_path / "STOP"
    stop.touch()
    assert _decide(profile, pol, tmp_path, stop=stop).verdict == "deny"


def test_daily_cap_counts_attempts_not_dry_runs(profile, pol, tmp_path):
    led = tmp_path / "l.jsonl"
    for i in range(3):
        ledger.append(led, {"action": "apply", "status": "DRY_RUN", "company": f"C{i}"})
    assert _decide(profile, pol, tmp_path, daily_cap=2).verdict == "auto"
    for i, st in enumerate(["VERIFIED", "FAILED"]):               # a failed try still touched the employer, so it counts
        ledger.append(led, {"action": "apply", "status": st, "company": f"Other{i}"})
    d = _decide(profile, pol, tmp_path, daily_cap=2)
    assert d.verdict == "ask" and "daily cap" in d.reason


def test_company_cooldown(profile, pol, tmp_path):
    ledger.append(tmp_path / "l.jsonl", {"action": "apply", "status": "VERIFIED", "company": "ACME Inc"})
    d = _decide(profile, pol, tmp_path)
    # ledger timestamps are 'now' (real clock); decide() compares to the injected NOW, so a record from the real
    # clock is in the future of NOW. Cooldown must still trigger.
    assert d.verdict == "ask" and "already applied" in d.reason
