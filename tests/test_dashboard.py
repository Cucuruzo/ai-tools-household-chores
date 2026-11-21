"""Weekly dashboard (#10)."""
import datetime
import html
import inspect
import re
from unittest import mock

import pytest
from django.db import connection
from django.test import Client

from chores import dashboard as dash
from chores import views
from chores.models import Chore, ChoreOccurrence, Member, RotationState
from chores.services import materialize_week

UTC = datetime.timezone.utc
WED = "2026-11-18T12:00:00"  # Wed 18 Nov 2026, current week 2026-W47 (Nov 16 - 22)
WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


def d(text):
    return datetime.date.fromisoformat(text)


def t(text):
    return datetime.time.fromisoformat(text)


def instant(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=UTC)


def at(text):
    return mock.patch("django.utils.timezone.now", return_value=instant(text))


@pytest.fixture
def client():
    return Client()


def get(client, week=None, when=WED, tz=None, data=None, **headers):
    if tz is not None:
        client.cookies["tz"] = tz
    params = dict(data or {})
    if week is not None:
        params["week"] = week
    with at(when):
        return client.get("/dashboard/", params, **headers)


def page(client, *args, **kwargs):
    response = get(client, *args, **kwargs)
    assert response.status_code == 200
    return response.content.decode()


def spy():
    return mock.patch("chores.views.materialize_week", wraps=materialize_week)


# --- Factories -------------------------------------------------------------------


def member(name, role="kid", order=None, avatar="😀"):
    return Member.objects.create(name=name, role=role, avatar=avatar, order=order)


def chore(title="Dishes", start_date="2026-11-16", recurrence="once", mode="unassigned", **kwargs):
    return Chore.objects.create(
        title=title, start_date=d(start_date), recurrence=recurrence, assignment_mode=mode, **kwargs
    )


def occ(title, day, status="todo", due=None, member=None, points=10, mode="unassigned", **kwargs):
    """A one-off chore on ``day`` with its stored row (never projected again)."""
    c = chore(title, start_date=day, mode=mode, points=points, **kwargs)
    return ChoreOccurrence.objects.create(
        chore=c, date=d(day), status=status, due_time=t(due) if due else None,
        member=member, points=points,
    )


# --- Parsing the page ---------------------------------------------------------------

SECTION = re.compile(r'<section id="section-([a-z-]+)">(.*?)</section>', re.S)
CARD = re.compile(r'<article class="chore-card"(?: id="occurrence-(\d+)")?>(.*?)</article>', re.S)
MAIN = re.compile(r'<main class="container">(.*?)</main>', re.S)
TITLE = re.compile(r'<h1 id="week-title">(.*?)</h1>', re.S)


