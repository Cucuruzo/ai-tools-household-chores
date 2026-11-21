"""Clicking the active member in the header switcher deselects them (#54)."""
import datetime
import html
import re
from unittest import mock

import pytest
from django.test import Client

from chores.models import Chore, ChoreOccurrence, Member

pytestmark = pytest.mark.django_db

WED = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.timezone.utc)
HTMX = {"HTTP_HX_REQUEST": "true"}
KEY = "active_member_id"
NAV = re.compile(r'<nav id="profile-switcher"[^>]*>.*?</nav>', re.S)
NEXT = re.compile(r'name="next" value="([^"]*)"')
ACTIVE_BUTTON = re.compile(r'<button type="submit" class="profile active" [^>]*>')


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def ann():
    return Member.objects.create(name="Ann", role="parent", avatar="A", order=1)


@pytest.fixture
def bob(ann):
    return Member.objects.create(name="Bob", role="kid", avatar="B", order=2)


def at(client_method, *args, **kwargs):
    with mock.patch("django.utils.timezone.now", return_value=WED):
        return client_method(*args, **kwargs)


def click(client, member, **data):
    extra = data.pop("extra", {})
    return client.post(f"/profile/{member.pk}/", data, **extra)


def nav_of(body):
    return NAV.search(body).group(0)


def nexts(nav):
    return {html.unescape(v) for v in NEXT.findall(nav)}


def set_session(client, value):
    session = client.session
    session[KEY] = value
    session.save()


def next_at(client, url, **extra):
    response = at(client.get, url, **extra)
    assert response.status_code == 200
    found = nexts(nav_of(response.content.decode()))
    assert len(found) == 1, found
    return found.pop()


# --- Clearing from every page type -------------------------------------------------


@pytest.fixture
def pages(ann, bob):
    one_off = Chore.objects.create(title="Vacuum", start_date=datetime.date(2026, 11, 18), recurrence="once")
    template = Chore.objects.create(title="Wipe", start_date=datetime.date(2026, 11, 1), is_template=True)
    return [
        "/dashboard/",
        f"/dashboard/?week=2026-W48&member={bob.pk}&day=3",
        "/dashboard/?week=2026-W40",
        "/dashboard/?week=2027-W10",
        "/calendar/",
        "/calendar/?month=2026-12",
        "/family/",
        "/contribution/",
        "/contribution/?period=week",
        "/history/",
        f"/history/?member={bob.pk}&page=1",
        "/progress/",
        "/chores/create/",
        "/chores/create/?date=2026-11-18&return_to=dashboard",
        f"/chores/{one_off.pk}/edit/",
        f"/chores/{one_off.pk}/edit/?week=2026-W48&member={bob.pk}",
        "/chores/templates/",
        f"/chores/templates/{template.pk}/edit/",
    ]


def test_clear_from_every_page_type(client, ann, pages):
    for url in pages:
        click(client, ann)
        assert client.session[KEY] == ann.pk
        value = next_at(client, url)
        response = click(client, ann, next=value)
        assert response.status_code == 302, url
        assert response["Location"] == value, url
        assert KEY not in client.session, url
        follow = at(client.get, response["Location"])
        assert follow.status_code == 200
        nav = nav_of(follow.content.decode())
        assert "Who are you?" in nav, url
        assert "aria-current" not in nav and "aria-pressed" not in nav, url
        assert nexts(nav) == {value}, url


def test_clicks_toggle(client, ann):
    assert click(client, ann).status_code == 302
    assert client.session[KEY] == ann.pk
    click(client, ann)
    assert KEY not in client.session
    click(client, ann)
    assert client.session[KEY] == ann.pk


def test_nobody_active_click_selects(client, ann):
    click(client, ann, next="/family/")
    assert client.session[KEY] == ann.pk


def test_other_member_switches_and_highlights_only_them(client, ann, bob):
    click(client, ann)
    response = click(client, bob, next="/family/")
    assert response.status_code == 302
    assert client.session[KEY] == bob.pk
    nav = nav_of(client.get("/family/").content.decode())
    assert nav.count('aria-pressed="true"') == 1
    assert len(ACTIVE_BUTTON.findall(nav)) == 1
    assert "Bob (click to deselect)" in nav and "Ann (click to deselect)" not in nav


# --- Stale session -----------------------------------------------------------------


def test_stale_active_member_then_click_sets_clicked(client, ann, bob):
    click(client, ann)
    ann.delete()
    response = click(client, bob, next="/family/")
    assert response.status_code == 302
    assert client.session[KEY] == bob.pk


@pytest.mark.parametrize("junk", ["abc", None, True, 1.5, "99999999999999999999", "", -3])
def test_invalid_session_value_then_click_sets_clicked(client, bob, junk):
    set_session(client, junk)
    response = click(client, bob)
    assert response.status_code == 302
    assert client.session[KEY] == bob.pk


# --- 404, 405, CSRF ----------------------------------------------------------------


@pytest.mark.parametrize("pk", ["999", str(2**63), "abc"])
@pytest.mark.parametrize("held", [True, False])
def test_unknown_id_is_404_and_session_untouched(client, ann, pk, held):
    if held:
        click(client, ann)
    response = client.post(f"/profile/{pk}/", {"next": "/family/"})
    assert response.status_code == 404
    if held:
        assert client.session[KEY] == ann.pk
    else:
        assert KEY not in client.session


def test_unknown_id_does_not_clean_stale_session(client, ann):
    set_session(client, "abc")
    assert client.post("/profile/999/").status_code == 404
    assert client.session[KEY] == "abc"


