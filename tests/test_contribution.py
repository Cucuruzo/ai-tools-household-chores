"""Points and contribution view (#15)."""
import datetime
import inspect
import re
from unittest import mock

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import contribution_views
from chores.contribution_views import parse_period, percentages, period_days
from chores.models import ChoreOccurrence, Member
from tests.test_dashboard import WED, at, blank_csrf, d, member, no_writes, occ

UTC = datetime.timezone.utc
ROW = re.compile(r'<li class="contribution-row" id="([a-z0-9-]+)">(.*?)</li>', re.S)
SECTION = re.compile(r'<section id="contribution">.*?</section>', re.S)
TITLE = re.compile(r'<h1 id="contribution-title">(.*?)</h1>')
TOTAL = re.compile(r'<p id="household-total">(.*?)</p>')


@pytest.fixture
def client():
    return Client()


def view(client, period=None, when=WED, tz=None, data=None, **headers):
    if tz is not None:
        client.cookies["tz"] = tz
    params = dict(data or {})
    if period is not None:
        params["period"] = period
    with at(when):
        return client.get("/contribution/", params, **headers)


def done(title="Job", done_at="2026-11-17T08:00:00", by=None, assignee=None, points=10, day="2026-11-17", **kw):
    row = occ(title, day, status="done", member=assignee, points=points)
    row.done_at = datetime.datetime.fromisoformat(done_at).replace(tzinfo=UTC) if done_at else None
    row.completed_by = by
    for key, value in kw.items():
        setattr(row, key, value)
    row.save()
    return row


def lines(response):
    """{row id: dict} in page order."""
    out = {}
    for rid, inner in ROW.findall(response.content.decode()):
        out[rid] = {
            "points": int(re.search(r'<span class="points">(\d+) points', inner).group(1)),
            "count": re.search(r'<span class="done-count">(.*?)</span>', inner).group(1),
            "share": re.search(r'<span class="share">(.*?)</span>', inner).group(1),
            "bar": re.search(r"<progress (.*?)>", inner).group(1),
            "html": inner,
        }
    return out


def pts(response):
    return {k: v["points"] for k, v in lines(response).items()}


def heading(response):
    return TITLE.search(response.content.decode()).group(1)


def trio():
    return member("Mom", "adult", 0), member("Dad", "adult", 1), member("Emma", "kid", 2)


# --- URL and period -----------------------------------------------------------------


@pytest.mark.django_db
def test_get_ok_and_extends_base(client):
    trio()
    response = view(client)
    assert response.status_code == 200
    body = response.content.decode()
    assert '<nav id="profile-switcher"' in body and "<header" in body


@pytest.mark.django_db
def test_post_is_405(client):
    assert client.post("/contribution/").status_code == 405


HOSTILE_PERIODS = [
    "", "week", "MONTH", " month", "month ", "all", "last", "month\x00", "x" * 10000,
    "<script>alert(1)</script>",
]


@pytest.mark.django_db
@pytest.mark.parametrize("raw", HOSTILE_PERIODS, ids=lambda s: repr(s[:20]))
def test_other_period_values_mean_week_and_are_not_echoed(client, raw):
    trio()
    response = view(client, raw, data={"x": "1"})
    assert response.status_code == 200
    body = response.content.decode()
    assert heading(response) == "This week (Nov 16 – 22, 2026)"
    baseline = view(client).content.decode()
    if raw.strip() and raw.strip() not in baseline:
        assert raw not in body and "alert(1)" not in body and "&lt;script&gt;" not in body
    assert re.search(r'name="next" value="/contribution/"', body)


@pytest.mark.django_db
def test_period_value_with_null_byte_in_query(client):
    trio()
    with at(WED):
        response = client.get("/contribution/?period=month%00")
    assert response.status_code == 200
    assert heading(response).startswith("This week")


@pytest.mark.django_db
def test_last_period_value_wins(client):
    with at(WED):
        assert heading(client.get("/contribution/?period=week&period=month")).startswith("This month")
        assert heading(client.get("/contribution/?period=month&period=week")).startswith("This week")