def text(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def part(inner, cls):
    m = re.search(rf'<(p|span|time) class="{cls}"[^>]*>(.*?)</\1>', inner, re.S)
    return text(m.group(2)) if m else None


def cards(fragment):
    found = []
    for pk, inner in CARD.findall(fragment):
        found.append({
            "id": int(pk) if pk else None,
            "title": part(inner, "title"),
            "assignee": part(inner, "assignee"),
            "date": part(inner, "date"),
            "time": part(inner, "due-time"),
            "points": part(inner, "points"),
            "status": part(inner, "status"),
            "struck": "<s>" in inner,
            "html": inner,
        })
    return found


def sections(body):
    return {key: cards(inner) for key, inner in SECTION.findall(body)}


def titles(body, key):
    return [c["title"] for c in sections(body)[key]]


def headings(body):
    return re.findall(r"<h2>(.*?)</h2>", body)


def week_title(body):
    return text(TITLE.search(body).group(1))


def link(body, label):
    m = re.search(rf'<a href="([^"]*)" aria-label="{label}"\s+hx-get="([^"]*)"\s+'
                  r'hx-target="([^"]*)" hx-swap="([^"]*)" hx-push-url="([^"]*)">', body)
    return m.groups() if m else None


CSRF_VALUE = re.compile(r'(name="csrfmiddlewaretoken" value=")[^"]*"')


def blank_csrf(fragment):
    """Django masks the CSRF token differently on every render (#5)."""
    return CSRF_VALUE.sub(r'\1"', fragment)


def main(body):
    return blank_csrf(MAIN.search(body).group(1))


class WriteSpy:
    """Records every write statement (no query-log limit)."""

    def __init__(self):
        self.writes = []

    def __call__(self, execute, sql, params, many, context):
        if WRITE_SQL.match(sql):
            self.writes.append(sql)
        return execute(sql, params, many, context)


def no_writes():
    spy_ = WriteSpy()
    return spy_, connection.execute_wrapper(spy_)


def snapshot():
    return (
        sorted(ChoreOccurrence.objects.values_list("id", "chore_id", "date", "member_id", "status")),
        sorted(RotationState.objects.values_list("id", "chore_id", "last_member_id")),
        Chore.objects.count(),
    )


# --- Setup -----------------------------------------------------------------------


@pytest.mark.django_db
def test_post_returns_405(client):
    assert client.post("/dashboard/").status_code == 405


@pytest.mark.django_db
def test_url_name_and_extends_base(client):
    from django.urls import reverse

    assert reverse("dashboard") == "/dashboard/"
    member("Ann")
    body = page(client)
    assert "<html" in body
    assert 'id="profile-switcher"' in body
    assert "Ann" in body


@pytest.mark.django_db
def test_today_is_viewer_local_date(client):
    # Sat 21 Nov 12:00 UTC is Sun 22 Nov 02:00 in Kiritimati.
    occ("Sunday job", "2026-11-22")
    occ("Saturday job", "2026-11-21")
    body = page(client, when="2026-11-21T12:00:00", tz="Pacific/Kiritimati")
    assert titles(body, "today") == ["Sunday job"]
    assert titles(body, "overdue") == ["Saturday job"]


def test_code_does_not_read_the_clock_directly():
    for module in (views, dash):
        source = inspect.getsource(module)
        for forbidden in ("datetime.now(", "date.today(", "TIME_ZONE", "datetime.datetime.now"):
            assert forbidden not in source


# --- Viewer's time zone -------------------------------------------------------------

SUNDAY_NOON = "2026-11-22T12:00:00"


@pytest.mark.django_db
@pytest.mark.parametrize("tz", [None, "UTC"], ids=["no-cookie", "utc"])
def test_sunday_in_utc(client, tz):
    occ("Sunday job", "2026-11-22")
    body = page(client, when=SUNDAY_NOON, tz=tz)
    assert week_title(body) == "This Week (Nov 16 – 22, 2026)"
    assert titles(body, "today") == ["Sunday job"]
    assert titles(body, "upcoming") == []
    assert "Nothing here" in dict(SECTION.findall(body))["upcoming"]


@pytest.mark.django_db
def test_kiritimati_is_already_next_week(client):
    occ("Sunday job", "2026-11-22")
    occ("Sunday done", "2026-11-22", status="done")
    with spy() as m:
        body = page(client, when=SUNDAY_NOON, tz="Pacific/Kiritimati")
    assert week_title(body) == "This Week (Nov 23 – 29, 2026)"
    assert titles(body, "overdue") == ["Sunday job"]
    m.assert_called_once_with(d("2026-11-23"), today=d("2026-11-23"))


@pytest.mark.django_db
def test_pago_pago_is_still_last_week(client):
    chore("Daily", recurrence="daily", start_date="2026-11-16")
    with spy() as m:
        body = page(client, when="2026-11-23T05:00:00", tz="Pacific/Pago_Pago")
    m.assert_called_once_with(d("2026-11-16"), today=d("2026-11-22"))
    assert week_title(body) == "This Week (Nov 16 – 22, 2026)"
    assert not ChoreOccurrence.objects.filter(date__gte=d("2026-11-23")).exists()
    assert all(c["date"] != "Mon, Nov 23" for c in sections(body)["overdue"])
    assert link(body, "Next week")[0] == "/dashboard/?week=2026-W48"
    nxt = page(client, "2026-W48", when="2026-11-23T05:00:00", tz="Pacific/Pago_Pago")
    planned = cards(main(nxt))
    assert planned and {c["status"] for c in planned} == {"Planned"}
    assert not ChoreOccurrence.objects.filter(date__gte=d("2026-11-23")).exists()


@pytest.mark.django_db
def test_week_param_follows_viewer_zone(client):
    chore("Daily", recurrence="daily", start_date="2026-11-16")
    page(client, when=SUNDAY_NOON)  # UTC viewer stores 2026-W47
    before = snapshot()
    with spy() as m:
        past = page(client, "2026-W47", when=SUNDAY_NOON, tz="Pacific/Kiritimati")
    m.assert_not_called()
    assert snapshot() == before
    assert week_title(past) == "Nov 16 – 22, 2026"
    assert 'id="this-week" href="/dashboard/"' in past
    with spy() as m:
        current = page(client, "2026-W48", when=SUNDAY_NOON, tz="Pacific/Kiritimati")
    m.assert_called_once_with(d("2026-11-23"), today=d("2026-11-23"))
    assert week_title(current) == "This Week (Nov 23 – 29, 2026)"
    assert 'id="this-week"' not in current


@pytest.mark.django_db
@pytest.mark.parametrize("tz, section", [("Europe/Vilnius", "overdue"), (None, "today")],
                         ids=["vilnius", "utc"])
def test_due_time_uses_local_wall_clock(client, tz, section):
    occ("Evening", "2026-11-18", due="18:30")
    body = page(client, when="2026-11-18T16:31:00", tz=tz)
    assert titles(body, section) == ["Evening"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "when, section",
    [
        ("2026-10-25T00:30:00", "today"),    # 03:30 +03:00, equal minute
        ("2026-10-25T00:31:00", "overdue"),  # 03:31 +03:00
        ("2026-10-25T01:15:00", "today"),    # 03:15 +02:00, repeated hour
        ("2026-10-25T01:31:00", "overdue"),  # 03:31 +02:00
    ],
)
def test_dst_autumn(client, when, section):
    occ("Night", "2026-10-25", due="03:30")
    body = page(client, when=when, tz="Europe/Vilnius")
    assert week_title(body) == "This Week (Oct 19 – 25, 2026)"
    assert titles(body, section) == ["Night"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "when, section",
    [("2027-03-28T00:59:00", "today"), ("2027-03-28T01:00:00", "overdue")],  # 02:59, 04:00
)
def test_dst_spring(client, when, section):
    occ("Night", "2027-03-28", due="03:30")
    body = page(client, when=when, tz="Europe/Vilnius")
    assert week_title(body) == "This Week (Mar 22 – 28, 2027)"
    assert titles(body, section) == ["Night"]


@pytest.mark.django_db
def test_two_members_in_different_zones(client):
    a, b = member("A", order=0), member("B", order=1)
    chore("Rota", recurrence="daily", mode="rotating", eligibility="kids", start_date="2026-11-16")
    chore("Daily", recurrence="daily", start_date="2026-11-16")
    kiri = page(client, when=SUNDAY_NOON, tz="Pacific/Kiritimati")
    assert week_title(kiri) == "This Week (Nov 23 – 29, 2026)"
    assert ChoreOccurrence.objects.filter(date__range=(d("2026-11-23"), d("2026-11-29"))).count() == 14
    other = Client()
    utc = page(other, when=SUNDAY_NOON)
    assert week_title(utc) == "This Week (Nov 16 – 22, 2026)"
    before = snapshot()
    monday = page(other, when="2026-11-23T00:30:00")
    assert week_title(monday) == "This Week (Nov 23 – 29, 2026)"
    assert snapshot() == before


@pytest.mark.django_db
@pytest.mark.parametrize("tz", ["UTC", "Europe/Vilnius", "Pacific/Kiritimati", "Pacific/Pago_Pago"])
@pytest.mark.parametrize("when", [
    SUNDAY_NOON, "2026-11-23T05:00:00", "2026-11-23T00:30:00", "2026-10-25T00:31:00",
    "2026-10-25T01:31:00", "2027-03-28T01:00:00", "2026-12-31T23:30:00", "2027-01-01T12:00:00",
])
def test_materialize_never_raises_to_viewer(client, tz, when):
    chore("Daily", recurrence="daily", start_date="2026-01-01")
    for week in (None, "2026-W47", "2026-W48", "2026-W53", "2027-W01", "bad"):
        assert get(client, week, when=when, tz=tz).status_code == 200


@pytest.mark.django_db
def test_invalid_cookie_is_utc(client):
    occ("Sunday job", "2026-11-22")
    body = page(client, when=SUNDAY_NOON, tz="Mars/Base")
    assert week_title(body) == "This Week (Nov 16 – 22, 2026)"
    assert titles(body, "today") == ["Sunday job"]


# --- Week parameter ---------------------------------------------------------------

BAD_WEEKS = [
    "abc", "", "2026-W00", "2026-W54", "2027-W53", "2026-W5", "2026-w47", " 2026-W47",
    "2026-W47x", "1999-W10", "2100-W01", "٢٠٢٦-W47", "9" * 10_000,
    "<script>alert(1)</script>", "2026-W47\x00",
]


@pytest.mark.django_db
@pytest.mark.parametrize("week", BAD_WEEKS, ids=lambda w: repr(w)[:20])
def test_invalid_week_shows_current_week(client, week):
    occ("Today job", "2026-11-18")
    chore("Daily", recurrence="daily", start_date="2026-11-16")
    with spy() as m:
        response = get(client, week)
    assert response.status_code == 200
    m.assert_called_once_with(d("2026-11-16"), today=d("2026-11-18"))
    assert ChoreOccurrence.objects.filter(chore__title="Daily").count() == 7
    body = response.content.decode()
    assert main(body) == main(page(Client()))  # same as /dashboard/
    assert week_title(body) == "This Week (Nov 16 – 22, 2026)"
    assert "error" not in main(body).lower()


@pytest.mark.django_db
def test_week_parameter_is_never_echoed(client):
    member("Ann")
    value = "<script>alert(1)</script>"
    body = page(client, value)
    assert value not in body
    assert "&lt;script&gt;" not in body
    assert "alert(1)" not in body


@pytest.mark.parametrize("week", BAD_WEEKS + [None, 2026], ids=lambda w: repr(w)[:20])
def test_parse_week_rejects(week):
    assert dash.parse_week(week) is None


@pytest.mark.parametrize("week, monday", [
    ("2026-W47", "2026-11-16"), ("2026-W53", "2026-12-28"), ("2026-W01", "2025-12-29"),
    ("2000-W01", "2000-01-03"), ("2099-W53", "2099-12-28"), ("2020-W53", "2020-12-28"),
])
def test_parse_week_accepts(week, monday):
    assert dash.parse_week(week) == d(monday)


@pytest.mark.django_db
def test_week_53_of_2026(client):
    body = page(client, "2026-W53")
    assert week_title(body) == "Dec 28, 2026 – Jan 3, 2027"


@pytest.mark.django_db
def test_last_repeated_week_wins_and_other_params_ignored(client):
    with at(WED):
        body = client.get("/dashboard/?week=2026-W40&week=2026-W49&member=1&day=mon").content.decode()
    assert week_title(body) == "Nov 30 – Dec 6, 2026"
    plain = main(page(Client()))
    with at(WED):
        filtered = client.get("/dashboard/?member=1&day=mon").content.decode()
    assert main(filtered) == plain


# --- Header and navigation ----------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("week, title", [
    (None, "This Week (Nov 16 – 22, 2026)"),
    ("2026-W49", "Nov 30 – Dec 6, 2026"),
    ("2026-W53", "Dec 28, 2026 – Jan 3, 2027"),
    ("2026-W01", "Dec 29, 2025 – Jan 4, 2026"),
])
def test_header_formats(client, week, title):
    assert week_title(page(client, week)) == title


