"""History view (#17)."""
import datetime
import html
import re
from unittest import mock
from urllib.parse import quote

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import history_views
from chores.models import Chore, ChoreOccurrence, Member, RotationState
from tests.test_dashboard import UTC, WED, at, chore, d, member, no_writes
from tests.test_delete import HOSTILE, OVERFLOW, delete
from tests.test_toggle import activate, toggle

pytestmark = pytest.mark.django_db

DONE_AT = datetime.datetime(2026, 11, 21, 23, 30, tzinfo=UTC)
ROW = re.compile(r'<li class="history-row" id="history-(\d+)">(.*?)</li>', re.S)
HEADING = re.compile(r'<h2><time datetime="([^"]*)">([^<]*)</time></h2>')
HREFS = re.compile(r'<nav aria-label="History pages">(.*?)</nav>', re.S)
HOSTILE_MEMBERS = ["", "abc", "0", "-3", "3.0", " 3", "٣", OVERFLOW, "Unassigned",
                   "x" * 10000, HOSTILE, "3\x00"]
HOSTILE_PAGES = ["", "abc", "-1", "+3", "3.0", " 3", "3 ", "٣", "3_0", "1e3", "3\x00", HOSTILE]


@pytest.fixture
def client():
    return Client()


def hist(client, tz=None, htmx=False, **data):
    if tz is not None:
        client.cookies["tz"] = tz
    extra = {"HTTP_HX_REQUEST": "true"} if htmx else {}
    return client.get("/history/", data, **extra)


def body(client, **kwargs):
    response = hist(client, **kwargs)
    assert response.status_code == 200
    return response.content.decode()


def main(text):
    return re.search(r"<main.*?</main>", text, re.S).group(0)


def make(title="Job", day="2026-11-18", status="done", points=10, **kwargs):
    """One stored row of a fresh one-off chore."""
    c = chore(title, start_date=day, points=points)
    if status == "done":
        kwargs.setdefault("done_at", DONE_AT)
    return ChoreOccurrence.objects.create(
        chore=c, date=d(day), status=status, points=points, **kwargs
    )


def ids(text):
    return [int(i) for i, _ in ROW.findall(text)]


def row_html(text, o):
    return dict(ROW.findall(text))[str(o.pk)]


def text_without_csrf(text):
    """The page minus per-render noise: CSRF tokens and the header clock."""
    text = re.sub(r'(csrfmiddlewaretoken" value=|X-CSRFToken": )"[^"]*"', "", text)
    return re.sub(r"<header.*?<nav", "<nav", text, flags=re.S)


def squash(fragment):
    return re.sub(r"\s+", " ", fragment).strip()


def links(text):
    nav = HREFS.search(text)
    return [html.unescape(h) for h in re.findall(r'href="([^"]*)"', nav.group(1))] if nav else []


def mom_emma():
    return member("Mom", role="adult", order=0, avatar="👩"), member("Emma", order=1, avatar="👧")


# --- What is listed ----------------------------------------------------------------


def test_only_done_and_skipped_rows(client):
    done = make("A", status="done")
    skipped = make("B", status="skipped")
    make("C", status="todo")
    make("D", day="2026-11-10", status="todo")  # overdue is derived from todo
    assert ids(body(client)) == [done.pk, skipped.pk]


def test_projected_occurrences_never_listed_and_nothing_materialized(client):
    chore("Daily", start_date="2026-11-01", recurrence="daily")
    with mock.patch("chores.views.materialize_week") as mat, \
            mock.patch("chores.views.project_occurrences") as proj:
        text = body(client)
    assert not mat.called and not proj.called
    assert "No completed or skipped chores yet" in text
    assert ChoreOccurrence.objects.count() == 0


@pytest.mark.parametrize("status", ["done", "skipped"])
def test_cancelled_rows_hidden(client, status):
    shown = make("Shown")
    make("Gone", status=status, is_cancelled=True)
    assert ids(body(client)) == [shown.pk]


