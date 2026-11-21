"""Return to the calendar month after adding a chore from a day (#36)."""
import datetime
import html
import re

import pytest
from django.contrib.messages import get_messages
from django.test import Client

from chores.models import Chore, ChoreOccurrence, Member
from tests.test_calendar import WED, at, cell, get as calendar_get
from tests.test_chores import payload
from tests.test_one_off import hidden

pytestmark = pytest.mark.django_db

CREATE = "/chores/create/"
NEXT = re.compile(r'name="next" value="([^"]*)"')
BACK_CAL = re.compile(r'<a id="back-to-calendar" href="([^"]*)">Back to calendar</a>')
FORM = re.compile(r'<form id="chore-form".*?</form>', re.S)
CSRF = re.compile(r'name="csrfmiddlewaretoken" value="[^"]*"|"X-CSRFToken": "[^"]*"')
PICKER = re.compile(r'<a href="([^"]*template=[^"]*)"\s+hx-get="([^"]*)"')
DEC = "/chores/create/?date=2026-12-03&return_to=calendar&month=2026-12"
HOSTILE_MONTHS = ["abc", "2026-13", "2026-1", "２０２６-12", "2026-12\n", "2028-01",
                  "//evil.example", "<script>alert(1)</script>"]
HOSTILE_RETURNS = ["Calendar", "calendar%00", "%2F%2Fevil.example", "https%3A%2F%2Fevil.example", "a" * 10000]


@pytest.fixture
def client():
    return Client()


@pytest.fixture(autouse=True)
def mom():
    return Member.objects.create(name="Mom", role="parent", avatar="M", order=1)


def tmpl(title="Mop"):
    return Chore.objects.create(title=title, is_template=True, recurrence="once", points=5,
                                start_date=datetime.date(2026, 1, 1))


def go(client, url):
    with at(WED):
        return client.get(url)


def body(client, url):
    response = go(client, url)
    assert response.status_code == 200
    return response.content.decode()


def form_hidden(text):
    return hidden(FORM.search(text).group(0))


def nexts(text):
    found = {html.unescape(v) for v in NEXT.findall(text)}
    assert len(found) == 1, found
    return found.pop()


def post(client, query="", **fields):
    with at(WED):
        return client.post(CREATE + query, payload(**fields))


def saved_post(client, extra, **fields):
    with at(WED):
        return client.post(CREATE, {**payload(**fields), **extra})


# --- "+" links ---------------------------------------------------------------------


def links_for(client, month=None):
    with at(WED):
        text = client.get("/calendar/", {"month": month} if month else {}).content.decode()
    return [html.unescape(h) for h in re.findall(r'href="(/chores/create/\?date=[^"]*)"', text)]


def test_plus_links_current_month_have_no_month(client):
    links = links_for(client)
    assert len(links) == 30
    for i, link in enumerate(links, 1):
        assert link == f"/chores/create/?date=2026-11-{i:02d}&return_to=calendar"


@pytest.mark.parametrize("month,count", [("2026-12", 31), ("2026-09", 30)])
def test_plus_links_other_months_carry_month(client, month, count):
    links = links_for(client, month)
    assert len(links) == count
    for i, link in enumerate(links, 1):
        assert link == f"/chores/create/?date={month}-{i:02d}&return_to=calendar&month={month}"


def test_plus_links_survive_htmx_navigation(client):
    with at(WED):
        text = client.get("/calendar/?month=2026-12", HTTP_HX_REQUEST="true").content.decode()
    assert 'href="/chores/create/?date=2026-12-03&amp;return_to=calendar&amp;month=2026-12"' in text


def test_empty_month_link_and_filler_unchanged(client):
    with at(WED):
        text = client.get("/calendar/?month=2027-03").content.decode()
    assert 'href="/chores/create/?return_to=calendar&amp;month=2027-03">Add a chore' in text
    assert cell(text, 1)["links"]


# --- The create page ---------------------------------------------------------------


def test_create_page_from_calendar(client):
    text = body(client, DEC)
    assert 'value="2026-12-03"' in text
    assert BACK_CAL.findall(text) == ["/calendar/?month=2026-12"]
    assert form_hidden(text) == {"return_to": "calendar", "month": "2026-12"}
    assert "Back to dashboard" not in text and "back-to-dashboard" not in text
    for name in ("week", "member", "day"):
        assert f'name="{name}"' not in text.split('id="chore-form"')[1].split("</form>")[0]
    assert nexts(text) == DEC


