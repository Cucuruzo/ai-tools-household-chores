"""Move a Planned date, or a stored row, into a later week (#48).

Examples use today = Wed 18 Nov 2026 (UTC), current week Mon 16 - Sun 22 Nov,
and the members Mom (adult, order 0), Emma (kid, 1) and Leo (kid, 2).
"Bins" is weekly on Thursday, rotating kids, start 1 Nov, 10 pts,
instructions "old", no due time. The current week is stored, so Thu 19 is
Emma's and RotationState is Emma. Projected: Thu 26 Nov Leo, Thu 3 Dec Emma,
Thu 10 Dec Leo.
"""
import datetime
import inspect
import re
from unittest import mock

import pytest
from django.db import IntegrityError, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import services, undo, views
from chores.models import Chore, ChoreOccurrence, Member, OccurrenceException, RotationState
from chores.services import (
    MoveRefused, apply_future_edit, materialize_week, move_occurrence, move_projected_date,
    project_occurrences, projected_occurrence, set_projected_member,
)
from tests.test_calendar import cell
from tests.test_calendar import get as calendar_get
from tests.test_dashboard import at, blank_csrf, cards, chore, d, member, no_writes, sections
from tests.test_edit_scope import daily, messages_of, url as edit_url, weekly
from tests.test_occurrence_move import date_errors, move
from tests.test_planned_delete import delete_after
from tests.test_undo_delete import deleted, state, undone

pytestmark = pytest.mark.django_db

WED = "2026-11-18T12:00:00"
WED_5 = "2026-11-18T12:05:00"
WED_25 = "2026-11-25T12:00:00"
WED_2 = "2026-12-02T12:00:00"
SUN_NOON = "2026-11-22T12:00:00"
TODAY = d("2026-11-18")
MONDAY = d("2026-11-16")
EXTRA_FIELDS = {"id", "chore_id", "date", "is_cancelled", "is_override", "member_id",
                "moved_from", "title", "instructions", "due_time", "points", "own_values"}
MOVE_DETAILS = re.compile(r'<details class="move-planned">.*?</details>', re.S)
HIDDEN = re.compile(r'<input type="hidden" name="((?!csrf)[^"]+)" value="([^"]*)">')
MESSAGE = '<p class="message" role="status"><mark>{}</mark></p>'


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def family():
    return member("Mom", "adult", 0), member("Emma", "kid", 1), member("Leo", "kid", 2)


# --- Helpers ---------------------------------------------------------------------


def bins(**kwargs):
    kwargs.setdefault("mode", "rotating")
    kwargs.setdefault("eligibility", "kids")
    c = weekly("Bins", 3, "2026-11-01", points=10, instructions="old", **kwargs)
    materialize_week(MONDAY, today=TODAY)
    return c, ChoreOccurrence.objects.get(chore=c, date=d("2026-11-19"))


def move_url(chore_id):
    return f"/dashboard/move/planned/{chore_id}/"


def pmove(client, chore_id, date="2026-12-03", target="2026-12-05", when=WED, tz=None, **data):
    if tz is not None:
        client.cookies["tz"] = tz
    payload = dict(data)
    if date is not None:
        payload["date"] = date
    if target is not None:
        payload["target"] = target
    with at(when):
        return client.post(move_url(chore_id), payload)


def dashboard(client, week=None, when=WED, tz=None, **params):
    if tz is not None:
        client.cookies["tz"] = tz
    if week is not None:
        params["week"] = week
    with at(when):
        response = client.get("/dashboard/", params)
    assert response.status_code == 200
    return response.content.decode()


def follow(client, response, when=WED):
    with at(when):
        page = client.get(response["Location"])
    assert page.status_code == 200
    return page.content.decode()


def bins_cards(body, c):
    return [x for x in cards(body) if x["title"] and x["title"].startswith("Bins")
            and f'chores/{c.pk}/edit/' in x["html"]]


def exceptions_of(c):
    return {str(e.date): e for e in OccurrenceException.objects.filter(chore=c)}


def extra(c, day, member=None, moved_from="2026-12-03", **kwargs):
    kwargs.setdefault("title", c.title)
    kwargs.setdefault("instructions", c.instructions)
    kwargs.setdefault("points", c.points)
    return OccurrenceException.objects.create(
        chore=c, date=d(day), moved_from=d(moved_from), is_override=True, member=member, **kwargs)


def projected(c, first, last, today="2026-11-18"):
    return [p for p in project_occurrences(d(first), d(last), today=d(today)) if p["chore_id"] == c.pk]


def who(c, first, last, today="2026-11-18"):
    names = dict(Member.objects.values_list("pk", "name"))
    return [(str(p["date"]), names.get(p["member_id"])) for p in projected(c, first, last, today)]


def stored(c):
    names = dict(Member.objects.values_list("pk", "name"))
    return {str(o.date): (names.get(o.member_id), o.is_cancelled, o.is_override, o.title,
                          o.instructions, o.points, o.due_time)
            for o in ChoreOccurrence.objects.filter(chore=c)}