def test_template_rows_hidden(client):
    shown = make("Shown")
    t = Chore.objects.create(title="Tpl", start_date=d("2026-11-18"), is_template=True)
    ChoreOccurrence.objects.create(chore=t, date=d("2026-11-18"), status="done",
                                   points=10, done_at=DONE_AT)
    assert ids(body(client)) == [shown.pk]


def test_undone_rows_vanish_and_return_when_done_again(client):
    mom, _ = mom_emma()
    c = chore("Dishes", start_date="2026-11-18")
    o = ChoreOccurrence.objects.create(chore=c, date=d("2026-11-18"), points=10)
    activate(client, mom)
    toggle(client, o.pk, "done")
    assert ids(body(client)) == [o.pk]
    toggle(client, o.pk, "undo")
    assert ids(body(client)) == []
    toggle(client, o.pk, "done")
    assert ids(body(client)) == [o.pk]
    toggle(client, o.pk, "undo")
    toggle(client, o.pk, "skip")
    assert ids(body(client)) == [o.pk]
    toggle(client, o.pk, "undo")
    assert ids(body(client)) == []


def test_chore_deleted_this_and_future_keeps_history(client):
    c = chore("Weekly", start_date="2026-11-01", recurrence="weekly", recurrence_day=6)
    before = ChoreOccurrence.objects.create(chore=c, date=d("2026-11-08"), status="done",
                                            points=10, done_at=DONE_AT)
    after = ChoreOccurrence.objects.create(chore=c, date=d("2026-11-29"), status="skipped", points=10)
    assert delete(client, c.pk, scope="future").status_code == 302
    c.refresh_from_db()
    assert c.end_date is not None and c.end_date < after.date
    text = body(client)
    assert ids(text) == [after.pk, before.pk]
    assert "Weekly" in text


def test_future_dated_done_row_is_first(client):
    old = make("Old", day="2026-11-01")
    future = make("Early", day="2026-12-25")
    assert ids(body(client)) == [future.pk, old.pk]


def test_legacy_done_without_done_at(client):
    o = make("Legacy", done_at=None)
    o.done_at = None
    o.save()
    text = body(client)
    assert squash(re.search(r'<p class="credit">(.*?)</p>', row_html(text, o), re.S).group(1)) \
        == "Done · not credited"
    assert "<time" not in row_html(text, o)


def test_points_are_the_row_snapshot(client):
    o = make("Job", points=10)
    Chore.objects.filter(pk=o.chore_id).update(points=20)
    assert '<span class="points">10 pts</span>' in row_html(body(client), o)


# --- Order and grouping ------------------------------------------------------------


def test_exact_sort(client):
    zed = make("Zed", day="2026-11-18", status="done")
    skip_a = make("Aaa", day="2026-11-18", status="skipped")
    done_b1 = make("Bbb", day="2026-11-18", status="done")
    done_b2 = make("Bbb", day="2026-11-18", status="done")
    newer = make("Zzz", day="2026-11-19", status="skipped")
    older = make("Aaa", day="2026-11-17", status="done")
    assert ids(body(client)) == [newer.pk, done_b1.pk, done_b2.pk, zed.pk, skip_a.pk, older.pk]


def test_sort_is_by_date_not_done_at(client):
    late = make("Late", day="2026-11-10", done_at=datetime.datetime(2026, 11, 30, tzinfo=UTC))
    recent = make("Recent", day="2026-11-12", done_at=datetime.datetime(2026, 11, 13, tzinfo=UTC))
    assert ids(body(client)) == [recent.pk, late.pk]


def test_date_headings(client):
    make("A", day="2026-11-21")
    make("B", day="2026-11-21", status="skipped")
    make("C", day="2026-11-05")
    text = body(client)
    assert HEADING.findall(text) == [("2026-11-21", "Sat 21 Nov 2026"), ("2026-11-05", "Thu 5 Nov 2026")]
    assert main(text).count("<ul>") == 2


