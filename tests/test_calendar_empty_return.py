"""The calendar's empty-month "Add a chore" link returns to that month (#46)."""
import datetime
import html
import re

import pytest
from django.test import Client

from chores.models import Chore, Member
from tests.test_calendar import WED, at
from tests.test_calendar_return import BACK_CAL, CREATE, FORM, PICKER, form_hidden, nexts
from tests.test_chores import payload

pytestmark = pytest.mark.django_db

LINK = re.compile(r'<section id="no-chores">.*?</section>', re.S)
CURRENT_LINK = 'href="/chores/create/?return_to=calendar">Add a chore</a>'
MARCH = "/chores/create/?return_to=calendar&month=2027-03"


@pytest.fixture
def client():
    return Client()


@pytest.fixture(autouse=True)
def mom():
    return Member.objects.create(name="Mom", role="parent", avatar="M", order=1)


def get(client, url, **extra):
    with at(WED):
        response = client.get(url, **extra)
    assert response.status_code == 200
    return response.content.decode()


def empty_section(text):
    return LINK.search(text).group(0)


def test_current_month_link(client):
    assert f"<a {CURRENT_LINK}" in empty_section(get(client, "/calendar/"))


def test_current_month_written_out(client):
    section = empty_section(get(client, "/calendar/?month=2026-11"))
    assert CURRENT_LINK in section and "month=" not in section


@pytest.mark.parametrize("month", ["2027-03", "2026-10"])
def test_other_and_past_months(client, month):
    section = empty_section(get(client, f"/calendar/?month={month}"))
    assert f'<a href="/chores/create/?return_to=calendar&amp;month={month}">Add a chore</a>' in section


def test_htmx_partial(client):
    text = get(client, "/calendar/?month=2027-03", HTTP_HX_REQUEST="true")
    assert 'href="/chores/create/?return_to=calendar&amp;month=2027-03">Add a chore</a>' in text


@pytest.mark.parametrize("bad", ["<script>alert(1)</script>", "//evil.example", "2026-13"])
def test_hostile_month_copies_nothing(client, bad):
    with at(WED):
        response = client.get("/calendar/", {"month": bad})
    text = response.content.decode()
    assert CURRENT_LINK in empty_section(text)
    assert "evil.example" not in text and "<script>alert" not in text and "2026-13" not in text


def test_non_empty_month_has_no_empty_section(client):
    Chore.objects.create(title="Dishes", recurrence="daily", points=5,
                         start_date=datetime.date(2026, 11, 18))
    assert "no-chores" not in get(client, "/calendar/")


def test_create_page_from_empty_month_link(client):
    text = get(client, MARCH)
    assert 'value="2026-11-18"' in text
    assert BACK_CAL.findall(text) == ["/calendar/?month=2027-03"]
    assert form_hidden(text) == {"return_to": "calendar", "month": "2027-03"}
    assert "date=" not in nexts(text)
    assert nexts(text) == MARCH
    for plain, hx in PICKER.findall(text):
        assert "date=" not in html.unescape(plain) + html.unescape(hx)


def test_save_redirects_to_month_with_message(client):
    with at(WED):
        response = client.post(CREATE, {**payload(), "return_to": "calendar", "month": "2027-03"})
    assert response.status_code == 302 and response["Location"] == "/calendar/?month=2027-03"
    with at(WED):
        text = client.get(response["Location"]).content.decode()
        again = client.get(response["Location"]).content.decode()
    assert "Chore saved" in text and "Chore saved" not in again


def test_save_from_current_month_redirects_to_calendar(client):
    with at(WED):
        response = client.post(CREATE, {**payload(), "return_to": "calendar"})
    assert response.status_code == 302 and response["Location"] == "/calendar/"


def test_no_js_flow_without_htmx_headers(client):
    link = html.unescape(re.search(r'<a href="([^"]*)">Add a chore', empty_section(get(client, "/calendar/?month=2027-03"))).group(1))
    page = get(client, link)
    assert FORM.search(page)
    with at(WED):
        response = client.post(CREATE, {**payload(), **form_hidden(page)})
    assert response["Location"] == "/calendar/?month=2027-03"


def test_dashboard_empty_state_unchanged(client):
    text = get(client, "/dashboard/")
    link = re.search(r'<section id="no-chores">.*?<a href="([^"]*)">Add a chore</a>', text, re.S).group(1)
    assert link == "/chores/create/?date=2026-11-18&amp;recurrence=once&amp;return_to=dashboard"
    assert "calendar" not in link
