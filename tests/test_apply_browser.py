"""Real Chromium against a local page: proves the 'required fields still empty' judge and that dry runs never submit."""
import json

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


def test_ashby_verdict_needs_the_real_success_type_not_just_a_200():
    ok = {"data": {"submitApplicationFormAction": {"applicationFormResult": {"__typename": "FormSubmitSuccess"}}}}
    assert ap.ashby_verdict(200, ok) == (True, "")
    assert not ap.ashby_verdict(500, ok)[0]
    assert not ap.ashby_verdict(200, {"errors": [{"message": "boom"}]})[0]
    assert not ap.ashby_verdict(200, {"data": {"submitApplicationFormAction": {"applicationFormResult": {"__typename": "FormRender", "errorMessages": [{"message": "bad"}]}}}})[0]
    spam = {"data": {"submitApplicationFormAction": {"applicationFormResult": {"__typename": "FormRender"}, "messages": {"blockMessageForCandidateHtml": "flagged as spam"}}}}
    assert "spam" in ap.ashby_verdict(200, spam)[1]
    assert not ap.ashby_verdict(200, {})[0]


SUBMIT_URL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction"
APP_URL = "https://jobs.ashbyhq.com/x/1/application"
# Decoys: the page says "thank you for applying" before anything is sent, and another type=submit button comes first.
ASHBY_PAGE = """<body>Thank you for applying (this sentence is always on the page)
<form onsubmit="event.preventDefault();fetch('%s',{method:'POST',body:'{}'}).then(r=>r.json()).then(j=>{document.title='SENT'})">
<button type=submit onclick="document.title='WRONG BUTTON'">Upload file</button>
<div><label for=_systemfield_name>Name</label><input type=text id=_systemfield_name required></div>
<div><label for=_systemfield_email>Email</label><input type=email id=_systemfield_email required></div>
<div><label for=_systemfield_resume>Resume</label><input type=file id=_systemfield_resume required></div>
%s<button type=submit>Submit Application</button></form></body>"""


def _ashby_run(tmp_path, profile, reply, extra="", **kw):
    """Real apply_job on a local Ashby-shaped page; the employer's reply is scripted. Returns (result, requests seen)."""
    from jobagent.forms import plan_basic
    from jobagent.models import Job
    seen = []
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    try:
        with ap.open_page("local", {}) as page:
            page.route("**/*", lambda r: r.abort())   # registered first = lowest priority: nothing leaves the machine
            page.route(APP_URL, lambda r: r.fulfill(status=200, content_type="text/html", body=ASHBY_PAGE % (SUBMIT_URL, extra)))
            def submit(r):
                seen.append(r.request.post_data)
                r.fulfill(status=200, content_type="application/json", headers={"access-control-allow-origin": "*"}, body=json.dumps(reply))
            page.route(SUBMIT_URL, submit)
            job = Job("ashby", "X", "t", APP_URL, board="x", ats_id="1")
            return ap.apply_job(page, job, profile, plan_basic(profile), "note", resume, tmp_path / "shots", **kw), seen
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e):
            pytest.skip("run `playwright install chromium`")
        raise


OK = {"data": {"submitApplicationFormAction": {"applicationFormResult": {"__typename": "FormSubmitSuccess"}}}}
REJECTED = {"data": {"submitApplicationFormAction": {"applicationFormResult": {"__typename": "FormRender", "errorMessages": []}}}}


def test_ashby_submit_is_verified_only_by_the_success_reply(tmp_path, profile):
    res, seen = _ashby_run(tmp_path, profile, OK, submit=True)
    assert res.status == "VERIFIED" and len(seen) == 1          # one submit request, from the right button
    res, seen = _ashby_run(tmp_path, profile, REJECTED, submit=True)
    assert res.status == "FAILED" and "did not accept" in res.note   # the 'thank you' text on the page does not count


def test_ashby_unanswered_required_question_blocks_submit_until_a_human_allows_it(tmp_path, profile):
    extra = '<div class="_fieldEntry_a"><label class="_required_a" for=sms>SMS consent</label><input id=sms type=text></div>'
    res, seen = _ashby_run(tmp_path, profile, OK, extra, submit=True)
    assert res.status == "DRY_RUN" and "SMS consent" in res.note and not seen    # nothing was sent
    res, seen = _ashby_run(tmp_path, profile, OK, extra, submit=True, allow_empty=("sms consent",))
    assert res.status == "VERIFIED" and len(seen) == 1


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


def test_ashby_captcha_challenge_is_reported_as_such_and_nothing_is_sent(tmp_path, profile):
    global ASHBY_PAGE
    saved = ASHBY_PAGE
    ASHBY_PAGE = saved.replace("</form>", "</form><iframe title='recaptcha challenge' style='width:300px;height:400px'></iframe>").replace(
        "fetch('%s'", "void('%s'")
    try:
        res, seen = _ashby_run(tmp_path, profile, OK, submit=True)
    finally:
        ASHBY_PAGE = saved
    assert res.status == "FAILED" and "captcha" in res.note and not seen
