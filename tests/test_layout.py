"""Base layout, site nav and stylesheet (#18)."""
import re
from pathlib import Path

import pytest
from django.test import Client

from chores.models import Chore
from tests.test_dashboard import WED, at, chore, member, occ

pytestmark = pytest.mark.django_db

NAV = re.compile(r'<nav id="site-nav" aria-label="Main">(.*?)</nav>', re.S)
LINK = re.compile(r'<a href="([^"]*)"( aria-current="page")?>([^<]*)</a>')
LINKS = [
    ("/dashboard/", "Dashboard"), ("/calendar/", "Calendar"), ("/family/", "Family"),
    ("/contribution/", "Contribution"), ("/progress/", "Progress"),
    ("/history/", "History"), ("/chores/templates/", "Templates"),
]
CSS = Path(__file__).resolve().parent.parent / "chores" / "static" / "chores" / "style.css"
PICO = '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2.0.6/css/pico.min.css">'
HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def data():
    m = member("Emma")
    occ("Dishes", "2026-11-18", member=m)
    c = chore("Weekly", recurrence="weekly", recurrence_day=2)
    t = Chore.objects.create(title="Tpl", start_date="2026-11-16", is_template=True)
    return {"member": m, "chore": c, "tpl": t, "once": Chore.objects.get(title="Dishes")}


def fetch(client, path, **extra):
    with at(WED):
        response = client.get(path, follow=True, **extra)
    assert response.status_code == 200
    return response.content.decode()


def check_nav(text, current):
    nav = NAV.search(text)
    assert nav, "site nav missing"
    found = LINK.findall(nav.group(1))
    assert [(h, label) for h, _, label in found] == LINKS
    marked = [label for _, mark, label in found if mark]
    assert marked == ([current] if current else [])


PAGES = [
    ("/", "Dashboard"), ("/dashboard/", "Dashboard"), ("/calendar/", "Calendar"),
    ("/family/", "Family"), ("/family/?edit={m}", "Family"), ("/family/?remove={m}", "Family"),
    ("/contribution/", "Contribution"), ("/contribution/?period=month", "Contribution"),
    ("/progress/", "Progress"), ("/history/", "History"),
    ("/chores/templates/", "Templates"), ("/chores/templates/{t}/edit/", "Templates"),
    ("/chores/create/", None), ("/chores/{c}/edit/", None),
]


@pytest.mark.parametrize("path,current", PAGES)
def test_nav_on_every_page(client, data, path, current):
    path = path.format(m=data["member"].pk, t=data["tpl"].pk, c=data["chore"].pk)
    check_nav(fetch(client, path), current)


def test_nav_on_rerendered_member_add(client, data):
    with at(WED):
        response = client.post("/family/add/", {"name": ""})
    assert response.status_code == 200
    check_nav(response.content.decode(), "Family")


def test_stylesheet_link_follows_pico(client, data):
    text = fetch(client, "/dashboard/")
    assert PICO + '\n  <link rel="stylesheet" href="/static/chores/style.css">' in text
    assert "#profile-switcher" not in text.split("</head>")[0]


def test_header_order(client, data):
    header = re.search(r"<header.*?</header>", fetch(client, "/dashboard/"), re.S).group(0)
    assert header.index("<time") < header.index('id="profile-switcher"') < header.index('id="site-nav"')
    assert header.count('id="site-nav"') == 1


def test_nav_absent_from_partials(client, data):
    occurrence = data["once"].occurrences.get()
    for path in ("/dashboard/", "/calendar/", "/contribution/"):
        with at(WED):
            assert "site-nav" not in client.get(path, **HTMX).content.decode()
    for url, post in ((f"/dashboard/assign/{occurrence.pk}/", {"assignee": str(data["member"].pk)}),
                      (f"/dashboard/toggle/{occurrence.pk}/", {"action": "done"})):
        with at(WED):
            response = client.post(url, post, **HTMX)
        assert response.status_code == 200
        assert "site-nav" not in response.content.decode()


def test_nav_on_history_restore(client, data):
    text = fetch(client, "/calendar/", HTTP_HX_HISTORY_RESTORE_REQUEST="true", HTTP_HX_REQUEST="true")
    check_nav(text, "Calendar")


def test_nav_without_members(client):
    text = fetch(client, "/dashboard/")
    assert "Add your family" in text
    check_nav(text, "Dashboard")


def test_stylesheet_has_no_literal_colours():
    css = CSS.read_text()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css)
    assert "rgb(" not in css and "hsl(" not in css


def test_calendar_legend_once_and_not_in_partial(client, data):
    assert fetch(client, "/calendar/").count('id="calendar-legend"') == 1
    with at(WED):
        assert "calendar-legend" not in client.get("/calendar/", **HTMX).content.decode()
