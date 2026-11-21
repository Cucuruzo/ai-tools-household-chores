"""Several family members selected in the header filter the dashboard and calendar (#55)."""
import html
import re

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import dashboard as dash
from chores.models import Chore, ChoreOccurrence, Member
from chores.profile import (
    LEGACY_KEY, MAX_SELECTED, SESSION_KEY, get_active_member, get_selected_members, toggle_member,
)
from tests.test_dashboard import WED, at, blank_csrf, cards, chore, d, occ, sections

pytestmark = pytest.mark.django_db

KEY = "active_member_ids"
LEGACY = "active_member_id"
HTMX = {"HTTP_HX_REQUEST": "true"}
NAV = re.compile(r'<nav id="profile-switcher"[^>]*>.*?</nav>', re.S)
OOB_NAV = re.compile(r'<nav id="profile-switcher" hx-swap-oob="true".*?</nav>', re.S)
NEXT = re.compile(r'name="next" value="([^"]*)"')
PRESSED = re.compile(r'action="/profile/(\d+)/">(?:(?!</form>).)*aria-pressed="true"', re.S)
HINT = '<li id="selection-hint">Done is not credited while several are selected</li>'
NOTE = re.compile(r'<p id="header-filter">(.*?)</p>', re.S)
LIST = re.compile(r'<div id="dashboard-list">.*</div>', re.S)
GRID = re.compile(r'<section id="calendar-grid">.*?</table>.*?</section>\s*(?=<|$)', re.S)
SELECT = re.compile(r'<select id="filter-member" name="member">(.*?)</select>', re.S)
OPTION = re.compile(r'<option value="([^"]*)"( selected)?>(.*?)</option>')
DOT = re.compile(r'<span class="dot dot-[a-z]+" title="([^"]*) · [^"]*">')
CREDIT = re.compile(r'<p class="credit">(.*?)</p>', re.S)
SAT = "2026-11-21T12:00:00"
EVERYONE = '<option value="all" selected>Everyone</option>'
HOSTILE_ALL = ["ALL", "All", " all", "all ", "all\x00", "all,1", "", "alll", "al", "аll"]
INVALID_MEMBERS = ["abc", "0", "-3", "3.0", " 3", "٣", "99999999999999999999",
                   "Unassigned", "UNASSIGNED", "x" * 10000, "<script>alert(1)</script>", "3\x00"]


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def fam():
    ann = Member.objects.create(name="Ann", role="adult", avatar="A", order=1)
    bob = Member.objects.create(name="Bob", role="adult", avatar="B", order=2)
    kid = Member.objects.create(name="Kid", role="kid", avatar="K", order=3)
    return ann, bob, kid


@pytest.fixture
def jobs(fam):
    """One-offs on Wed 18 Nov 2026 (the issue's consequences)."""
    ann, bob, kid = fam
    return {
        "AnnJob": occ("AnnJob", "2026-11-18", member=ann),
        "BobJob": occ("BobJob", "2026-11-18", member=bob),
        "KidJob": occ("KidJob", "2026-11-18", member=kid),
        "NoOne": occ("NoOne", "2026-11-18"),
    }


# --- Helpers ---------------------------------------------------------------------


def click(client, member, **data):
    extra = data.pop("extra", {})
    return client.post(f"/profile/{member.pk}/", data, **extra)


def select(client, *members):
    for m in members:
        assert click(client, m).status_code == 302


def set_value(client, key, value):
    session = client.session
    session[key] = value
    session.save()


def get(client, url, data=None, when=WED, tz=None, **extra):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        response = client.get(url, data or {}, **extra)
    assert response.status_code == 200, url
    return response.content.decode()


def dash_body(client, when=WED, **data):
    return get(client, "/dashboard/", data, when=when)


def shown(body):
    """Sorted titles of every card in the dashboard list."""
    return sorted(c["title"] for c in cards(LIST.search(body).group(0)))


def note(body):
    m = NOTE.search(body)
    return " ".join(html.unescape(m.group(1)).split()) if m else None


def nav_of(body):
    return NAV.search(body).group(0)


def nexts(fragment):
    return {html.unescape(v) for v in NEXT.findall(fragment)}


def pressed(body):
    return [int(pk) for pk in PRESSED.findall(nav_of(body))]


def options(body):
    return [(v, bool(s), label) for v, s, label in OPTION.findall(SELECT.search(body).group(1))]


def dots(body, day):
    """Titles of the calendar dots in the cell of ``day`` (1-31) of the shown month."""
    cell = re.search(rf'<td[^>]*>\s*<a class="day-number"[^>]*>{day}</a>(.*?)</td>', body, re.S)
    return sorted(html.unescape(t) for t in DOT.findall(cell.group(1)))


def all_dots(body):
    return sorted(html.unescape(t) for t in DOT.findall(body))


def credit_text(fragment):
    m = CREDIT.search(fragment)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).split()) if m else None


def toggle(client, pk, action="done", htmx=True, **data):
    with at(WED):
        return client.post(f"/dashboard/toggle/{pk}/", {"action": action, **data}, **(HTMX if htmx else {}))


def assign(client, pk, assignee, htmx=True, **data):
    with at(WED):
        return client.post(f"/dashboard/assign/{pk}/", {"assignee": assignee, **data},
                           **(HTMX if htmx else {}))