@pytest.mark.django_db
def test_week_and_month_params_are_ignored(client):
    done()
    response = view(client, data={"week": "2026-W46", "month": "2026-10"})
    assert heading(response) == "This week (Nov 16 – 22, 2026)"


@pytest.mark.django_db
def test_headings(client):
    assert heading(view(client)) == "This week (Nov 16 – 22, 2026)"
    assert heading(view(client, "month")) == "This month (November 2026)"


@pytest.mark.django_db
@pytest.mark.parametrize("period,current,other", [
    (None, "/contribution/", "/contribution/?period=month"),
    ("month", "/contribution/?period=month", "/contribution/"),
])
def test_toggle(client, period, current, other):
    body = view(client, period).content.decode()
    nav = re.search(r'<nav id="period-toggle" aria-label="Period">(.*?)</nav>', body, re.S).group(1)
    links = re.findall(r"<a ([^>]*)>(.*?)</a>", nav)
    assert [text for _, text in links] == ["This week", "This month"]
    for attrs, _ in links:
        href = re.search(r'href="([^"]*)"', attrs).group(1)
        assert f'hx-get="{href}"' in attrs
        assert 'hx-target="#contribution"' in attrs and 'hx-swap="outerHTML"' in attrs
        assert 'hx-push-url="true"' in attrs
        assert ('aria-current="page"' in attrs) == (href == current)
    assert 'href="/contribution/"' in links[0][0] and 'href="/contribution/?period=month"' in links[1][0]


@pytest.mark.django_db
def test_month_view_switcher_next_is_canonical(client):
    trio()
    body = view(client, "month", data={"x": "1"}).content.decode()
    assert 'name="next" value="/contribution/?period=month"' in body
    assert 'value="/contribution/"' not in body


# --- Time zone rule -----------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("stamp,counted", [
    ("2026-11-15T23:59:59", False), ("2026-11-16T00:00:00", True),
    ("2026-11-22T23:59:59", True), ("2026-11-23T00:00:00", False),
])
def test_week_boundary_utc(client, stamp, counted):
    mom = member("Mom", "adult", 0)
    done(done_at=stamp, by=mom)
    assert pts(view(client))["contribution-member-%d" % mom.pk] == (10 if counted else 0)


@pytest.mark.django_db
@pytest.mark.parametrize("stamp,kiritimati,utc", [
    ("2026-11-15T09:59:59", False, False),
    ("2026-11-15T10:00:00", True, False),
])
def test_week_boundary_kiritimati(client, stamp, kiritimati, utc):
    mom = member("Mom", "adult", 0)
    done(done_at=stamp, by=mom)
    key = "contribution-member-%d" % mom.pk
    assert pts(view(client, tz="Pacific/Kiritimati"))[key] == (10 if kiritimati else 0)
    client.cookies.pop("tz")
    assert pts(view(client))[key] == (10 if utc else 0)


@pytest.mark.django_db
def test_today_is_local_sunday_utc_monday_kiritimati(client):
    mom = member("Mom", "adult", 0)
    done(done_at="2026-11-22T09:00:00", by=mom)
    key = "contribution-member-%d" % mom.pk
    kiri = view(client, when="2026-11-22T12:00:00", tz="Pacific/Kiritimati")
    assert heading(kiri) == "This week (Nov 23 – 29, 2026)"
    assert pts(kiri)[key] == 0
    client.cookies.pop("tz")
    plain = view(client, when="2026-11-22T12:00:00")
    assert heading(plain) == "This week (Nov 16 – 22, 2026)"
    assert pts(plain)[key] == 10


@pytest.mark.django_db
def test_month_boundary_los_angeles(client):
    mom = member("Mom", "adult", 0)
    done("a", "2026-12-01T05:00:00", by=mom)
    done("b", "2026-11-01T07:00:00", by=mom)
    done("c", "2026-11-01T06:59:59", by=mom)
    key = "contribution-member-%d" % mom.pk
    la = view(client, "month", when="2026-12-01T06:00:00", tz="America/Los_Angeles")
    assert heading(la) == "This month (November 2026)"
    assert pts(la)[key] == 20
    client.cookies.pop("tz")
    plain = view(client, "month", when="2026-12-01T06:00:00")
    assert heading(plain) == "This month (December 2026)"
    assert pts(plain)[key] == 10


