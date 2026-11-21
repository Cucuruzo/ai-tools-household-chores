"""Moving one occurrence of a recurring chore to another day of this week (#40).

Examples use today = Wed 2026-11-18 (UTC), current week Mon 16 - Sun 22 Nov,
and the members Mom (adult, order 0), Emma (kid, 1) and Leo (kid, 2).
"Bins" is a weekly Thursday chore rotating among the kids, start 1 Nov.
"""
import datetime
import inspect
import re
from unittest import mock

import pytest
from django.db import IntegrityError, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import progress, services
from chores.models import Chore, ChoreOccurrence, RotationState
from chores.services import MoveRefused, apply_future_edit, materialize_week, move_occurrence
from tests.test_calendar import cell
from tests.test_calendar import page as calendar_page
from tests.test_dashboard import WED, chore, d, member, page, sections, titles
from tests.test_edit_scope import (
    OCC_FORM, CHORE_FORM, form_of, get, messages_of, nov, post, projected, row, rows_of,
    snapshot, url, values, weekly, daily,
)
from tests.test_history import body as history_body

TODAY = d("2026-11-18")
MONDAY = d("2026-11-16")
SAT_NOON = "2026-11-21T12:00:00"
SUN_NOON = "2026-11-22T12:00:00"
OCC_DATE = re.compile(r'<input type="date" name="date"[^>]*>')


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def family():
    return member("Mom", "adult", 0), member("Emma", "kid", 1), member("Leo", "kid", 2)


# --- Helpers ---------------------------------------------------------------------


def bins(**kwargs):
    """Weekly Thursday "Bins", rotating kids, 10 pts; the week is stored, so
    Thu 19 is Emma's. Returns the chore and the Thu 19 row."""
    kwargs.setdefault("mode", "rotating")
    kwargs.setdefault("eligibility", "kids")
    c = weekly("Bins", 3, "2026-11-01", points=10, instructions="old", **kwargs)
    materialize_week(MONDAY, today=TODAY)
    return c, ChoreOccurrence.objects.get(chore=c, date=d(nov(19)))


def move(client, c, o, date, when=WED, tz=None, title=None, instructions=None,
         due_time="", points=None, **extra):
    data = {
        "scope": "this", "occurrence": str(o.pk), "date": date,
        "title": o.title if title is None else title,
        "instructions": o.instructions if instructions is None else instructions,
        "due_time": due_time, "points": str(o.points if points is None else points),
    }
    data.update(extra)
    return post(client, c, data, when=when, tz=tz)


def date_errors(response):
    return list(response.context["occurrence_form"].errors.get("date", []))


def tombstone(c, day):
    return ChoreOccurrence.objects.get(chore=c, date=d(day))


# --- Form and page ------------------------------------------------------------------


@pytest.mark.django_db
def test_date_field_is_the_last_field_of_the_occurrence_form(client, family):
    c, o = bins()
    form = OCC_FORM.search(get(client, c, f"?occurrence={o.pk}").content.decode()).group(1)
    assert '<input type="date" name="date" value="2026-11-19" id="occ_date">' in form
    order = [form.index(s) for s in (
        '<label for="occ_title">', '<label for="occ_instructions">', '<div class="grid">',
        '<label for="occ_due_time">', '<label for="occ_points">', '<label for="occ_date">Date</label>',
        'id="occ_date"', '<button type="submit" name="scope" value="this">',
    )]
    assert order == sorted(order)
    assert form.index("</div>\n      </div>") < form.index('<label for="occ_date">')
    tag = OCC_DATE.search(form).group(0)
    assert "min=" not in tag and "max=" not in tag
    assert "required" not in tag


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["done", "skipped"])
def test_done_and_skipped_rows_still_have_no_form(client, family, status):
    c, o = bins()
    ChoreOccurrence.objects.filter(pk=o.pk).update(status=status)
    text = get(client, c, f"?occurrence={o.pk}").content.decode()
    assert OCC_FORM.search(text) is None
    assert "Undo it on the dashboard" in text
    assert 'name="date" value="2026-11-19"' not in text


