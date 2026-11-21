"""Move a row back onto a cancelled date of this week (#49).

Examples use today = Wed 2026-11-18 (UTC), current week Mon 16 - Sun 22 Nov,
and the members Mom (adult, 0), Emma (kid, 1) and Leo (kid, 2). "Bins" is
weekly on Thursday, rotating kids, start 1 Nov; Thu 19 is Emma's.
"Dishes" is daily from 16 Nov, rotating kids, stored for the week.
"""
import pytest
from django.db import IntegrityError, connection, models
from django.test import Client
from django.test.utils import CaptureQueriesContext
from unittest import mock

from chores import services
from chores.models import ChoreOccurrence, OccurrenceException, RotationState
from chores.services import MoveRefused, materialize_week, move_occurrence
from tests.test_calendar import cell
from tests.test_calendar import page as calendar_page
from tests.test_dashboard import WED, chore, d, member
from tests.test_edit_scope import messages_of, nov, row, rows_of, snapshot
from tests.test_occurrence_move import TODAY, MONDAY, bins, date_errors, move, tombstone
from tests.test_undo_delete import REFUSED, deleted, slot, state, undo_post

pytestmark = pytest.mark.django_db

WED_5 = "2026-11-18T12:05:00"
TAKEN = "This chore already has an occurrence on {}."


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def family():
    return member("Mom", "adult", 0), member("Emma", "kid", 1), member("Leo", "kid", 2)


def dishes():
    c = chore("Dishes", start_date="2026-11-16", recurrence="daily", mode="rotating",
              eligibility="kids", points=10)
    materialize_week(MONDAY, today=TODAY)
    return c


def rotation():
    return list(RotationState.objects.values())


# --- Back onto a tombstone ----------------------------------------------------------


def test_back_onto_the_source(client, family):
    c, o = bins()
    before = rotation()
    first = move(client, c, o, "2026-11-21")
    o.refresh_from_db()
    old = tombstone(c, "2026-11-19").pk
    response = move(client, c, o, "2026-11-19")
    assert response.status_code == 302
    assert response["Location"] == f"/chores/{c.pk}/edit/?occurrence={o.pk}"
    assert messages_of(response)[-1] == "Occurrence moved to Thu 19 Nov"
    rows = rows_of(c)
    assert sorted(rows) == [19, 21]
    assert rows[19].pk == o.pk and rows[19].status == "todo" and not rows[19].is_cancelled
    assert rows[21].is_cancelled and rows[21].status == "todo"
    assert not ChoreOccurrence.objects.filter(pk=old).exists()
    assert rotation() == before
    assert first.status_code == 302
    projected = services.project_occurrences(d("2026-11-23"), d("2026-11-29"), today=TODAY)
    assert [(x["date"], x["member_id"]) for x in projected] == [
        (d("2026-11-26"), family[2].pk)]


