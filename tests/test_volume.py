import json
from types import SimpleNamespace

from conftest import make_job
from jobagent import volume


def test_ats_urls_and_adapter_pause():
    assert volume.ats_from_url("https://job-boards.greenhouse.io/acme/jobs/123") == ("greenhouse", "acme", "123")
    assert volume.ats_from_url("https://jobs.lever.co/acme/108f3a42-7115-4bab-b15c-1088c3c3cd0d/apply")[0] == "lever"
    assert volume.ats_from_url("https://remoteok.com/remote-jobs/x") is None
    assert not volume.paused({"ashby": (4, 2, 2)}, "ashby")       # too few to judge
    assert volume.paused({"ashby": (3, 0, 3)}, "ashby")           # 3 misses in a row
    assert volume.paused({"ashby": (5, 3, 0)}, "ashby")           # 60% < 80%
    assert not volume.paused({"ashby": (5, 4, 0)}, "ashby")
    assert volume.halted([True] * 7 + [False] * 3) and not volume.halted([True] * 8 + [False] * 2)


def test_jev_score_minus_penalties_and_cached_drafts(profile, monkeypatch):
    out = json.dumps({"answers": {"answer": {"score": 3.2}}, "usage": {"cost": 0.00001}})
    monkeypatch.setattr(volume.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=out, stderr=""))
    good, flagged = make_job(), make_job(description="Commission only. Google Ads.")
    s = volume.score_all([good, flagged], profile, good.posted)
    assert s[0].total == 80 and s[0].parts["jev_fit"] == 80
    assert s[1].total == 65                                       # a red flag still costs 15
    calls = []
    llm = volume.cached({"u1": "draft"}, lambda sys, u: calls.append(u) or "fresh")
    assert llm("s", "u1") == "draft" and llm("s", "u1") == "fresh" and calls == ["u1"]   # a retry goes to the live writer
    big = make_job(description="x" * 20000)
    assert len(f"{volume.SYSTEM}\n\n{volume.prompt(volume.fits_brief(big, profile), profile)}".encode()) <= 15800


def test_greenhouse_code_is_read_from_the_newest_matching_email(tmp_path, monkeypatch):
    from jobagent import apply as ap
    msg = {"messageTimestamp": "2026-10-14T00:55:30Z", "subject": "Security code for your application to Walker Sands",
           "payload": "application:</p>\r\n<h1>Ab3dE6gH</h1>"}
    f = tmp_path / "out.json"
    f.write_text(json.dumps({"data": {"messages": [msg]}}))
    monkeypatch.setattr(ap.shutil, "which", lambda _: "/bin/composio")
    monkeypatch.setattr(ap.time, "sleep", lambda _: None)
    monkeypatch.setattr(ap.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps({"storedInFile": True, "outputFilePath": str(f)})))
    since = ap.datetime.fromisoformat("2026-10-14T00:55:00+00:00").timestamp()
    assert ap.email_code("Walker Sands", since, wait=10) == "Ab3dE6gH" and not f.exists()
    f.write_text(json.dumps({"data": {"messages": [msg]}}))
    assert ap.email_code("Found", since, wait=10) is None          # another company's code is never used


def test_residency_state_and_current_employer_come_from_the_profile(profile):
    from jobagent.forms import ANSWER_RULES, answers
    import re
    key = lambda q: next((k for rx, k in ANSWER_RULES if re.search(rx, q, re.I)), None)
    assert key("Are you located in the United States?") == "us_resident"
    assert key("Which U.S. state do you live in?") == "state" and key("State/Province of residence.") == "state"
    assert key("Applicant Privacy Policy") == "privacy_ack" and key("I consent to receive SMS messages") == "consent"
    assert key("Who is your most recent/current employer?") == "current_employer"
    assert answers(profile)["current_employer"] == [profile["experience"][0]["employer"]]