def test_get_is_405_and_member_stays_active(client, ann):
    click(client, ann)
    assert client.get(f"/profile/{ann.pk}/").status_code == 405
    assert client.session[KEY] == ann.pk


def test_missing_csrf_is_403_and_member_stays_active(ann):
    strict = Client(enforce_csrf_checks=True)
    set_session(strict, ann.pk)
    assert strict.post(f"/profile/{ann.pk}/", {"next": "/family/"}).status_code == 403
    assert strict.session[KEY] == ann.pk


# --- Hostile next / Referer --------------------------------------------------------


@pytest.mark.parametrize("bad", ["https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)"])
def test_hostile_next_redirects_home_and_still_clears(client, ann, bad):
    click(client, ann)
    response = click(client, ann, next=bad)
    assert response.status_code == 302
    assert response["Location"] == "/"
    assert KEY not in client.session


def test_external_referer_without_next_redirects_home_and_clears(client, ann):
    click(client, ann)
    response = click(client, ann, extra={"HTTP_REFERER": "https://evil.example/x"})
    assert response["Location"] == "/"
    assert KEY not in client.session


def test_same_host_referer_without_next(client, ann):
    click(client, ann)
    response = click(client, ann, extra={"HTTP_REFERER": "http://testserver/progress/"})
    assert response["Location"] == "http://testserver/progress/"
    assert KEY not in client.session


# --- Markup ------------------------------------------------------------------------


def test_active_button_markup_is_exact(client, ann, bob):
    click(client, ann)
    nav = nav_of(client.get("/family/").content.decode())
    assert (
        '<button type="submit" class="profile active" aria-pressed="true" '
        'title="Ann (click to deselect)" aria-current="true">'
    ) in nav
    assert re.search(r'aria-current="true">\s*<span class="avatar">A</span>', nav)
    assert nav.count('aria-pressed="true"') == 1
    assert '<button type="submit" class="profile outline secondary">' in nav


def test_name_is_escaped_in_title(client):
    evil = Member.objects.create(name="<b>x</b>", role="kid", avatar="X", order=1)
    click(client, evil)
    nav = nav_of(client.get("/family/").content.decode())
    assert 'title="&lt;b&gt;x&lt;/b&gt; (click to deselect)"' in nav
    assert "<b>x</b> (click" not in nav


def test_no_aria_pressed_when_nobody_active(client, ann, bob):
    nav = nav_of(client.get("/family/").content.decode())
    assert "aria-pressed" not in nav and "aria-current" not in nav and "click to deselect" not in nav


def test_switcher_has_no_hx_attributes_besides_oob(client, ann, bob):
    click(client, ann)
    nav = nav_of(client.get("/family/").content.decode())
    assert "hx-" not in nav
    oob = nav_of(at(client.get, "/dashboard/", {"week": "2026-W48"}, **HTMX).content.decode())
    assert re.findall(r"hx-[\w-]+", oob) == ["hx-swap-oob"]


# --- Credit after clearing ---------------------------------------------------------


def todo_card():
    chore = Chore.objects.create(title="Job", start_date=datetime.date(2026, 11, 18), recurrence="once")
    return ChoreOccurrence.objects.create(chore=chore, date=datetime.date(2026, 11, 18), points=10)


def credit(fragment):
    m = re.search(r'<p class="credit">(.*?)</p>', fragment, re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else None


@pytest.mark.parametrize("extra", [HTMX, {}], ids=["htmx", "no-js"])
def test_done_after_clear_is_not_credited_and_after_reselect_credits(client, ann, extra):
    click(client, ann)
    click(client, ann)
    assert KEY not in client.session
    first = todo_card()
    response = at(client.post, f"/dashboard/toggle/{first.pk}/", {"action": "done"}, **extra)
    first.refresh_from_db()
    assert first.status == "done" and first.completed_by is None
    body = response.content.decode() if extra else at(client.get, "/dashboard/").content.decode()
    assert "Done · not credited" in html.unescape(re.sub(r"<[^>]+>", "", body))

    click(client, ann)
    second = todo_card()
    at(client.post, f"/dashboard/toggle/{second.pk}/", {"action": "done"}, **extra)
    second.refresh_from_db()
    assert second.completed_by == ann


# --- Out-of-band switchers ---------------------------------------------------------

OOB_SOURCES = [
    ("get", "/dashboard/", {"week": "2026-W48"}),
    ("get", "/calendar/", {"month": "2026-12"}),
    ("get", "/contribution/", {"period": "week"}),
    ("post", "/family/{bob}/edit/", {"name": "Bobby", "role": "adult", "avatar": "😀"}),
]


@pytest.mark.parametrize("verb,url,data", OOB_SOURCES)
def test_oob_switchers_reflect_cleared_and_active_state(client, ann, bob, verb, url, data):
    url = url.format(bob=bob.pk)

    def fetch():
        return at(getattr(client, verb), url, data, **HTMX).content.decode()

    click(client, ann)
    body = fetch()
    oob = re.search(r'<nav id="profile-switcher" hx-swap-oob="true".*?</nav>', body, re.S)
    assert oob, url
    assert oob.group(0).count('aria-pressed="true"') == 1
    assert "Ann (click to deselect)" in oob.group(0)
    before = nexts(oob.group(0))

    click(client, ann)
    body = fetch()
    oob = re.search(r'<nav id="profile-switcher" hx-swap-oob="true".*?</nav>', body, re.S)
    assert oob, url
    assert "Who are you?" in oob.group(0)
    assert "aria-pressed" not in oob.group(0) and "aria-current" not in oob.group(0)
    assert len(nexts(oob.group(0))) == 1
    assert len(before) == 1