def test_there_and_back_again_over_three_days(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-21")
    move(client, c, o, "2026-11-22")
    response = move(client, c, o, "2026-11-21")
    assert response.status_code == 302
    rows = rows_of(c)
    assert sorted(rows) == [19, 21, 22]
    assert [rows[x].is_cancelled for x in (19, 21, 22)] == [True, False, True]
    assert rows[21].pk == o.pk


def test_calendar_shows_the_row_on_its_date_only(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-21")
    move(client, c, o, "2026-11-19")
    body = calendar_page(client, "2026-11")
    assert len(cell(body, 19)["dots"]) == 1
    assert cell(body, 21)["dots"] == []


# --- Other cancelled rows ---------------------------------------------------------------


def test_this_only_delete_then_move_onto_it_and_undo_is_refused(client, family):
    c = dishes()
    thu, fri = (ChoreOccurrence.objects.get(chore=c, date=d(nov(x))) for x in (19, 20))
    before = rotation()
    token = deleted(client, c, when=WED, occurrence=thu.pk, scope="this")
    assert ChoreOccurrence.objects.get(pk=thu.pk).is_cancelled
    assert slot(client)["kind"] == "cancel"
    response = move(client, c, fri, "2026-11-19")
    assert response.status_code == 302
    assert not ChoreOccurrence.objects.filter(pk=thu.pk).exists()
    moved = rows_of(c)
    assert moved[19].pk == fri.pk and not moved[19].is_cancelled
    assert moved[20].is_cancelled and moved[20].pk != fri.pk
    assert rotation() == before
    after = state()
    undone = undo_post(client, token, WED_5)
    assert undone.status_code == 400 and REFUSED in undone.content
    assert state() == after
    assert slot(client) is None


def test_undo_first_then_the_move_is_refused(client, family):
    c = dishes()
    thu, fri = (ChoreOccurrence.objects.get(chore=c, date=d(nov(x))) for x in (19, 20))
    token = deleted(client, c, when=WED, occurrence=thu.pk, scope="this")
    assert undo_post(client, token, WED_5).status_code == 302
    before = snapshot()
    response = move(client, c, fri, "2026-11-19")
    assert response.status_code == 200
    assert date_errors(response) == [TAKEN.format("Thu 19 Nov")]
    assert snapshot() == before


def test_stored_planned_cancel(client, family):
    c = chore("Dishes", start_date="2026-11-16", recurrence="daily", mode="rotating",
              eligibility="kids", points=10)
    OccurrenceException.objects.create(chore=c, date=d(nov(21)), is_cancelled=True)
    materialize_week(MONDAY, today=TODAY)
    cancelled = ChoreOccurrence.objects.get(chore=c, date=d(nov(21)))
    assert cancelled.is_cancelled
    fri = ChoreOccurrence.objects.get(chore=c, date=d(nov(20)))
    assert move(client, c, fri, "2026-11-21").status_code == 302
    assert not ChoreOccurrence.objects.filter(pk=cancelled.pk).exists()
    rows = rows_of(c)
    assert rows[21].pk == fri.pk and rows[20].is_cancelled


def test_onto_a_later_week_source_row(client, family):
    c, o = bins()
    move(client, c, o, "2026-11-27")  # #48: Thu 19 is cancelled, an extra is on Fri 27
    cancelled = ChoreOccurrence.objects.get(pk=o.pk)
    assert cancelled.is_cancelled
    overdue = row(c, nov(12), member=family[1])
    extras = list(OccurrenceException.objects.values())
    response = move(client, c, overdue, "2026-11-19")
    assert response.status_code == 302
    rows = rows_of(c)
    assert rows[19].pk == overdue.pk and not rows[19].is_cancelled
    assert rows[12].is_cancelled and rows[12].pk != overdue.pk
    assert list(OccurrenceException.objects.values()) == extras


# --- Refusals -------------------------------------------------------------------------


@pytest.mark.parametrize("status, cancelled", [
    ("todo", False), ("done", False), ("skipped", False), ("done", True), ("skipped", True),
])
def test_other_rows_still_block(client, family, status, cancelled):
    c, o = bins()
    row(c, nov(21), status, is_cancelled=cancelled)
    before = snapshot()
    response = move(client, c, o, "2026-11-21")
    assert response.status_code == 200
    assert date_errors(response) == [TAKEN.format("Sat 21 Nov")]
    assert snapshot() == before


def test_a_cancelled_row_out_of_range_is_not_deleted(client, family):
    c, o = bins()
    gone = row(c, nov(17), is_cancelled=True)
    before = snapshot()
    response = move(client, c, o, "2026-11-17")
    assert date_errors(response) == ["Pick a day from Wed 18 Nov to Thu 31 Dec 2099."]
    assert snapshot() == before
    assert ChoreOccurrence.objects.filter(pk=gone.pk).exists()


def test_lost_race_refuses_and_writes_nothing(client, family):
    c, o = bins()
    held = row(c, nov(21), is_cancelled=True)
    before = snapshot()
    real = models.QuerySet.delete
    calls = []

    def undo_first(queryset):
        if not calls:
            calls.append(1)  # a concurrent Undo un-cancels the row first
            ChoreOccurrence.objects.filter(pk=held.pk).update(is_cancelled=False)
        return real(queryset)

    with mock.patch.object(models.QuerySet, "delete", undo_first):
        response = move(client, c, o, "2026-11-21")
    assert response.status_code == 200
    assert date_errors(response) == [TAKEN.format("Sat 21 Nov")]
    assert snapshot() == before  # the simulated Undo shares the rolled-back transaction


def test_integrity_error_rolls_back_the_delete_and_is_retried_once(family):
    c, o = bins()
    held = row(c, nov(21), is_cancelled=True)
    real_create = ChoreOccurrence.objects.create
    calls = []

    def failing(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            assert not ChoreOccurrence.objects.filter(pk=held.pk).exists()  # deleted in the attempt
            raise IntegrityError("unique_occurrence_per_chore_date")
        return real_create(**kwargs)

    with mock.patch.object(ChoreOccurrence.objects, "create", side_effect=failing):
        assert move_occurrence(o, d(nov(21)), None, 10, "Bins", "old", TODAY) is True
    assert len(calls) == 2
    assert rows_of(c)[21].pk == o.pk
    assert not ChoreOccurrence.objects.filter(pk=held.pk).exists()


# --- Query count ------------------------------------------------------------------------


def _reuse_queries(client, others):
    c, o = bins()
    row(c, nov(21), is_cancelled=True)
    for i in range(others):
        chore(f"Other {i}", start_date="2026-11-02", recurrence="weekly", recurrence_day=i % 7,
              points=10)
    materialize_week(MONDAY, today=TODAY)
    with CaptureQueriesContext(connection) as queries:
        assert move(client, c, o, "2026-11-21").status_code == 302
    return len(queries)


def test_query_count_is_constant(client, family):
    none = _reuse_queries(client, 0)
    ChoreOccurrence.objects.all().delete()
    ten = _reuse_queries(client, 10)
    assert none == ten
