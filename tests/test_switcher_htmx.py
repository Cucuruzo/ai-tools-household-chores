"""Profile switcher ``next`` follows HTMX navigation (#25)."""
import datetime
import html
import re
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

import pytest
from django.test import Client

from chores.models import Chore, ChoreOccurrence, Member

pytestmark = pytest.mark.django_db

WED = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.timezone.utc)  # W47, 2026-11
HTMX = {"HTTP_HX_REQUEST": "true"}
RESTORE = {**HTMX, "HTTP_HX_HISTORY_RESTORE_REQUEST": "true"}
HOSTILE = "<script>alert(1)</script>"
NAV = re.compile(r'<nav id="profile-switcher"[^>]*>.*?</nav>', re.S)
NEXT = re.compile(r'name="next" value="([^"]*)"')
CSRF = re.compile(r'(name="csrfmiddlewaretoken" value=")[^"]*"')


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def ann():
    return Member.objects.create(name="Ann", role="parent", avatar="A", order=1)


@pytest.fixture
def bob(ann):
    return Member.objects.create(name="Bob", role="kid", avatar="B", order=2)


def fetch(client, path, params=None, **headers):
    with mock.patch("django.utils.timezone.now", return_value=WED):
        return client.get(path, params or {}, **headers)


def body_of(client, path, params=None, **headers):
    response = fetch(client, path, params, **headers)
    assert response.status_code == 200
    return response.content.decode()


def nexts(body):
    return {html.unescape(v) for v in NEXT.findall(body)}


def next_of(body):
    found = nexts(body)
    assert len(found) == 1, found
    return found.pop()


def partial_next(client, path, params=None):
    return next_of(body_of(client, path, params, **HTMX))


def full_next(client, path, params=None):
    return next_of(body_of(client, path, params))


# --- Dashboard ------------------------------------------------------------------


def test_forward_twice_and_back(client, ann):
    assert partial_next(client, "/dashboard/", {"week": "2026-W48"}) == "/dashboard/?week=2026-W48"
    assert partial_next(client, "/dashboard/", {"week": "2026-W49"}) == "/dashboard/?week=2026-W49"
    assert partial_next(client, "/dashboard/", {"week": "2026-W46"}) == "/dashboard/?week=2026-W46"


def test_this_week(client, ann):
    assert partial_next(client, "/dashboard/") == "/dashboard/"
    assert partial_next(client, "/dashboard/", {"week": "2026-W47"}) == "/dashboard/"


def test_this_week_keeps_filters(client, ann):
    params = {"member": ann.pk, "day": "3"}
    assert partial_next(client, "/dashboard/", params) == f"/dashboard/?member={ann.pk}&day=3"


@pytest.mark.parametrize("extra,suffix", [
    ({}, ""),
    ({"member": "1"}, "&member=1"),
    ({"member": "1", "day": "3"}, "&member=1&day=3"),
    ({"member": "unassigned"}, "&member=unassigned"),
    ({"day": "3"}, "&day=3"),
])
def test_filters_on_another_week(client, ann, extra, suffix):
    params = {"week": "2026-W49", **extra}
    if extra.get("member") == "1":
        params["member"] = ann.pk
        suffix = suffix.replace("member=1", f"member={ann.pk}")
    assert partial_next(client, "/dashboard/", params) == "/dashboard/?week=2026-W49" + suffix


@pytest.mark.parametrize("params", [
    {},
    {"week": "2026-W49"},
    {"week": "2026-W49", "member": "unassigned", "day": "5"},
    {"member": "unassigned"},
    {"week": "abc", "member": "abc", "day": "9", "x": "1"},
])
def test_partial_next_equals_full_page_next(client, ann, params):
    assert partial_next(client, "/dashboard/", params) == full_next(client, "/dashboard/", params)


def test_hostile_dashboard_values(client, ann):
    deleted = ann.pk + 1000
    params = {"week": "abc", "member": HOSTILE, "day": "9", "x": "1"}
    assert partial_next(client, "/dashboard/", params) == "/dashboard/"
    for bad in ({"week": "abc"}, {"week": "2026-W99"}, {"member": "abc"},
                {"member": str(deleted)}, {"member": HOSTILE}, {"day": "9"}, {"x": "1"}):
        body = body_of(client, "/dashboard/", bad, **HTMX)
        assert next_of(body) == "/dashboard/"
        assert "alert(1)" not in body
        for token in ("abc", "W99", "alert(1)", "%3Cscript"):
            assert token not in html.unescape(next_of(body))
        assert "abc" not in "".join(NEXT.findall(body)) and "W99" not in "".join(NEXT.findall(body))


def test_every_next_is_canonical(client, ann):
    allowed = {"/dashboard/": ["week", "member", "day"], "/calendar/": ["month"]}
    cases = [("/dashboard/", {"week": "2026-W49", "member": ann.pk, "day": "3"}),
             ("/dashboard/", {"week": "2026-W49", "member": "unassigned"}),
             ("/calendar/", {"month": "2026-12"}), ("/calendar/", {"month": "abc"})]
    for path, params in cases:
        url = urlsplit(partial_next(client, path, params))
        assert url.path == path
        keys = [k for k, v in parse_qsl(url.query, keep_blank_values=True)]
        assert all(v for k, v in parse_qsl(url.query, keep_blank_values=True))
        assert keys == [k for k in allowed[path] if k in keys]