def test_heading_is_split_across_pages(client):
    for i in range(30):
        make(f"A{i:02}", day="2026-11-21")
    for i in range(30):
        make(f"B{i:02}", day="2026-11-20")
    first, second = body(client), body(client, page=2)
    assert len(ids(first)) == 50 and len(ids(second)) == 10
    # 30 of 21 Nov + 20 of 20 Nov on page 1, 10 of 20 Nov on page 2
    assert [h[0] for h in HEADING.findall(first)] == ["2026-11-21", "2026-11-20"]
    assert [h[0] for h in HEADING.findall(second)] == ["2026-11-20"]


# --- Rows --------------------------------------------------------------------------


def test_done_credited_by_someone_else(client):
    mom, emma = mom_emma()
    o = make("Dishes", member=emma, completed_by=mom)
    assert squash(row_html(body(client), o)) == (
        '<strong class="title">Dishes</strong> <span class="status">Done</span> '
        '<span class="points">10 pts</span> '
        '<span class="assignee">Assigned to <span class="avatar">👧</span> <span class="name">Emma</span></span> '
        '<p class="credit">Done by <span class="avatar">👩</span> <span class="name">Mom</span> · '
        '<time datetime="2026-11-21T23:30:00+00:00">Sat 21 Nov, 23:30</time></p>'
    )


def test_done_not_credited(client):
    _, emma = mom_emma()
    o = make("Dishes", member=emma)
    credit = re.search(r'<p class="credit">(.*?)</p>', row_html(body(client), o), re.S).group(1)
    assert squash(credit) == 'Done · not credited · <time datetime="2026-11-21T23:30:00+00:00">Sat 21 Nov, 23:30</time>'


def test_skipped_unassigned_row(client):
    o = make("Dishes", status="skipped")
    assert squash(row_html(body(client), o)) == (
        '<strong class="title">Dishes</strong> <span class="status">Skipped</span> '
        '<span class="points">—</span> <span class="assignee">Unassigned</span>'
    )


@pytest.mark.parametrize("tz, iso, text", [
    (None, "2026-11-21T23:30:00+00:00", "Sat 21 Nov, 23:30"),
    ("UTC", "2026-11-21T23:30:00+00:00", "Sat 21 Nov, 23:30"),
    ("Europe/Vilnius", "2026-11-22T01:30:00+02:00", "Sun 22 Nov, 01:30"),
    ("Pacific/Pago_Pago", "2026-11-21T12:30:00-11:00", "Sat 21 Nov, 12:30"),
    (HOSTILE, "2026-11-21T23:30:00+00:00", "Sat 21 Nov, 23:30"),
])
def test_done_at_in_viewer_zone_heading_unchanged(client, tz, iso, text):
    o = make("Dishes", day="2026-11-21")
    page = body(client, tz=tz)
    assert f'<time datetime="{iso}">{text}</time></p>' in row_html(page, o)
    assert HEADING.findall(page) == [("2026-11-21", "Sat 21 Nov 2026")]


def test_rows_are_read_only(client):
    make("Dishes")
    text = main(body(client))
    for needle in ("<form method=\"post\"", "hx-", "Undo", "Delete", "<input type=\"text\""):
        assert needle not in text


# --- Member filter -----------------------------------------------------------------


def test_filter_assignee_or_credited_each_row_once(client):
    mom, emma = mom_emma()
    a = make("A", member=emma, completed_by=mom)
    b = make("B", member=mom, completed_by=mom)
    c = make("C", member=emma)
    e = make("E", member=None, completed_by=emma)
    assert sorted(ids(body(client, member=mom.pk))) == sorted([a.pk, b.pk])
    assert sorted(ids(body(client, member=emma.pk))) == sorted([a.pk, c.pk, e.pk])


def test_filter_unassigned_whoever_credited(client):
    mom, emma = mom_emma()
    make("A", member=emma)
    u = make("U", member=None, completed_by=mom)
    s = make("S", member=None, status="skipped")
    assert sorted(ids(body(client, member="unassigned"))) == sorted([u.pk, s.pk])