@pytest.mark.django_db
@pytest.mark.parametrize("week, previous, following", [
    ("2026-W47", "2026-W46", "2026-W48"),
    ("2026-W53", "2026-W52", "2027-W01"),
    ("2027-W01", "2026-W53", "2027-W02"),
    ("2026-W01", "2025-W52", "2026-W02"),
])
def test_prev_next_links(client, week, previous, following):
    body = page(client, week)
    for label, target in (("Previous week", previous), ("Next week", following)):
        url = f"/dashboard/?week={target}"
        assert link(body, label) == (url, url, "#dashboard-list", "outerHTML", "true")


@pytest.mark.django_db
def test_no_previous_on_2000_w01(client):
    body = page(client, "2000-W01")
    assert link(body, "Previous week") is None
    assert 'aria-label="Previous week"' not in body
    assert link(body, "Next week")[0] == "/dashboard/?week=2000-W02"


@pytest.mark.django_db
def test_no_next_on_2099_w53(client):
    body = page(client, "2099-W53")
    assert 'aria-label="Next week"' not in body
    assert link(body, "Previous week")[0] == "/dashboard/?week=2099-W52"


@pytest.mark.django_db
@pytest.mark.parametrize("week", ["2026-W46", "2026-W48", "2000-W01"])
def test_this_week_link_on_other_weeks(client, week):
    body = page(client, week)
    assert re.search(r'<a id="this-week" href="/dashboard/"\s+hx-get="/dashboard/"\s+'
                     r'hx-target="#dashboard-list" hx-swap="outerHTML" hx-push-url="true">This week</a>', body)


