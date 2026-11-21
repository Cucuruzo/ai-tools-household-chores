"""Monthly calendar (#14)."""
import datetime
import html
import re
from unittest import mock
from urllib.parse import quote

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import month as month_helpers
from chores.models import Chore, ChoreOccurrence, Member, RotationState
from chores.services import materialize_week, project_occurrences

pytestmark = pytest.mark.django_db

UTC = datetime.timezone.utc
WED = "2026-11-18T12:00:00"  # Wed 18 Nov 2026: current month 2026-11, window 2025-11..2027-11


def d(text):
    return datetime.date.fromisoformat(text)


def at(text):
    return mock.patch(
        "django.utils.timezone.now",
        return_value=datetime.datetime.fromisoformat(text).replace(tzinfo=UTC),
    )


@pytest.fixture
def client():
    return Client()


def get(client, month=None, when=WED, tz=None, data=None, **headers):
    if tz is not None:
        client.cookies["tz"] = tz
    params = dict(data or {})
    if month is not None:
        params["month"] = month
    with at(when):
        return client.get("/calendar/", params, **headers)


def page(client, *args, **kwargs):
    response = get(client, *args, **kwargs)
    assert response.status_code == 200
    return response.content.decode()


def spy_materialize():
    return mock.patch("chores.views.materialize_week", wraps=materialize_week)


# --- Factories -------------------------------------------------------------------


def member(name, role="kid", order=None):
    return Member.objects.create(name=name, role=role, avatar="😀", order=order)


def chore(title="Dishes", start_date="2026-11-16", recurrence="daily", mode="unassigned", **kwargs):
    return Chore.objects.create(
        title=title, start_date=d(start_date), recurrence=recurrence, assignment_mode=mode, **kwargs
    )


def occ(title, day, status="todo", due=None, cancelled=False, **kwargs):
    c = chore(title, start_date=day, recurrence="once", **kwargs)
    return ChoreOccurrence.objects.create(
        chore=c, date=d(day), status=status, points=10, is_cancelled=cancelled,
        due_time=datetime.time.fromisoformat(due) if due else None,
    )


def counts():
    return (
        ChoreOccurrence.objects.count(), RotationState.objects.count(), Chore.objects.count(),
        sorted(RotationState.objects.values_list("chore_id", "last_member_id")),
    )


# --- Parsing the page ----------------------------------------------------------------

ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
CELL = re.compile(r"<td([^>]*)>(.*?)</td>", re.S)
DOT = re.compile(r'<span class="dot dot-([a-z]+)" title="([^"]*)"></span>')


def grid(body):
    """Rows of cells; a cell has day, filler, today, count, dots, more and its links."""
    rows = []
    tbody = body.split("<tbody>")[1].split("</tbody>")[0]
    for row in ROW.findall(tbody):
        cells = []
        for attrs, inner in CELL.findall(row):
            number = re.search(r'class="day-number"[^>]*>(\d+)</a>', inner)
            count = re.search(r'class="count" aria-label="([^"]*)">(\d+)<', inner)
            more = re.search(r'class="more">\+(\d+)<', inner)
            cells.append({
                "day": int(number.group(1)) if number else int(re.sub(r"<[^>]+>", "", inner).strip()),
                "filler": 'class="filler"' in attrs,
                "today": 'class="today"' in attrs and 'aria-current="date"' in attrs,
                "attrs": attrs,
                "count": int(count.group(2)) if count else 0,
                "count_label": count.group(1) if count else None,
                "dots": [(s, html.unescape(t)) for s, t in DOT.findall(inner)],
                "more": int(more.group(1)) if more else 0,
                "links": [html.unescape(h) for h in re.findall(r'href="([^"]*)"', inner)],
                "html": inner,
            })
        rows.append(cells)
    return rows


def cell(body, day):
    found = [c for row in grid(body) for c in row if c["day"] == day and not c["filler"]]
    assert len(found) == 1
    return found[0]


def flat(body):
    return [c for row in grid(body) for c in row]


def title_of(body):
    return re.search(r'<h1 id="month-title">(.*?)</h1>', body).group(1)