def queries(client, url, data=None):
    with CaptureQueriesContext(connection) as captured:
        get(client, url, data)
    return len(captured)


# --- Consequences (today Wed 18 Nov 2026) --------------------------------------------


def test_click_ann_then_bob(client, fam, jobs):
    ann, bob, _ = fam
    select(client, ann, bob)
    assert client.session[KEY] == [ann.pk, bob.pk]
    body = dash_body(client)
    assert pressed(body) == [ann.pk, bob.pk]
    assert HINT in nav_of(body)
    assert "Who are you?" not in nav_of(body)
    assert shown(body) == ["AnnJob", "BobJob"]
    assert note(body) == "Showing chores of Ann, Bob · chosen in the header"
    cal = get(client, "/calendar/")
    assert dots(cal, 18) == ["AnnJob", "BobJob"]
    assert note(cal) == "Showing chores of Ann, Bob · chosen in the header"


def test_note_names_are_in_family_order_not_click_order(client, fam, jobs):
    ann, bob, _ = fam
    select(client, bob, ann)
    assert client.session[KEY] == [bob.pk, ann.pk]
    body = dash_body(client)
    assert note(body) == "Showing chores of Ann, Bob · chosen in the header"
    assert pressed(body) == [ann.pk, bob.pk]


def test_note_is_escaped(client, fam, jobs):
    evil = Member.objects.create(name="<b>x</b>", role="kid", avatar="X", order=9)
    select(client, evil)
    body = dash_body(client)
    assert '<p id="header-filter">Showing chores of &lt;b&gt;x&lt;/b&gt; · chosen in the header</p>' in body


def test_explicit_member_and_unassigned_win_without_note(client, fam, jobs):
    ann, bob, kid = fam
    select(client, ann, bob)
    body = dash_body(client, member=str(kid.pk))
    assert shown(body) == ["KidJob"] and note(body) is None
    body = dash_body(client, member="unassigned")
    assert shown(body) == ["NoOne"] and note(body) is None


def test_everyone_overrides_selection(client, fam, jobs):
    ann, bob, _ = fam
    select(client, ann, bob)
    body = dash_body(client, member="all")
    assert shown(body) == ["AnnJob", "BobJob", "KidJob", "NoOne"]
    assert note(body) is None
    assert EVERYONE in body
    assert nexts(nav_of(body)) == {"/dashboard/?member=all"}
    assert 'href="/dashboard/?week=2026-W48&amp;member=all" aria-label="Next week"' in body
    back = dash_body(client, member="")
    assert shown(back) == ["AnnJob", "BobJob"]
    assert '<option value="" selected>Chosen in the header</option>' in back
    assert nexts(nav_of(back)) == {"/dashboard/"}


def test_nobody_selected_options_and_everyone(client, fam, jobs):
    ann, bob, kid = fam
    body = dash_body(client)
    assert options(body) == [
        ("all", True, "Everyone"), (str(ann.pk), False, "Ann"), (str(bob.pk), False, "Bob"),
        (str(kid.pk), False, "Kid"), ("unassigned", False, "Unassigned"),
    ]
    assert shown(body) == ["AnnJob", "BobJob", "KidJob", "NoOne"] and note(body) is None
    body = dash_body(client, member="all")
    assert shown(body) == ["AnnJob", "BobJob", "KidJob", "NoOne"] and note(body) is None
    assert nexts(nav_of(body)) == {"/dashboard/?member=all"}


@pytest.mark.parametrize("extra", [HTMX, {}], ids=["htmx", "no-js"])
def test_credit_story(client, fam, jobs, extra):
    ann, bob, _ = fam
    select(client, ann, bob)
    a = jobs["AnnJob"]
    response = toggle(client, a.pk, htmx=bool(extra))
    a.refresh_from_db()
    assert a.status == "done" and a.completed_by is None
    body = response.content.decode() if extra else dash_body(client)
    assert credit_text(body) == "Done · not credited"

    click(client, bob)
    assert client.session[KEY] == [ann.pk]
    assert HINT not in nav_of(dash_body(client))
    b = jobs["BobJob"]
    ChoreOccurrence.objects.filter(pk=b.pk).update(member=ann)
    toggle(client, b.pk, htmx=bool(extra))
    b.refresh_from_db()
    assert b.completed_by == ann

    click(client, ann)
    assert KEY not in client.session
    body = dash_body(client)
    assert "Who are you?" in nav_of(body)
    assert shown(body) == ["AnnJob", "BobJob", "KidJob", "NoOne"]


def test_old_session_migrates(client, fam, jobs):
    _, bob, _ = fam
    set_value(client, LEGACY, bob.pk)
    body = dash_body(client)
    assert client.session[KEY] == [bob.pk] and LEGACY not in client.session
    assert pressed(body) == [bob.pk]
    assert HINT not in body
    toggle(client, jobs["BobJob"].pk)
    assert ChoreOccurrence.objects.get(pk=jobs["BobJob"].pk).completed_by == bob


# --- Toggle and redirect ---------------------------------------------------------------