@pytest.mark.django_db
@pytest.mark.parametrize("week", [None, "2026-W47"])
def test_no_this_week_link_on_current_week(client, week):
    assert 'id="this-week"' not in page(client, week)


@pytest.mark.django_db
def test_current_week_on_new_years_day_2027(client):
    with spy() as m:
        body = page(client, when="2027-01-01T12:00:00")
    m.assert_called_once_with(d("2026-12-28"), today=d("2027-01-01"))
    assert week_title(body) == "This Week (Dec 28, 2026 – Jan 3, 2027)"
    assert week_title(page(client, "2026-W53", when="2027-01-01T12:00:00")).startswith("This Week")
    assert not week_title(page(client, "2027-W01", when="2027-01-01T12:00:00")).startswith("This Week")


@pytest.mark.django_db
def test_htmx_request_returns_partial(client):
    member("Ann")
    occ("Today job", "2026-11-18")
    response = get(client, "2026-W48", HTTP_HX_REQUEST="true")
    body = response.content.decode()
    assert "<html" not in body
    assert body.count('id="profile-switcher"') == 1
    assert body.count('<nav id="profile-switcher" hx-swap-oob="true"') == 1
    assert body.rindex("</div>") < body.index('id="profile-switcher"')
    assert "<script" not in body
    assert body.strip().startswith('<div id="dashboard-list">')
    assert week_title(body) == "Nov 23 – 29, 2026"
    assert link(body, "Previous week")[0] == "/dashboard/?week=2026-W47"
    assert "HX-Request" in response["Vary"]


@pytest.mark.django_db
def test_htmx_partial_equals_full_page_list(client):
    occ("Today job", "2026-11-18")
    partial = page(client, HTTP_HX_REQUEST="true")
    full = page(client)
    oob = re.compile(r'<nav id="profile-switcher" hx-swap-oob="true".*?</nav>', re.S)
    assert blank_csrf(oob.sub("", partial)).strip() == main(full).strip()