def link(body, label):
    m = re.search(rf'<a [^>]*aria-label="{label}"[^>]*>', body)
    return m.group(0) if m else None


def attr(tag, name):
    return html.unescape(re.search(rf'{name}="([^"]*)"', tag).group(1))


# --- Setup ---------------------------------------------------------------------------


def test_get_ok_extends_base_with_switcher(client):
    member("Ann")
    body = page(client)
    assert 'id="profile-switcher"' in body and "<html" in body
    assert title_of(body) == "November 2026"
    assert get(client).status_code == 200


def test_post_not_allowed(client):
    assert client.post("/calendar/").status_code == 405


def test_url_name():
    from django.urls import reverse
    assert reverse("calendar") == "/calendar/"


def test_view_reads_the_clock_once_through_localdate():
    import inspect
    from chores import views
    source = inspect.getsource(views._calendar_context) + inspect.getsource(views.calendar)
    for forbidden in ("datetime.now", "date.today", "TIME_ZONE"):
        assert forbidden not in source
    assert source.count("timezone.localdate()") == 1


# --- ?month= and the window ----------------------------------------------------------


def test_default_and_explicit_month(client):
    assert title_of(page(client)) == "November 2026"
    assert title_of(page(client, "2026-09")) == "September 2026"


@pytest.mark.parametrize("value, shown", [
    ("2025-11", "November 2025"), ("2027-11", "November 2027"),
    ("2025-10", "November 2026"), ("2027-12", "November 2026"),
])
def test_window_edges(client, value, shown):
    assert title_of(page(client, value)) == shown


HOSTILE = [
    "abc", "", "2026-00", "2026-13", "2026-9", "26-11", "2026/11", "2026-11-01", " 2026-11",
    "2026-11x", "2026-11\n", "2026-11\x00", "2026-11%00", "٢٠٢٦-١١", "2025-10", "2027-12",
    "0000-01", "9999-12", "x" * 10_000, "<script>alert(1)</script>",
]


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_month_shows_current_month_like_default(client, value):
    member("Ann")
    chore("Dishes")
    default = page(client)
    response = get(client, value)
    assert response.status_code == 200
    body = response.content.decode()
    # The only difference allowed is the CSRF token in the switcher form.
    strip = lambda b: re.sub(r"[A-Za-z0-9]{32,}", "", b)
    assert strip(body) == strip(default)


@pytest.mark.parametrize("value", ["<script>alert(1)</script>", "abc", "2026-11x", "9999-12"])
def test_hostile_month_never_echoed(client, value):
    member("Ann")
    body = page(client, value)
    for form in (value, html.escape(value), html.escape(value, quote=False), quote(value), quote(value, safe="")):
        assert form not in body
    assert "&lt;script&gt;" not in body and "%3Cscript%3E" not in body


def test_overflow_spy_never_projects_outside_window(client):
    chore("Dishes")
    with mock.patch("chores.views.project_occurrences", wraps=project_occurrences) as projected, \
            mock.patch("chores.views._shown_occurrences", wraps=__import__("chores.views", fromlist=["x"])._shown_occurrences) as stored:
        body = page(client, "9999-12")
    assert title_of(body) == "November 2026"
    for call in projected.call_args_list:
        start, end = call.args[:2]
        assert d("2025-11-01") <= start and end <= d("2027-11-30")
    assert projected.called and stored.called
    assert not ChoreOccurrence.objects.filter(date__gt=d("2027-11-30")).exists()


def test_overflow_queries_stay_in_window(client):
    chore("Dishes")
    with CaptureQueriesContext(connection) as queries:
        page(client, "9999-12")
    sql = " ".join(q["sql"] for q in queries)
    assert "9999" not in sql


def test_last_month_value_wins(client):
    assert title_of(get(client, data={"month": ["abc", "2026-09"]}).content.decode()) == "September 2026"
    body = get(client, data={"month": ["2026-09", "abc"]}).content.decode()
    assert title_of(body) == "November 2026"


