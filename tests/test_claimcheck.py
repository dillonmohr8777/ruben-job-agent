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