def test_single_oob_nav_after_dashboard_list(client, ann):
    body = body_of(client, "/dashboard/", {"week": "2026-W48"}, **HTMX)
    assert body.count('id="profile-switcher"') == 1
    assert body.count('<nav id="profile-switcher" hx-swap-oob="true"') == 1
    assert body.lstrip().startswith('<div id="dashboard-list">')
    assert body.rindex("</div>") < body.index('<nav id="profile-switcher"')
    assert body.rstrip().endswith("</nav>")
    for bad in ("<html", "<header", "<script"):
        assert bad not in body


def test_oob_nav_marks_active_member_and_every_form_has_next(client, ann, bob):
    client.post(f"/profile/{bob.pk}/")
    body = body_of(client, "/dashboard/", {"week": "2026-W49"}, **HTMX)
    nav = NAV.search(body).group(0)
    assert nav.count('name="next"') == 2
    assert nexts(nav) == {"/dashboard/?week=2026-W49"}
    assert nav.count('aria-current="true"') == 1 and "<strong>Bob</strong>" in nav


def test_oob_nav_without_members(client):
    body = body_of(client, "/dashboard/", {"week": "2026-W49"}, **HTMX)
    full = body_of(client, "/dashboard/", {"week": "2026-W49"})
    nav = NAV.search(body).group(0)
    assert "Add your family" in nav and 'name="next"' not in nav
    assert re.sub(r'\s+hx-swap-oob="true"', "", nav) == NAV.search(full).group(0)


def test_full_pages_and_history_restore_have_no_oob(client, ann):
    for headers in ({}, {"HTTP_HX_REQUEST": "false"}, RESTORE):
        body = body_of(client, "/dashboard/", {"week": "2026-W49"}, **headers)
        assert "<html" in body
        assert body.count('id="profile-switcher"') == 1 and "hx-swap-oob" not in body
        assert next_of(body) == "/dashboard/?week=2026-W49"
    for headers in ({}, RESTORE):
        body = body_of(client, "/calendar/", {"month": "2026-12"}, **headers)
        assert body.count('id="profile-switcher"') == 1 and "hx-swap-oob" not in body
        assert next_of(body) == "/calendar/?month=2026-12"


@pytest.mark.parametrize("path", ["/dashboard/", "/calendar/"])
@pytest.mark.parametrize("headers", [{}, HTMX, RESTORE])
def test_vary_kept(client, ann, path, headers):
    vary = fetch(client, path, **headers)["Vary"]
    assert "HX-Request" in vary and "HX-History-Restore-Request" in vary


# --- Calendar -------------------------------------------------------------------


def test_calendar_forward_and_back(client, ann):
    assert partial_next(client, "/calendar/", {"month": "2026-12"}) == "/calendar/?month=2026-12"
    assert partial_next(client, "/calendar/", {"month": "2026-10"}) == "/calendar/?month=2026-10"
    assert partial_next(client, "/calendar/", {"month": "2026-09"}) == "/calendar/?month=2026-09"


def test_calendar_this_month(client, ann):
    assert partial_next(client, "/calendar/") == "/calendar/"
    assert partial_next(client, "/calendar/", {"month": "2026-11"}) == "/calendar/"


@pytest.mark.parametrize("value", ["abc", "2026-11x", "9999-12", HOSTILE])
def test_calendar_invalid_month(client, ann, value):
    body = body_of(client, "/calendar/", {"month": value}, **HTMX)
    assert next_of(body) == "/calendar/"
    assert "alert(1)" not in body


@pytest.mark.parametrize("month_value", [None, "2026-12", "abc"])
def test_calendar_partial_next_equals_full(client, ann, month_value):
    params = {} if month_value is None else {"month": month_value}
    assert partial_next(client, "/calendar/", params) == full_next(client, "/calendar/", params)


def test_calendar_single_oob_nav_after_grid(client, ann):
    body = body_of(client, "/calendar/", {"month": "2026-12"}, **HTMX)
    assert body.count('id="profile-switcher"') == 1
    assert body.count('<nav id="profile-switcher" hx-swap-oob="true"') == 1
    assert body.lstrip().startswith('<section id="calendar-grid">')
    assert body.rindex("</section>") < body.index('<nav id="profile-switcher"')
    for bad in ("<html", "<header", "<script"):
        assert bad not in body


def test_calendar_oob_nav_without_members(client):
    body = body_of(client, "/calendar/", {"month": "2026-12"}, **HTMX)
    assert "Add your family" in NAV.search(body).group(0)


# --- Other HTMX responses are unchanged -------------------------------------------


def test_toggle_assign_delete_have_no_switcher(client, ann):
    chore = Chore.objects.create(title="Job", start_date=datetime.date(2026, 11, 18),
                                 recurrence="once", assignment_mode="unassigned", points=5)
    occ = ChoreOccurrence.objects.create(chore=chore, date=datetime.date(2026, 11, 18), points=5)
    client.post(f"/profile/{ann.pk}/")
    with mock.patch("django.utils.timezone.now", return_value=WED):
        toggle = client.post(f"/dashboard/toggle/{occ.pk}/", {"action": "done"}, **HTMX)
        assign = client.post(f"/dashboard/assign/{occ.pk}/", {"assignee": str(ann.pk)}, **HTMX)
        delete = client.post(f"/chores/{chore.pk}/delete/", {}, **HTMX)
    for response in (toggle, assign, delete):
        assert "profile-switcher" not in response.content.decode()