def test_other_parameters_ignored(client):
    assert title_of(page(client, "2026-09", data={"week": "2026-W01", "x": "y"})) == "September 2026"


# --- Time zone ------------------------------------------------------------------------

T_END = "2026-11-30T12:00:00"


def test_tz_default_utc_month_edge(client):
    with spy_materialize() as materialize:
        body = page(client, when=T_END)
    assert title_of(body) == "November 2026"
    assert [c["day"] for c in flat(body) if c["today"]] == [30]
    materialize.assert_called_once_with(d("2026-11-30"), today=d("2026-11-30"))
    assert title_of(page(client, "2025-11", when=T_END)) == "November 2025"
    assert title_of(page(client, "2027-11", when=T_END)) == "November 2027"
    assert title_of(page(client, "2027-12", when=T_END)) == "November 2026"


def test_tz_kiritimati_is_next_month(client):
    with spy_materialize() as materialize:
        body = page(client, when=T_END, tz="Pacific/Kiritimati")
    assert title_of(body) == "December 2026"
    assert [c["day"] for c in flat(body) if c["today"]] == [1]
    materialize.assert_called_once_with(d("2026-11-30"), today=d("2026-12-01"))
    assert title_of(page(client, "2025-12", when=T_END, tz="Pacific/Kiritimati")) == "December 2025"
    assert title_of(page(client, "2027-12", when=T_END, tz="Pacific/Kiritimati")) == "December 2027"
    assert title_of(page(client, "2025-11", when=T_END, tz="Pacific/Kiritimati")) == "December 2026"
    assert title_of(page(client, "2028-01", when=T_END, tz="Pacific/Kiritimati")) == "December 2026"


def test_tz_pago_pago_is_previous_month(client):
    when = "2026-12-01T05:00:00"
    body = page(client, when=when, tz="Pacific/Pago_Pago")
    assert title_of(body) == "November 2026"
    assert [c["day"] for c in flat(body) if c["today"]] == [30]
    assert title_of(page(client, "2027-12", when=when, tz="Pacific/Pago_Pago")) == "November 2026"


def test_tz_year_change(client):
    with spy_materialize() as materialize:
        body = page(client, when="2026-12-31T12:00:00", tz="Pacific/Kiritimati")
    assert title_of(body) == "January 2027"
    materialize.assert_called_once_with(d("2026-12-28"), today=d("2027-01-01"))


def test_invalid_tz_cookie_uses_utc(client):
    body = page(client, when=T_END, tz="Mars/Base")
    assert title_of(body) == "November 2026"
    assert [c["day"] for c in flat(body) if c["today"]] == [30]


# --- Grid layout ----------------------------------------------------------------------


def test_table_caption_and_headers(client):
    body = page(client)
    assert "<caption>November 2026</caption>" in body
    heads = re.findall(r'<th scope="col">(.*?)</th>', body)
    assert heads == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def shape(client, month, when=WED):
    rows = grid(page(client, month, when=when))
    return len(rows), rows[0][0], rows[-1][-1], rows


@pytest.mark.parametrize("month, nrows, first, last, when", [
    ("2026-11", 6, "2026-10-26", "2026-12-06", WED),
    ("2026-03", 6, "2026-02-23", "2026-04-05", WED),
    ("2026-02", 5, "2026-01-26", "2026-03-01", WED),
    ("2027-02", 4, "2027-02-01", "2027-02-28", WED),
    ("2028-02", 5, "2028-01-31", "2028-03-05", "2027-12-15T12:00:00"),
    ("2026-12", 5, "2026-11-30", "2027-01-03", WED),
    ("2027-01", 5, "2026-12-28", "2027-01-31", WED),
])
def test_grid_shapes(client, month, nrows, first, last, when):
    n, first_cell, last_cell, rows = shape(client, month, when)
    assert n == nrows
    assert all(len(r) == 7 for r in rows)
    assert first_cell["day"] == d(first).day and first_cell["filler"] == (d(first).month != int(month[-2:]))
    assert last_cell["day"] == d(last).day
    # Jan 2027 ends on Sunday the 31st; Dec 2026 ends on the 3rd (filler).
    assert last_cell["filler"] == (d(last).month != int(month[-2:]))


