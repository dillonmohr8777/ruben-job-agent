"""Real Chromium against a local page: proves the 'required fields still empty' judge and that dry runs never submit."""
import pytest

from jobagent import apply as ap

pw = pytest.importorskip("playwright.sync_api")

HTML = """<form onsubmit="document.title='SUBMITTED';return false">
<label for=first_name>First</label><input id=first_name required>
<label for=q1>Why us?</label><input id=q1 required>
<label for=gender>Gender</label><input id=gender required>
<input type=file id=resume required><button type=submit>Submit</button></form>"""


def test_unfilled_required_is_reported_and_nothing_submits(tmp_path):
    try:
        with ap.open_page("local", {}) as page:
            page.set_content(HTML)
            assert len(ap.unfilled_required(page)) == 4
            page.fill("#first_name", "Alex")
            page.set_input_files("#resume", __file__)
            left = ap.unfilled_required(page)
            assert any("Why us" in x for x in left) and any("Gender" in x for x in left) and not any("First" in x for x in left)
            assert page.title() != "SUBMITTED"
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e):
            pytest.skip("run `playwright install chromium`")
        raise


ASHBY_HTML = """<form onsubmit="document.title='SUBMITTED';return false">
<div><label for=_systemfield_name>Name</label><input type=text id=_systemfield_name name=_systemfield_name required></div>
<div><label for=_systemfield_email>Email</label><input type=email id=_systemfield_email name=_systemfield_email required></div>
<div><label for=_systemfield_resume>Resume</label><input type=file id=_systemfield_resume required></div>
<div><label for=q1>LinkedIn Profile</label><input type=text id=q1></div>
<div><label for=q2>Additional Information</label><textarea id=q2></textarea></div>
<div><label for=q3>Why do you want to work here?</label><textarea id=q3 required></textarea></div>
<button type=submit>Submit Application</button></form>"""


def test_ashby_fill_answers_known_fields_reports_the_rest_and_never_submits(tmp_path, profile):
    from jobagent.forms import plan_basic
    try:
        with ap.open_page("local", {}) as page:
            page.set_content(ASHBY_HTML)
            resume = tmp_path / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4 test")
            ap._fill_ashby(page, plan_basic(profile), profile, resume, "Cover note text")
            assert page.input_value("#_systemfield_name") == "Alex Sample" and page.input_value("#_systemfield_email") == profile["contact"]["email"]
            assert page.input_value("#q1") == profile["contact"]["linkedin"] and page.input_value("#q2") == "Cover note text"
            assert ap.unfilled_required(page) == ["Why do you want to work here?"]    # the free-text question is left for the human, not guessed
            assert page.title() != "SUBMITTED"
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e):
            pytest.skip("run `playwright install chromium`")
        raise


def test_ashby_submit_is_refused(tmp_path):
    from jobagent.models import Job
    with pytest.raises(ap.ApplyError, match="not verified"):
        ap.apply_job(None, Job("ashby", "X", "t", "u", board="x", ats_id="1"), {}, None, "", __file__, tmp_path, submit=True)


LEVER_RADIO_HTML = """<form>
<li class="application-question"><div class="application-label">Are you legally authorized to work in the United States? *</div>
<label><input type=radio name="q1" value="Yes" required> Yes</label><label><input type=radio name="q1" value="No"> No</label></li>
<li class="application-question"><div class="application-label">Will you now or in the future require sponsorship? *</div>
<label><input type=radio name="q2" value="Yes" required> Yes</label><label><input type=radio name="q2" value="No"> No</label></li>
<li class="application-question"><div class="application-label">Do you consent to SMS messages? *</div>
<label><input type=radio name="q3" value="Yes" required> Yes</label><label><input type=radio name="q3" value="No"> No</label></li>
<li class="application-question"><div class="application-label">Gender</div>
<label><input type=radio name="q4" value="Yes" required> Yes</label><label><input type=radio name="q4" value="No"> No</label></li></form>"""


def test_radio_questions_are_answered_from_the_profile_and_unanswered_groups_are_reported(profile):
    try:
        with ap.open_page("local", {}) as page:
            page.set_content(LEVER_RADIO_HTML)
            assert len(ap.unfilled_required(page)) == 4        # radios with a value used to look 'filled'
            ap._answer_radio_groups(page, profile)
            assert page.is_checked('input[name="q1"][value="Yes"]') and page.is_checked('input[name="q2"][value="No"]')   # authorized: Yes, sponsorship: No
            assert not page.is_checked('input[name="q3"][value="Yes"]') and not page.is_checked('input[name="q3"][value="No"]')   # consent never auto-answered
            assert not page.is_checked('input[name="q4"][value="Yes"]')                                                          # EEO never touched
            assert ap.unfilled_required(page) == ["Do you consent to SMS messages? *", "Gender"]     # what is left is named for the human
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e):
            pytest.skip("run `playwright install chromium`")
        raise