@pytest.fixture
def pages(fam):
    ann, bob, kid = fam
    one_off = chore("Vacuum", start_date="2026-11-18")
    template = chore("Wipe", start_date="2026-11-01", is_template=True)
    return [
        "/dashboard/",
        "/dashboard/?member=all",
        f"/dashboard/?week=2026-W48&member={bob.pk}&day=3",
        "/dashboard/?week=2026-W48&member=all&day=3",
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
        "/chores/create/?date=2026-11-18&return_to=dashboard&member=all",
        f"/chores/{one_off.pk}/edit/",
        f"/chores/{one_off.pk}/edit/?week=2026-W48&member=all",
        "/chores/templates/",
        f"/chores/templates/{template.pk}/edit/",
    ]


def test_toggle_from_every_page_type(client, fam, pages):
    ann, bob, _ = fam
    for url in pages:
        select(client, ann)
        value = nexts(nav_of(get(client, url)))
        assert len(value) == 1, url
        value = value.pop()
        response = click(client, bob, next=value)
        assert response.status_code == 302 and response["Location"] == value, url
        assert client.session[KEY] == [ann.pk, bob.pk], url
        follow = get(client, response["Location"])
        assert nexts(nav_of(follow)) == {value}, url
        assert pressed(follow) == [ann.pk, bob.pk], url
        response = click(client, ann, next=value)
        assert response["Location"] == value and client.session[KEY] == [bob.pk], url
        click(client, bob)
        assert KEY not in client.session


@pytest.mark.parametrize("pk", ["999", str(2**63), "abc"])
def test_unknown_id_is_404_session_unchanged(client, fam, pk):
    ann, bob, _ = fam
    select(client, ann, bob)
    assert client.post(f"/profile/{pk}/", {"next": "/family/"}).status_code == 404
    assert client.session[KEY] == [ann.pk, bob.pk]


def test_get_is_405_session_unchanged(client, fam):
    ann, bob, _ = fam
    select(client, ann, bob)
    assert client.get(f"/profile/{ann.pk}/").status_code == 405
    assert client.session[KEY] == [ann.pk, bob.pk]


def test_missing_csrf_is_403(fam):
    ann, bob, _ = fam
    strict = Client(enforce_csrf_checks=True)
    set_value(strict, KEY, [ann.pk])
    assert strict.post(f"/profile/{bob.pk}/", {"next": "/family/"}).status_code == 403
    assert strict.session[KEY] == [ann.pk]


@pytest.mark.parametrize("bad", ["https://evil.example/", "//evil.example/", "/\\evil.example",
                                 "javascript:alert(1)"])
def test_hostile_next_redirects_home_and_toggles(client, fam, bad):
    ann, bob, _ = fam
    select(client, ann)
    response = click(client, bob, next=bad)
    assert response["Location"] == "/"
    assert client.session[KEY] == [ann.pk, bob.pk]


def test_hostile_referer_redirects_home_and_toggles(client, fam):
    ann, bob, _ = fam
    select(client, ann)
    response = click(client, bob, extra={"HTTP_REFERER": "https://evil.example/x"})
    assert response["Location"] == "/"
    assert client.session[KEY] == [ann.pk, bob.pk]


def test_switcher_markup_with_several(client, fam):
    ann, bob, kid = fam
    select(client, ann, bob)
    nav = nav_of(get(client, "/family/"))
    for m in (ann, bob):
        assert (
            '<button type="submit" class="profile active" aria-pressed="true" '
            f'title="{m.name} (click to deselect)" aria-current="true">\n'
            f'                <span class="avatar">{m.avatar}</span> <strong>{m.name}</strong>'
        ) in nav
    assert nav.count('<button type="submit" class="profile outline secondary">') == 1
    assert nav.count("<li") == 4 and "Who are you?" not in nav
    assert nav.index(HINT) > nav.index(f"/profile/{kid.pk}/")
    assert "hx-" not in nav
    assert nav.count("<form method=\"post\"") == 3


# --- Session hygiene -------------------------------------------------------------------


def test_mixed_list_is_cleaned(client, fam):
    ann, bob, _ = fam
    gone = Member.objects.create(name="Gone", role="kid", avatar="G")
    gone_pk = gone.pk
    gone.delete()
    set_value(client, KEY, [ann.pk, "abc", 1.5, True, None, 0, -1, 2**63, ann.pk, gone_pk])
    body = get(client, "/family/")
    assert pressed(body) == [ann.pk]
    assert client.session[KEY] == [ann.pk]


def test_digit_strings_are_stored_back_as_ints(client, fam):
    ann, bob, _ = fam
    set_value(client, KEY, [str(bob.pk), "0" * 5 + str(ann.pk)])
    get(client, "/family/")
    assert client.session[KEY] == [bob.pk, ann.pk]


@pytest.mark.parametrize("value", ["abc", 3, {"a": 1}, None, True, "", [], ["abc", None], [999]],
                         ids=["str", "int", "dict", "none", "bool", "empty", "empty-list", "junk-list", "stale"])
def test_non_list_or_nothing_valid_removes_key(client, fam, value):
    set_value(client, KEY, value)
    nav = nav_of(get(client, "/family/"))
    assert "Who are you?" in nav and "aria-pressed" not in nav
    assert KEY not in client.session and LEGACY not in client.session