@pytest.mark.django_db
@pytest.mark.parametrize("headers", [
    {"HTTP_HX_REQUEST": "true", "HTTP_HX_HISTORY_RESTORE_REQUEST": "true"},
    {},
    {"HTTP_HX_REQUEST": "false"},
], ids=["history-restore", "plain", "hx-request-false"])
@pytest.mark.parametrize("week", [None, "2026-W46", "2026-W48"])
def test_full_page(client, headers, week):
    member("Ann")
    body = page(client, week, **headers)
    assert body.lstrip().startswith("<!doctype html>")
    assert 'id="profile-switcher"' in body
    assert '<div id="dashboard-list">' in body


@pytest.mark.django_db
def test_htmx_landing_on_current_week_materializes(client):
    chore("Daily", recurrence="daily", start_date="2026-11-16")
    with spy() as m:
        body = page(client, "2026-W47", HTTP_HX_REQUEST="true")
    m.assert_called_once_with(d("2026-11-16"), today=d("2026-11-18"))
    assert ChoreOccurrence.objects.count() == 7
    assert "<html" not in body


@pytest.mark.django_db
def test_hrefs_work_as_plain_links_without_javascript(client):
    body = page(client)
    href = link(body, "Next week")[0]
    with at(WED):
        followed = client.get(href).content.decode()
    assert followed.lstrip().startswith("<!doctype html>")
    assert week_title(followed) == "Nov 23 – 29, 2026"
    with at(WED):
        back = client.get(re.search(r'id="this-week" href="([^"]+)"', followed).group(1))
    assert week_title(back.content.decode()) == "This Week (Nov 16 – 22, 2026)"


# --- Current week ---------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("week", [None, "2026-W47", "bad"])
def test_current_week_materializes(client, week):
    chore("Daily", recurrence="daily", start_date="2026-11-16")
    with spy() as m:
        page(client, week)
    m.assert_called_once_with(d("2026-11-16"), today=d("2026-11-18"))
    assert ChoreOccurrence.objects.count() == 7


@pytest.mark.django_db
def test_reload_is_idempotent(client):
    member("A", order=0), member("B", order=1)
    chore("Rota", recurrence="daily", mode="rotating", eligibility="kids", start_date="2026-11-16")
    chore("Daily", recurrence="daily", start_date="2026-11-16")
    first = page(client)
    before = snapshot()
    assert len(before[0]) == 14 and len(before[1]) == 1
    second = page(client)
    assert snapshot() == before
    assert main(first) == main(second)


@pytest.mark.django_db
def test_lists_stored_rows_only(client):
    chore("Daily", recurrence="daily", start_date="2026-11-16")
    body = page(client)
    shown = [c["id"] for cs in sections(body).values() for c in cs]
    assert len(shown) == 7
    assert sorted(shown) == sorted(ChoreOccurrence.objects.values_list("pk", flat=True))
    assert None not in shown


@pytest.mark.django_db
def test_overdue_contents_and_order(client):
    a = occ("Zed", "2026-11-17")
    b = occ("Alpha", "2026-11-17", due="09:00")
    c = occ("Alpha", "2026-11-17", due="09:00")
    last_month = occ("Old", "2026-10-02")
    last_year = occ("Ancient", "2025-12-31")
    passed = occ("Morning", "2026-11-18", due="11:59")
    occ("Noon", "2026-11-18", due="12:00")  # equal minute: not yet
    occ("No time", "2026-11-18")            # no due_time: only tomorrow
    occ("Done yesterday", "2026-11-17", status="done")
    occ("Skipped old", "2026-10-01", status="skipped")
    body = page(client)
    overdue = sections(body)["overdue"]
    assert [x["id"] for x in overdue] == [last_year.pk, last_month.pk, b.pk, c.pk, a.pk, passed.pk]
    assert {x["status"] for x in overdue} == {"Overdue"}
    assert [x["date"] for x in overdue[:2]] == ["Wed, Dec 31", "Fri, Oct 2"]
    assert titles(body, "today") == ["Noon", "No time"]


@pytest.mark.django_db
def test_today_contents_and_order(client):
    occ("Later", "2026-11-18", due="20:00")
    occ("Untimed", "2026-11-18", status="skipped")
    occ("B", "2026-11-18", due="13:00", status="done")
    occ("A", "2026-11-18", due="13:00")
    body = page(client)
    today = sections(body)["today"]
    assert [x["title"] for x in today] == ["A", "B", "Later", "Untimed"]
    assert [x["status"] for x in today] == ["To do", "Done", "To do", "Skipped"]
    assert [x["struck"] for x in today] == [False, True, False, True]