@pytest.mark.django_db
def test_date_field_is_not_on_the_chore_form(client, family):
    c, o = bins()
    form = CHORE_FORM.search(get(client, c, f"?occurrence={o.pk}").content.decode()).group(1)
    assert 'id="occ_date"' not in form


@pytest.mark.django_db
@pytest.mark.parametrize("date", [None, "", "2026-11-19"], ids=["missing", "empty", "same"])
def test_empty_or_same_date_is_the_39_save(client, family, date):
    c, o = bins()
    extra = {} if date is None else {"date": date}
    data = {"scope": "this", "occurrence": str(o.pk), "title": "Bins!", "instructions": "new",
            "due_time": "07:30", "points": "20", **extra}
    with mock.patch("chores.views.update_occurrence", wraps=services.update_occurrence) as update, \
            mock.patch("chores.services.materialize_week", wraps=materialize_week) as mat, \
            mock.patch("chores.views.move_occurrence") as moving:
        response = post(client, c, data)
    assert response.status_code == 302
    assert update.call_count == 1 and mat.call_count == 0 and moving.call_count == 0
    assert messages_of(response) == ["Occurrence saved"]
    after = values(o)
    assert (after["date"], after["title"], after["instructions"], after["points"]) == (
        d(nov(19)), "Bins!", "new", 20)
    assert ChoreOccurrence.objects.filter(chore=c).count() == 1  # no tombstone


@pytest.mark.django_db
def test_move_example_thu_19_to_sat_21(client, family):
    mom, emma, leo = family
    c, o = bins()
    assert o.member_id == emma.pk
    with mock.patch("chores.views.update_occurrence") as update:
        response = move(client, c, o, "2026-11-21", title="Bins (Sat)", instructions="new",
                        points=20, week="2026-W47", day="5")
    assert update.call_count == 0
    assert response.status_code == 302
    assert response["Location"] == url(c) + f"?week=2026-W47&day=5&occurrence={o.pk}"
    assert messages_of(response) == ["Occurrence moved to Sat 21 Nov"]
    moved = values(o)
    assert (moved["date"], moved["title"], moved["instructions"], moved["points"],
            moved["member_id"], moved["is_override"], moved["status"], moved["is_cancelled"]) == (
        d(nov(21)), "Bins (Sat)", "new", 20, emma.pk, True, "todo", False)
    old = tombstone(c, nov(19))
    assert old.pk != o.pk
    assert (old.title, old.instructions, old.points, old.due_time, old.member_id,
            old.status, old.is_cancelled, old.is_override) == (
        "Bins", "old", 10, None, emma.pk, "todo", True, False)
    c.refresh_from_db()
    assert (c.title, c.instructions, c.points) == ("Bins", "old", 10)
    assert RotationState.objects.get(chore=c).last_member_id == emma.pk
    later = projected(c, "2026-11-23", "2026-12-06")
    assert [(p["date"], p["member_id"], p["title"]) for p in later] == [
        (d("2026-11-26"), leo.pk, "Bins"), (d("2026-12-03"), emma.pk, "Bins")]


@pytest.mark.django_db
def test_move_keeps_due_time_on_the_tombstone(client, family):
    c, o = bins(due_time=datetime.time(18))
    o.refresh_from_db()
    move(client, c, o, "2026-11-21", due_time="07:00")
    assert values(o)["due_time"] == datetime.time(7)
    assert tombstone(c, nov(19)).due_time == datetime.time(18)