def test_zero_padded_member(client):
    mom, emma = mom_emma()
    a = make("A", member=mom)
    make("B", member=emma)
    assert ids(body(client, member=f"0{mom.pk}")) == [a.pk]


def test_deleted_member_pk_means_everyone(client):
    mom, emma = mom_emma()
    make("A", member=mom)
    make("B", member=emma)
    pk = emma.pk
    emma.delete()
    text = body(client, member=pk)
    assert text_without_csrf(text) == text_without_csrf(body(client))
    assert len(ids(text)) == 2
    assert f'value="{pk}"' not in main(text)
    assert re.search(r'<option value=""\s+selected>', text)


@pytest.mark.parametrize("value", HOSTILE_MEMBERS, ids=lambda v: repr(v[:12]))
def test_hostile_member_is_everyone(client, value):
    mom, _ = mom_emma()
    make("A", member=mom)
    plain = body(client)
    text = body(client, member=value)
    assert text_without_csrf(text) == text_without_csrf(plain)
    assert HOSTILE not in text and quote(HOSTILE) not in text and "&lt;script&gt;" not in text


def test_repeated_member_last_wins(client):
    mom, emma = mom_emma()
    a = make("A", member=mom)
    b = make("B", member=emma)
    assert ids(hist_repeat(client, "member", [mom.pk, emma.pk])) == [b.pk]
    assert sorted(ids(hist_repeat(client, "member", [emma.pk, "abc"]))) == sorted([a.pk, b.pk])


def hist_repeat(client, name, values):
    response = client.get("/history/?" + "&".join(f"{name}={v}" for v in values))
    assert response.status_code == 200
    return response.content.decode()


def test_unrelated_parameters_ignored_and_not_carried(client):
    for i in range(51):
        make(f"J{i:02}")
    text = body(client, week="2026-W47", day="3", x=HOSTILE)
    assert links(text) == ["/history/?page=2"]
    assert "week" not in text.replace("weekday", "") and "2026-W47" not in text


def test_filter_form(client):
    mom, emma = mom_emma()
    make("A", member=emma)
    text = main(body(client, member=emma.pk))
    assert '<form method="get" action="/history/">' in text
    assert '<label for="history-member">Member</label>' in text
    assert '<select id="history-member" name="member">' in text
    assert '<button type="submit">Show</button>' in text
    assert "page" not in re.search(r"<form.*?</form>", text, re.S).group(0)
    options = re.findall(r'<option value="([^"]*)"( selected)?>([^<]*)</option>', text)
    assert options == [("", "", "Everyone"), (str(mom.pk), "", "Mom"),
                       (str(emma.pk), " selected", "Emma"), ("unassigned", "", "Unassigned")]


def test_filter_form_selection_and_member_order(client):
    b = member("Bo", order=1)
    a = member("Al", order=0)
    for value, selected in [("unassigned", "unassigned"), ("abc", ""), (str(b.pk), str(b.pk))]:
        options = re.findall(r'<option value="([^"]*)"( selected)?>', body(client, member=value))
        assert [v for v, s in options if s] == [selected]
        assert [v for v, _ in options] == ["", str(a.pk), str(b.pk), "unassigned"]


def test_filter_form_without_members(client):
    options = re.findall(r'<option value="([^"]*)"', body(client))
    assert options == ["", "unassigned"]


# --- Pagination --------------------------------------------------------------------


def fill(n):
    c = chore("Bulk", start_date="2026-01-01")
    base = d("2026-01-01")
    for i in range(n):
        ChoreOccurrence.objects.create(
            chore=Chore.objects.create(title=f"T{i:03}", start_date=base),
            date=base + datetime.timedelta(days=i), status="skipped", points=5,
        )
    c.delete()