@pytest.mark.django_db
def test_upcoming_contents_and_order(client):
    occ("Sun", "2026-11-22")
    occ("Thu late", "2026-11-19", due="21:00")
    occ("Thu untimed", "2026-11-19")
    occ("Thu early", "2026-11-19", due="07:00", status="done")
    occ("Next week", "2026-11-23")
    body = page(client)
    upcoming = sections(body)["upcoming"]
    assert [x["title"] for x in upcoming] == ["Thu early", "Thu late", "Thu untimed", "Sun"]
    assert upcoming[0]["status"] == "Done"


@pytest.mark.django_db
def test_done_earlier_this_week(client):
    occ("Mon done", "2026-11-16", status="done")
    occ("Tue skipped", "2026-11-17", status="skipped")
    occ("Last week done", "2026-11-13", status="done")
    occ("Today done", "2026-11-18", status="done")
    body = page(client)
    assert "Done earlier this week" in headings(body)
    assert titles(body, "done-earlier") == ["Mon done", "Tue skipped"]
    assert titles(body, "today") == ["Today done"]
    assert "Last week done" not in body


@pytest.mark.django_db
def test_no_done_earlier_on_monday(client):
    occ("Sun done", "2026-11-15", status="done")
    occ("Mon done", "2026-11-16", status="done")
    body = page(client, when="2026-11-16T12:00:00")
    assert "Done earlier this week" not in body
    assert "section-done-earlier" not in body


@pytest.mark.django_db
def test_section_headings_and_nothing_here(client):
    occ("Thu", "2026-11-19")
    body = page(client)
    assert headings(body) == ["Overdue", "Today", "Upcoming"]
    parts = dict(SECTION.findall(body))
    assert "Nothing here" in parts["overdue"]
    assert "Nothing here" in parts["today"]
    assert "Nothing here" not in parts["upcoming"]


@pytest.mark.django_db
def test_sunday_upcoming_is_empty(client):
    occ("Sun", "2026-11-22")
    body = page(client, when=SUNDAY_NOON)
    assert titles(body, "upcoming") == []
    assert "Nothing here" in dict(SECTION.findall(body))["upcoming"]


def assert_no_chores(body):
    assert "No chores this week" in body
    [empty] = re.findall(r'<section id="no-chores">.*?<a href="([^"]*)">Add a chore</a>', body, re.S)
    [add] = re.findall(r'<a id="add-chore" role="button" href="([^"]*)">', body)
    assert empty == add
    assert empty.startswith("/chores/create/?date=")
    assert "recurrence=once" in empty and "return_to=dashboard" in empty
    assert "<h2>" not in body


@pytest.mark.django_db
def test_empty_no_members_no_chores(client):
    body = page(client)
    assert_no_chores(body)
    assert "Add your family" in body


@pytest.mark.django_db
def test_empty_members_but_no_chores(client):
    member("Ann")
    assert_no_chores(page(client))


@pytest.mark.django_db
def test_empty_only_templates(client):
    assert Chore.objects.filter(is_template=True).exists()  # built-ins (#6)
    chore("My template", recurrence="daily", is_template=True)
    assert_no_chores(page(client))
    assert ChoreOccurrence.objects.count() == 0


@pytest.mark.django_db
def test_empty_chores_outside_this_week(client):
    chore("Later", start_date="2026-12-15")
    chore("Monthly", recurrence="monthly", recurrence_day=1, start_date="2026-11-01")
    assert_no_chores(page(client))


@pytest.mark.django_db
def test_monday_shows_last_sundays_todo_as_overdue(client):
    occ("Sunday", "2026-11-15")
    body = page(client, when="2026-11-16T08:00:00")
    assert titles(body, "overdue") == ["Sunday"]


@pytest.mark.django_db
def test_year_edge(client):
    occ("Dec 30", "2026-12-30")
    occ("Jan 2", "2027-01-02")
    body = page(client, when="2027-01-01T12:00:00")
    assert titles(body, "overdue") == ["Dec 30"]
    assert titles(body, "upcoming") == ["Jan 2"]


@pytest.mark.django_db
def test_template_rows_and_deleted_chores_never_appear(client):
    tpl = chore("Template job", is_template=True)
    ChoreOccurrence.objects.create(chore=tpl, date=d("2026-11-17"), points=10)
    gone = occ("Gone", "2026-11-17")
    gone.chore.delete()
    assert not ChoreOccurrence.objects.filter(pk=gone.pk).exists()
    body = page(client)
    assert "Template job" not in body and "Gone" not in body


# --- Past weeks ---------------------------------------------------------------------


@pytest.mark.django_db
def test_past_week_one_ordered_list(client):
    occ("Wed", "2026-11-11", status="done")
    occ("Mon b", "2026-11-09", due="10:00")
    occ("Mon untimed", "2026-11-09", status="skipped")
    occ("Mon a", "2026-11-09", due="08:00")
    occ("Other week", "2026-11-16")
    body = page(client, "2026-W46")
    assert "<h2>" not in body
    listed = cards(main(body))
    assert [c["title"] for c in listed] == ["Mon a", "Mon b", "Mon untimed", "Wed"]
    assert [c["status"] for c in listed] == ["Overdue", "Overdue", "Skipped", "Done"]


