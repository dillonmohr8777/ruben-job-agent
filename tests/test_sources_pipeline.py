from conftest import NOW
from jobagent import pipeline, sources

GH_LIST = {"jobs": [
    {"id": 11, "title": "Growth Marketing Manager", "company_name": "Acme", "absolute_url": "https://boards.greenhouse.io/acme/jobs/11",
     "location": {"name": "Remote, United States"}, "first_published": "2026-10-12T09:00:00-04:00",
     "content": "&lt;p&gt;We use Google Ads, SEO and HubSpot. $120,000 - $150,000&lt;/p&gt;"},
    {"id": 12, "title": "Staff Data Engineer", "company_name": "Acme", "absolute_url": "https://boards.greenhouse.io/acme/jobs/12",
     "location": {"name": "Remote, United States"}, "first_published": "2026-10-12T09:00:00-04:00", "content": ""}]}
GH_Q = {"compliance": [], "demographic_questions": [], "questions": [
    {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
    {"label": "Last Name", "required": True, "fields": [{"name": "last_name", "type": "input_text"}]},
    {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text"}]},
    {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file"}]}]}
LEVER = [{"id": "abc", "text": "Lifecycle Marketing Manager", "hostedUrl": "https://jobs.lever.co/zed/abc", "createdAt": 1791800000000,
          "categories": {"location": "Remote - US", "commitment": "Full-time"}, "workplaceType": "remote", "descriptionPlain": "HubSpot and Klaviyo.",
          "salaryRange": {"min": 130000, "max": 160000}}]
ASHBY = {"jobs": [{"id": "z1", "title": " SEO Manager", "jobUrl": "https://jobs.ashbyhq.com/zz/z1", "publishedAt": "2026-10-12T10:00:00.000+00:00",
                   "isListed": True, "workplaceType": "Remote", "location": "Remote", "descriptionPlain": "SEO", "compensation": {"compensationTierSummary": "$100K - $130K"}}]}
RSS = """<rss><channel><item><title>Beta Inc: Paid Media Manager</title><link>https://wwr.example/1</link><pubDate>Mon, 12 Oct 2026 10:00:00 +0000</pubDate>
<description>&lt;p&gt;Google Ads&lt;/p&gt;</description><region>Anywhere</region></item></channel></rss>"""
HN_SEARCH = {"hits": [{"title": "Ask HN: Who is hiring? (October 2026)", "objectID": "1"}]}
HN_ITEM = {"children": [{"id": 5, "created_at": "2026-10-12T10:00:00Z", "text": "Gamma | Growth Marketing Manager | REMOTE (US) | $100k-$120k<p>We need SEO."},
                        {"id": 6, "created_at": "2026-10-12T10:00:00Z", "text": "free-form post with no pipes"}]}


def fake_get(url, data=None, text=False, timeout=30):
    if "boards-api.greenhouse.io" in url and "questions=true" in url:
        return GH_Q
    if "boards-api.greenhouse.io/v1/boards/dead" in url:
        raise OSError("404")
    if "boards-api.greenhouse.io" in url:
        return GH_LIST
    if "api.lever.co" in url:
        return LEVER
    if "ashbyhq" in url:
        return ASHBY
    if url.endswith(".rss"):
        return RSS
    if "search_by_date" in url:
        return HN_SEARCH
    if "hn.algolia.com/api/v1/items" in url:
        return HN_ITEM
    raise AssertionError(url)


CFG = {"greenhouse": ["acme", "dead"], "lever": ["zed"], "ashby": ["zz"], "rss": ["https://x/feed.rss"], "hn": True}


def test_every_source_parses_and_dead_board_is_named():
    jobs, errors = sources.fetch_all(CFG, fake_get)
    assert {j.ats for j in jobs} == {"greenhouse", "lever", "ashby", "rss", "hn"}
    assert len(errors) == 1 and "greenhouse:dead" in errors[0]
    gh = next(j for j in jobs if j.ats == "greenhouse" and j.ats_id == "11")
    assert "Google Ads" in gh.description and "<" not in gh.description and gh.posted.year == 2026   # double-escaped HTML cleaned
    assert next(j for j in jobs if j.ats == "rss").company == "Beta Inc"
    assert [j.company for j in jobs if j.ats == "hn"] == ["Gamma"]                                    # free-form post skipped, not guessed


def test_pipeline_end_to_end_offline(profile, pol, tmp_path):
    rep = pipeline.run(profile, CFG, pol, pipeline.Paths(tmp_path, "2026-10-13"), mode="plan", get=fake_get, now=NOW)
    by = {(i.job.ats, i.job.title): i.decision.verdict for i in rep.items}
    assert not any("Engineer" in t for _, t in by) and sum(rep.dropped.values()) >= 1   # off-target job never reaches the shortlist
    assert by[("greenhouse", "Growth Marketing Manager")] == "auto"   # strong score, form fully answerable, claims clean
    assert by[("hn", "Growth Marketing Manager")] == "ask"            # same role posted on HN: not an auto-apply form
    assert all(v in ("auto", "ask") for v in by.values())
    out = tmp_path / "out" / "2026-10-13"
    assert (out / "report.md").exists() and len(list((out / "packets").glob("*.json"))) == len(rep.items)
    assert not (tmp_path / "data").exists()                     # a plan run can never record an application or touch the tracker


def test_live_mode_records_ledger_and_tracker_and_never_repeats(profile, pol, tmp_path, monkeypatch):
    from contextlib import contextmanager
    import json
    from jobagent import apply as ap, tracker

    @contextmanager
    def fake_open(mode, env):
        yield object()

    monkeypatch.setattr(ap, "open_page", fake_open)
    submitted = []

    def fake_apply(page, job, profile, plan, note, resume, shots, submit=False):
        submitted.append((job.title, submit))
        return ap.ApplyResult("VERIFIED", [str(shots / "after.png")])

    monkeypatch.setattr(ap, "apply_job", fake_apply)
    paths = pipeline.Paths(tmp_path, "2026-10-13")
    pipeline.run(profile, CFG, pol, paths, mode="live", get=fake_get, now=NOW)
    assert submitted == [("Growth Marketing Manager", True)]                       # only the auto-eligible Greenhouse job was submitted
    rows = {r["job_id"]: r for r in tracker.load(paths.tracker)}
    done = [r for r in rows.values() if r["status"] == "applied"]
    assert len(done) == 1 and done[0]["follow_up_on"] and done[0]["stage_max"] == "applied"
    assert any(r["status"] == "review" for r in rows.values())                      # HN posting waits for the human
    led = [json.loads(x) for x in paths.ledger.read_text().splitlines()]
    assert [r["status"] for r in led].count("VERIFIED") == 1
    pipeline.run(profile, CFG, pol, paths, mode="live", get=fake_get, now=NOW)       # same day, same jobs
    assert len(submitted) == 1                                                       # already in the tracker: never applied twice


def test_workable_pages_are_all_read():
    pages = {None: {"results": [{"title": "SEO Manager", "shortcode": "A1", "published": "2026-10-12"}], "nextPage": "p2", "total": 2},
             "p2": {"results": [{"title": "Paid Media Manager", "shortcode": "B2", "published": "2026-10-12"}], "total": 2}}
    jobs = sources.workable("acme", lambda url, data=None, **kw: pages[(data or {}).get("token")])
    assert [j.ats_id for j in jobs] == ["A1", "B2"]   # before the fix only the first page (10 jobs) was ever seen


def test_names_map_replaces_url_slug_company():
    jobs, _ = sources.fetch_all({"lever": ["zed"], "names": {"zed": "Zed Corp"}}, fake_get)
    assert jobs[0].company == "Zed Corp"        # Lever's API only knows the slug; the cover note and exclude list need the real name


def test_rate_limited_board_is_retried_before_it_is_called_dead(monkeypatch):
    import io, urllib.error
    calls = []

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_open(req, timeout=0):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "0"}, None)
        return R(b'{"ok": 1}')

    monkeypatch.setattr(sources.urllib.request, "urlopen", fake_open)
    assert sources.http_get("https://x.example/api") == {"ok": 1} and len(calls) == 3


def test_half_title_matches_are_dropped_before_scoring(profile, pol, tmp_path):
    pm = {"jobs": [{**GH_LIST["jobs"][0], "id": 99, "title": "Senior Product Manager, Growth", "absolute_url": "https://boards.greenhouse.io/acme/jobs/99"}]}
    get = lambda url, data=None, text=False, timeout=30: pm if ("boards-api" in url and "questions" not in url) else fake_get(url, data, text, timeout)
    rep = pipeline.run(profile, {"greenhouse": ["acme"]}, pol, pipeline.Paths(tmp_path, "2026-10-13"), mode="plan", get=get, now=NOW)
    assert rep.items == [] and rep.dropped["title is not close to a target role"] == 1


def test_only_redoes_the_named_jobs(profile, pol, tmp_path):
    every = pipeline.run(profile, CFG, pol, pipeline.Paths(tmp_path / "a", "2026-10-13"), mode="plan", get=fake_get, now=NOW)
    one = every.items[0].job.id
    rep = pipeline.run(profile, CFG, pol, pipeline.Paths(tmp_path / "b", "2026-10-13"), mode="plan", get=fake_get, only={one}, now=NOW)
    assert [i.job.id for i in rep.items] == [one] and len(every.items) > 1
