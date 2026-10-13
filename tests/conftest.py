import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent.models import Job  # noqa: E402

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def profile():
    return yaml.safe_load((ROOT / "profile.example.yaml").read_text())


@pytest.fixture
def pol():
    return yaml.safe_load((ROOT / "policy.yaml").read_text())


def make_job(**kw) -> Job:
    base = dict(ats="greenhouse", company="Acme", title="Growth Marketing Manager", url="https://boards.greenhouse.io/acme/jobs/1",
                description="We use Google Ads, Meta Ads, SEO and HubSpot. Pay range $120,000 - $150,000.", location="Remote, United States",
                remote=True, posted=NOW - timedelta(days=1), board="acme", ats_id="1")
    base.update(kw)
    return Job(**base)


@pytest.fixture
def job():
    return make_job()