@pytest.mark.django_db
def test_daylight_saving_berlin(client):
    mom = member("Mom", "adult", 0)
    done("in", "2026-10-25T22:59:59", by=mom)
    done("out", "2026-10-25T23:00:00", by=mom)
    response = view(client, when="2026-10-25T20:00:00", tz="Europe/Berlin")
    assert heading(response) == "This week (Oct 19 – 25, 2026)"
    assert pts(response)["contribution-member-%d" % mom.pk] == 10


@pytest.mark.django_db
def test_counted_by_done_day_not_due_day(client):
    mom = member("Mom", "adult", 0)
    key = "contribution-member-%d" % mom.pk
    done("late", "2026-11-17T08:00:00", by=mom, day="2026-11-15")
    done("early", "2026-11-18T08:00:00", by=mom, day="2026-11-20")
    assert pts(view(client))[key] == 20
    # Not in last week.
    assert pts(view(client, when="2026-11-11T12:00:00"))[key] == 0


@pytest.mark.django_db
def test_due_in_october_done_in_november_counts_in_november(client):
    mom = member("Mom", "adult", 0)
    done("x", "2026-11-02T09:00:00", by=mom, day="2026-10-31")
    key = "contribution-member-%d" % mom.pk
    assert pts(view(client, "month"))[key] == 10
    assert pts(view(client, "month", when="2026-10-20T12:00:00"))[key] == 0


@pytest.mark.django_db
def test_done_without_done_at_counts_nowhere(client):
    mom = member("Mom", "adult", 0)
    done(done_at=None, by=mom)
    for period in (None, "month"):
        response = view(client, period)
        assert response.status_code == 200
        assert pts(response) == {"contribution-member-%d" % mom.pk: 0}


# --- What does not count ------------------------------------------------------------


@pytest.mark.django_db
def test_skipped_todo_cancelled_and_template_rows_add_nothing(client):
    mom = member("Mom", "adult", 0)
    stamp = datetime.datetime(2026, 11, 17, 8, tzinfo=UTC)
    for status in ("skipped", "todo"):
        row = occ(status, "2026-11-17", status=status)
        row.done_at, row.completed_by = stamp, mom
        row.save()
    cancelled = done("gone", by=mom)
    cancelled.is_cancelled = True
    cancelled.save()
    template = done("tpl", by=mom)
    template.chore.is_template = True
    template.chore.save()
    response = view(client)
    assert pts(response) == {"contribution-member-%d" % mom.pk: 0}
    assert TOTAL.search(response.content.decode()).group(1) == "Household total: 0 points · 0 chores done"


@pytest.mark.django_db
def test_undo_removes_points_and_done_again_counts(client):
    mom = member("Mom", "adult", 0)
    row = occ("Dishes", "2026-11-18")
    key = "contribution-member-%d" % mom.pk
    client.post(f"/profile/{mom.pk}/")
    with at(WED):
        client.post(f"/dashboard/toggle/{row.pk}/", {"action": "done"})
    first = view(client)
    assert lines(first)[key]["points"] == 10 and lines(first)[key]["count"] == "1 chore done"
    with at(WED):
        client.post(f"/dashboard/toggle/{row.pk}/", {"action": "undo"})
    after = view(client)
    assert lines(after)[key]["points"] == 0
    assert TOTAL.search(after.content.decode()).group(1) == "Household total: 0 points · 0 chores done"
    with at("2026-11-19T09:00:00"):
        client.post(f"/dashboard/toggle/{row.pk}/", {"action": "done"})
    assert pts(view(client))[key] == 10


@pytest.mark.django_db
def test_deleting_a_chore_with_history_keeps_points(client):
    mom = member("Mom", "adult", 0)
    row = done(by=mom)
    with at(WED):
        client.post(f"/chores/{row.chore_id}/delete/", {"scope": "this"})
    assert pts(view(client))["contribution-member-%d" % mom.pk] == 10