def test_list_is_capped_at_fifty(client, fam):
    extra = [Member.objects.create(name=f"M{i}", role="kid", avatar="x") for i in range(57)]
    everyone = list(fam) + extra
    assert len(everyone) == 60
    set_value(client, KEY, [m.pk for m in everyone])
    body = get(client, "/family/")
    assert client.session[KEY] == [m.pk for m in everyone[:MAX_SELECTED]]
    assert len(pressed(body)) == MAX_SELECTED
    response = click(client, everyone[55], next="/family/")
    assert response.status_code == 302
    assert client.session[KEY] == [m.pk for m in everyone[:MAX_SELECTED]]
    click(client, everyone[0])
    assert client.session[KEY] == [m.pk for m in everyone[1:MAX_SELECTED]]


def test_huge_list_parses_only_what_it_needs(client, fam):
    ann, _, _ = fam
    set_value(client, KEY, [ann.pk] + ["abc"] * 100_000)
    get(client, "/family/")
    assert client.session[KEY] == [ann.pk]


def test_helpers_on_a_request(rf, fam):
    from django.contrib.sessions.backends.db import SessionStore

    ann, bob, kid = fam
    request = rf.get("/")
    request.session = SessionStore()
    assert get_selected_members(request) == [] and get_active_member(request) is None
    toggle_member(request, kid)
    toggle_member(request, ann)
    assert request.session[SESSION_KEY] == [kid.pk, ann.pk]
    assert get_selected_members(request) == [ann, kid]
    assert get_active_member(request) is None
    toggle_member(request, kid)
    assert get_active_member(request) == ann
    toggle_member(request, ann)
    assert SESSION_KEY not in request.session and get_selected_members(request) == []
    assert SESSION_KEY == KEY and LEGACY_KEY == LEGACY and MAX_SELECTED == 50


def test_selection_is_read_with_one_query_per_request(rf, fam):
    from django.contrib.sessions.backends.db import SessionStore

    ann, bob, _ = fam
    request = rf.get("/")
    request.session = SessionStore()
    request.session[SESSION_KEY] = [ann.pk, bob.pk]
    with CaptureQueriesContext(connection) as captured:
        first = get_selected_members(request)
        second = get_selected_members(request)
    assert first == second == [ann, bob]
    assert len(captured) == 1
    request.session[SESSION_KEY] = [bob.pk]  # a changed value is read again
    assert get_selected_members(request) == [bob]


# --- Legacy key ------------------------------------------------------------------------


def test_legacy_key_alone_migrates(client, fam):
    _, bob, _ = fam
    set_value(client, LEGACY, bob.pk)
    get(client, "/family/")
    assert client.session[KEY] == [bob.pk] and LEGACY not in client.session


@pytest.mark.parametrize("value", [999, "abc", 1.5, True, None, 0, 2**63])
def test_stale_or_invalid_legacy_leaves_neither_key(client, fam, value):
    set_value(client, LEGACY, value)
    nav = nav_of(get(client, "/family/"))
    assert "Who are you?" in nav
    assert KEY not in client.session and LEGACY not in client.session


def test_both_keys_list_wins(client, fam):
    ann, bob, _ = fam
    session = client.session
    session[KEY] = [ann.pk]
    session[LEGACY] = bob.pk
    session.save()
    assert pressed(get(client, "/family/")) == [ann.pk]
    assert client.session[KEY] == [ann.pk] and LEGACY not in client.session


def test_legacy_then_toggle_adds(client, fam):
    ann, bob, _ = fam
    set_value(client, LEGACY, ann.pk)
    click(client, bob)
    assert client.session[KEY] == [ann.pk, bob.pk] and LEGACY not in client.session


# --- Deleting a selected member --------------------------------------------------------


@pytest.mark.parametrize("extra", [HTMX, {}], ids=["htmx", "no-js"])
def test_deleting_selected_member_drops_them(client, fam, extra):
    ann, bob, kid = fam
    select(client, ann, bob, kid)
    response = client.post(f"/family/{bob.pk}/delete/", **extra)
    body = response.content.decode() if extra else get(client, response["Location"])
    assert pressed(body) == [ann.pk, kid.pk]
    assert HINT in nav_of(body)
    assert client.session[KEY] == [ann.pk, kid.pk]
    client.post(f"/family/{kid.pk}/delete/", **extra)
    body = get(client, "/family/")
    assert pressed(body) == [ann.pk] and HINT not in body


# --- Credit ------------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [HTMX, {}], ids=["htmx", "no-js"])
@pytest.mark.parametrize("how_many, credited", [(0, None), (1, "Ann"), (2, None), (3, None)])
def test_credit_by_selection_size(client, fam, jobs, extra, how_many, credited):
    select(client, *fam[:how_many])
    o = jobs["AnnJob"]
    response = toggle(client, o.pk, htmx=bool(extra), member="all")
    o.refresh_from_db()
    assert o.status == "done"
    assert (o.completed_by.name if o.completed_by else None) == credited
    body = response.content.decode() if extra else dash_body(client, member="all")
    article = re.search(rf'<article class="chore-card" id="occurrence-{o.pk}".*?</article>', body, re.S).group(0)
    assert credit_text(article) == ("Done by A Ann" if credited else "Done · not credited")


# --- member=all ----------------------------------------------------------------------------


