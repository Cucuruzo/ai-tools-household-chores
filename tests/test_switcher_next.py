"""The profile switcher's ``next`` is built from normalized values, never the raw query (#34)."""
import datetime
import html
import re
from unittest import mock

import pytest
from django.test import Client
from django.urls import reverse

from chores import urls
from chores.models import Chore, ChoreOccurrence, Member

pytestmark = pytest.mark.django_db

WED = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.timezone.utc)
HTMX = {"HTTP_HX_REQUEST": "true"}
NAV = re.compile(r'<nav id="profile-switcher"[^>]*>.*?</nav>', re.S)
NEXT = re.compile(r'name="next" value="([^"]*)"')
JUNK = (
    "date=<script>alert(1)</script>&template=zzz&recurrence=x&return_to=//evil.example"
    "&week=//evil.example&member=https://evil.example&day=0&occurrence=99999999999999999999"
    "&next=//evil.example&page=x&period=x&month=x&x=" + "a" * 10_000 + "%00"
)
FORBIDDEN = ["evil.example", "<script>", "%3Cscript", "zzz", "a" * 100, "next=", "x="]


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def mom():
    return Member.objects.create(name="Mom", role="parent", avatar="M", order=1)


@pytest.fixture
def emma(mom):
    return Member.objects.create(name="Emma", role="kid", avatar="E", order=2)


@pytest.fixture
def dishes(emma):
    c = Chore.objects.create(title="Dishes", start_date=datetime.date(2026, 11, 1), recurrence="daily")
    ChoreOccurrence.objects.create(chore=c, date=datetime.date(2026, 11, 12), points=10)
    return c


@pytest.fixture
def vacuum(emma):
    return Chore.objects.create(title="Vacuum", start_date=datetime.date(2026, 11, 18), recurrence="once")


@pytest.fixture
def mop(emma):
    return Chore.objects.create(title="Mop", start_date=datetime.date(2026, 11, 1), is_template=True)


def occ_pk(chore):
    return chore.occurrences.get().pk


def get(client, url, **extra):
    with mock.patch("django.utils.timezone.now", return_value=WED):
        return client.get(url, **extra)


def post(client, url, data, **extra):
    with mock.patch("django.utils.timezone.now", return_value=WED):
        return client.post(url, data, **extra)


def next_of(response):
    assert response.status_code == 200
    found = {html.unescape(v) for v in NEXT.findall(response.content.decode())}
    assert len(found) == 1, found
    return found.pop()


def next_at(client, url):
    return next_of(get(client, url))


# --- Create page -----------------------------------------------------------------


def test_create_drops_invalid_date_and_template(client, mom):
    assert next_at(client, "/chores/create/?date=abc&template=zzz") == "/chores/create/"


def test_create_keeps_valid_date_and_normalizes_template(client, mom, mop):
    assert next_at(client, "/chores/create/?date=2026-11-18") == "/chores/create/?date=2026-11-18"
    assert next_at(client, f"/chores/create/?template={mop.pk}") == f"/chores/create/?template={mop.pk}"
    url = f"/chores/create/?template=00{mop.pk}&date=2026-11-18"
    assert next_at(client, url) == f"/chores/create/?date=2026-11-18&template={mop.pk}"


def test_create_recurrence_only_when_valid_and_no_template(client, mom, mop):
    assert next_at(client, "/chores/create/?recurrence=once") == "/chores/create/?recurrence=once"
    assert next_at(client, "/chores/create/?recurrence=ONCE") == "/chores/create/"
    assert next_at(client, "/chores/create/?recurrence=x") == "/chores/create/"
    url = f"/chores/create/?template={mop.pk}&recurrence=weekly"
    assert next_at(client, url) == f"/chores/create/?template={mop.pk}"


def test_create_dashboard_link_is_already_canonical(client, mom, emma):
    url = (f"/chores/create/?date=2026-11-25&recurrence=once&return_to=dashboard"
           f"&week=2026-W48&member={emma.pk}&day=3")
    assert next_at(client, url) == url


