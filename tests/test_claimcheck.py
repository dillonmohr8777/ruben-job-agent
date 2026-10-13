import pytest

from jobagent import claimcheck

ALLOWED = "Acme Growth Marketing Manager"


@pytest.mark.parametrize("text", [
    "Managed $85,000 a month in Google Ads and Meta Ads spend for a B2B SaaS product, cutting cost per lead by 31% in two quarters.",
    "Managed $85k a month in Google Ads.",                       # 85k == 85,000
    "I'm applying to Acme for the Growth Marketing Manager role.",  # company and title come from the job, not a claim about you
    "Optimized campaigns at Northwind Software since 2022.",
])
def test_true_statements_pass(profile, text):
    assert claimcheck.check(text, profile, ALLOWED).ok, claimcheck.check(text, profile, ALLOWED)


@pytest.mark.parametrize("text,kind", [
    ("Cut cost per lead by 45%.", "number"),
    ("Managed $850,000 a month in Google Ads.", "number"),
    ("Lifted conversion 40x.", "number"),                            # 40 tests exist, 40x lift does not
    ("Ran paid campaigns with Marketo and Salesforce.", "name"),
    ("Led the team at Globex Corporation.", "name"),
    ("I am a Certified Google Ads professional.", "credential"),
    ("Earned an MBA from Wharton.", "credential"),
    ("Salesforce admin with 9 years experience.", "name"),
])
def test_invented_claims_are_flagged(profile, text, kind):
    rep = claimcheck.check(text, profile, ALLOWED)
    assert not rep.ok and any(kind in k for _, k in rep.flagged)


def test_facts_from_the_job_post_are_not_claims_about_you(profile):
    # The posting says 10 years and Salesforce; saying you have them must not pass just because the JD does.
    rep = claimcheck.check("I have 10 years of Salesforce experience.", profile, ALLOWED)
    assert not rep.ok


def test_profile_with_a_comment_swallowed_bullet_is_warned():
    import yaml
    from jobagent.claimcheck import profile_warnings
    bad = yaml.safe_load("experience:\n  - employer: Acme\n    bullets:\n      - text: Earned multiple #1 Google rankings.\n")   # YAML drops ' #1 Google rankings.'
    assert bad["experience"][0]["bullets"][0]["text"] == "Earned multiple"
    assert len(profile_warnings(bad)) == 1
    good = yaml.safe_load('experience:\n  - employer: Acme\n    bullets:\n      - text: "Earned multiple #1 Google rankings."\n')
    assert profile_warnings(good) == []


@pytest.mark.parametrize("text", [
    "I cut cost per lead by 31% in two quarters at Northwind Software.",
    "I managed 85,000 dollars a month in Google Ads. I wrote Klaviyo flows at Brightside Goods.",
    "At Northwind I ran 40 landing page A/B tests with the design team.",         # reworded, same numbers next to the same words
    "I am at alex.sample@example.com or 555-0100.",                            # contact digits are not claims
])
def test_context_checks_do_not_flag_true_rewordings(profile, text):
    assert claimcheck.check(text, profile, ALLOWED).ok, claimcheck.check(text, profile, ALLOWED)


@pytest.mark.parametrize("text", [
    "I ran 31 landing page tests.",                          # 31% and 40 tests both exist, not in this combination
    "At Brightside Goods I cut cost per lead by 31%.",       # right number, wrong employer
    "I doubled pipeline in a quarter.",                      # no digit, no capital letter
    "I built dashboards in tableau.",                        # lower-case tool
    "I launched twelve campaigns.",                          # spelled-out number
    "At Google I ran paid search.",                          # Google is a tool in the profile, not an employer
    "I was ranked 1st in the agency.",
])
def test_inventions_that_a_bag_of_facts_cannot_see(profile, text):
    assert not claimcheck.check(text, profile, ALLOWED).ok


def test_irregular_opening_verbs_months_and_compound_words_are_not_inventions(profile):
    assert claimcheck.check("Took over paid social in January.\nAI-enabled reporting cut cost per lead by 31%.", profile, ALLOWED + " AI").ok
    assert not claimcheck.check("Salesforce-enabled reporting.", profile, ALLOWED).ok          # but a tool inside a compound word is still checked


def test_more_than_is_not_the_same_as_plus(profile):
    assert not claimcheck.check("I managed more than 85,000 dollars a month in Google Ads.", profile, ALLOWED).ok
    assert claimcheck.check("Managed $85,000 a month in Google Ads.", profile, ALLOWED).ok


def test_second_judge_blocks_plain_prose_invention_but_only_warns_on_stretch(profile):
    import json
    from jobagent.claimcheck import judge_with_model
    reply = json.dumps({"claims": [{"claim": "I think in funnels", "support": None, "verdict": "unsupported"},
                                   {"claim": "I always QA", "support": "x", "verdict": "stretched"},
                                   {"claim": "ok", "support": "y", "verdict": "supported"}]})
    assert judge_with_model("note", profile, lambda s, u: reply) == (["I think in funnels"], ["I always QA"])


def test_a_wrong_number_is_not_rescued_by_a_neighbouring_clause(profile):
    # real draft: "ten AI sources" for "five named plus seven other"; "ten" exists elsewhere in the profile and the next clause shared words with that line
    from jobagent.claimcheck import number_in_context
    p = {"experience": [{"employer": "Acme", "bullets": [
        {"text": "Set up AI-referral attribution across ChatGPT, Claude and seven other AI sources in HubSpot."},
        {"text": "Added schema across ten service pages and built a 25-prompt scorecard."}]}]}
    bad = "I set up AI-referral attribution across ten AI sources in HubSpot and built a 25-prompt scorecard."
    assert [k for t, k in number_in_context(bad, p)] == ["number not tied to the profile line it came from"]
    assert number_in_context("I set up AI-referral attribution across seven other AI sources in HubSpot and built a 25-prompt scorecard.", p) == []