def rotation(c):
    state_ = RotationState.objects.filter(chore=c).select_related("last_member").first()
    return state_ and state_.last_member and state_.last_member.name


def moved_bins(client):
    """Bins with Thu 3 Dec moved to Sat 5 Dec from the Planned card."""
    c, o = bins()
    response = pmove(client, c.pk, week="2026-W49")
    assert response.status_code == 302
    return c, o


# --- Planned card control ----------------------------------------------------------


def test_move_control_follows_the_delete_details(client, family):
    mom, emma, leo = family
    c, _ = bins()
    body = blank_csrf(dashboard(client, "2026-W49", member=str(emma.pk), day="4"))
    assert delete_after(body, c, "2026-12-03")
    expected = (
        '<details class="delete-confirm">(?:(?!<article).)*?</details>\n'
        + re.escape(
            '<details class="move-planned">\n'
            '  <summary role="button" class="outline secondary">Move</summary>\n'
            f'  <form method="post" action="/dashboard/move/planned/{c.pk}/">\n'
            '    <input type="hidden" name="csrfmiddlewaretoken" value="">\n'
            '    <input type="hidden" name="date" value="2026-12-03">\n'
            '    <input type="hidden" name="week" value="2026-W49">\n'
            f'    <input type="hidden" name="member" value="{emma.pk}">\n'
            '    <input type="hidden" name="day" value="4">\n'
            f'    <label for="move-planned-{c.pk}-2026-12-03">New date</label>\n'
            f'    <input type="date" id="move-planned-{c.pk}-2026-12-03" name="target" value="2026-12-03">\n'
            '    <button type="submit">Move</button>\n'
            '  </form>\n'
            '</details>'
        )
    )
    assert re.search(rf'id="planned-{c.pk}-2026-12-03">.*?</article>\s*<form class="assign".*?</form>\s*'
                     + expected, body, re.S)
    details = MOVE_DETAILS.findall(body)
    assert len(details) == 1 and "hx-" not in details[0]


def test_move_form_has_the_same_hidden_inputs_as_delete(client, family):
    c, _ = bins()
    body = blank_csrf(dashboard(client, "2026-W49"))
    [details] = MOVE_DETAILS.findall(body)
    assert HIDDEN.findall(details) == HIDDEN.findall(delete_after(body, c, "2026-12-03"))
    assert HIDDEN.findall(details) == [("date", "2026-12-03"), ("week", "2026-W49")]