@pytest.fixture
def weeks(fam):
    """Stored rows in a past week (W46), a weekly fixed chore per member planned
    for the future (W48), and the current week's jobs."""
    ann, bob, kid = fam
    rows = {
        "PastAnn": occ("PastAnn", "2026-11-11", status="done", member=ann),
        "PastBob": occ("PastBob", "2026-11-12", status="done", member=bob),
        "PastKid": occ("PastKid", "2026-11-11", status="done", member=kid),
        "PastNoOne": occ("PastNoOne", "2026-11-12", status="done"),
    }
    for name, m in (("Ann", ann), ("Bob", bob), ("Kid", kid), ("NoOne", None)):
        chore(f"Fut{name}", start_date="2026-11-23", recurrence="weekly", recurrence_day=2,
              mode="fixed" if m else "unassigned", assigned_to=m)
    return rows


ALL_CURRENT = ["AnnJob", "BobJob", "KidJob", "NoOne"]
ALL_PAST = ["PastAnn", "PastBob", "PastKid", "PastNoOne"]
ALL_FUTURE = ["FutAnn", "FutBob", "FutKid", "FutNoOne"]


@pytest.mark.parametrize("how_many", [0, 1, 2])
def test_member_all_shows_every_card(client, fam, jobs, weeks, how_many):
    select(client, *fam[:how_many])
    for week, expected in ((None, ALL_CURRENT), ("2026-W46", ALL_PAST), ("2026-W48", ALL_FUTURE)):
        data = {"member": "all", **({"week": week} if week else {})}
        body = dash_body(client, **data)
        assert shown(body) == expected, week
        assert note(body) is None
        assert EVERYONE in body
        if how_many:
            assert '<option value="">Chosen in the header</option>' in body
    body = dash_body(client, member="all", week="2026-W46", day="4")
    assert shown(body) == ["PastBob", "PastNoOne"]
    body = dash_body(client, member="all", day="3")
    assert shown(body) == ALL_CURRENT


def test_member_all_is_kept_in_every_link(client, fam, jobs):
    ann, bob, _ = fam
    select(client, ann, bob)
    body = dash_body(client, member="all", week="2026-W46", day="3")
    assert nexts(nav_of(body)) == {"/dashboard/?week=2026-W46&member=all&day=3"}
    assert 'href="/dashboard/?week=2026-W45&amp;member=all&amp;day=3" aria-label="Previous week"' in body
    assert 'href="/dashboard/?week=2026-W47&amp;member=all&amp;day=3" aria-label="Next week"' in body
    assert 'id="this-week" href="/dashboard/?member=all&amp;day=3"' in body
    assert ('href="/chores/create/?date=2026-11-11&amp;recurrence=once&amp;return_to=dashboard'
            '&amp;week=2026-W46&amp;member=all&amp;day=3"') in body
    current = dash_body(client, member="all")
    forms = re.findall(r'<form[^>]*action="/dashboard/(?:toggle|assign)/\d+/"[^>]*>(.*?)</form>', current, re.S)
    assert forms
    for form in forms:
        assert '<input type="hidden" name="member" value="all">' in form


def test_member_all_no_js_redirects_keep_it(client, fam, jobs):
    ann, bob, kid = fam
    select(client, ann, bob)
    response = toggle(client, jobs["KidJob"].pk, htmx=False, member="all", week="2026-W47")
    assert response.status_code == 302
    assert response["Location"] == "/dashboard/?week=2026-W47&member=all"
    response = assign(client, jobs["NoOne"].pk, str(kid.pk), htmx=False, member="all")
    assert response["Location"] == "/dashboard/?member=all"


def test_member_all_on_create_and_edit_pages(client, fam, jobs):
    ann, _, _ = fam
    select(client, ann)
    body = get(client, "/chores/create/", {"date": "2026-11-18", "return_to": "dashboard", "member": "all"})
    assert 'id="back-to-dashboard" href="/dashboard/?member=all"' in body
    assert '<input type="hidden" name="member" value="all">' in body
    c = jobs["AnnJob"].chore
    body = get(client, f"/chores/{c.pk}/edit/", {"member": "all", "week": "2026-W48"})
    assert 'id="back-to-dashboard" href="/dashboard/?week=2026-W48&amp;member=all"' in body
    assert '<input type="hidden" name="member" value="all">' in body


def test_member_all_htmx_assign_outside_selection_does_not_rerender(client, fam, jobs):
    ann, bob, kid = fam
    select(client, ann, bob)
    response = assign(client, jobs["AnnJob"].pk, str(kid.pk), member="all")
    assert response.status_code == 200
    assert "HX-Retarget" not in response
    assert response.content.decode().strip().startswith('<article class="chore-card"')


# --- Hostile and near-miss values -----------------------------------------------------------


@pytest.mark.parametrize("value", HOSTILE_ALL + INVALID_MEMBERS)
def test_near_miss_values_fall_back_to_selection(client, fam, jobs, value):
    ann, bob, _ = fam
    select(client, ann, bob)
    body = dash_body(client, member=value)
    assert shown(body) == ["AnnJob", "BobJob"]
    assert note(body) == "Showing chores of Ann, Bob · chosen in the header"
    assert '<option value="" selected>Chosen in the header</option>' in body
    assert nexts(nav_of(body)) == {"/dashboard/"}
    # Never echoed: no link, hidden field or option carries the value.
    assert '<input type="hidden" name="member"' not in body
    assert not [h for h in re.findall(r'(?:href|hx-get)="([^"]*)"', body) if "member=" in h]