# --- Grouping and credit ------------------------------------------------------------


@pytest.mark.django_db
def test_grouped_by_completed_by_not_assignee(client):
    mom, _, emma = member("Mom", "adult", 0), member("Dad", "adult", 1), member("Emma", "kid", 2)
    done(by=mom, assignee=emma)
    got = pts(view(client))
    assert got["contribution-member-%d" % mom.pk] == 10
    assert got["contribution-member-%d" % emma.pk] == 0


@pytest.mark.django_db
def test_points_come_from_the_row_snapshot(client):
    mom = member("Mom", "adult", 0)
    row = done(by=mom, points=10)
    row.chore.points = 20
    row.chore.save()
    assert pts(view(client))["contribution-member-%d" % mom.pk] == 10


@pytest.mark.django_db
def test_not_credited_line_including_assigned_rows(client):
    mom, _, emma = trio()
    done(by=None, assignee=emma)
    response = view(client)
    got = lines(response)
    assert list(got)[-1] == "contribution-not-credited"
    assert got["contribution-not-credited"]["points"] == 10
    assert got["contribution-member-%d" % emma.pk]["points"] == 0
    html_ = got["contribution-not-credited"]["html"]
    assert '<span class="name">Not credited</span>' in html_ and "avatar" not in html_


@pytest.mark.django_db
def test_not_credited_hidden_when_empty(client):
    mom = member("Mom", "adult", 0)
    done(by=mom)
    assert "contribution-not-credited" not in lines(view(client))


@pytest.mark.django_db
def test_deleted_credited_member_moves_to_not_credited(client):
    mom, dad, _ = trio()
    done(by=mom)
    done("two", by=dad)
    before = TOTAL.search(view(client).content.decode()).group(1)
    mom_key = "contribution-member-%d" % mom.pk
    mom.delete()
    response = view(client)
    got = pts(response)
    assert mom_key not in got
    assert got["contribution-not-credited"] == 10
    assert TOTAL.search(response.content.decode()).group(1) == before


@pytest.mark.django_db
def test_deleted_assignee_who_is_not_credited_changes_nothing(client):
    mom, _, emma = trio()
    done(by=mom, assignee=emma)
    before = pts(view(client))
    emma_key = "contribution-member-%d" % emma.pk
    emma.delete()
    after = pts(view(client))
    assert {k: v for k, v in before.items() if k != emma_key} == after


@pytest.mark.django_db
def test_page_is_the_same_for_every_active_profile(client):
    mom, _, emma = trio()
    done(by=mom)
    sections = []
    for pk in (mom.pk, emma.pk, None):
        c = Client()
        if pk:
            c.post(f"/profile/{pk}/")
        sections.append(blank_csrf(SECTION.search(view(c).content.decode()).group(0)))
    assert sections[0] == sections[1] == sections[2]


# --- Order, zeros, ties -------------------------------------------------------------


@pytest.mark.django_db
def test_every_member_listed_in_member_order_with_zero_rows(client):
    a = member("A", "kid", 0)
    b = member("B", "kid", 1)
    c = member("C", "kid", 2)
    done(by=c)
    response = view(client)
    got = lines(response)
    assert list(got) == [f"contribution-member-{x.pk}" for x in (a, b, c)]
    zero = got[f"contribution-member-{a.pk}"]
    assert zero["points"] == 0 and zero["count"] == "0 chores done" and zero["share"] == "0%"
    assert 'value="0"' in zero["bar"]


@pytest.mark.django_db
def test_row_markup_order(client):
    mom = member("Mom", "adult", 0, avatar="🙂")
    done(by=mom)
    inner = lines(view(client))["contribution-member-%d" % mom.pk]["html"]
    positions = [inner.index(s) for s in (
        '<span class="avatar">', '<span class="name">', '<span class="points">',
        '<span class="done-count">', '<span class="share">', "<progress",
    )]
    assert positions == sorted(positions)
    assert '<progress value="10" max="10">100%</progress>' in inner