def test_no_move_control_on_one_off_stored_or_card_only(client, family):
    mom, emma, leo = family
    c, o = bins()
    chore("Party", start_date="2026-11-25", recurrence="once", points=10)
    body = dashboard(client, "2026-W48")
    assert len(MOVE_DETAILS.findall(body)) == 1  # Bins only, not the one-off Party
    assert f'action="/dashboard/move/planned/{c.pk}/"' in body
    current = dashboard(client)
    assert 'class="move-planned"' not in current
    with at(WED):
        response = client.post(f"/dashboard/assign/planned/{c.pk}/",
                               {"date": "2026-11-26", "assignee": str(mom.pk)}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    text = response.content.decode()
    assert 'class="move-planned"' not in text and 'class="delete-confirm"' not in text
    assert text.strip().startswith('<article class="chore-card"')


# --- Moving a Planned date ---------------------------------------------------------------


def test_bins_example_thu_3_to_sat_5(client, family):
    mom, emma, leo = family
    c, _ = bins()
    rows_before = list(ChoreOccurrence.objects.values())
    rotation_before = list(RotationState.objects.values())
    response = pmove(client, c.pk, week="2026-W49")
    assert response.status_code == 302
    assert response["Location"] == "/dashboard/?week=2026-W49"
    assert messages_of(response) == ["Occurrence moved to Sat 5 Dec"]
    assert list(ChoreOccurrence.objects.values()) == rows_before
    assert list(RotationState.objects.values()) == rotation_before
    found = exceptions_of(c)
    assert set(found) == {"2026-12-03", "2026-12-05"}
    source, target = found["2026-12-03"], found["2026-12-05"]
    assert (source.is_cancelled, source.moved_from) == (True, None)
    assert (target.moved_from, target.is_override, target.is_cancelled, target.member_id,
            target.title, target.instructions, target.due_time, target.points) == (
        d("2026-12-03"), True, False, emma.pk, "Bins", "old", None, 10)

    w49 = follow(client, response)
    assert MESSAGE.format("Occurrence moved to Sat 5 Dec") in w49
    shown = bins_cards(w49, c)
    assert [(x["date"], x["assignee"], x["status"]) for x in shown] == [("Sat, Dec 5", "😀 Emma", "Planned")]
    assert f'id="planned-{c.pk}-2026-12-05"' in w49 and f'id="planned-{c.pk}-2026-12-03"' not in w49
    assert [(x["date"], x["assignee"]) for x in bins_cards(dashboard(client, "2026-W48"), c)] == [
        ("Thu, Nov 26", "😀 Leo")]
    assert [(x["date"], x["assignee"]) for x in bins_cards(dashboard(client, "2026-W50"), c)] == [
        ("Thu, Dec 10", "😀 Leo")]
    with at(WED):
        month = calendar_get(client, "2026-12").content.decode()
    assert [s for s, _ in cell(month, 5)["dots"]] == ["planned"]
    assert cell(month, 3)["dots"] == []


def test_shown_equals_stored(client, family):
    mom, emma, leo = family
    c, _ = moved_bins(client)
    [shown] = [p for p in projected(c, "2026-12-05", "2026-12-05")]
    dashboard(client, when=WED_25)
    dashboard(client, when=WED_2)
    assert stored(c) == {
        "2026-11-19": ("Emma", False, False, "Bins", "old", 10, None),
        "2026-11-26": ("Leo", False, False, "Bins", "old", 10, None),
        "2026-12-03": ("Emma", True, False, "Bins", "old", 10, None),
        "2026-12-05": ("Emma", False, True, "Bins", "old", 10, None),
    }
    assert rotation(c) == "Emma"
    assert not OccurrenceException.objects.filter(date__lte=d("2026-12-06")).exists()
    assert who(c, "2026-12-07", "2026-12-13", today="2026-12-02") == [("2026-12-10", "Leo")]
    row = ChoreOccurrence.objects.get(chore=c, date=d("2026-12-05"))
    assert (shown["title"], shown["instructions"], shown["date"], shown["due_time"],
            shown["member_id"], shown["points"]) == (
        row.title, row.instructions, row.date, row.due_time, row.member_id, row.points)


def test_skipped_week_shifts_the_source_but_not_the_extra(client, family):
    c, _ = moved_bins(client)
    dashboard(client, when=WED_2)
    found = stored(c)
    assert found["2026-12-03"][:3] == ("Leo", True, False)
    assert found["2026-12-05"][:3] == ("Emma", False, True)
    assert "2026-11-26" not in found


def test_override_source_gives_its_member_to_the_extra(client, family):
    mom, emma, leo = family
    c, _ = bins()
    set_projected_member(c, d("2026-11-26"), mom.pk)
    response = pmove(client, c.pk, date="2026-11-26", target="2026-11-27")
    assert messages_of(response) == ["Occurrence moved to Fri 27 Nov"]
    found = exceptions_of(c)
    assert (found["2026-11-27"].member_id, found["2026-11-27"].moved_from) == (mom.pk, d("2026-11-26"))
    source = found["2026-11-26"]
    assert (source.is_cancelled, source.is_override, source.member_id) == (True, True, mom.pk)
    assert who(c, "2026-11-23", "2026-12-06") == [("2026-11-27", "Mom"), ("2026-12-03", "Emma")]


def test_moving_an_extra_again_updates_the_same_row(client, family):
    c, _ = moved_bins(client)
    first = exceptions_of(c)["2026-12-05"]
    response = pmove(client, c.pk, date="2026-12-05", target="2026-12-06")
    assert messages_of(response)[-1:] == ["Occurrence moved to Sun 6 Dec"]
    found = exceptions_of(c)
    assert set(found) == {"2026-12-03", "2026-12-06"}
    assert found["2026-12-06"].pk == first.pk and found["2026-12-06"].moved_from == d("2026-12-03")
    assert found["2026-12-03"].is_cancelled


def test_same_target_writes_nothing(client, family):
    c, _ = bins()
    before = state()
    response = pmove(client, c.pk, target="2026-12-03", week="2026-W49", day="4")
    assert response.status_code == 302
    assert response["Location"] == "/dashboard/?week=2026-W49&day=4"
    assert messages_of(response) == []
    assert state() == before


@pytest.mark.parametrize("target, message, setup", [
    ("2026-11-19", "This chore already has an occurrence on Thu 19 Nov.", None),
    ("2026-11-17", "Pick a day from Wed 18 Nov to Thu 31 Dec 2099.", None),
    ("2026-12-05", "Pick a day from Wed 18 Nov to Fri 4 Dec.", "end"),
    ("2026-11-26", "This chore already has an occurrence on Thu 26 Nov.", None),
    ("2027-01-07", "This chore already has an occurrence on Thu 7 Jan 2027.", None),
    ("2026-12-12", "This chore already has an occurrence on Sat 12 Dec.", "extra"),
    (None, "Enter a valid date.", None),
    ("", "Enter a valid date.", None),
    ("abc", "Enter a valid date.", None),
    ("2026-02-30", "Enter a valid date.", None),
    ("2100-01-01", "Enter a valid date.", None),
    ("2026-12-5", "Enter a valid date.", None),
    ("<script>", "Enter a valid date.", None),
], ids=["thu-19-row", "tue-17", "end-date", "projected", "projected-2027", "extra",
        "missing", "empty", "abc", "feb-30", "2100", "short", "script"])
def test_refusals_write_nothing(client, family, target, message, setup):
    c, _ = bins()
    if setup == "end":
        Chore.objects.filter(pk=c.pk).update(end_date=d("2026-12-04"))
    elif setup == "extra":
        extra(c, "2026-12-12", moved_from="2026-12-17")
    before = state()
    response = pmove(client, c.pk, target=target, week="2026-W49")
    assert response.status_code == 302
    assert response["Location"] == "/dashboard/?week=2026-W49"
    assert messages_of(response) == [message]
    assert state() == before
    body = follow(client, response)
    assert MESSAGE.format(message) in body
    assert "<script>" not in body.split("<main")[1]


@pytest.mark.parametrize("date", [None, "", "abc", "2026-02-30", "2100-01-01", "x" * 10_000])
def test_invalid_date_is_400(client, family, date):
    c, _ = bins()
    before = state()
    response = pmove(client, c.pk, date=date)
    assert (response.status_code, response.content.decode()) == (400, "Invalid date")
    assert state() == before


@pytest.mark.parametrize("date", ["2026-11-19", "2026-12-04", "2026-12-10", "2026-11-12"],
                         ids=["this-week", "off-rule", "cancelled", "past"])
def test_date_not_a_later_projected_date_is_404(client, family, date):
    c, _ = bins()
    services.cancel_projected_date(c, d("2026-12-10"))
    before = state()
    assert pmove(client, c.pk, date=date).status_code == 404
    assert state() == before


def test_bad_chores_are_404_and_one_off_is_400(client, family):
    c, _ = bins()
    template = Chore.objects.create(title="T", start_date=d("2026-11-01"), recurrence="weekly",
                                    recurrence_day=3, is_template=True)
    one_off = chore("Party", start_date="2026-12-03", recurrence="once")
    before = state()
    for pk in [999999, template.pk, "99999999999999999999"]:
        assert pmove(client, pk).status_code == 404
    with at(WED):
        assert client.post("/dashboard/move/planned/abc/", {"date": "2026-12-03"}).status_code == 404
        assert client.post("/dashboard/move/planned/-1/", {"date": "2026-12-03"}).status_code == 404
    response = pmove(client, one_off.pk)
    assert (response.status_code, response.content.decode()) == (400, "Move a one-off chore by editing its date")
    assert state() == before


def test_get_is_405_and_no_csrf_is_403(family):
    c, _ = bins()
    before = state()
    with at(WED):
        assert Client().get(move_url(c.pk)).status_code == 405
        response = Client(enforce_csrf_checks=True).post(
            move_url(c.pk), {"date": "2026-12-03", "target": "2026-12-05"})
    assert response.status_code == 403
    assert state() == before


def test_hostile_filters_never_500(client, family):
    c, _ = bins()
    for bad in ["<script>", "x" * 10_000, "2026-W99", "-1"]:
        response = pmove(client, c.pk, target="abc", week=bad, member=bad, day=bad)
        assert response.status_code == 302 and response["Location"] == "/dashboard/"


def test_integrity_error_is_retried_once(family):
    c, _ = bins()
    real = services._planned_move_attempt
    calls = []

    def concurrent(*args):
        calls.append(args)
        if len(calls) == 1:
            services.set_projected_member(c, d("2026-12-05"), None)  # a concurrent write on T
            raise IntegrityError("unique_exception_per_chore_date")
        return real(*args)

    with mock.patch("chores.services._planned_move_attempt", side_effect=concurrent):
        with pytest.raises(MoveRefused) as refused:
            move_projected_date(c, d("2026-12-03"), d("2026-12-05"), TODAY)
    assert str(refused.value) == "This chore already has an occurrence on Sat 5 Dec."
    assert len(calls) == 2
    assert not exceptions_of(c)["2026-12-05"].moved_from


def test_integrity_error_rolls_the_attempt_back(family):
    c, _ = bins()
    real_create = OccurrenceException.objects.create
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise IntegrityError("unique_exception_per_chore_date")
        return real_create(**kwargs)

    with mock.patch.object(OccurrenceException.objects, "create", side_effect=create):
        assert move_projected_date(c, d("2026-12-03"), d("2026-12-05"), TODAY) is True
    assert len(calls) == 2
    assert set(exceptions_of(c)) == {"2026-12-03", "2026-12-05"}


def test_second_integrity_error_leaves_everything_as_before(family):
    c, _ = bins()
    before = state()
    with mock.patch.object(OccurrenceException.objects, "create", side_effect=IntegrityError("x")):
        with pytest.raises(IntegrityError):
            move_projected_date(c, d("2026-12-03"), d("2026-12-05"), TODAY)
    assert state() == before


def test_service_returns_false_for_a_non_projected_source(family):
    c, _ = bins()
    before = state()
    for day in ["2026-11-19", "2026-12-04", "2026-11-22"]:
        assert move_projected_date(c, d(day), d("2026-12-05"), TODAY) is False
    assert state() == before


# --- Moving a stored row into a later week -------------------------------------------------


def test_stored_row_into_a_later_week(client, family):
    mom, emma, leo = family
    c, o = bins()
    response = move(client, c, o, "2026-11-27", title="Bins (Fri)", instructions="new",
                    due_time="18:00", points=20, week="2026-W48", day="5")
    assert response.status_code == 302
    assert response["Location"] == edit_url(c) + "?week=2026-W48&day=5"
    assert messages_of(response) == ["Occurrence moved to Fri 27 Nov"]
    row = ChoreOccurrence.objects.get(pk=o.pk)
    assert (row.date, row.is_cancelled, row.title, row.instructions, row.points, row.member_id) == (
        d("2026-11-19"), True, "Bins", "old", 10, emma.pk)
    assert ChoreOccurrence.objects.filter(chore=c).count() == 1
    [e] = OccurrenceException.objects.filter(chore=c)
    assert (e.date, e.moved_from, e.is_override, e.is_cancelled, e.member_id, e.title,
            e.instructions, e.due_time, e.points) == (
        d("2026-11-27"), d("2026-11-19"), True, False, emma.pk, "Bins (Fri)", "new",
        datetime.time(18), 20)
    assert rotation(c) == "Emma"

    assert bins_cards(dashboard(client), c) == []
    w48 = dashboard(client, "2026-W48")
    shown = bins_cards(w48, c)
    assert [(x["title"], x["date"], x["assignee"], x["time"], x["points"]) for x in shown] == [
        ("Bins", "Thu, Nov 26", "😀 Leo", None, "10 pts"),
        ("Bins (Fri)", "Fri, Nov 27", "😀 Emma", "18:00", "20 pts"),
    ]
    assert '<p class="instructions">old</p>' in shown[0]["html"]
    assert '<p class="instructions">new</p>' in shown[1]["html"]

    dashboard(client, when=WED_25)
    assert stored(c) == {
        "2026-11-19": ("Emma", True, False, "Bins", "old", 10, None),
        "2026-11-26": ("Leo", False, False, "Bins", "old", 10, None),
        "2026-11-27": ("Emma", False, True, "Bins (Fri)", "new", 20, datetime.time(18)),
    }
    assert rotation(c) == "Leo"


def test_followed_edit_page_shows_the_message(client, family):
    c, o = bins()
    response = move(client, c, o, "2026-11-27")
    with at(WED):
        text = client.get(response["Location"]).content.decode()
    assert "Occurrence moved to Fri 27 Nov" in text


def test_overdue_row_into_a_later_week(client, family):
    c = weekly("Trash", 0, "2026-11-02", points=10)
    o = ChoreOccurrence.objects.create(chore=c, date=d("2026-11-09"), points=10)
    response = move(client, c, o, "2026-12-01")
    assert response.status_code == 302
    found = stored(c)
    assert sorted(found) == ["2026-11-09", "2026-11-16"]
    assert found["2026-11-09"][1] is True and found["2026-11-16"][1] is False
    [e] = OccurrenceException.objects.filter(chore=c)
    assert (e.date, e.moved_from) == (d("2026-12-01"), d("2026-11-09"))


@pytest.mark.parametrize("target, message, end", [
    ("2026-11-17", "Pick a day from Wed 18 Nov to Thu 31 Dec 2099.", None),
    ("2026-11-27", "Pick a day from Wed 18 Nov to Fri 20 Nov.", "2026-11-20"),
    ("2026-11-26", "This chore already has an occurrence on Thu 26 Nov.", None),
    ("9999-12-31", "Pick a day from Wed 18 Nov to Thu 31 Dec 2099.", None),
    ("2100-01-01", "Pick a day from Wed 18 Nov to Thu 31 Dec 2099.", None),
])
def test_stored_move_limits(client, family, target, message, end):
    c, o = bins()
    if end:
        Chore.objects.filter(pk=c.pk).update(end_date=d(end))
    before = state()
    response = move(client, c, o, target)
    assert response.status_code == 200
    assert date_errors(response) == [message]
    assert state() == before


def test_stored_move_onto_an_exception_is_refused(client, family):
    c, o = bins()
    extra(c, "2026-12-12")
    before = state()
    for target, text in [("2026-12-12", "Sat 12 Dec")]:
        response = move(client, c, o, target)
        assert date_errors(response) == [f"This chore already has an occurrence on {text}."]
    assert state() == before


def test_daily_chore_refuses_every_later_date(client, family):
    c = daily(points=10)
    materialize_week(MONDAY, today=TODAY)
    o = ChoreOccurrence.objects.get(chore=c, date=TODAY)
    before = state()
    for target in ["2026-11-23", "2026-12-01", "2027-03-04"]:
        response = move(client, c, o, target)
        assert response.status_code == 200
        assert date_errors(response)[0].startswith("This chore already has an occurrence on ")
    assert state() == before


def test_move_within_this_week_is_40_unchanged(client, family):
    c, o = bins()
    response = move(client, c, o, "2026-11-21")
    assert response["Location"] == edit_url(c) + f"?occurrence={o.pk}"
    assert ChoreOccurrence.objects.get(pk=o.pk).date == d("2026-11-21")
    assert ChoreOccurrence.objects.get(chore=c, date=d("2026-11-19")).is_cancelled
    assert not OccurrenceException.objects.exists()


def test_stored_service_writes_no_rotation_and_no_occurrence(family):
    c, o = bins()
    with mock.patch("chores.services.advance_rotation") as advance, \
            mock.patch("chores.services.create_occurrence") as create:
        assert move_occurrence(o, d("2026-11-27"), None, 10, "Bins", "old", TODAY) is True
    assert advance.call_count == 0 and create.call_count == 0


# --- Projection and materialization of extras --------------------------------------------------


def test_extra_on_a_rule_date_replaces_it_and_keeps_the_turn(client, family):
    mom, emma, leo = family
    c, _ = bins()
    extra(c, "2026-11-26", member=mom, moved_from="2026-11-20", title="X", instructions="i",
          due_time=datetime.time(9), points=5)
    found = projected(c, "2026-11-23", "2026-12-06")
    assert [(str(p["date"]), p["member_id"], p["title"], p["instructions"], p["points"],
             p["due_time"], p["is_override"]) for p in found] == [
        ("2026-11-26", mom.pk, "X", "i", 5, datetime.time(9), True),
        ("2026-12-03", emma.pk, "Bins", "old", 10, None, False),
    ]
    dashboard(client, when=WED_25)
    assert stored(c)["2026-11-26"] == ("Mom", False, True, "X", "i", 5, datetime.time(9))
    assert rotation(c) == "Leo"  # the date took its turn


def test_cancelled_extra_on_a_rule_date_is_stored_cancelled_with_its_turn(client, family):
    c, _ = bins()
    e = extra(c, "2026-11-26", moved_from="2026-11-20")
    OccurrenceException.objects.filter(pk=e.pk).update(is_cancelled=True)
    assert projected(c, "2026-11-23", "2026-11-29") == []
    dashboard(client, when=WED_25)
    assert stored(c)["2026-11-26"][:3] == ("Leo", True, False)
    assert rotation(c) == "Leo"


def test_one_off_ignores_moved_from(client, family):
    mom, emma, leo = family
    c = chore("Party", start_date="2026-11-25", recurrence="once", points=10, instructions="p")
    extra(c, "2026-11-25", member=mom, moved_from="2026-11-20", title="X", points=20)
    extra(c, "2026-11-27", member=mom, moved_from="2026-11-25")
    [p] = projected(c, "2026-11-23", "2026-11-29")
    assert (str(p["date"]), p["member_id"], p["title"], p["instructions"], p["points"], p["is_override"]) == (
        "2026-11-25", mom.pk, "Party", "p", 10, True)
    dashboard(client, when=WED_25)
    assert stored(c) == {"2026-11-25": ("Mom", False, True, "Party", "p", 10, None)}


def test_extra_before_a_stored_row_is_not_projected(family):
    c, _ = bins()
    ChoreOccurrence.objects.create(chore=c, date=d("2026-11-25"), points=10)
    extra(c, "2026-11-24", moved_from="2026-11-19")
    assert [str(p["date"]) for p in projected(c, "2026-11-23", "2026-11-29")] == ["2026-11-26"]


def test_extra_is_limited_by_end_date_but_not_start_date(family):
    c, _ = bins()
    extra(c, "2026-11-28", moved_from="2026-11-26")
    Chore.objects.filter(pk=c.pk).update(start_date=d("2026-12-01"))
    assert [str(p["date"]) for p in projected(c, "2026-11-23", "2026-12-06")] == ["2026-11-28", "2026-12-03"]
    Chore.objects.filter(pk=c.pk).update(end_date=d("2026-11-27"))
    assert projected(c, "2026-11-23", "2026-12-06") == []


def test_extra_takes_no_turn_in_projection(family):
    mom, emma, leo = family
    c, _ = bins()
    extra(c, "2026-11-24", member=mom, moved_from="2026-11-19")
    assert who(c, "2026-11-23", "2026-12-13") == [
        ("2026-11-24", "Mom"), ("2026-11-26", "Leo"), ("2026-12-03", "Emma"), ("2026-12-10", "Leo")]


def test_materialize_still_calls_project_occurrences_first(family):
    c, _ = bins()
    extra(c, "2026-11-28", moved_from="2026-11-26")
    with mock.patch("chores.services.project_occurrences", wraps=project_occurrences) as spy:
        materialize_week(d("2026-11-23"), today=d("2026-11-25"))
    assert spy.call_count == 1
    assert spy.call_args.args[:2] == (d("2026-11-23"), d("2026-11-29"))


# --- Interactions -------------------------------------------------------------------------------


def test_22_cancel_on_the_extra_then_undo(client, family):
    c, _ = moved_bins(client)
    before = {f: v for f, v in undo.fields(exceptions_of(c)["2026-12-05"]).items()}
    snap = state()
    token = deleted(client, c, when=WED, date="2026-12-05", scope="this")
    found = exceptions_of(c)
    assert found["2026-12-05"].is_cancelled and found["2026-12-03"].is_cancelled
    assert bins_cards(dashboard(client, "2026-W49"), c) == []
    undone(client, token, when=WED_5)
    assert undo.fields(exceptions_of(c)["2026-12-05"]) == before
    assert state() == snap
    assert [x["date"] for x in bins_cards(dashboard(client, "2026-W49"), c)] == ["Sat, Dec 5"]


def test_cancelled_extra_stores_nothing_and_takes_no_turn(client, family):
    c, _ = moved_bins(client)
    with at(WED):
        client.post(f"/chores/{c.pk}/delete/", {"date": "2026-12-05", "scope": "this"})
    dashboard(client, when=WED_2)
    found = stored(c)
    assert "2026-12-05" not in found
    assert found["2026-12-03"][:2] == ("Leo", True)
    assert rotation(c) == "Leo"  # only Thu 3's turn
    assert not OccurrenceException.objects.filter(date__lte=d("2026-12-06")).exists()


def test_22_this_and_future_from_the_extra_then_undo(client, family):
    c, _ = moved_bins(client)
    extra_before = undo.fields(exceptions_of(c)["2026-12-05"])
    snap = state()
    token = deleted(client, c, when=WED, date="2026-12-05", scope="future")
    assert set(exceptions_of(c)) == {"2026-12-03"}
    c.refresh_from_db()
    assert c.end_date == d("2026-12-04")
    undone(client, token, when=WED_5)
    assert state() == snap
    assert undo.fields(exceptions_of(c)["2026-12-05"]) == extra_before


def test_22_this_and_future_from_thu_26(client, family):
    c, _ = moved_bins(client)
    snap = state()
    token = deleted(client, c, when=WED, date="2026-11-26", scope="future")
    assert exceptions_of(c) == {}
    undone(client, token, when=WED_5)
    assert state() == snap


def test_24_snapshot_items_have_every_extra_field(client, family):
    c, _ = moved_bins(client)
    items = undo.snapshot(c)["exceptions"]
    assert len(items) == 2 and all(set(i) == EXTRA_FIELDS for i in items)


def test_exception_concrete_fields_are_pinned():
    assert {f.attname for f in OccurrenceException._meta.concrete_fields} == EXTRA_FIELDS


def test_24_whole_chore_delete_and_undo_restores_the_extra(client, family):
    c, o = moved_bins(client)
    ChoreOccurrence.objects.filter(pk=o.pk).update(is_cancelled=True)  # nothing left before 26 Nov
    snap = state()
    token = deleted(client, c, when=WED, date="2026-11-26", scope="future")
    assert not Chore.objects.filter(pk=c.pk).exists()
    undone(client, token, when=WED_5)
    assert state() == snap


def test_24_whole_chore_from_a_stored_card_with_an_extra(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-27", title="Bins (Fri)", due_time="18:00", points=20)
    snap = state()
    token = deleted(client, c, when=WED, date="2026-11-26", scope="future")
    assert not Chore.objects.filter(pk=c.pk).exists()
    undone(client, token, when=WED_5)
    assert state() == snap


def test_31_assign_on_the_extra(client, family):
    mom, emma, leo = family
    c, _ = moved_bins(client)
    before = exceptions_of(c)["2026-12-05"]
    with at(WED):
        response = client.post(f"/dashboard/assign/planned/{c.pk}/",
                               {"date": "2026-12-05", "assignee": str(mom.pk), "week": "2026-W49"})
    assert response.status_code == 302
    after = exceptions_of(c)["2026-12-05"]
    assert after.pk == before.pk and after.member_id == mom.pk
    assert (after.moved_from, after.title, after.instructions, after.points, after.is_override) == (
        before.moved_from, before.title, before.instructions, before.points, True)
    assert [(x["date"], x["assignee"]) for x in bins_cards(dashboard(client, "2026-W49"), c)] == [
        ("Sat, Dec 5", "😀 Mom")]
    dashboard(client, when=WED_2)
    assert stored(c)["2026-12-05"][:3] == ("Mom", False, True)


def test_40_tombstone_is_shown_nowhere_and_never_projected(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-27")
    assert bins_cards(dashboard(client), c) == []
    with at(WED):
        month = calendar_get(client).content.decode()
    assert cell(month, 19)["dots"] == []
    assert [s for s, _ in cell(month, 27)["dots"]] == ["planned"]
    materialize_week(MONDAY, today=TODAY)
    assert ChoreOccurrence.objects.filter(chore=c).count() == 1
    assert [str(p["date"]) for p in projected(c, "2026-11-16", "2026-11-22")] == []


def test_rule_change_keeps_the_extra(client, family):
    c, _ = bins()
    response = pmove(client, c.pk, date="2026-12-03", target="2026-12-04")
    assert response.status_code == 302
    before = list(OccurrenceException.objects.order_by("pk").values())
    old = {name: getattr(c, name) for name in services.RULE_FIELDS + services.ASSIGNMENT_FIELDS}
    c.refresh_from_db()
    c.recurrence_day, c.title, c.points = 4, "Recycling", 20
    apply_future_edit(c, old, TODAY, TODAY)
    assert list(OccurrenceException.objects.order_by("pk").values()) == before
    assert [(str(p["date"]), p["title"]) for p in projected(c, "2026-11-23", "2026-12-13")] == [
        ("2026-11-27", "Recycling"), ("2026-12-04", "Bins"), ("2026-12-11", "Recycling")]
    assert who(c, "2026-11-23", "2026-12-13") == [
        ("2026-11-27", "Emma"), ("2026-12-04", "Emma"), ("2026-12-11", "Emma")]


# --- Viewing writes nothing, query count, clock ----------------------------------------------------


def test_viewing_future_weeks_writes_nothing(client, family):
    c, _ = moved_bins(client)
    spy_, wrapper = no_writes()
    with wrapper:
        for week in ["2026-W48", "2026-W49", "2026-W50"]:
            dashboard(client, week)
        with at(WED):
            assert calendar_get(client, "2026-12").status_code == 200
    assert [w for w in spy_.writes if "django_session" not in w] == []


def _others_with_extras(count):
    for i in range(count):
        other = weekly(f"Other {i}", 0, "2026-11-02", points=10)
        extra(other, "2026-12-05", moved_from="2026-11-30")


def _count(run):
    with CaptureQueriesContext(connection) as queries:
        run()
    return len(queries)


def test_projection_queries_do_not_depend_on_extras(family):
    c, _ = bins()

    def counts():
        return (_count(lambda: project_occurrences(d("2026-11-16"), d("2027-03-31"), TODAY)),
                _count(lambda: projected_occurrence(c, d("2026-12-05"), TODAY)))

    none = counts()
    _others_with_extras(10)
    assert len([p for p in project_occurrences(d("2026-12-05"), d("2026-12-05"), TODAY)]) == 10
    assert counts() == none


def _view_queries(client, others):
    Chore.objects.all().delete()
    c, o = bins()
    _others_with_extras(others)
    materialize_week(MONDAY, today=TODAY)
    week = _count(lambda: dashboard(client, "2026-W49"))
    planned = _count(lambda: pmove(client, c.pk))
    stored_move = _count(lambda: move(client, c, o, "2026-11-27"))
    assert exceptions_of(c)["2026-11-27"].moved_from == d("2026-11-19")
    return week, planned, stored_move


def test_view_queries_do_not_depend_on_extras(client, family):
    assert _view_queries(client, 0) == _view_queries(client, 10)


def test_planned_move_reads_today_once(client, family):
    c, _ = bins()
    with mock.patch("chores.views.timezone.localdate", wraps=views.timezone.localdate) as localdate, \
            mock.patch("chores.views.move_projected_date", wraps=move_projected_date) as service:
        pmove(client, c.pk)
    assert localdate.call_count == 1
    assert service.call_args.args[-1] == TODAY


def test_services_never_read_the_clock():
    for fn in (services.move_projected_date, services._planned_move_attempt, services._later_target_taken,
               services._store_extra, services._day_text, services._range_refused, services._taken_refused,
               services._occurrence_dict, services._project, services._exceptions_in,
               services._materialize_attempt, services._move_attempt, services.move_occurrence):
        source = inspect.getsource(fn)
        assert "timezone" not in source and "_resolve_today" not in source
        assert "localdate" not in source and "now(" not in source


def test_kiritimati_vs_utc(client, family):
    c, _ = bins()
    before = state()
    # Sun 22 12:00 UTC is Mon 23 in Kiritimati: Thu 26 is in the current week there.
    assert pmove(client, c.pk, date="2026-11-26", target="2026-11-27",
                 when=SUN_NOON, tz="Pacific/Kiritimati").status_code == 404
    assert state() == before
    response = pmove(client, c.pk, date="2026-12-03", target="2026-11-22",
                     when=SUN_NOON, tz="Pacific/Kiritimati")
    assert messages_of(response) == ["Pick a day from Mon 23 Nov to Thu 31 Dec 2099."]
    assert state() == before
    response = pmove(client, c.pk, date="2026-11-26", target="2026-11-27", when=SUN_NOON, tz="UTC")
    assert messages_of(response)[-1:] == ["Occurrence moved to Fri 27 Nov"]
    assert set(exceptions_of(c)) == {"2026-11-26", "2026-11-27"}


def test_no_js_plain_post_with_csrf(family):
    c, _ = bins()
    csrf_client = Client(enforce_csrf_checks=True)
    body = dashboard(csrf_client, "2026-W49")
    [details] = MOVE_DETAILS.findall(body)
    assert "hx-" not in details
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', details).group(1)
    data = dict(HIDDEN.findall(details))
    data.update({"csrfmiddlewaretoken": token, "target": "2026-12-05"})
    action = re.search(r'<form method="post" action="([^"]+)">', details).group(1)
    with at(WED):
        response = csrf_client.post(action, data)
    assert response.status_code == 302
    followed = follow(csrf_client, response)
    assert MESSAGE.format("Occurrence moved to Sat 5 Dec") in followed
    assert f'id="planned-{c.pk}-2026-12-05"' in followed