@pytest.mark.parametrize("value", HOSTILE_ALL)
def test_near_miss_all_is_not_parsed(value):
    from chores.profile import _parse_pk

    assert dash.parse_member_filter(value, _parse_pk) not in (dash.ALL_FILTER,)
    assert dash.parse_member_filter("all", _parse_pk) == dash.ALL_FILTER == "all"


def test_repeated_member_params_last_wins(client, fam, jobs):
    ann, bob, kid = fam
    select(client, ann, bob)
    with at(WED):
        body = client.get(f"/dashboard/?member=all&member={kid.pk}").content.decode()
    assert shown(body) == ["KidJob"] and note(body) is None
    with at(WED):
        body = client.get(f"/dashboard/?member={kid.pk}&member=all").content.decode()
    assert shown(body) == ALL_CURRENT and note(body) is None
    with at(WED):
        body = client.get("/dashboard/?member=all&member=ALL").content.decode()
    assert shown(body) == ["AnnJob", "BobJob"] and note(body) is not None


# --- Member select markup -------------------------------------------------------------------


@pytest.mark.parametrize("param", [None, "all", "pk", "unassigned"])
@pytest.mark.parametrize("selected", [False, True])
def test_member_select_markup(client, fam, param, selected):
    ann, bob, kid = fam
    if selected:
        select(client, ann, bob)
    value = str(bob.pk) if param == "pk" else param
    body = dash_body(client, **({"member": value} if value else {}))
    got = SELECT.search(body).group(1)
    sel = {None: "", "all": "all", "pk": str(bob.pk), "unassigned": "unassigned"}[param]
    if not selected and sel == "":
        sel = "all"
    expected = []
    if selected:
        expected.append(("", "Chosen in the header"))
    expected += [("all", "Everyone"), (str(ann.pk), "Ann"), (str(bob.pk), "Bob"),
                 (str(kid.pk), "Kid"), ("unassigned", "Unassigned")]
    for v, label in expected:
        mark = " selected" if v == sel else ""
        assert f'<option value="{v}"{mark}>{label}</option>' in got
    assert options(body) == [(v, v == sel, label) for v, label in expected]


# --- History and calendar ignore all -------------------------------------------------------


@pytest.mark.parametrize("how_many", [0, 2])
def test_history_member_all_is_no_filter(client, fam, jobs, how_many):
    select(client, *fam[:how_many])
    for o in jobs.values():
        ChoreOccurrence.objects.filter(pk=o.pk).update(status="done")
    plain = get(client, "/history/")
    with_all = get(client, "/history/", {"member": "all"})
    assert scrub(with_all) == scrub(plain)
    assert nexts(nav_of(with_all)) == {"/history/"}
    assert '<option value="" selected>Everyone</option>' in with_all


def test_calendar_member_all_is_ignored(client, fam, jobs):
    ann, bob, _ = fam
    select(client, ann, bob)
    plain = get(client, "/calendar/")
    with_all = get(client, "/calendar/", {"member": "all"})
    assert scrub(with_all) == scrub(plain)
    assert dots(with_all, 18) == ["AnnJob", "BobJob"]
    assert nexts(nav_of(with_all)) == {"/calendar/"}
    with_month = get(client, "/calendar/", {"month": "2026-12", "member": "all"})
    assert nexts(nav_of(with_month)) == {"/calendar/?month=2026-12"}


def test_calendar_day_links_carry_no_member(client, fam, jobs):
    select(client, fam[0])
    body = get(client, "/calendar/")
    links = re.findall(r'class="day-number" href="([^"]*)"', body)
    assert links and all("member" not in link for link in links)


# --- Dashboard weeks follow the selection ---------------------------------------------------


@pytest.fixture
def current_week(fam):
    ann, bob, kid = fam
    occ("OldAnn", "2026-11-02", member=ann)
    occ("OldKid", "2026-11-02", member=kid)
    occ("MonDoneAnn", "2026-11-16", status="done", member=ann)
    occ("MonDoneKid", "2026-11-16", status="done", member=kid)
    occ("TodayBob", "2026-11-18", member=bob)
    occ("TodayNoOne", "2026-11-18")
    occ("FriAnn", "2026-11-20", member=ann)
    occ("FriKid", "2026-11-20", member=kid)


def section_titles(body):
    return {k: [c["title"] for c in v] for k, v in sections(body).items()}


def test_current_week_sections_follow_selection(client, fam, current_week):
    ann, bob, kid = fam
    select(client, ann, bob)
    assert section_titles(dash_body(client)) == {
        "overdue": ["OldAnn"], "today": ["TodayBob"], "upcoming": ["FriAnn"],
        "done-earlier": ["MonDoneAnn"],
    }
    assert section_titles(dash_body(client, day="5")) == {
        "overdue": [], "today": [], "upcoming": ["FriAnn"],
    }
    assert section_titles(dash_body(client, member="unassigned")) == {
        "overdue": [], "today": ["TodayNoOne"], "upcoming": [],
    }
    assert section_titles(dash_body(client, member=str(kid.pk), day="5")) == {
        "overdue": [], "today": [], "upcoming": ["FriKid"],
    }