@pytest.mark.django_db
def test_past_week_writes_nothing(client):
    member("A")
    chore("Rota", recurrence="daily", mode="rotating", eligibility="kids", start_date="2026-11-01")
    writes, wrapper = no_writes()
    with wrapper:
        page(client)  # current week stored
    assert writes.writes  # the spy does see writes
    before = snapshot()
    writes, wrapper = no_writes()
    with spy() as m, wrapper:
        page(client, "2026-W46")
    m.assert_not_called()
    assert snapshot() == before
    assert writes.writes == []


@pytest.mark.django_db
def test_past_week_without_rows(client):
    chore("Daily", recurrence="daily", start_date="2020-01-01")
    assert_no_chores(page(client, "2026-W40"))
    assert_no_chores(page(client, "2000-W01"))


@pytest.mark.django_db
def test_past_todo_is_overdue_in_both_places(client):
    o = occ("Missed", "2026-11-10")
    past = cards(main(page(client, "2026-W46")))
    assert [(c["id"], c["status"]) for c in past] == [(o.pk, "Overdue")]
    current = sections(page(client))["overdue"]
    assert [(c["id"], c["status"]) for c in current] == [(o.pk, "Overdue")]


# --- Future weeks --------------------------------------------------------------------


@pytest.mark.django_db
def test_future_week_projected_plus_stored(client):
    chore("Weekly", recurrence="weekly", recurrence_day=2, start_date="2026-11-18", points=20)
    stored = occ("Stored", "2026-11-23", due="09:00")
    body = page(client, "2026-W48")
    listed = cards(main(body))
    assert [(c["title"], c["status"], c["id"]) for c in listed] == [
        ("Stored", "To do", stored.pk), ("Weekly", "Planned", None),
    ]
    assert listed[1]["points"] == "20 pts"
    assert listed[1]["date"] == "Wed, Nov 25"


@pytest.mark.django_db
def test_future_weeks_write_nothing(client):
    member("A"), member("B")
    chore("Rota", recurrence="daily", mode="rotating", eligibility="kids", start_date="2026-11-01")
    chore("Daily", recurrence="daily", start_date="2026-11-01")
    page(client)
    before = snapshot()
    for week in ("2026-W48", "2027-W08", "2099-W52"):
        writes, wrapper = no_writes()
        with spy() as m, wrapper:
            body = page(client, week)
        m.assert_not_called()
        assert writes.writes == []
        assert {c["status"] for c in cards(main(body))} == {"Planned"}
    assert snapshot() == before


@pytest.mark.django_db
def test_future_rotation_follows_projection(client):
    a, b = member("A", order=0), member("B", order=1)
    c = chore("Rota", recurrence="weekly", recurrence_day=0, mode="rotating", eligibility="kids")
    ChoreOccurrence.objects.create(chore=c, date=d("2026-11-16"), member=a, points=10)
    RotationState.objects.create(chore=c, last_member=a)
    assert [x["assignee"] for x in cards(main(page(client, "2026-W48")))] == ["😀 B"]
    assert [x["assignee"] for x in cards(main(page(client, "2026-W49")))] == ["😀 A"]


@pytest.mark.django_db
def test_future_week_with_nothing_projected(client):
    chore("Once", start_date="2026-11-18")
    assert_no_chores(page(client, "2026-W50"))


# --- Chore card -------------------------------------------------------------------------


@pytest.mark.django_db
def test_card_fields(client):
    ann = member("Ann", avatar="🦊")
    o = occ("Laundry", "2026-11-18", due="18:30", member=ann, points=20, mode="fixed", assigned_to=ann)
    o.chore.points = 5
    o.chore.save()
    body = page(client)
    [card] = sections(body)["today"]
    assert card["id"] == o.pk
    assert card["title"] == "Laundry"
    assert f'<a href="/chores/{o.chore_id}/edit/">Laundry</a>' in card["html"]
    assert card["assignee"] == "🦊 Ann"
    assert card["date"] == "Wed, Nov 18"
    assert card["time"] == "18:30"
    assert card["points"] == "20 pts"  # from the occurrence, not the chore
    assert card["status"] == "To do"
    assert f'id="occurrence-{o.pk}"' in body


@pytest.mark.django_db
def test_card_without_due_time(client):
    occ("Untimed", "2026-11-19")
    [card] = sections(page(client))["upcoming"]
    assert card["time"] is None
    assert card["date"] == "Thu, Nov 19"


@pytest.mark.django_db
def test_projected_card_has_no_occurrence_id(client):
    chore("Weekly", recurrence="weekly", recurrence_day=0, start_date="2026-11-16", due_time=t("07:05"))
    body = main(page(client, "2026-W48"))
    [card] = cards(body)
    assert card["id"] is None
    assert "occurrence-" not in body
    assert card["time"] == "07:05"
    assert card["status"] == "Planned"