def test_february_2027_has_no_fillers(client):
    assert not any(c["filler"] for c in flat(page(client, "2027-02")))


def test_leap_february_2028(client):
    body = page(client, "2028-02", when="2027-12-15T12:00:00")
    assert cell(body, 29)["filler"] is False
    assert len([c for c in flat(body) if not c["filler"]]) == 29


def test_in_month_cells_show_day_number_filler_has_nothing(client):
    occ("Next month chore", "2026-12-02")
    chore("Dishes", start_date="2026-11-01")
    body = page(client, "2026-11")
    fillers = [c for c in flat(body) if c["filler"]]
    assert len(fillers) == 6 + 6  # Oct 26-31 and Dec 1-6
    for f in fillers:
        assert f["count"] == 0 and not f["dots"] and not f["links"] and "<a" not in f["html"]
    # Dec 2 chore shows only in December.
    assert cell(page(client, "2026-12"), 2)["count"] >= 1


# --- Today ----------------------------------------------------------------------------


def test_today_marker_only_in_current_month(client):
    body = page(client)
    marked = [c for c in flat(body) if c["today"]]
    assert len(marked) == 1 and marked[0]["day"] == 18 and not marked[0]["filler"]
    assert body.count('aria-current="date"') == 1
    for other in ("2026-10", "2026-12", "2027-11"):
        assert 'aria-current="date"' not in page(client, other)


def test_today_filler_not_marked(client):
    body = page(client, "2026-12", when=T_END)
    assert 'aria-current="date"' not in body
    first = grid(body)[0][0]
    assert first["day"] == 30 and first["filler"] and not first["today"]


# --- What a day shows -----------------------------------------------------------------


def test_stored_and_projected_counts_and_aria_label(client):
    chore("Dishes", start_date="2026-11-01")
    occ("One off", "2026-11-18")
    body = page(client)
    c = cell(body, 18)
    assert c["count"] == 2 and c["count_label"] == "2 chores"
    one = cell(page(client, "2026-12"), 5)
    assert one["count"] == 1 and one["count_label"] == "1 chore"


def test_empty_day_has_no_number_or_dots(client):
    c = cell(page(client), 5)
    assert c["count"] == 0 and not c["dots"] and c["more"] == 0 and 'class="count"' not in c["html"]


def test_past_month_shows_stored_rows_only_and_writes_nothing(client):
    chore("Dishes", start_date="2026-01-01")
    occ("Old", "2026-10-28", status="done")
    before = counts()
    with spy_materialize() as materialize, \
            mock.patch("chores.views.project_occurrences") as projected:
        body = page(client, "2026-10")
    assert not materialize.called and not projected.called
    assert counts() == before
    assert cell(body, 28)["count"] == 1 and cell(body, 28)["dots"] == [("done", "Old · Done")]
    assert cell(body, 27)["count"] == 0


def test_past_days_of_current_week_in_previous_month_not_backfilled(client):
    chore("Dishes", start_date="2026-10-01")
    body = page(client, "2026-10", when="2026-11-01T12:00:00")  # Sun Nov 1
    assert cell(body, 28)["count"] == 0
    # The current week (Oct 26 - Nov 1) was materialized for the current month only.
    with spy_materialize():
        page(client, "2026-10", when="2026-11-01T12:00:00")
    assert not ChoreOccurrence.objects.filter(date=d("2026-10-27")).exists()


def test_past_month_shows_only_stored_for_days_before_the_week(client):
    chore("Dishes", start_date="2026-10-01")
    occ("Kept", "2026-10-29")
    body = page(client, "2026-10", when="2026-11-01T12:00:00")
    assert cell(body, 29)["count"] == 1
    assert cell(body, 30)["count"] == 0