def test_past_and_future_weeks_follow_selection(client, fam, weeks):
    ann, bob, kid = fam
    select(client, ann, kid)
    past = dash_body(client, week="2026-W46")
    assert shown(past) == ["PastAnn", "PastKid"]
    assert note(past) == "Showing chores of Ann, Kid · chosen in the header"
    future = dash_body(client, week="2026-W48")
    assert shown(future) == ["FutAnn", "FutKid"]
    assert all(c["status"] == "Planned" for c in cards(LIST.search(future).group(0)))
    assert shown(dash_body(client, week="2026-W46", day="4")) == []
    assert shown(dash_body(client, week="2026-W48", member="unassigned")) == ["FutNoOne"]
    assert shown(dash_body(client, week="2026-W46", member="unassigned")) == ["PastNoOne"]
    assert shown(dash_body(client, week="2026-W48", member=str(bob.pk))) == ["FutBob"]


# --- HTMX -------------------------------------------------------------------------------------


TOKEN = re.compile(r'("X-CSRFToken": ")[^"]*"')


def scrub(body):
    return TOKEN.sub(r'\1"', blank_csrf(body))


def list_of(body):
    return blank_csrf(LIST.search(body).group(0))


@pytest.mark.parametrize("data", [
    {}, {"day": "3"}, {"member": "all"}, {"member": "unassigned"}, {"week": "2026-W46"},
    {"week": "2026-W48"}, {"week": "2026-W48", "member": "all", "day": "3"},
])
def test_htmx_list_matches_full_page(client, fam, jobs, weeks, data):
    ann, bob, _ = fam
    select(client, ann, bob)
    full = dash_body(client, **data)
    partial = get(client, "/dashboard/", data, **HTMX)
    assert partial.strip().startswith('<div id="dashboard-list">')
    assert list_of(partial) == list_of(full)
    oob = OOB_NAV.search(partial).group(0)
    assert nexts(oob) == nexts(nav_of(full))
    assert PRESSED.findall(oob) == [str(ann.pk), str(bob.pk)] and HINT in oob


def test_toggle_sibling_oob_cards_limited_to_shown_set(client, fam):
    ann, bob, _ = fam
    c = chore("Daily", start_date="2026-11-16", recurrence="daily")
    rows = {}
    for day, m in (("2026-11-16", ann), ("2026-11-17", bob), ("2026-11-18", ann), ("2026-11-19", bob),
                   ("2026-11-20", None)):
        rows[day] = ChoreOccurrence.objects.create(chore=c, title="Daily", date=d(day), member=m, points=10)
    select(client, ann)
    response = toggle(client, rows["2026-11-18"].pk)
    body = response.content.decode()
    oob = [int(pk) for pk in re.findall(r'id="occurrence-(\d+)" hx-swap-oob="true"', body)]
    assert oob == [rows["2026-11-16"].pk]
    select(client, bob)  # Ann + Bob
    response = toggle(client, rows["2026-11-18"].pk, action="undo")
    oob = sorted(int(pk) for pk in re.findall(r'id="occurrence-(\d+)" hx-swap-oob="true"', response.content.decode()))
    assert oob == sorted([rows["2026-11-16"].pk, rows["2026-11-17"].pk, rows["2026-11-19"].pk])
    response = toggle(client, rows["2026-11-18"].pk, member="all")
    oob = sorted(int(pk) for pk in re.findall(r'id="occurrence-(\d+)" hx-swap-oob="true"', response.content.decode()))
    assert len(oob) == 4


def test_assign_out_of_selection_rerenders_list(client, fam, jobs):
    ann, bob, kid = fam
    select(client, ann, bob)
    response = assign(client, jobs["AnnJob"].pk, str(bob.pk))
    assert "HX-Retarget" not in response
    response = assign(client, jobs["AnnJob"].pk, str(kid.pk))
    assert response["HX-Retarget"] == "#dashboard-list" and response["HX-Reswap"] == "outerHTML"
    body = response.content.decode()
    assert shown(body) == ["BobJob"]
    assert note(body) == "Showing chores of Ann, Bob · chosen in the header"
    assert list_of(body) == list_of(dash_body(client))
    response = assign(client, jobs["NoOne"].pk, "", member="unassigned")
    assert "HX-Retarget" not in response
    response = assign(client, jobs["KidJob"].pk, "")
    assert "HX-Retarget" in response


def test_planned_assign_out_of_selection_rerenders_list(client, fam, weeks):
    ann, bob, kid = fam
    select(client, ann, bob)
    fut = Chore.objects.get(title="FutAnn")
    url = f"/dashboard/assign/planned/{fut.pk}/"
    with at(WED):
        response = client.post(url, {"date": "2026-11-25", "assignee": str(bob.pk), "week": "2026-W48"}, **HTMX)
    assert "HX-Retarget" not in response
    with at(WED):
        response = client.post(url, {"date": "2026-11-25", "assignee": str(kid.pk), "week": "2026-W48"}, **HTMX)
    assert response["HX-Retarget"] == "#dashboard-list"
    body = response.content.decode()
    assert shown(body) == ["FutBob"]
    assert list_of(body) == list_of(dash_body(client, week="2026-W48"))
    with at(WED):
        response = client.post(url, {"date": "2026-11-25", "assignee": "", "week": "2026-W48",
                                      "member": "all"}, **HTMX)
    assert "HX-Retarget" not in response