@pytest.mark.django_db
def test_ties_keep_member_order_with_identical_bars(client):
    a, b = member("A", "kid", 0), member("B", "kid", 1)
    done("1", by=a)
    done("2", by=b)
    got = lines(view(client))
    assert list(got) == [f"contribution-member-{a.pk}", f"contribution-member-{b.pk}"]
    assert got[f"contribution-member-{a.pk}"]["bar"] == got[f"contribution-member-{b.pk}"]["bar"]
    assert "tie" not in view(client).content.decode().lower()


@pytest.mark.django_db
def test_member_reorder_moves_line(client):
    a, b = member("A", "kid", 0), member("B", "kid", 1)
    Member.objects.filter(pk=a.pk).update(order=5)
    assert list(lines(view(client))) == [f"contribution-member-{b.pk}", f"contribution-member-{a.pk}"]


# --- Rounding -----------------------------------------------------------------------


@pytest.mark.parametrize("points,expected", [
    ([10, 10, 10], [34, 33, 33]),
    ([20, 5, 5], [67, 17, 16]),
    ([10, 0, 0], [100, 0, 0]),
    ([5, 5, 10], [25, 25, 50]),
    ([0, 0, 0, 20], [0, 0, 0, 100]),
    ([10, 20, 5, 5, 10, 20], [14, 29, 7, 7, 14, 29]),
    ([0, 0, 0], [0, 0, 0]),
    ([], []),
])
def test_percentages(points, expected):
    assert percentages(points) == expected
    if sum(points):
        assert sum(expected) == 100


@pytest.mark.parametrize("points", [[1] * 7, [3, 3, 1], [1, 2, 3, 4, 5, 6, 7], [99, 1], [1, 1, 98]])
def test_percentages_always_sum_to_100(points):
    assert sum(percentages(points)) == 100


@pytest.mark.django_db
def test_rounding_examples_on_the_page(client):
    a, b, c = trio()
    for m in (a, b, c):
        done(m.name, by=m)
    response = view(client)
    assert [v["share"] for v in lines(response).values()] == ["34%", "33%", "33%"]
    assert {v["bar"] for v in lines(response).values()} == {'value="10" max="30"'}


@pytest.mark.django_db
def test_zero_total_bars_use_max_one(client):
    trio()
    got = lines(view(client))
    assert all(v["bar"] == 'value="0" max="1"' and v["share"] == "0%" for v in got.values())


@pytest.mark.django_db
def test_not_credited_share_is_counted_in_the_hundred(client):
    a, b = member("A", "kid", 0), member("B", "kid", 1)
    done("1", by=a)
    done("2", by=None, points=20)
    got = lines(view(client))
    assert [v["share"] for v in got.values()] == ["33%", "0%", "67%"]


# --- Empty states and wording -------------------------------------------------------


@pytest.mark.django_db
def test_no_members(client):
    body = view(client).content.decode()
    assert '<p id="no-members"><a href="/family/">Add your family to see contributions</a></p>' in body
    assert 'id="contribution-list"' not in body
    assert TOTAL.search(body).group(1) == "Household total: 0 points · 0 chores done"


@pytest.mark.django_db
def test_no_members_but_not_credited_rows(client):
    done(by=None)
    response = view(client)
    body = response.content.decode()
    assert 'id="no-members"' in body
    got = lines(response)
    assert list(got) == ["contribution-not-credited"] and got["contribution-not-credited"]["share"] == "100%"
    assert TOTAL.search(body).group(1) == "Household total: 10 points · 1 chores done"


@pytest.mark.django_db
def test_nothing_done(client):
    trio()
    response = view(client)
    assert "contribution-not-credited" not in response.content.decode()
    assert TOTAL.search(response.content.decode()).group(1) == "Household total: 0 points · 0 chores done"


@pytest.mark.django_db
def test_household_total_includes_not_credited(client):
    mom = member("Mom", "adult", 0)
    done("a", by=mom)
    done("b", by=None, points=20)
    assert TOTAL.search(view(client).content.decode()).group(1) == "Household total: 30 points · 2 chores done"


@pytest.mark.django_db
def test_singular_chore_done(client):
    mom = member("Mom", "adult", 0)
    done(by=mom)
    assert lines(view(client))["contribution-member-%d" % mom.pk]["count"] == "1 chore done"