def test_fifty_per_page_and_pager(client):
    fill(120)
    assert [len(ids(body(client, page=p))) for p in (1, 2, 3)] == [50, 50, 20]
    one = body(client)
    assert "Page 1 of 3" in one and links(one) == ["/history/?page=2"]
    two = body(client, page=2)
    assert "Page 2 of 3" in two and links(two) == ["/history/", "/history/?page=3"]
    assert [a for a in re.findall(r"<a [^>]*>(.*?)</a>", HREFS.search(two).group(1))] == ["Newer", "Older"]
    three = body(client, page=3)
    assert links(three) == ["/history/?page=2"] and "Page 3 of 3" in three


def test_single_page_has_no_pager(client):
    fill(50)
    text = body(client)
    assert len(ids(text)) == 50 and "History pages" not in text


@pytest.mark.parametrize("value", HOSTILE_PAGES + ["0"], ids=lambda v: repr(v[:12]))
def test_bad_or_zero_page_is_first(client, value):
    fill(120)
    assert ids(body(client, page=value)) == ids(body(client))


@pytest.mark.parametrize("value", [OVERFLOW, "1000000", "9" * 10000, "999"], ids=lambda v: repr(v[:12]))
def test_long_or_past_the_end_page_is_last(client, value):
    fill(120)
    with mock.patch.object(history_views, "int", create=True, side_effect=int) as spy_int:
        text = body(client, page=value)
    assert ids(text) == ids(body(client, page=3))
    assert "Page 3 of 3" in text
    if len(value) > 6:
        assert not any(c.args and c.args[0] == value for c in spy_int.call_args_list)


def test_leading_zeros_page(client):
    fill(120)
    assert ids(body(client, page="002")) == ids(body(client, page=2))


def test_page_number_rule_directly():
    assert history_views._page_number(None) == 1
    assert history_views._page_number("0") == 1
    assert history_views._page_number("1234567") is None


def test_repeated_page_last_wins(client):
    fill(120)
    assert ids(hist_repeat(client, "page", [3, 2])) == ids(body(client, page=2))
    assert ids(hist_repeat(client, "page", [2, "abc"])) == ids(body(client))


def test_links_order_member_then_page(client):
    mom, _ = mom_emma()
    for i in range(120):
        make(f"J{i:03}", day="2026-11-18", member=mom)
    assert links(body(client, member=mom.pk)) == [f"/history/?member={mom.pk}&page=2"]
    assert links(body(client, member=mom.pk, page=2)) == [
        f"/history/?member={mom.pk}", f"/history/?member={mom.pk}&page=3"]
    assert links(body(client, member="unassigned", page=2)) == []
    for i in range(60):
        make(f"U{i:03}", member=None)
    assert links(body(client, member="unassigned", page=2)) == ["/history/?member=unassigned"]
    assert links(body(client, member="unassigned")) == ["/history/?member=unassigned&page=2"]


def test_constant_query_count(client):
    mom, emma = mom_emma()
    for i in range(3):
        make(f"J{i:03}", member=emma, completed_by=mom)
    with CaptureQueriesContext(connection) as small:
        hist(client)
    for i in range(117):
        make(f"K{i:03}", member=emma, completed_by=mom)
    with CaptureQueriesContext(connection) as big:
        text = body(client)
    assert len(ids(text)) == 50
    assert len(small) == len(big)


# --- Empty states ------------------------------------------------------------------


@pytest.mark.parametrize("data", [{}, {"member": "unassigned"}, {"page": "5"}])
def test_nothing_ever_done(client, data):
    mom_emma()
    make("Open", status="todo")
    text = body(client, **data)
    assert "No completed or skipped chores yet" in text
    assert '<select id="history-member"' in text and "History pages" not in text


def test_filter_leaves_nothing(client):
    mom, emma = mom_emma()
    make("A", member=mom)
    text = body(client, member=emma.pk)
    assert "Nothing here" in text and "No completed or skipped" not in text
    assert '<select id="history-member"' in text and "History pages" not in text


# --- Deleted members ---------------------------------------------------------------


