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