def test_cancelled_rows_not_counted_and_not_projected_again(client):
    c = chore("Dishes", start_date="2026-11-16")
    cancelled = ChoreOccurrence.objects.create(
        chore=c, date=d("2026-11-18"), points=10, is_cancelled=True)
    occ("Cancelled one-off", "2026-10-20", cancelled=True)
    body = page(client)
    assert cell(body, 18)["count"] == 0
    assert cell(body, 19)["count"] == 1 and cell(body, 20)["count"] == 1
    assert cell(page(client, "2026-10"), 20)["count"] == 0
    # A cancelled future row is not projected again either.
    ChoreOccurrence.objects.create(chore=c, date=d("2026-12-10"), points=10, is_cancelled=True)
    assert cell(page(client, "2026-12"), 10)["count"] == 0
    assert cell(page(client, "2026-12"), 11)["count"] == 1
    cancelled.refresh_from_db()
    assert cancelled.is_cancelled


def test_end_date_stops_future_projection_keeps_stored(client):
    chore("Dishes", start_date="2026-11-01", end_date=d("2026-11-25"))
    body = page(client)
    assert cell(body, 18)["count"] == 1 and cell(body, 25)["count"] == 1
    assert cell(body, 26)["count"] == 0
    assert cell(page(client, "2026-12"), 1)["count"] == 0


def test_templates_never_show(client):
    chore("Household template", start_date="2026-11-01", is_template=True)
    chore("Built-in", start_date="2026-11-01", is_template=True, is_builtin=True)
    ChoreOccurrence.objects.create(
        chore=Chore.objects.get(title="Household template"), date=d("2026-10-20"), points=10)
    assert all(c["count"] == 0 for c in flat(page(client)))
    assert all(c["count"] == 0 for c in flat(page(client, "2026-12")))
    assert all(c["count"] == 0 for c in flat(page(client, "2026-10")))
    assert "No chores this month" in page(client)


def test_dot_statuses_and_labels(client):
    occ("Todo", "2026-11-20")
    occ("Overdue", "2026-11-10")
    occ("Done", "2026-11-11", status="done")
    occ("Skipped", "2026-11-12", status="skipped")
    chore("Planned", start_date="2026-11-30", recurrence="once")
    body = page(client)
    assert cell(body, 20)["dots"] == [("todo", "Todo · To do")]
    assert cell(body, 10)["dots"] == [("overdue", "Overdue · Overdue")]
    assert cell(body, 11)["dots"] == [("done", "Done · Done")]
    assert cell(body, 12)["dots"] == [("skipped", "Skipped · Skipped")]
    assert cell(body, 30)["dots"] == [("planned", "Planned · Planned")]


def test_todo_today_is_todo_not_overdue_even_with_past_due_time(client):
    occ("Today", "2026-11-18", due="01:00")
    assert cell(page(client), 18)["dots"] == [("todo", "Today · To do")]


def test_status_classes_have_distinct_colours():
    from pathlib import Path
    css = Path("chores/templates/chores/calendar.html").read_text()
    rules = dict(re.findall(r"\.dot-(\w+) \{ ([^}]*) \}", css))
    assert set(rules) == {"todo", "overdue", "done", "skipped", "planned"}
    assert len(set(rules.values())) == 5


def test_three_dots_and_more(client):
    for i in range(3):
        occ(f"C{i}", "2026-11-20")
    c = cell(page(client), 20)
    assert c["count"] == 3 and len(c["dots"]) == 3 and c["more"] == 0 and "+" not in c["html"].split("add-chore")[0]
    for i in range(3, 5):
        occ(f"C{i}", "2026-11-20")
    c = cell(page(client), 20)
    assert c["count"] == 5 and len(c["dots"]) == 3 and c["more"] == 2


def test_dot_order_is_due_time_title_then_chore_id(client):
    occ("Zed", "2026-11-20", due="08:00")
    occ("Beta", "2026-11-20")
    occ("Alpha", "2026-11-20")
    occ("Early", "2026-11-20", due="07:00")
    dots = cell(page(client), 20)["dots"]
    assert [t.split(" · ")[0] for _, t in dots] == ["Early", "Zed", "Alpha"]
    # Same title and time: chore id decides.
    entries = [("todo", None, "Same", 9), ("done", None, "Same", 2)]
    assert [x["status"] for x in month_helpers.day_cell(entries)["dots"]] == ["done", "todo"]