def test_deleted_assignee_shows_unassigned(client):
    mom, emma = mom_emma()
    o = make("A", member=emma, completed_by=mom)
    emma.delete()
    assert '<span class="assignee">Unassigned</span>' in row_html(body(client), o)
    assert ids(body(client, member="unassigned")) == [o.pk]


def test_deleted_credited_member_is_not_credited(client):
    mom, emma = mom_emma()
    o = make("A", member=emma, completed_by=mom)
    mom.delete()
    fragment = row_html(body(client), o)
    assert "Done · not credited · <time" in fragment and "Done by" not in fragment
    o.refresh_from_db()
    assert o.status == "done" and o.done_at == DONE_AT


# --- Full page, methods, writes ----------------------------------------------------


def test_full_page_even_with_htmx(client):
    mom_emma()
    make("A")
    for text in (body(client), body(client, htmx=True)):
        assert text.startswith("<!doctype html>")
        assert '<nav id="profile-switcher"' in text and "<header" in text
        assert "hx-get" not in main(text) and "hx-post" not in main(text)
    assert 'hx-' not in re.sub(r"<body[^>]*>", "", body(client, htmx=True)).split("<main", 1)[1]


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_other_methods_405(client, method):
    assert getattr(client, method)("/history/").status_code == 405


def test_writes_nothing(client):
    mom, emma = mom_emma()
    c = chore("Rot", start_date="2026-11-01", mode="rotating", eligibility="everyone")
    o = ChoreOccurrence.objects.create(chore=c, date=d("2026-11-18"), status="done",
                                       points=10, done_at=DONE_AT, member=emma)
    RotationState.objects.create(chore=c, last_member=mom)

    def state():
        return (list(ChoreOccurrence.objects.values()), list(Chore.objects.values()),
                list(RotationState.objects.values()))

    before = state()
    spy, wrapper = no_writes()
    with wrapper, at(WED):
        for data in ({}, {"member": mom.pk}, {"member": "unassigned", "page": 9}, {"page": HOSTILE}):
            assert hist(client, **data).status_code == 200
    assert spy.writes == [] and state() == before


def test_fresh_db_creates_no_rows(client):
    with at(WED):
        assert hist(client).status_code == 200
    assert ChoreOccurrence.objects.count() == 0


# --- Switcher next, escaping and echo ----------------------------------------------


def next_value(text):
    values = set(re.findall(r'name="next" value="([^"]*)"', text))
    assert len(values) == 1
    return html.unescape(values.pop())


def test_switcher_next_is_canonical(client):
    mom, _ = mom_emma()
    for i in range(120):
        make(f"J{i:03}", member=mom)
    assert next_value(body(client, member=mom.pk, page=999)) == f"/history/?member={mom.pk}&page=3"
    assert next_value(body(client, member=HOSTILE, x=1)) == "/history/"
    assert next_value(body(client, week="2026-W47", page="002")) == "/history/?page=2"
    assert next_value(body(client, member=f"0{mom.pk}", page=1)) == f"/history/?member={mom.pk}"


@pytest.mark.parametrize("param", ["member", "page"])
def test_raw_value_never_echoed(client, param):
    mom_emma()
    for i in range(60):
        make(f"J{i:03}")
    response = hist(client, **{param: HOSTILE})
    assert response.status_code == 200
    text = response.content.decode()
    for form in (HOSTILE, html.escape(HOSTILE), quote(HOSTILE), "&lt;script&gt;", "%3Cscript%3E",
                 "%3cscript%3e", "alert(1)"):
        assert form not in text


def test_markup_in_data_is_escaped(client):
    bo = member("<b>Bo</b>", avatar="👧")
    o = make("<i>x</i>", member=bo, completed_by=bo)
    text = body(client)
    assert "<i>x</i>" not in text and "<b>Bo</b>" not in text
    fragment = row_html(text, o)
    assert "&lt;i&gt;x&lt;/i&gt;" in fragment
    assert fragment.count("&lt;b&gt;Bo&lt;/b&gt;") == 2
    assert "&lt;b&gt;Bo&lt;/b&gt;</option>" in text