# --- Empty states ------------------------------------------------------------------------------


def test_filtered_out_week_and_month_show_nothing_here(client, fam):
    ann, _, kid = fam
    occ("KidOnly", "2026-11-18", member=kid)
    occ("KidPast", "2026-11-11", member=kid)
    chore("KidFuture", start_date="2026-11-23", recurrence="weekly", recurrence_day=2,
          mode="fixed", assigned_to=kid)
    select(client, ann)
    for week in (None, "2026-W46", "2026-W48"):
        body = dash_body(client, **({"week": week} if week else {}))
        assert 'id="no-chores"' not in body, week
        assert "Nothing here" in body and shown(body) == [], week
    for month in (None, "2026-10", "2026-12"):
        body = get(client, "/calendar/", {"month": month} if month else {})
        if month == "2026-10":
            assert "No chores this month" in body
            continue
        assert "No chores this month" not in body and all_dots(body) == [], month
        assert note(body) == "Showing chores of Ann · chosen in the header"


def test_truly_empty_week_and_month_keep_their_empty_state(client, fam):
    select(client, fam[0])
    body = dash_body(client)
    assert "No chores this week" in body
    assert note(body) == "Showing chores of Ann · chosen in the header"
    assert "No chores this month" in get(client, "/calendar/")


# --- Calendar -----------------------------------------------------------------------------------


def test_calendar_months_follow_selection(client, fam, jobs, weeks):
    ann, bob, kid = fam
    select(client, bob, kid)
    cur = get(client, "/calendar/")
    assert dots(cur, 18) == ["BobJob", "KidJob"]
    assert dots(cur, 11) == ["PastKid"] and dots(cur, 12) == ["PastBob"]
    assert dots(cur, 25) == ["FutBob", "FutKid"]
    dec = get(client, "/calendar/", {"month": "2026-12"})
    assert dots(dec, 2) == ["FutBob", "FutKid"]
    assert note(dec) == "Showing chores of Bob, Kid · chosen in the header"
    swap = get(client, "/calendar/", {"month": "2026-12"}, **HTMX)
    assert swap.strip().startswith('<section id="calendar-grid">')
    assert dots(swap, 2) == ["FutBob", "FutKid"]
    assert note(swap) == "Showing chores of Bob, Kid · chosen in the header"
    assert PRESSED.findall(OOB_NAV.search(swap).group(0)) == [str(bob.pk), str(kid.pk)]
    assert nexts(OOB_NAV.search(swap).group(0)) == {"/calendar/?month=2026-12"}
    # Nothing selected: everyone, no note.
    click(client, bob)
    click(client, kid)
    cur = get(client, "/calendar/")
    assert note(cur) is None
    assert dots(cur, 25) == ["FutAnn", "FutBob", "FutKid"]  # three dots at most per cell
    assert dots(cur, 12) == ["PastBob", "PastNoOne"]


# --- Other pages are unaffected ------------------------------------------------------------------


@pytest.mark.parametrize("url", ["/contribution/", "/contribution/?period=week", "/progress/",
                                 "/history/", "/family/", "/chores/templates/"])
def test_other_pages_same_content(client, fam, jobs, url):
    for o in list(jobs.values())[:2]:
        ChoreOccurrence.objects.filter(pk=o.pk).update(status="done", completed_by=o.member)

    def content():
        body = get(client, url)
        return blank_csrf(re.search(r'<main class="container">(.*?)</main>', body, re.S).group(1))

    nobody = content()
    select(client, fam[0])
    one = content()
    select(client, fam[1], fam[2])
    three = content()
    assert nobody == one == three


# --- Query counts and time zone -------------------------------------------------------------------


@pytest.mark.parametrize("url, expected", [("/dashboard/", 14), ("/calendar/", 18)])
def test_query_count_same_for_one_and_three(client, fam, jobs, url, expected):
    get(client, url)  # materialize first
    select(client, fam[0])
    one = queries(client, url)
    select(client, fam[1], fam[2])
    three = queries(client, url)
    assert one == three == expected


def test_viewer_time_zone_decides_today(client, fam):
    ann, bob, _ = fam
    occ("AnnThu", "2026-11-19", member=ann)
    occ("BobThu", "2026-11-19", member=bob)
    select(client, ann)
    utc = section_titles(dash_body(client))
    assert utc["today"] == [] and utc["upcoming"] == ["AnnThu"]
    # 12:00 UTC on Wed is 02:00 on Thu in Kiritimati (UTC+14).
    body = get(client, "/dashboard/", tz="Pacific/Kiritimati")
    assert section_titles(body)["today"] == ["AnnThu"]
    cal = get(client, "/calendar/", tz="Pacific/Kiritimati")
    assert re.search(r'<td class="today" aria-current="date">\s*<a class="day-number"[^>]*>19</a>', cal)
    assert dots(cal, 19) == ["AnnThu"]


def test_no_js_needed(client, fam, jobs):
    ann, bob, _ = fam
    select(client, ann, bob)
    body = dash_body(client)
    nav = nav_of(body)
    assert "hx-" not in nav
    assert re.findall(r'<form method="post" action="/profile/\d+/">', nav)
    assert '<noscript><button type="submit">Apply</button></noscript>' in body
    assert 'id="header-filter"' in body
