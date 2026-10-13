from datetime import timedelta

import pytest

from conftest import NOW, make_job
from jobagent import filters, scoring


@pytest.mark.parametrize("text,expect", [
    ("Salary: $120,000 - $150,000 a year", (120000, 150000)),
    ("$120k-$150k plus equity", (120000, 150000)),
    ("$62.50 - $80 per hour", (130000, 166400)),
    ("Compensation $211.4K – $290.6K", (211400, 290600)),
    ("We raised $5 million - $10 million", (None, None)),
    ("competitive pay", (None, None)),
])
def test_parse_salary(text, expect):
    assert filters.parse_salary(text) == expect


def test_seniority_levels():
    assert filters.seniority("Senior Paid Media Manager") == (3, True)
    assert filters.seniority("Marketing Associate")[0] == 1
    assert filters.seniority("Head of Growth")[0] == 5
    assert filters.seniority("SEO Specialist") == (2, False)


@pytest.mark.parametrize("change,reason", [
    ({}, None),
    ({"posted": NOW - timedelta(days=8)}, "older than 7 days"),
    ({"posted": None}, "no posted date"),
    ({"title": "Director of Growth"}, "title has 'director'"),
    ({"title": "Marketing Intern"}, "title has 'intern'"),
    ({"remote": False, "location": "Austin, TX"}, "not remote"),
    ({"location": "Remote, Germany"}, "remote only in germany"),
    ({"location": "Remote, Austria"}, "remote only in austria"),
    ({"description": "Pay $50,000 - $60,000."}, "pay tops out at 60000 < 80000"),
    ({"company": "Evil Corp"}, None),
])
def test_hard_filter(profile, change, reason):
    profile["search"]["exclude_companies"] = ["Evil Corp"] if change.get("company") else []
    got = filters.hard_filter(make_job(**change), profile, NOW)
    assert got == ("excluded company" if change.get("company") else reason)


def test_visa_and_clearance(profile):
    j = make_job(description="We are unable to provide visa sponsorship. $100,000 - $120,000")
    assert filters.hard_filter(j, profile, NOW) is None  # candidate does not need sponsorship
    profile["search"]["visa"]["needs_sponsorship"] = True
    assert filters.hard_filter(j, profile, NOW) == "no visa sponsorship"
    c = make_job(description="Active TS/SCI clearance required. $100,000 - $120,000")
    assert filters.hard_filter(c, profile, NOW) == "citizenship or clearance required"


def test_dedupe_across_sources_prefers_ats():
    gh = make_job(company="GitLab", url="https://x.com/jobs?gh_jid=1", ats_id="1")
    hn = make_job(ats="hn", company="Gitlab Inc", title="Growth Marketing Manager (Remote)", url="https://news.ycombinator.com/item?id=9", ats_id="9")
    other = make_job(company="GitLab", title="Staff Data Engineer", url="https://x.com/jobs?gh_jid=2", ats_id="2")
    kept, dropped = filters.dedupe([hn, other, gh])
    assert dropped == 1 and {j.ats for j in kept} == {"greenhouse"} and len(kept) == 2


def test_dedupe_keeps_distinct_ids_on_same_page_and_skips_known():
    a = make_job(url="https://stripe.com/jobs/search?gh_jid=1&utm_source=x", title="Role A", ats_id="1")
    b = make_job(url="https://stripe.com/jobs/search?gh_jid=2", title="Totally Different", ats_id="2")
    assert len(filters.dedupe([a, b])[0]) == 2
    assert filters.dedupe([a, b], known={a.id})[1] == 1


def test_title_match_ignores_generic_words(profile):
    t = profile["search"]["target_titles"]
    assert scoring.title_match("Product Manager", t) == 0
    assert scoring.title_match("Senior Growth Marketing Manager", t) == 1.0


def test_score_ranks_and_explains(profile):
    good = scoring.score(make_job(), profile, NOW)
    weak = scoring.score(make_job(title="Office Manager", description="Answer phones.", remote=None, location="Remote"), profile, NOW)
    assert good.total > weak.total and good.total >= 80
    assert {"Google Ads", "Meta Ads", "SEO", "HubSpot"} <= set(good.skills_hit)
    assert set(good.parts) == set(scoring.WEIGHTS)


def test_red_flag_penalty(profile):
    clean = scoring.score(make_job(), profile, NOW).total
    flagged = scoring.score(make_job(description=make_job().description + " Commission only."), profile, NOW)
    assert flagged.total == clean - 15 and "red flags" in flagged.notes[-1]