@pytest.mark.parametrize("return_to", ["Dashboard", "%2F%2Fevil.example", "dashboard%00"])
def test_create_filters_need_exact_return_to(client, mom, return_to):
    assert next_at(client, f"/chores/create/?return_to={return_to}&week=2026-W48") == "/chores/create/"


def test_create_invalid_filters_leave_only_return_to(client, mom):
    url = "/chores/create/?return_to=dashboard&week=2026-W99&member=999&day=9"
    assert next_at(client, url) == "/chores/create/?return_to=dashboard"


def test_create_invalid_post_keeps_return_filters_only(client, mom, emma):
    data = {"title": "", "return_to": "dashboard", "week": "2026-W48", "member": str(emma.pk)}
    response = post(client, "/chores/create/?date=2026-12-24", data)
    assert "title" in response.context["form"].errors
    assert next_of(response) == f"/chores/create/?return_to=dashboard&week=2026-W48&member={emma.pk}"
    response = post(client, "/chores/create/?date=2026-12-24", {"title": ""})
    assert next_of(response) == "/chores/create/"


# --- Edit page -------------------------------------------------------------------


def test_edit_keeps_filters_and_occurrence(client, mom, emma, dishes):
    row = occ_pk(dishes)
    url = f"/chores/{dishes.pk}/edit/?week=2026-W46&member={emma.pk}&day=4&occurrence={row}"
    assert next_at(client, url) == url
    messy = f"/chores/{dishes.pk}/edit/?occurrence=00{row}&member=0{emma.pk}"
    assert next_at(client, messy) == f"/chores/{dishes.pk}/edit/?member={emma.pk}&occurrence={row}"


def test_edit_drops_junk(client, mom, dishes):
    url = f"/chores/{dishes.pk}/edit/?week=2026-W99&member=abc&day=9&date=2026-12-24&x=1"
    assert next_at(client, url) == f"/chores/{dishes.pk}/edit/"


def test_one_off_edit_ignores_occurrence(client, mom, vacuum):
    url = f"/chores/{vacuum.pk}/edit/?occurrence=garbage&week=2026-W48"
    assert next_at(client, url) == f"/chores/{vacuum.pk}/edit/?week=2026-W48"


def test_edit_invalid_post_keeps_filters_and_occurrence(client, mom, dishes):
    row = occ_pk(dishes)
    base = {"week": "2026-W46", "occurrence": str(row)}
    expected = f"/chores/{dishes.pk}/edit/?week=2026-W46&occurrence={row}"
    future = {"title": "", "scope": "future", **base}
    assert next_of(post(client, f"/chores/{dishes.pk}/edit/", future)) == expected
    this = {"scope": "this", "points": "7", **base}
    assert next_of(post(client, f"/chores/{dishes.pk}/edit/", this)) == expected


# --- Other pages -----------------------------------------------------------------


def test_default_pages_use_their_path(client, mom, mop):
    household = Chore.objects.create(title="Wipe", start_date=datetime.date(2026, 11, 1), is_template=True)
    assert next_at(client, "/progress/?x=1") == "/progress/"
    assert next_at(client, "/chores/templates/?x=1") == "/chores/templates/"
    edit = f"/chores/templates/{household.pk}/edit/"
    assert next_at(client, edit + "?x=1") == edit


def test_htmx_template_delete_rerender_points_at_the_list(client, mom, mop):
    other = Chore.objects.create(title="Wipe", start_date=datetime.date(2026, 11, 1), is_template=True)
    response = post(client, f"/chores/templates/{other.pk}/delete/", {}, **HTMX)
    assert next_of(response) == "/chores/templates/"


# --- Hostile values --------------------------------------------------------------