@pytest.mark.django_db
@pytest.mark.parametrize("status, label, struck", [
    ("todo", "To do", False), ("done", "Done", True), ("skipped", "Skipped", True),
])
def test_card_status_and_strike(client, status, label, struck):
    occ("Job", "2026-11-19", status=status)
    [card] = sections(page(client))["upcoming"]
    assert card["status"] == label
    assert card["struck"] is struck
    if struck:
        assert "<s>Job</s>" in card["html"]


@pytest.mark.django_db
def test_card_has_no_controls(client):
    member("Ann")
    occ("Job", "2026-11-19")
    [card] = sections(page(client))["upcoming"]
    for tag in ("<button", "<form", "<select", "<input"):
        assert tag not in card["html"]


@pytest.mark.django_db
def test_unassigned_variants(client):
    kid = member("Kid")
    occ("Unassigned chore", "2026-11-19")
    occ("Fixed nobody", "2026-11-19", mode="fixed")
    gone = member("Gone", role="adult")
    occ("Deleted member", "2026-11-19", mode="fixed", member=gone, assigned_to=gone)
    gone.delete()
    rot = occ("Hand unassigned", "2026-11-19", mode="rotating", eligibility="kids")
    rot.is_override = True
    rot.save()
    rot2 = occ("Rotating pool ok", "2026-11-19", mode="rotating", eligibility="kids")
    assert kid.pk
    upcoming = {c["title"]: c["assignee"] for c in sections(page(client))["upcoming"]}
    assert upcoming == {t_: "Unassigned" for t_ in (
        "Unassigned chore", "Fixed nobody", "Deleted member", "Hand unassigned", "Rotating pool ok")}
    assert rot2.member is None


@pytest.mark.django_db
def test_needs_eligible_kids_with_only_adults(client):
    member("Mum", role="adult")
    occ("Kids job", "2026-11-19", mode="rotating", eligibility="kids")
    chore("Kids weekly", recurrence="weekly", recurrence_day=0, mode="rotating",
          eligibility="kids", start_date="2026-11-23")
    [card] = sections(page(client))["upcoming"]
    assert card["assignee"] == "Unassigned · needs eligible member"
    future = cards(main(page(client, "2026-W48")))
    assert [c["assignee"] for c in future] == ["Unassigned · needs eligible member"]


@pytest.mark.django_db
def test_needs_eligible_no_members(client):
    occ("Anyone job", "2026-11-19", mode="rotating", eligibility="everyone")
    chore("Anyone weekly", recurrence="weekly", recurrence_day=0, mode="rotating",
          eligibility="everyone", start_date="2026-11-23")
    [card] = sections(page(client))["upcoming"]
    assert card["assignee"] == "Unassigned · needs eligible member"
    future = cards(main(page(client, "2026-W48")))
    assert [c["assignee"] for c in future] == ["Unassigned · needs eligible member"]


@pytest.mark.django_db
def test_needs_eligible_becomes_plain_unassigned(client):
    occ("Kids job", "2026-11-19", mode="rotating", eligibility="kids")
    assert sections(page(client))["upcoming"][0]["assignee"] == "Unassigned · needs eligible member"
    member("Kid")
    assert sections(page(client))["upcoming"][0]["assignee"] == "Unassigned"


@pytest.mark.django_db
def test_user_text_is_escaped(client):
    baseline = page(Client()).count("<script")
    bo = member("<b>Bo</b>")
    occ("<script>alert(1)</script>", "2026-11-19", member=bo, mode="fixed", assigned_to=bo)
    body = page(client)
    [card] = sections(body)["upcoming"]
    assert card["title"] == "<script>alert(1)</script>"
    assert card["assignee"] == "😀 <b>Bo</b>"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "&lt;b&gt;Bo&lt;/b&gt;" in body
    assert "<b>Bo</b>" not in body
    assert body.count("<script") == baseline


# --- Pure helpers ----------------------------------------------------------------------


@pytest.mark.parametrize("monday, expected", [
    ("2026-11-16", "Nov 16 – 22, 2026"),
    ("2026-11-30", "Nov 30 – Dec 6, 2026"),
    ("2026-12-28", "Dec 28, 2026 – Jan 3, 2027"),
    ("2025-12-29", "Dec 29, 2025 – Jan 4, 2026"),
])
def test_week_range_text(monday, expected):
    assert dash.week_range_text(d(monday)) == expected


@pytest.mark.parametrize("due, now, overdue", [
    ("18:30", "18:30:59", False), ("18:30", "18:31:00", True), (None, "23:59:59", False),
])
def test_is_overdue_by_minute(due, now, overdue):
    card = dash.Card(pk=1, chore_id=1, title="x", date=d("2026-11-18"),
                     due_time=t(due) if due else None, points=10, status="todo")
    assert dash.is_overdue(card, d("2026-11-18"), t(now)) is overdue