def test_create_page_current_month_has_no_month_input(client):
    text = body(client, "/chores/create/?date=2026-11-18&return_to=calendar")
    assert BACK_CAL.findall(text) == ["/calendar/"]
    assert form_hidden(text) == {"return_to": "calendar"}
    assert nexts(text) == "/chores/create/?date=2026-11-18&return_to=calendar"


def test_explicit_current_month_is_normalized_away(client):
    a = body(client, "/chores/create/?date=2026-11-18&return_to=calendar&month=2026-11")
    b = body(client, "/chores/create/?date=2026-11-18&return_to=calendar")
    csrf = CSRF
    assert csrf.sub("", a) == csrf.sub("", b)


def test_next_with_template_and_recurrence(client):
    t = tmpl()
    assert nexts(body(client, DEC + f"&template={t.pk}")) == (
        f"/chores/create/?date=2026-12-03&template={t.pk}&return_to=calendar&month=2026-12")
    assert nexts(body(client, DEC + "&recurrence=once")) == (
        "/chores/create/?date=2026-12-03&recurrence=once&return_to=calendar&month=2026-12")


def test_picker_links_keep_the_return(client):
    tmpl()
    for query, tail in ((DEC, "&return_to=calendar&month=2026-12"),
                        ("/chores/create/?date=2026-12-03&return_to=calendar", "&return_to=calendar")):
        links = PICKER.findall(body(client, query))
        assert links
        for href, hx in links:
            expected = re.compile(r"/chores/create/\?template=\d+&date=2026-12-03" + re.escape(tail))
            assert expected.fullmatch(html.unescape(href))
            assert html.unescape(href) == html.unescape(hx)


def test_picking_a_template_keeps_inputs_and_back_link(client):
    t = tmpl()
    text = body(client, f"/chores/create/?template={t.pk}&date=2026-12-03&return_to=calendar&month=2026-12")
    assert form_hidden(text)["return_to"] == "calendar"
    assert form_hidden(text)["month"] == "2026-12"
    assert BACK_CAL.findall(text) == ["/calendar/?month=2026-12"]


def test_edit_page_ignores_the_return(client):
    chore = Chore.objects.create(title="Dishes", recurrence="once", points=5,
                                start_date=datetime.date(2026, 11, 20))
    plain = body(client, f"/chores/{chore.pk}/edit/")
    text = body(client, f"/chores/{chore.pk}/edit/?return_to=calendar&month=2026-12")
    assert "back-to-calendar" not in text and "2026-12" not in text
    assert "return_to" not in form_hidden(text) and "month" not in form_hidden(text)
    assert nexts(text) == nexts(plain) == f"/chores/{chore.pk}/edit/"


# --- Saving ------------------------------------------------------------------------


def test_valid_save_returns_to_month_and_shows_message_once(client):
    response = saved_post(client, {"return_to": "calendar", "month": "2026-12"}, start_date="2026-12-03")
    assert response.status_code == 302 and response["Location"] == "/calendar/?month=2026-12"
    with at(WED):
        first = client.get(response["Location"]).content.decode()
        second = client.get(response["Location"]).content.decode()
    assert first.count("Chore saved") == 1
    assert '<p class="message" role="status"><mark>Chore saved</mark></p>' in first
    assert "Chore saved" not in second


def test_valid_save_without_month_goes_to_calendar(client):
    response = saved_post(client, {"return_to": "calendar"})
    assert response.status_code == 302 and response["Location"] == "/calendar/"


def test_save_as_template_message(client):
    response = saved_post(client, {"return_to": "calendar"}, save_as_template="on")
    with at(WED):
        text = client.get(response["Location"]).content.decode()
    assert "Chore saved and template added" in text


def test_redirect_ignores_a_changed_start_date(client):
    response = saved_post(client, {"return_to": "calendar", "month": "2026-12"}, start_date="2027-02-10")
    assert response["Location"] == "/calendar/?month=2026-12"


def test_one_off_appears_in_the_day_cell_without_duplicates(client):
    saved_post(client, {"return_to": "calendar"}, title="Window", start_date="2026-11-12")
    saved_post(client, {"return_to": "calendar"}, title="Garage", start_date="2026-11-25")
    with at(WED):
        text = client.get("/calendar/").content.decode()
    assert cell(text, 12)["count"] and cell(text, 25)["count"]
    assert ChoreOccurrence.objects.filter(chore__title="Window").count() == 1
    assert ChoreOccurrence.objects.filter(chore__title="Garage").count() == 0  # projected only