def pages(mom, emma, dishes, vacuum, mop):
    household = Chore.objects.create(title="Wipe", start_date=datetime.date(2026, 11, 1), is_template=True)
    return {
        "dashboard": ("/dashboard/", 200),
        "calendar": ("/calendar/", 200),
        "family": ("/family/", 200),
        "contribution": ("/contribution/", 200),
        "history": ("/history/", 200),
        "progress": ("/progress/", 200),
        "chore_create": ("/chores/create/", 200),
        "chore_edit": (f"/chores/{vacuum.pk}/edit/", 200),
        "chore_edit_recurring": (f"/chores/{dishes.pk}/edit/", 404),
        "template_list": ("/chores/templates/", 200),
        "template_edit": (f"/chores/templates/{household.pk}/edit/", 200),
    }


@pytest.mark.parametrize("name", [
    "dashboard", "calendar", "family", "contribution", "history", "progress", "chore_create",
    "chore_edit", "chore_edit_recurring", "template_list", "template_edit",
])
def test_junk_query_is_never_echoed(client, mom, emma, dishes, vacuum, mop, name):
    path, status = pages(mom, emma, dishes, vacuum, mop)[name]
    response = get(client, f"{path}?{JUNK}")
    assert response.status_code == status
    if status == 404:  # the page is not rendered; the same page without the junk occurrence works
        return
    nav = NAV.search(response.content.decode()).group(0)
    for bad in FORBIDDEN:
        assert bad not in nav and bad not in html.unescape(nav), bad
    assert next_of(response) == path


def test_recurring_edit_page_has_a_canonical_next_without_junk(client, mom, dishes):
    assert next_at(client, f"/chores/{dishes.pk}/edit/") == f"/chores/{dishes.pk}/edit/"


@pytest.mark.parametrize("url", [
    "/progress/?x=1",
    "/chores/templates/?x=1",
    "/chores/create/?date=2026-11-18&template=zzz",
    "/chores/create/?return_to=dashboard&week=2026-W48",
    "/chores/EDIT/?week=2026-W46",
])
def test_switch_round_trip_changes_nothing(client, mom, emma, dishes, url):
    url = url.replace("EDIT", f"{dishes.pk}/edit")
    value = next_at(client, url)
    response = post(client, f"/profile/{emma.pk}/", {"next": value})
    assert response.status_code == 302
    assert response["Location"] == value
    assert next_of(get(client, response["Location"])) == value


def test_switch_still_rejects_foreign_next(client, mom):
    response = post(client, f"/profile/{mom.pk}/", {"next": "//evil.example/"})
    assert response["Location"] == "/"


# --- Guard and code --------------------------------------------------------------

GET_PAGES = {
    "dashboard", "progress", "calendar", "family", "chore_create", "chore_edit",
    "template_list", "template_edit", "contribution", "history",
}
POST_ONLY = {
    "member_add", "member_edit", "member_move", "member_delete", "occurrence_toggle",
    "occurrence_assign", "planned_assign", "planned_move", "chore_delete", "template_delete", "profile_switch",
    "chore_delete_undo",
}
REDIRECTS = {"home"}


def test_every_url_name_is_classified_once():
    names = [p.name for p in urls.urlpatterns]
    groups = [GET_PAGES, POST_ONLY, REDIRECTS]
    assert len(names) == len(set(names))
    assert set(names) == GET_PAGES | POST_ONLY | REDIRECTS, set(names) ^ (GET_PAGES | POST_ONLY | REDIRECTS)
    assert sum(len(g) for g in groups) == len(names)


def test_every_get_page_is_in_the_hostile_test():
    covered = {"dashboard", "calendar", "family", "contribution", "history", "progress",
               "chore_create", "chore_edit", "template_list", "template_edit"}
    assert covered == GET_PAGES


def test_no_raw_query_fallback_remains():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "chores"
    assert not [p for p in root.rglob("*") if p.is_file() and p.suffix in {".py", ".html"}
                and "get_full_path" in p.read_text()]
    template = (root / "templates/chores/_profile_switcher.html").read_text()
    assert 'value="{{ switcher_next }}"' in template


def test_reverse_names_resolve():
    assert reverse("chore_create") == "/chores/create/"