def test_empty_month_message(client):
    body = page(client, "2026-10")
    assert "<table>" in body and "No chores this month" in body
    assert 'href="/chores/create/?return_to=calendar&amp;month=2026-10">Add a chore' in body
    chore("Dishes")
    assert "No chores this month" not in page(client)


def test_title_escaped_in_dot(client):
    occ("<script>alert(1)</script>", "2026-11-20")
    body = page(client)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt; · To do" in body
    assert len(re.findall(r"<script", body)) == len(re.findall(r"<script", page(client, "2026-10")))


# --- Materialization ------------------------------------------------------------------


@pytest.fixture
def household():
    a, b = member("Ann", "adult", 0), member("Bo", "kid", 1)
    chore("Daily rot", start_date="2026-11-01", mode="rotating", eligibility="everyone")
    chore("Weekly", start_date="2026-11-01", recurrence="weekly", recurrence_day=2,
          mode="fixed", assigned_to=a)
    chore("Monthly", start_date="2026-11-01", recurrence="monthly", recurrence_day=20)
    return a, b


@pytest.mark.parametrize("month", [None, "2026-11", "abc", "2027-12", "9999-12"])
def test_current_month_materializes_current_week_with_matching_dates(client, household, month):
    with spy_materialize() as materialize:
        page(client, month)
    materialize.assert_called_once_with(d("2026-11-16"), today=d("2026-11-18"))
    assert ChoreOccurrence.objects.filter(date__gte=d("2026-11-16"), date__lte=d("2026-11-22")).count() > 0
    assert not ChoreOccurrence.objects.filter(date__gt=d("2026-11-22")).exists()


def test_htmx_request_landing_on_current_month_materializes(client, household):
    with spy_materialize() as materialize:
        page(client, "abc", HTTP_HX_REQUEST="true")
    assert materialize.call_count == 1


def test_second_load_creates_nothing(client, household):
    page(client)
    first = counts()
    members = {s.chore_id: s.last_member_id for s in RotationState.objects.all()}
    page(client)
    assert counts() == first
    assert members == {s.chore_id: s.last_member_id for s in RotationState.objects.all()}


def test_calendar_then_dashboard_equals_dashboard_alone(household):
    def rows():
        return sorted(ChoreOccurrence.objects.values_list("chore_id", "date", "member_id", "status"))

    def last_members():
        return sorted(RotationState.objects.values_list("chore_id", "last_member_id"))

    first = Client()
    with at(WED):
        first.get("/calendar/")
        first.get("/dashboard/")
    both = (rows(), last_members())
    ChoreOccurrence.objects.all().delete()
    RotationState.objects.all().delete()
    with at(WED):
        Client().get("/dashboard/")
    assert (rows(), last_members()) == both


@pytest.mark.parametrize("month", ["2026-10", "2026-12", "2027-11"])
def test_other_months_write_nothing(client, household, month):
    before = counts()
    with spy_materialize() as materialize:
        page(client, month)
    assert not materialize.called
    assert counts() == before


def test_december_on_nov_30_projects_current_week_days_without_storing(client, household):
    before = counts()
    with spy_materialize() as materialize:
        body = page(client, "2026-12", when=T_END)
    assert not materialize.called and counts() == before
    for day in range(1, 7):
        assert ("planned", "Daily rot · Planned") in cell(body, day)["dots"]
    assert not ChoreOccurrence.objects.filter(date__gte=d("2026-12-01")).exists()


# --- Calendar vs dashboard --------------------------------------------------------------