@pytest.mark.django_db
def test_moved_row_shows_on_the_target_day_only(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-21", title="Bins (Sat)")
    body = page(client)
    assert "Bins (Sat)" in titles(body, "upcoming")
    assert all(card["date"] != "Thu, Nov 19" for cards in sections(body).values() for card in cards)
    assert [card["id"] for cards in sections(body).values() for card in cards
            if card["title"] and card["title"].startswith("Bins")] == [o.pk]
    month = calendar_page(client)
    dots = cell(month, 21)["dots"]
    assert [status for status, _ in dots] == ["todo"] and dots[0][1].startswith("Bins (Sat)")
    assert cell(month, 19)["dots"] == []


@pytest.mark.django_db
def test_move_to_today_is_under_today_and_the_tombstone_blocks_the_19th(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-18")
    body = page(client)
    assert "Bins" in titles(body, "today")
    page(client)
    assert sorted(rows_of(c)) == [18, 19]
    assert tombstone(c, nov(19)).is_cancelled is True


@pytest.mark.django_db
def test_move_again_leaves_two_tombstones(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-21")
    o.refresh_from_db()
    response = move(client, c, o, "2026-11-22")
    assert messages_of(response)[-1] == "Occurrence moved to Sun 22 Nov"
    rows = rows_of(c)
    assert sorted(rows) == [19, 21, 22]
    assert rows[22].pk == o.pk and not rows[22].is_cancelled
    assert rows[19].is_cancelled and rows[21].is_cancelled


@pytest.mark.django_db
def test_move_back_onto_the_source_is_refused(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-21")
    o.refresh_from_db()
    before = snapshot()
    response = move(client, c, o, "2026-11-19")
    assert response.status_code == 200
    assert date_errors(response) == ["This chore already has an occurrence on Thu 19 Nov."]
    assert snapshot() == before


@pytest.mark.django_db
def test_one_off_ignores_date(client, family):
    c = chore("Job", start_date="2026-11-19", points=10)
    o = ChoreOccurrence.objects.create(chore=c, date=d(nov(19)), points=10)
    response = post(client, c, {**form_of(c), "scope": "this", "occurrence": str(o.pk),
                                "date": "2026-11-21"})
    assert response.status_code == 302
    assert values(o)["date"] == d(nov(19))
    assert ChoreOccurrence.objects.filter(chore=c).count() == 1


# --- Overdue rows and unstored weeks ---------------------------------------------------


@pytest.mark.django_db
def test_overdue_row_of_this_week_moves_forward(client, family):
    c = weekly("Trash", 0, "2026-11-02", points=10)
    o = row(c, nov(16))
    response = move(client, c, o, "2026-11-20")
    assert response.status_code == 302
    rows = rows_of(c)
    assert sorted(rows) == [16, 20]
    assert rows[16].is_cancelled and rows[20].pk == o.pk


@pytest.mark.django_db
def test_overdue_row_of_an_earlier_week_moves_into_this_week(client, family):
    c = weekly("Trash", 0, "2026-11-02", points=10)
    row(c, nov(16))
    o = row(c, nov(9))
    move(client, c, o, "2026-11-19")
    rows = rows_of(c)
    assert sorted(rows) == [9, 16, 19]
    assert rows[9].is_cancelled and not rows[16].is_cancelled and rows[19].pk == o.pk


@pytest.mark.django_db
def test_unstored_week_is_materialized_before_the_move(client, family):
    mom, emma, leo = family
    c = weekly("Trash", 0, "2026-11-02", mode="rotating", eligibility="kids", points=10)
    o = row(c, nov(9), member=emma)
    RotationState.objects.create(chore=c, last_member=emma)
    move(client, c, o, "2026-11-21")
    rows = rows_of(c)
    assert sorted(rows) == [9, 16, 21]
    assert rows[9].is_cancelled and rows[21].pk == o.pk and rows[21].member_id == emma.pk
    assert rows[16].member_id == leo.pk and not rows[16].is_cancelled
    assert RotationState.objects.get(chore=c).last_member_id == leo.pk  # only Mon 16's turn
    assert [p["date"] for p in projected(c, "2026-11-23", "2026-11-29")] == [d("2026-11-23")]


@pytest.mark.django_db
def test_daily_chore_always_refuses(client, family):
    c = daily(points=10)
    o = row(c, nov(18))
    materialize_week(MONDAY, today=TODAY)
    before = snapshot()
    for day in range(18, 23):
        if day == 18:
            continue
        response = move(client, c, o, nov(day))
        assert response.status_code == 200
        assert date_errors(response) == [f"This chore already has an occurrence on "
                                         f"{d(nov(day)):%a} {day} Nov."]
    assert snapshot() == before


@pytest.mark.django_db
def test_daily_chore_refuses_without_a_stored_week(client, family):
    c = daily(points=10)
    o = row(c, nov(18))
    before = snapshot()
    response = move(client, c, o, "2026-11-20")
    assert date_errors(response) == ["This chore already has an occurrence on Fri 20 Nov."]
    assert snapshot() == before  # the materialization is rolled back too


# --- Refusals ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["2026-11-17", "2026-11-23", "2026-11-25", "9999-12-31",
                                    "2026-11-01", "0001-01-01"])
def test_outside_the_week_is_refused(client, family, target):
    c, o = bins()
    before = snapshot()
    response = move(client, c, o, target, title="Changed", instructions="new", points=20)
    assert response.status_code == 200
    assert date_errors(response) == ["Pick a day from Wed 18 Nov to Sun 22 Nov."]
    assert snapshot() == before
    text = response.content.decode()
    form = OCC_FORM.search(text).group(1)
    assert 'value="Changed"' in form and ">\nnew</textarea>" in form
    assert '<option value="20" selected>' in form
    assert f'value="{target}"' in form
    assert "Pick a day from Wed 18 Nov to Sun 22 Nov." in form
    chore_form = CHORE_FORM.search(text).group(1)
    assert 'value="Bins"' in chore_form


@pytest.mark.django_db
def test_error_is_under_the_date_field(client, family):
    c, o = bins()
    form = OCC_FORM.search(move(client, c, o, "2026-11-23").content.decode()).group(1)
    assert form.index('id="occ_date"') < form.index('class="errorlist"') < form.index(
        '<button type="submit" name="scope" value="this">')


@pytest.mark.django_db
def test_end_date_limits_the_week(client, family):
    c, o = bins()
    Chore.objects.filter(pk=c.pk).update(end_date=d(nov(20)))
    before = snapshot()
    response = move(client, c, o, "2026-11-21")
    assert date_errors(response) == ["Pick a day from Wed 18 Nov to Fri 20 Nov."]
    assert snapshot() == before
    assert move(client, c, o, "2026-11-20").status_code == 302
    assert values(o)["date"] == d(nov(20))


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["2026-11-17", "2026-11-18", "2026-11-21"])
def test_ended_chore_cannot_move(client, family, target):
    c, o = bins()
    Chore.objects.filter(pk=c.pk).update(end_date=d(nov(17)))
    before = snapshot()
    response = move(client, c, o, target)
    assert response.status_code == 200
    assert date_errors(response) == ["This chore has ended, so this occurrence can't move."]
    assert snapshot() == before


@pytest.mark.django_db
@pytest.mark.parametrize("status, cancelled", [
    ("todo", False), ("done", False), ("skipped", False), ("todo", True),
], ids=["todo", "done", "skipped", "cancelled"])
def test_occupied_target_is_refused(client, family, status, cancelled):
    c, o = bins()
    row(c, nov(21), status, is_cancelled=cancelled)
    before = snapshot()
    response = move(client, c, o, "2026-11-21")
    assert response.status_code == 200
    assert date_errors(response) == ["This chore already has an occurrence on Sat 21 Nov."]
    assert snapshot() == before


@pytest.mark.django_db
def test_another_chores_row_on_the_target_does_not_block(client, family):
    c, o = bins()
    other = weekly("Lawn", 5, "2026-11-01")
    row(other, nov(21))
    assert move(client, c, o, "2026-11-21").status_code == 302


@pytest.mark.django_db
@pytest.mark.parametrize("raw", ["abc", "2026-02-30", "2026-13-01", "10000-01-01", "-1",
                                 "x" * 10_000, "<script>", "2026-11-21T00:00", "٢٠٢٦-١١-٢١"])
def test_unparseable_dates(client, family, raw):
    c, o = bins()
    before = snapshot()
    response = move(client, c, o, raw)
    assert response.status_code == 200
    assert date_errors(response) == ["Enter a valid date."]
    assert snapshot() == before
    assert "<script>" not in OCC_FORM.search(response.content.decode()).group(1)


@pytest.mark.django_db
def test_last_repeated_date_counts(client, family):
    c, o = bins()
    response = move(client, c, o, ["2026-11-25", "2026-11-21"])
    assert response.status_code == 302
    assert values(o)["date"] == d(nov(21))
    o2 = ChoreOccurrence.objects.get(pk=o.pk)
    response = move(client, c, o2, ["2026-11-22", "2026-11-30"])
    assert date_errors(response) == ["Pick a day from Wed 18 Nov to Sun 22 Nov."]


@pytest.mark.django_db
@pytest.mark.parametrize("title", ["", "   ", "x" * 101])
def test_form_errors_of_39_come_first(client, family, title):
    c, o = bins()
    before = snapshot()
    with mock.patch("chores.views.move_occurrence") as moving:
        response = move(client, c, o, "2026-11-21", title=title)
    assert moving.call_count == 0
    assert response.status_code == 200
    assert response.context["occurrence_form"].errors.get("title")
    assert snapshot() == before


@pytest.mark.django_db
def test_existing_guards_stay(client, family):
    c, o = bins()
    lawn = weekly("Lawn", 5, "2026-11-01")
    foreign = row(lawn, nov(21))
    assert post(client, c, {"scope": "this", "occurrence": str(foreign.pk), "date": "2026-11-21",
                            "title": "x", "points": "10"}).status_code == 404
    for bad in ["abc", "-1", "9" * 30]:
        assert post(client, c, {"scope": "this", "occurrence": bad, "date": "2026-11-21",
                                "title": "x", "points": "10"}).status_code == 404
    bad_scope = move(client, c, o, "2026-11-21", scope="THIS")
    assert bad_scope.status_code == 400
    ChoreOccurrence.objects.filter(pk=o.pk).update(status="done")
    before = snapshot()
    response = move(client, c, o, "2026-11-21")
    assert (response.status_code, response.content.decode()) == (400, "Undo first")
    assert snapshot() == before


# --- Consistency ---------------------------------------------------------------------------


def _race(change):
    real = services.move_occurrence

    def racing(occurrence, *args):
        change(occurrence.pk)
        return real(occurrence, *args)

    return mock.patch("chores.views.move_occurrence", side_effect=racing)


@pytest.mark.django_db
@pytest.mark.parametrize("change, code", [
    (lambda pk: ChoreOccurrence.objects.filter(pk=pk).update(is_cancelled=True), 404),
    (lambda pk: ChoreOccurrence.objects.filter(pk=pk).delete(), 404),
    (lambda pk: ChoreOccurrence.objects.filter(pk=pk).update(status="done"), 400),
    (lambda pk: ChoreOccurrence.objects.filter(pk=pk).update(status="skipped"), 400),
], ids=["cancelled", "deleted", "done", "skipped"])
def test_lost_race_writes_nothing(client, family, change, code):
    c = weekly("Trash", 0, "2026-11-02", points=10)
    o = row(c, nov(9))  # the week of the 16th is not stored yet
    with _race(change):
        response = move(client, c, o, "2026-11-21", points=20)
    assert response.status_code == code
    if code == 400:
        assert response.content.decode() == "Undo first"
    stored = ChoreOccurrence.objects.filter(chore=c)
    assert [r.date for r in stored] in ([], [d(nov(9))])  # no Mon 16, no tombstone
    assert not RotationState.objects.filter(chore=c).exists()


@pytest.mark.django_db
def test_integrity_error_is_retried_once(family):
    c, o = bins()
    real = services._move_attempt
    calls = []

    def concurrent(*args):
        calls.append(args)
        if len(calls) == 1:
            row(c, nov(21))  # a concurrent insert on the target commits first
            raise IntegrityError("unique_occurrence_per_chore_date")
        return real(*args)

    with mock.patch("chores.services._move_attempt", side_effect=concurrent):
        with pytest.raises(MoveRefused) as refused:
            move_occurrence(o, d(nov(21)), None, 10, "Bins", "old", TODAY)
    assert str(refused.value) == "This chore already has an occurrence on Sat 21 Nov."
    assert len(calls) == 2
    assert values(o)["date"] == d(nov(19))


@pytest.mark.django_db
def test_integrity_error_rolls_the_attempt_back(family):
    c, o = bins()
    real_create = ChoreOccurrence.objects.create
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise IntegrityError("unique_occurrence_per_chore_date")
        return real_create(**kwargs)

    with mock.patch.object(ChoreOccurrence.objects, "create", side_effect=create):
        assert move_occurrence(o, d(nov(21)), None, 20, "Bins", "old", TODAY) is True
    assert len(calls) == 2
    rows = rows_of(c)
    assert sorted(rows) == [19, 21]
    assert rows[21].pk == o.pk and rows[19].is_cancelled


@pytest.mark.django_db
def test_second_integrity_error_leaves_everything_as_before(family):
    c, o = bins()
    before = snapshot()
    with mock.patch.object(ChoreOccurrence.objects, "create",
                           side_effect=IntegrityError("taken")) as create:
        with pytest.raises(IntegrityError):
            move_occurrence(o, d(nov(21)), None, 20, "Bins", "old", TODAY)
    assert create.call_count == 2
    assert snapshot() == before


@pytest.mark.django_db
def test_service_writes_one_update_then_the_tombstone(family):
    c, o = bins()
    with CaptureQueriesContext(connection) as queries:
        assert move_occurrence(o, d(nov(21)), None, 20, "Bins (Sat)", "new", TODAY) is True
    writes = [q["sql"] for q in queries if re.match(r"\s*(INSERT|UPDATE|DELETE)", q["sql"])
              and "chores_choreoccurrence" in q["sql"]]
    assert len(writes) == 2
    assert writes[0].startswith("UPDATE") and writes[1].startswith("INSERT")
    for column in ('"date"', '"due_time"', '"points"', '"title"', '"instructions"', '"is_override"'):
        assert column in writes[0]
    assert not any("chores_rotationstate" in q["sql"] and not q["sql"].startswith("SELECT")
                   for q in queries)


@pytest.mark.django_db
def test_service_does_not_advance_rotation_or_create_occurrences(family):
    c, o = bins()
    with mock.patch("chores.services.advance_rotation") as advance, \
            mock.patch("chores.services.create_occurrence") as create:
        move_occurrence(o, d(nov(21)), None, 10, "Bins", "old", TODAY)
    assert advance.call_count == 0 and create.call_count == 0


def test_service_reads_no_clock():
    source = inspect.getsource(services.move_occurrence) + inspect.getsource(services._move_attempt)
    assert "timezone" not in source and "_resolve_today" not in source
    assert "localdate" not in source and "now(" not in source


def test_move_refused_is_an_exception():
    assert issubclass(MoveRefused, Exception)
    assert str(MoveRefused("x")) == "x"


@pytest.mark.django_db
def test_future_edit_never_touches_the_moved_row_or_the_tombstone(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-21", title="Bins (Sat)", points=20)
    moved_before, stone_before = values(o), values(tombstone(c, nov(19)))
    old = {name: getattr(c, name) for name in services.RULE_FIELDS + services.ASSIGNMENT_FIELDS}
    c.refresh_from_db()
    c.points, c.title = 5, "Recycling"
    apply_future_edit(c, old, d(nov(18)), TODAY)
    assert values(o) == moved_before
    assert values(tombstone(c, nov(19))) == stone_before


@pytest.mark.django_db
def test_future_rule_change_does_not_fill_over_the_tombstone(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-21")
    o.refresh_from_db()
    response = post(client, c, {**form_of(c, recurrence="weekly", recurrence_day="3"),
                                "scope": "future"})
    assert response.status_code == 302
    rows = rows_of(c)
    assert sorted(rows) == [19, 21]
    assert rows[19].is_cancelled and rows[21].pk == o.pk


@pytest.mark.django_db
def test_delete_this_occurrence_on_the_moved_row(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-21")
    with mock.patch("django.utils.timezone.now",
                    return_value=datetime.datetime(2026, 11, 18, 12, tzinfo=datetime.timezone.utc)):
        response = client.post(f"/chores/{c.pk}/delete/", {"occurrence": str(o.pk), "scope": "this"})
    assert response.status_code == 302
    assert values(o)["is_cancelled"] is True
    body = page(client)
    assert not any(card["title"] and card["title"].startswith("Bins")
                   for cards in sections(body).values() for card in cards)
    month = calendar_page(client)
    assert cell(month, 19)["dots"] == [] and cell(month, 21)["dots"] == []


@pytest.mark.django_db
def test_delete_this_and_future_from_the_moved_row(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-21")
    with mock.patch("django.utils.timezone.now",
                    return_value=datetime.datetime(2026, 11, 18, 12, tzinfo=datetime.timezone.utc)):
        response = client.post(f"/chores/{c.pk}/delete/", {"occurrence": str(o.pk), "scope": "future"})
    assert response.status_code == 302
    # #5 unchanged: the moved row goes, and only the tombstone was left, so the chore goes.
    assert not ChoreOccurrence.objects.filter(pk=o.pk).exists()
    assert not Chore.objects.filter(pk=c.pk).exists()


@pytest.mark.django_db
def test_streak_counts_the_moved_row_on_its_new_date(client, family):
    c = weekly("Trash", 0, "2026-11-02", points=10)
    row(c, nov(2), "done")
    row(c, nov(9), "done")
    o = row(c, nov(16))
    assert progress.chore_streaks(TODAY).get(c.pk, 0) == 0
    move(client, c, o, "2026-11-20")
    assert progress.chore_streaks(TODAY)[c.pk] == 2
    assert progress.chore_streaks(d(nov(20)))[c.pk] == 2
    assert progress.chore_streaks(d(nov(21)))[c.pk] == 0


@pytest.mark.django_db
def test_history_shows_the_moved_row_on_its_new_date(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-18", title="Bins (Wed)")
    ChoreOccurrence.objects.filter(pk=o.pk).update(
        status="done", done_at=datetime.datetime(2026, 11, 18, 9, tzinfo=datetime.timezone.utc))
    text = history_body(client)
    assert f'id="history-{o.pk}"' in text
    assert '<time datetime="2026-11-18">' in text
    assert '<time datetime="2026-11-19">' not in text
    stone = tombstone(c, nov(19))
    assert f'id="history-{stone.pk}"' not in text


@pytest.mark.django_db
def test_rotating_chore_with_empty_pool_becomes_unassigned(client, family):
    mom, emma, leo = family
    c = weekly("Bins", 3, "2026-11-01", mode="rotating", eligibility="kids", points=10)
    o = row(c, nov(19), member=None)
    emma.delete()
    leo.delete()
    body = page(client)
    note = [card["assignee"] for card in sections(body)["upcoming"] if card["id"] == o.pk]
    assert note and note[0] != "Unassigned"
    move(client, c, o, "2026-11-21")
    body = page(client)
    note = [card["assignee"] for card in sections(body)["upcoming"] if card["id"] == o.pk]
    assert note == ["Unassigned"]
    assert values(o)["member_id"] is None


# --- Query count, time zone, no JS -------------------------------------------------------


def _move_queries(client, others):
    c = weekly("Trash", 0, "2026-11-02", points=10)
    o = row(c, nov(16))
    for i in range(others):
        weekly(f"Other {i}", i % 7, "2026-11-02", points=10)
    materialize_week(MONDAY, today=TODAY)  # every chore is already stored this week
    with CaptureQueriesContext(connection) as queries:
        response = move(client, c, o, "2026-11-20")
    assert response.status_code == 302
    assert ChoreOccurrence.objects.get(pk=o.pk).date == d(nov(20))
    return len(queries)


@pytest.mark.django_db
def test_query_count_is_constant(client, family):
    none = _move_queries(client, 0)
    Chore.objects.all().delete()
    ten = _move_queries(client, 10)
    assert ChoreOccurrence.objects.filter(is_cancelled=False).count() >= 10
    assert none == ten


@pytest.mark.django_db
def test_today_is_read_once_from_the_scope(client, family):
    c, o = bins()
    with mock.patch("chores.views.move_occurrence", wraps=services.move_occurrence) as moving:
        move(client, c, o, "2026-11-21")
    assert moving.call_args.args[-1] == TODAY


@pytest.mark.django_db
def test_kiritimati_viewers_today_limits_the_move(client, family):
    c, o = bins()
    # 12:00 UTC on Wed 18 is Thu 19 02:00 in Kiritimati.
    response = move(client, c, o, "2026-11-18", tz="Pacific/Kiritimati")
    assert date_errors(response) == ["Pick a day from Thu 19 Nov to Sun 22 Nov."]
    assert values(o)["date"] == d(nov(19))
    client.cookies.pop("tz")
    assert move(client, c, o, "2026-11-18").status_code == 302
    assert values(o)["date"] == d(nov(18))


@pytest.mark.django_db
def test_kiritimati_sunday_is_already_next_week(client, family):
    mom, emma, leo = family
    c, o = bins()
    # Sun 22 12:00 UTC is Mon 23 in Kiritimati: Thu 19 is an overdue row of last week.
    response = move(client, c, o, "2026-11-24", when=SUN_NOON, tz="Pacific/Kiritimati")
    assert response.status_code == 302
    rows = rows_of(c)
    assert sorted(rows) == [19, 24, 26]
    assert rows[24].pk == o.pk and rows[19].is_cancelled and rows[26].member_id == leo.pk
    c2 = weekly("Lawn", 2, "2026-11-01")
    o2 = row(c2, nov(18))
    utc = move(client, c2, o2, "2026-11-24", when=SUN_NOON, tz="UTC")
    assert date_errors(utc) == ["Pick a day from Sun 22 Nov to Sun 22 Nov."]


@pytest.mark.django_db
def test_no_js_plain_post_with_csrf(family):
    c, o = bins()
    csrf_client = Client(enforce_csrf_checks=True)
    text = get(csrf_client, c, f"?occurrence={o.pk}").content.decode()
    form = OCC_FORM.search(text).group(1)
    assert "hx-" not in form
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', form).group(1)
    data = dict(re.findall(r'<input type="hidden" name="([a-z]+)" value="([^"]*)">', form))
    data.update({"scope": "this", "title": "Bins", "instructions": "old", "due_time": "",
                 "points": "10", "date": "2026-11-21", "csrfmiddlewaretoken": token})
    response = post(csrf_client, c, data)
    assert response.status_code == 302
    assert values(o)["date"] == d(nov(21))
    followed = get(csrf_client, c, response["Location"].split(url(c))[1]).content.decode()
    assert '<input type="date" name="date" value="2026-11-21" id="occ_date">' in followed
    assert "Occurrence moved to Sat 21 Nov" in followed
