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


def test_fallback_claim_check_sees_bullets_and_note_as_separate_lines(profile):
    for e in profile["experience"]:
        for b in e["bullets"]:
            b["text"] = b["text"].rstrip(".") + " and lowered CPA."          # a bullet ending in an acronym used to fuse with "Hi <company> team"
    _, _, p = _packet(profile, llm=lambda s, u: "sure! here you go")
    assert p.mode == "model-rejected-fell-back" and p.claims.ok, str(p.claims)


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


def test_company_name_from_a_slug_is_not_retitled(profile):
    from jobagent.tailor import _company
    from conftest import make_job
    assert _company(make_job(company="829studios")) == "829studios" and _company(make_job(company="wpromote")) == "Wpromote"
    assert _company(make_job(company="PostHog")) == "PostHog"


def test_empty_model_reply_gives_a_readable_error(monkeypatch):
    import io, json, pytest
    from jobagent import llm

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout=0: R(json.dumps({"choices": [{"message": {"content": None}, "finish_reason": "length"}]}).encode()))
    with pytest.raises(ValueError, match="no text.*LLM_MAX_TOKENS"):
        llm.from_env({"LLM_API_KEY": "x"})("sys", "user")


def test_model_draft_that_a_second_judge_rejects_falls_back(profile):
    import json
    good = json.dumps({"bullets": [profile["experience"][0]["bullets"][0]["text"]] * 5, "cover_note": "Hi Acme team, I run paid media. Alex Sample"})
    judge = lambda s, u: json.dumps({"claims": [{"claim": "I think in funnels", "support": None, "verdict": "unsupported"}]})
    job, sc, _ = _packet(profile)
    p = tailor.tailor(job, profile, sc, llm=lambda s, u: good, judge=judge)
    assert p.mode == "model-rejected-fell-back" and p.claims.ok
    ok_judge = lambda s, u: json.dumps({"claims": [{"claim": "x", "support": "y", "verdict": "stretched"}]})
    p2 = tailor.tailor(job, profile, sc, llm=lambda s, u: good, judge=ok_judge)
    assert p2.mode == "model-rejected-fell-back"          # a stretch is rejected too; only a clean verdict passes
    clean = lambda s, u: json.dumps({"claims": [{"claim": "x", "support": "y", "verdict": "supported"}]})
    assert tailor.tailor(job, profile, sc, llm=lambda s, u: good, judge=clean).mode == "model"