def test_counts_and_statuses_match_dashboard_for_six_weeks(client, household):
    gone = chore("Gone", start_date="2026-11-01", recurrence="weekly", recurrence_day=3)
    ChoreOccurrence.objects.create(chore=gone, date=d("2026-11-19"), points=10, is_cancelled=True)
    chore("Template", start_date="2026-11-01", is_template=True)
    occ("Once", "2026-11-17", status="done")
    occ("Overdue once", "2026-11-16")
    months = {m: page(client, m) for m in ("2026-11", "2026-12", "2027-01")}
    card = re.compile(r'<article class="chore-card".*?<span class="status">(.*?)</span>', re.S)
    start = d("2026-11-16")
    for offset in range(6 * 7):
        day = start + datetime.timedelta(days=offset)
        monday = day - datetime.timedelta(days=day.weekday())
        iso = monday.isocalendar()
        week = f"{iso[0]}-W{iso[1]:02d}"
        with at(WED):
            dash = client.get("/dashboard/", {"week": week, "day": day.isoweekday()}).content.decode()
        # Cards in a section appear once; "Done earlier" repeats nothing.
        statuses = sorted(card.findall(dash))
        body = months[f"{day.year}-{day.month:02d}"]
        c = cell(body, day.day)
        assert c["count"] == len(statuses), day
        if c["count"] <= 3:
            assert sorted(x[1].rsplit(" · ", 1)[1] for x in c["dots"]) == statuses, day


# --- Performance ----------------------------------------------------------------------


def big_household():
    members = [member(f"M{i}", "adult" if i % 2 else "kid", i) for i in range(6)]
    chores = []
    for i in range(20):
        chores.append(Chore(title=f"Daily {i}", start_date=d("2026-11-01"), recurrence="daily",
                            assignment_mode="rotating", eligibility="everyone"))
        chores.append(Chore(title=f"Weekly {i}", start_date=d("2026-11-01"), recurrence="weekly",
                            recurrence_day=i % 7, assignment_mode="rotating", eligibility="everyone"))
        chores.append(Chore(title=f"Monthly {i}", start_date=d("2026-11-01"), recurrence="monthly",
                            recurrence_day=i + 1, assignment_mode="fixed", assigned_to=members[0]))
    Chore.objects.bulk_create(chores)


def query_count(client, month):
    with CaptureQueriesContext(connection) as queries:
        get(client, month)
    return len(queries)


def test_query_count_equal_for_furthest_month(client):
    big_household()
    assert query_count(client, "2027-11") == query_count(client, "2026-12")


# --- Navigation -----------------------------------------------------------------------


def test_prev_next_links(client):
    body = page(client, "2026-12")
    prev, nxt = link(body, "Previous month"), link(body, "Next month")
    assert attr(prev, "href") == "/calendar/"  # November is the current month
    assert attr(nxt, "href") == "/calendar/?month=2027-01"
    body = page(client, "2027-01")
    assert attr(link(body, "Previous month"), "href") == "/calendar/?month=2026-12"
    assert attr(link(body, "Next month"), "href") == "/calendar/?month=2027-02"


def test_current_month_links(client):
    body = page(client)
    assert attr(link(body, "Previous month"), "href") == "/calendar/?month=2026-10"
    assert attr(link(body, "Next month"), "href") == "/calendar/?month=2026-12"


def test_edges_hide_links(client):
    first, last = page(client, "2025-11"), page(client, "2027-11")
    assert link(first, "Previous month") is None and link(first, "Next month")
    assert link(last, "Next month") is None and link(last, "Previous month")


def test_this_month_link(client):
    assert 'id="this-month"' not in page(client)
    for other in ("2026-10", "2026-12", "2027-11"):
        body = page(client, other)
        tag = re.search(r'<a id="this-month"[^>]*>', body).group(0)
        assert attr(tag, "href") == "/calendar/"


def test_links_carry_only_normalized_months(client):
    body = page(client, "2026-09")
    for url in re.findall(r'(?:href|hx-get)="(/calendar/[^"]*)"', body):
        assert re.fullmatch(r"/calendar/(\?month=[0-9]{4}-[0-9]{2})?", url)


# --- HTMX vs full page ------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Previous month", "Next month"])
def test_nav_links_carry_htmx_attributes(client, name):
    tag = link(page(client, "2026-12"), name)
    assert attr(tag, "hx-get") == attr(tag, "href")
    assert attr(tag, "hx-target") == "#calendar-grid"
    assert attr(tag, "hx-swap") == "outerHTML"
    assert attr(tag, "hx-push-url") == "true"