def test_invalid_post_rerenders_with_the_return(client):
    with at(WED):
        response = client.post(CREATE, payload(title="", return_to="calendar", month="2026-12"))
    assert response.status_code == 200
    text = response.content.decode()
    assert form_hidden(text) == {"return_to": "calendar", "month": "2026-12"}
    assert BACK_CAL.findall(text) == ["/calendar/?month=2026-12"]
    assert nexts(text) == "/chores/create/?return_to=calendar&month=2026-12"
    assert not Chore.objects.filter(is_template=False).exists()


def test_month_left_the_window_by_post_goes_to_calendar(client):
    response = saved_post(client, {"return_to": "calendar", "month": "2028-01"})
    assert response["Location"] == "/calendar/"


# --- Hostile and invalid values ----------------------------------------------------


@pytest.mark.parametrize("value", HOSTILE_RETURNS)
def test_bad_return_to_means_no_return(client, value):
    text = body(client, f"{CREATE}?date=2026-12-03&return_to={value}&month=2026-12")
    assert form_hidden(text) == {}
    assert "back-to-calendar" not in text and "back-to-dashboard" not in text
    assert "evil.example" not in text and "2026-12\"" not in text.split('id="chore-form"')[1].split("</form>")[0]
    response = saved_post(client, {"return_to": html.unescape(value.replace("%2F", "/").replace("%3A", ":").replace("%00", "\x00")), "month": "2026-12"})
    assert response.status_code == 302 and re.fullmatch(r"/chores/\d+/edit/", response["Location"])


@pytest.mark.parametrize("value", HOSTILE_MONTHS)
def test_bad_month_is_dropped(client, value):
    from urllib.parse import quote
    text = body(client, f"{CREATE}?return_to=calendar&month={quote(value)}")
    assert form_hidden(text) == {"return_to": "calendar"}
    assert BACK_CAL.findall(text) == ["/calendar/"]
    for raw in (value, html.escape(value), quote(value)):
        assert f"month={raw}" not in text and f'value="{raw}"' not in text
        assert f"month={html.escape(raw)}" not in text and "evil.example" not in text
        assert "<script>alert" not in text
    response = saved_post(client, {"return_to": "calendar", "month": value})
    assert response["Location"] == "/calendar/"


def test_last_return_to_wins(client):
    text = body(client, f"{CREATE}?return_to=dashboard&return_to=calendar&month=2026-12")
    assert form_hidden(text) == {"return_to": "calendar", "month": "2026-12"}
    text = body(client, f"{CREATE}?return_to=calendar&return_to=dashboard&month=2026-12")
    assert form_hidden(text).get("return_to") == "dashboard"
    assert "month" not in form_hidden(text)


def test_calendar_ignores_dashboard_filters(client):
    with_junk = body(client, DEC + "&week=2026-W47&member=1&day=3")
    clean = body(client, DEC)
    csrf = CSRF
    assert csrf.sub("", with_junk) == csrf.sub("", clean)
    response = saved_post(client, {"return_to": "calendar", "month": "2026-12", "week": "2026-W47", "day": "3"})
    assert response["Location"] == "/calendar/?month=2026-12"


def test_dashboard_ignores_month(client):
    base = f"{CREATE}?date=2026-12-03&return_to=dashboard"
    csrf = CSRF
    assert csrf.sub("", body(client, base + "&month=2026-12")) == csrf.sub("", body(client, base))
    a = saved_post(client, {"return_to": "dashboard", "month": "2026-12"})
    b = saved_post(client, {"return_to": "dashboard"})
    assert a["Location"] == b["Location"]


def test_edit_post_ignores_the_return(client):
    chore = Chore.objects.create(title="Dishes", recurrence="once", points=5,
                                start_date=datetime.date(2026, 11, 20))
    with at(WED):
        response = client.post(f"/chores/{chore.pk}/edit/", payload(return_to="calendar", month="2026-12"))
    assert response["Location"] == f"/chores/{chore.pk}/edit/"


def test_no_return_to_creates_as_before(client):
    response = saved_post(client, {})
    assert re.fullmatch(r"/chores/\d+/edit/", response["Location"])