@pytest.mark.django_db
@pytest.mark.parametrize("period", [None, "month"])
def test_no_ranking_words(client, period):
    mom, dad, _ = trio()
    done("a", by=mom, points=20)
    done("b", by=dad)
    body = view(client, period).content.decode().lower()
    for word in ("winner", "loser", "rank", "leader", "1st", "2nd", "3rd", "place", "🏆", "🥇", "🥈", "🥉", "medal"):
        assert word not in body


@pytest.mark.django_db
def test_member_names_are_escaped(client):
    mom = member("<b>Bo</b>", "adult", 0)
    done(by=mom)
    body = view(client).content.decode()
    assert "&lt;b&gt;Bo&lt;/b&gt;" in body and "<b>Bo</b>" not in body


# --- HTMX ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("period,url", [(None, "/contribution/"), ("month", "/contribution/?period=month")])
def test_htmx_partial_with_oob_switcher(client, period, url):
    trio()
    response = view(client, period, data={"x": "1"}, HTTP_HX_REQUEST="true")
    body = response.content.decode()
    assert response.status_code == 200 and "<html" not in body
    assert body.count('<section id="contribution">') == 1
    assert body.lstrip().startswith('<section id="contribution">')
    assert 'hx-swap-oob="true"' in body
    assert f'name="next" value="{url}"' in body


@pytest.mark.django_db
def test_history_restore_gets_full_page(client):
    body = view(client, HTTP_HX_REQUEST="true", HTTP_HX_HISTORY_RESTORE_REQUEST="true").content.decode()
    assert "<html" in body


@pytest.mark.django_db
@pytest.mark.parametrize("period", [None, "month"])
def test_partial_section_equals_full_page_section(client, period):
    mom, _, _ = trio()
    done(by=mom)
    full = view(client, period).content.decode()
    part = view(client, period, HTTP_HX_REQUEST="true").content.decode()
    assert blank_csrf(SECTION.search(full).group(0)) == blank_csrf(SECTION.search(part).group(0))


@pytest.mark.django_db
@pytest.mark.parametrize("headers", [{}, {"HTTP_HX_REQUEST": "true"}])
def test_vary(client, headers):
    vary = view(client, **headers)["Vary"]
    assert "HX-Request" in vary and "HX-History-Restore-Request" in vary


# --- Read-only, queries, clock ------------------------------------------------------


@pytest.mark.django_db
def test_page_writes_nothing_and_does_not_materialize(client):
    from chores import services, views

    trio()
    spy, ctx = no_writes()
    with ctx, mock.patch.object(services, "materialize_week") as sv, mock.patch.object(
        views, "materialize_week"
    ) as vw:
        view(client)
        view(client, "month")
    assert spy.writes == []
    sv.assert_not_called()
    vw.assert_not_called()
    assert ChoreOccurrence.objects.count() == 0


def _count(client):
    with CaptureQueriesContext(connection) as ctx:
        response = view(client)
    assert response.status_code == 200
    return len(ctx)


@pytest.mark.django_db
def test_query_count_is_constant(client):
    mom = member("Mom", "adult", 0)
    done(by=mom)
    small = _count(client)
    for i in range(5):
        member(f"M{i}", "kid", i + 1)
    members = list(Member.objects.all())
    for i in range(59):
        done(f"j{i}", by=members[i % 6])
    assert _count(client) == small


def test_module_does_not_read_the_clock_directly():
    source = inspect.getsource(contribution_views)
    for forbidden in ("datetime.now(", "date.today(", "TIME_ZONE", "datetime.datetime.now"):
        assert forbidden not in source


def test_parse_period_and_days():
    assert parse_period("month") == "month"
    assert parse_period(None) == parse_period("MONTH") == "week"
    assert period_days("month", d("2026-02-10")) == (d("2026-02-01"), d("2026-02-28"))
    assert period_days("month", d("2026-12-31")) == (d("2026-12-01"), d("2026-12-31"))
    assert period_days("week", d("2026-11-18")) == (d("2026-11-16"), d("2026-11-22"))