def test_this_month_carries_htmx_attributes(client):
    tag = re.search(r'<a id="this-month"[^>]*>', page(client, "2026-12")).group(0)
    assert attr(tag, "hx-get") == attr(tag, "href") == "/calendar/"
    assert (attr(tag, "hx-target"), attr(tag, "hx-swap"), attr(tag, "hx-push-url")) == (
        "#calendar-grid", "outerHTML", "true")


def test_htmx_returns_partial_only(client):
    member("Ann")
    response = get(client, "2026-12", HTTP_HX_REQUEST="true")
    body = response.content.decode()
    assert response.status_code == 200
    assert "<html" not in body
    assert body.count('<nav id="profile-switcher" hx-swap-oob="true"') == 1
    assert body.rindex("</section>") < body.index('id="profile-switcher"')
    assert body.count('<section id="calendar-grid">') == 1
    assert "December 2026" in body and "<table>" in body
    assert link(body, "Previous month") and link(body, "Next month")


@pytest.mark.parametrize("headers", [
    {}, {"HTTP_HX_REQUEST": "true", "HTTP_HX_HISTORY_RESTORE_REQUEST": "true"},
])
def test_full_page_for_plain_and_history_restore(client, headers):
    member("Ann")
    response = get(client, "2026-12", **headers)
    body = response.content.decode()
    assert "<html" in body
    assert body.count('id="calendar-grid"') == 1 and body.count('id="profile-switcher"') == 1


@pytest.mark.parametrize("headers", [{}, {"HTTP_HX_REQUEST": "true"}])
def test_vary_header(client, headers):
    vary = get(client, **headers)["Vary"]
    assert "HX-Request" in vary and "HX-History-Restore-Request" in vary


def test_switcher_next_is_canonical_url(client):
    member("Ann")

    def nxt(body):
        return html.unescape(re.search(r'name="next" value="([^"]*)"', body).group(1))

    assert nxt(page(client)) == "/calendar/"
    assert nxt(page(client, "2026-09")) == "/calendar/?month=2026-09"
    assert nxt(page(client, "abc")) == "/calendar/"
    assert nxt(page(client, "9999-12")) == "/calendar/"
    assert nxt(page(client, "<script>alert(1)</script>")) == "/calendar/"


# --- Opening a day ----------------------------------------------------------------------


def test_day_links(client):
    c = cell(page(client), 18)
    assert c["links"][0] == "/dashboard/?week=2026-W47&day=3"
    assert 'aria-label="Show Wed, Nov 18 on the dashboard"' in c["html"]
    assert c["links"][-1] == "/chores/create/?date=2026-11-18&return_to=calendar"
    assert 'aria-label="Add a chore on Wed, Nov 18"' in c["html"]
    jan = cell(page(client, "2027-01"), 1)
    assert jan["links"][0] == "/dashboard/?week=2026-W53&day=5"


def test_every_in_month_cell_has_add_link_including_past_days(client):
    body = page(client)
    for c in flat(body):
        if c["filler"]:
            continue
        assert re.fullmatch(r"/chores/create/\?date=2026-11-\d\d&return_to=calendar", c["links"][-1])
    assert cell(body, 1)["links"][-1] == "/chores/create/?date=2026-11-01&return_to=calendar"


def test_day_link_opens_dashboard_week(client):
    chore("Dishes", start_date="2026-11-01")
    link_ = cell(page(client), 18)["links"][0]
    with at(WED):
        response = client.get(link_)
    assert response.status_code == 200
    assert "Dishes" in response.content.decode()


# --- Month helpers (pure) ------------------------------------------------------------------


def test_helpers_are_pure():
    import inspect
    source = inspect.getsource(month_helpers)
    for forbidden in ("now(", "today()", "objects", "settings"):
        assert forbidden not in source
    assert month_helpers.parse_month("2026-11", d("2026-11-18")) == d("2026-11-01")
    assert month_helpers.parse_month("2027-12", d("2026-11-18")) is None
    assert month_helpers.parse_month(None, d("2026-11-18")) is None
