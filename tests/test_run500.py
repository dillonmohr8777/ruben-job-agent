from datetime import datetime

from jobagent import forms, tailor
from jobagent.models import Job

PROFILE = {"name": "Dillon Mohr", "summary": "Hands-on marketing leader.", "contact": {"first_name": "Dillon", "email": "d@example.com", "phone": "1", "location": "Pittsburgh, PA"},
           "experience": [{"employer": "Momentum 360", "title": "Director", "bullets": [{"text": "Own strategy across Google Ads, Meta, SEO.", "skills": ["Google Ads"]},
                                                                                     {"text": "Built a reporting console.", "skills": []}]}],
           "answers": {"work_authorized": ["Yes"], "requires_sponsorship": ["No"]}, "skills": []}


def test_clean_title_drops_pay_hours_and_requisition():
    assert tailor.clean_title("Associate Marketing Writer Admin - To 25/hr - Part-Time - REMOTE - Job 3303") == "Associate Marketing Writer Admin"
    assert tailor.clean_title("Senior Copywriter, DTC (Freelance)") == "Senior Copywriter, DTC (Freelance)"


def test_template_note_is_deterministic_and_claim_checked():
    p = tailor.template_packet(Job("csv", "Acme", "Paid Media Manager", "u"), PROFILE)
    assert p.mode == "template:paid" and p.claims.ok and "Acme" in p.cover_note and "—" not in p.cover_note
    assert p.cover_note == tailor.template_packet(Job("csv", "Acme", "Paid Media Manager", "u"), PROFILE).cover_note


def test_workable_plan_answers_known_questions_and_blocks_the_rest():
    form = [{"fields": [{"id": "firstname", "label": "First name", "required": True, "type": "text"},
                        {"id": "email", "label": "Email", "required": True, "type": "email"},
                        {"id": "QA_1", "label": "Are you legally authorized to work in the US?", "required": True, "type": "boolean"},
                        {"id": "QA_2", "label": "What is your expected hourly rate?", "required": True, "type": "paragraph"},
                        {"id": "QA_3", "label": "Do you consent to marketing texts?", "required": True, "type": "boolean"}]}]
    plan = forms.plan_workable(form, PROFILE)
    assert {f.name: f.value for f in plan.fills} == {"firstname": "Dillon", "email": "d@example.com", "QA_1": "Yes"}
    assert len(plan.blockers) == 2 and any("hourly rate" in b for b in plan.blockers) and any("consent" in b for b in plan.blockers)
