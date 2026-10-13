from datetime import date

import pytest

from jobagent import forms, tracker, vault

GH = {"compliance": [{"type": "eeoc", "questions": [{"label": "Race", "fields": [{"name": "race", "type": "multi_value_single_select"}]}]}],
      "demographic_questions": [],
      "questions": [
          {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
          {"label": "Last Name", "required": True, "fields": [{"name": "last_name", "type": "input_text"}]},
          {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text"}]},
          {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file"}]},
          {"label": "Will you now or in the future require sponsorship?", "required": True,
           "fields": [{"name": "question_1", "type": "multi_value_single_select", "values": [{"label": "Yes", "value": "1"}, {"label": "No", "value": "0"}]}]},
          {"label": "What is your favourite Pokemon?", "required": True, "fields": [{"name": "question_2", "type": "input_text"}]},
          {"label": "Gender identity", "required": True, "fields": [{"name": "question_3", "type": "input_text"}]},
          {"label": "Anything else?", "required": False, "fields": [{"name": "question_4", "type": "textarea"}]},
      ]}


def test_greenhouse_plan(profile):
    plan = forms.plan_greenhouse(GH, profile)
    by = {f.name: f for f in plan.fills}
    assert by["first_name"].value == "Alex" and by["question_1"].value == "No" and by["question_1"].kind == "select"
    assert any("Pokemon" in b for b in plan.blockers)                       # unknown required question blocks auto-apply
    assert not any("Gender" in f.label for f in plan.fills)                 # EEO never filled, even though required here
    assert plan.eeo_skipped == 2 and plan.left_blank == ["Anything else?"]


def test_pick_option_prefers_exact_then_prefix_then_unique_substring():
    opts = ["Yes, I am authorized", "No", "Not sure"]
    assert forms.pick_option(["No"], opts) == "No"
    assert forms.pick_option(["Yes"], opts) == "Yes, I am authorized"
    assert forms.pick_option(["authorized"], opts) == "Yes, I am authorized"
    assert forms.pick_option(["N"], ["Nope", "Never"]) == "Nope"                  # prefix: first listed
    assert forms.pick_option(["e"], ["Nope", "Never", "Other"]) is None            # ambiguous substring is not a match
    assert forms.pick_option(["Maybe", "No"], opts) == "No"                        # falls through the list in order


def test_unmatched_select_option_blocks_rather_than_guesses(profile):
    profile["answers"]["requires_sponsorship"] = "Maybe"
    assert forms.plan_greenhouse(GH, profile).blockers[0].startswith("'Will you now")


def test_tracker_metrics_and_followups(tmp_path):
    f = tmp_path / "a.csv"
    for i in range(10):
        tracker.upsert(f, {"job_id": f"j{i}", "company": f"C{i}", "title": "T", "url": "u", "score": 85 if i < 4 else 70, "status": "review"}, date(2026, 10, 1))
        tracker.mark_applied(f, f"j{i}", 7, date(2026, 10, 1))
    tracker.set_status(f, "j0", "interview")
    tracker.set_status(f, "j0", "rejected")                  # furthest stage must survive a later rejection
    tracker.set_status(f, "j1", "replied")
    rows = tracker.load(f)
    m = tracker.metrics(rows)
    assert m["applied"] == 10 and m["interviews"] == 1 and m["interviews_per_10_applications"] == 1.0
    assert m["by_score_band"][">=85"]["interviews"] == 1 and m["by_score_band"]["<75"]["interviews"] == 0
    d = tracker.digest(rows, date(2026, 10, 9))
    assert "Follow-ups due (8)" in d and "Replies to handle (1)" in d and "Interviews per 10 applications: 1.0" in d
    with pytest.raises(ValueError):
        tracker.set_status(f, "j0", "bogus")


def test_vault_never_leaks_password():
    run = lambda cmd: '{"login": {"username": "user1", "password": "hunter2"}}'  # noqa: E731
    lg = vault.get_login("bitwarden", "item-id", run)
    assert lg.username == "user1" and "hunter2" not in repr(lg) and "hunter2" not in str(lg)
    assert vault.scrub("typed hunter2 for user1", lg) == "typed <redacted> for <redacted>"
    with pytest.raises(vault.VaultError):
        vault.get_login("bitwarden", "x", lambda c: '{"login": {}}')
