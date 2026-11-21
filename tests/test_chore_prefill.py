"""``?date=`` pre-fill on /chores/create/ (#14), alone and with ``?template=`` (#6)."""
import datetime
import html
import re
from unittest import mock
from urllib.parse import quote

import pytest
from django.test import Client

from chores.models import Chore

pytestmark = pytest.mark.django_db

NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.timezone.utc)
TODAY = "2026-11-18"


@pytest.fixture
def client():
    return Client()


def get(client, path="/chores/create/", **params):
    with mock.patch("django.utils.timezone.now", return_value=NOW):
        return client.get(path, params)


def start_date(body):
    tag = re.search(r'<input[^>]*name="start_date"[^>]*>', body).group(0)
    return re.search(r'value="([^"]*)"', tag).group(1)


def rest(page):
    """The page without CSRF tokens and the start date, to compare the rest."""
    page = re.sub(r"[A-Za-z0-9]{32,}", "", page)
    page = re.sub(r"&amp;date=\d{4}-\d\d-\d\d", "", page)  # the picker carries a valid date
    return re.sub(r'(name="start_date"[^>]*value=")[^"]*', r"\1", page)


def template(title="Mop", **kwargs):
    values = dict(
        title=title, start_date=datetime.date(2026, 1, 1), recurrence="weekly", recurrence_day=2,
        assignment_mode="unassigned", points=20, is_template=True, instructions="Use the blue mop",
    )
    values.update(kwargs)
    return Chore.objects.create(**values)


def test_valid_date_prefills_start_date_only(client):
    plain = get(client).content.decode()
    body = get(client, date="2026-12-24").content.decode()
    assert start_date(plain) == TODAY and start_date(body) == "2026-12-24"
    assert rest(body) == rest(plain)


def test_past_date_accepted(client):
    assert start_date(get(client, date="2026-01-05").content.decode()) == "2026-01-05"


def test_saving_with_prefilled_date(client):
    page = get(client, date="2026-12-24").content.decode()
    assert start_date(page) == "2026-12-24"
    response = client.post("/chores/create/?date=2026-12-24", {
        "title": "Tree", "start_date": "2026-12-25", "assignment_mode": "unassigned",
        "recurrence": "once", "points": "10",
    })
    assert response.status_code == 302
    assert Chore.objects.get(title="Tree").start_date == datetime.date(2026, 12, 25)


HOSTILE = [
    "abc", "", "2026-02-30", "2026-2-3", "20261118", "2026-11-18T00:00", " 2026-11-18",
    "2026-11-18%00", "2026-11-18\x00", "2026-11-18\n", "٢٠٢٦-١١-١٨", "1999-12-31", "2100-01-01",
    "9999-12-31", "x" * 10_000, "<script>alert(1)</script>",
]


@pytest.mark.parametrize("value", HOSTILE)
def test_invalid_date_ignored(client, value):
    response = get(client, date=value)
    body = response.content.decode()
    assert response.status_code == 200
    assert start_date(body) == TODAY
    assert rest(body) == rest(get(client).content.decode())
    if value.strip():
        for form in (value, html.escape(value), quote(value), quote(value, safe="")):
            assert form not in body


def test_date_ignored_on_edit_and_post(client):
    chore = Chore.objects.create(title="Dishes", start_date=datetime.date(2026, 3, 3), recurrence="once")
    body = get(client, f"/chores/{chore.pk}/edit/", date="2026-12-24").content.decode()
    assert start_date(body) == "2026-03-03"
    # POST reads the form field only; an invalid POST re-render keeps the posted value.
    with mock.patch("django.utils.timezone.now", return_value=NOW):
        response = client.post("/chores/create/?date=2026-12-24", {
            "title": "", "start_date": "2026-05-05", "assignment_mode": "unassigned",
            "recurrence": "once", "points": "10",
        })
    assert response.status_code == 200 and start_date(response.content.decode()) == "2026-05-05"
    assert 'date=2026-12-24' not in response.content.decode()


# --- With a template --------------------------------------------------------------


def hidden_template(body):
    return re.search(r'<input[^>]*name="template"[^>]*value="([^"]*)"', body)


def test_template_with_date(client):
    t = template()
    body = get(client, template=t.pk, date="2026-12-24").content.decode()
    assert start_date(body) == "2026-12-24"
    assert "Mop" in body and "Use the blue mop" in body
    assert hidden_template(body).group(1) == str(t.pk)
    plain = get(client, template=t.pk).content.decode()
    assert start_date(plain) == TODAY and "Use the blue mop" in plain


def test_saving_sets_template_source(client):
    t = template()
    client.post("/chores/create/?template=1&date=2026-12-24", {
        "title": "Mop", "start_date": "2026-12-24", "assignment_mode": "unassigned",
        "recurrence": "daily", "points": "20", "template": str(t.pk),
    })
    assert Chore.objects.get(title="Mop", is_template=False).template_source == t


@pytest.mark.parametrize("bad", ["abc", "999999", None])
def test_valid_date_invalid_template_is_default_form(client, bad):
    t = template()
    plain = Chore.objects.create(title="Plain", start_date=datetime.date(2026, 1, 1), recurrence="once")
    params = {"date": "2026-12-24"}
    params["template"] = plain.pk if bad is None else bad
    body = get(client, **params).content.decode()
    assert start_date(body) == "2026-12-24"
    assert "Use the blue mop" not in body and "Plain" not in re.sub(r"<(a|li)[^>]*>.*?</\1>", "", body, flags=re.S)
    hidden = hidden_template(body)
    assert hidden is None or hidden.group(1) == ""
    assert t.pk  # the template exists but was not asked for


def test_valid_template_invalid_date_is_template_prefill_today(client):
    t = template()
    body = get(client, template=t.pk, date="2026-02-30").content.decode()
    assert start_date(body) == TODAY and "Use the blue mop" in body


def picker_links(body):
    return re.findall(r'<a href="([^"]*template=[^"]*)"\s+hx-get="([^"]*)"', body)


def test_picker_links_keep_valid_date(client):
    a, b = template("Mop"), template("Wash", is_builtin=True)
    for params in ({"date": "2026-11-18"}, {"date": "2026-11-18", "template": a.pk}):
        links = picker_links(get(client, **params).content.decode())
        assert len(links) == Chore.objects.filter(is_template=True).count() > 1
        for href, hx in links:
            assert html.unescape(href) == html.unescape(hx)
            assert re.fullmatch(r"/chores/create/\?template=\d+&date=2026-11-18", html.unescape(href))


@pytest.mark.parametrize("params", [{}, {"date": "abc"}, {"date": "2026-02-30"}, {"date": "1999-01-01"}])
def test_picker_links_without_valid_date_are_plain(client, params):
    template()
    links = picker_links(get(client, **params).content.decode())
    assert links
    for href, hx in links:
        assert re.fullmatch(r"/chores/create/\?template=\d+", href)
        assert re.fullmatch(r"/chores/create/\?template=\d+", hx)
