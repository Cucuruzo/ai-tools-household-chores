"""Per-date exceptions for projected occurrences (#31): model, projection,
materialization. The worked examples are the ones in the issue: today Wed
18 Nov 2026, "Bins" weekly on Thursday, rotating over the kids Ann, Ben and
Cat, Thu 19 Nov stored for Ann."""
import datetime
import re
from pathlib import Path
from unittest import mock

import pytest
from django.contrib import admin
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import services
from chores.models import Chore, ChoreOccurrence, Member, OccurrenceException, RotationState
from chores.services import (
    materialize_week, project_occurrences, projected_occurrence, set_projected_member,
)
from tests.test_dashboard import at, main

TODAY = datetime.date(2026, 11, 18)
WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


def d(text):
    return datetime.date.fromisoformat(text)


def member(name, role="kid", order=0):
    return Member.objects.create(name=name, role=role, avatar="😀", order=order)


@pytest.fixture
def family():
    return {
        "ann": member("Ann", order=0), "ben": member("Ben", order=1),
        "cat": member("Cat", order=2), "mom": member("Mom", role="adult", order=3),
    }


@pytest.fixture
def bins(family):
    c = Chore.objects.create(
        title="Bins", start_date=d("2026-11-05"), recurrence="weekly", recurrence_day=3,
        assignment_mode="rotating", eligibility="kids",
    )
    ChoreOccurrence.objects.create(chore=c, date=d("2026-11-19"), member=family["ann"], points=10)
    RotationState.objects.create(chore=c, last_member=family["ann"])
    return c


def exception(c, day, **kwargs):
    return OccurrenceException.objects.create(chore=c, date=d(day), **kwargs)


def members_of(c, start="2026-11-16", end="2026-12-20", today=TODAY):
    """``{date: member name or None}`` that projection gives ``c``."""
    names = dict(Member.objects.values_list("pk", "name"))
    return {
        o["date"].isoformat(): names.get(o["member_id"])
        for o in project_occurrences(d(start), d(end), today=today) if o["chore_id"] == c.pk
    }


def stored_of(c):
    return {
        o.date.isoformat(): (getattr(o.member, "name", None), o.is_override, o.is_cancelled)
        for o in ChoreOccurrence.objects.filter(chore=c).select_related("member")
    }


def state_of(c):
    return RotationState.objects.get(chore=c).last_member.name


def open_week(monday):
    """The week becomes current on its Monday and someone opens it."""
    return materialize_week(d(monday), today=d(monday))


BEFORE = {"2026-11-26": "Ben", "2026-12-03": "Cat", "2026-12-10": "Ann", "2026-12-17": "Ben"}


# --- Model and migration ----------------------------------------------------------


def test_migration_0009_creates_only_the_table():
    import importlib

    from django.db import migrations

    folder = Path(services.__file__).parent / "migrations"
    assert (folder / "0009_occurrenceexception.py").exists()
    migration = importlib.import_module("chores.migrations.0009_occurrenceexception").Migration
    assert migration.dependencies == [("chores", "0008_choreoccurrence_completed_by")]
    kinds = [type(op) for op in migration.operations]
    assert kinds == [migrations.CreateModel, migrations.AddConstraint]
    assert all(getattr(op, "name", "OccurrenceException") == "OccurrenceException"
               or op.model_name == "occurrenceexception" for op in migration.operations)


def test_model_fields_and_admin():
    fields = {f.name: f for f in OccurrenceException._meta.get_fields()}
    assert fields["chore"].remote_field.on_delete.__name__ == "CASCADE"
    assert fields["chore"].remote_field.related_name == "exceptions"
    assert fields["member"].null and fields["member"].blank
    assert fields["member"].remote_field.on_delete.__name__ == "SET_NULL"
    assert fields["member"].remote_field.related_name == "+"
    assert fields["is_cancelled"].default is False and fields["is_override"].default is False
    [constraint] = OccurrenceException._meta.constraints
    assert (constraint.name, tuple(constraint.fields)) == ("unique_exception_per_chore_date", ("chore", "date"))
    assert OccurrenceException in admin.site._registry


@pytest.mark.django_db
def test_one_exception_per_chore_and_date(bins):
    exception(bins, "2026-12-03", is_override=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        exception(bins, "2026-12-03", is_cancelled=True)


@pytest.mark.django_db
def test_deleting_the_chore_deletes_its_exceptions(bins):
    exception(bins, "2026-12-03", is_cancelled=True)
    bins.delete()
    assert not OccurrenceException.objects.exists()


@pytest.mark.django_db
def test_deleting_the_override_member_leaves_an_unassigned_override(bins, family):
    exception(bins, "2026-12-03", is_override=True, member=family["mom"])
    family["mom"].delete()
    e = OccurrenceException.objects.get()
    assert (e.member_id, e.is_override) == (None, True)
    o = projected_occurrence(bins, d("2026-12-03"), TODAY)
    assert (o["member_id"], o["is_override"]) == (None, True)


# --- Projection: examples A, B, C ---------------------------------------------------


@pytest.mark.django_db
def test_no_exception_is_the_plain_rotation(bins):
    assert members_of(bins) == BEFORE
    assert all(o["is_override"] is False for o in project_occurrences(d("2026-11-16"), d("2026-12-20"), TODAY))


@pytest.mark.django_db
def test_example_a_assign_3_dec_to_ann(bins, family):
    exception(bins, "2026-12-03", is_override=True, member=family["ann"])
    assert members_of(bins) == {**BEFORE, "2026-12-03": "Ann"}
    flags = {o["date"].isoformat(): o["is_override"]
             for o in project_occurrences(d("2026-11-16"), d("2026-12-20"), TODAY)}
    assert flags == {"2026-11-26": False, "2026-12-03": True, "2026-12-10": False, "2026-12-17": False}


@pytest.mark.django_db
def test_example_b_unassign_3_dec(bins):
    exception(bins, "2026-12-03", is_override=True, member=None)
    assert members_of(bins) == {**BEFORE, "2026-12-03": None}
    assert projected_occurrence(bins, d("2026-12-03"), TODAY)["is_override"] is True


@pytest.mark.django_db
def test_example_c_cancel_3_dec(bins):
    exception(bins, "2026-12-03", is_cancelled=True)
    expected = dict(BEFORE)
    del expected["2026-12-03"]
    assert members_of(bins) == expected
    assert projected_occurrence(bins, d("2026-12-03"), TODAY) is None


@pytest.mark.django_db
def test_a_range_starting_after_the_exception_still_counts_its_turn(bins, family):
    exception(bins, "2026-12-03", is_cancelled=True)
    assert members_of(bins, start="2026-12-07") == {"2026-12-10": "Ann", "2026-12-17": "Ben"}
    assert members_of(bins, start="2026-12-03", end="2026-12-03") == {}


@pytest.mark.django_db
def test_fixed_chore_mom_to_ben_on_one_sunday(family):
    c = Chore.objects.create(
        title="Laundry", start_date=d("2026-11-16"), recurrence="weekly", recurrence_day=6,
        assignment_mode="fixed", assigned_to=family["mom"],
    )
    exception(c, "2026-11-29", is_override=True, member=family["ben"])
    assert members_of(c) == {
        "2026-11-22": "Mom", "2026-11-29": "Ben", "2026-12-06": "Mom", "2026-12-13": "Mom", "2026-12-20": "Mom",
    }


@pytest.mark.django_db
def test_unassigned_one_off_on_a_future_saturday_to_cat(family):
    c = Chore.objects.create(title="Garage", start_date=d("2026-12-05"), recurrence="once")
    assert members_of(c) == {"2026-12-05": None}
    exception(c, "2026-12-05", is_override=True, member=family["cat"])
    assert members_of(c) == {"2026-12-05": "Cat"}


@pytest.mark.django_db
@pytest.mark.parametrize("day, change", [
    ("2026-11-19", {"is_override": True}),     # stored row
    ("2026-12-02", {"is_cancelled": True}),    # a Wednesday: off the rule
    ("2026-10-29", {"is_cancelled": True}),    # before start_date
    ("2026-12-17", {"is_cancelled": True}),    # after end_date
    ("2026-11-12", {"is_cancelled": True}),    # a past week
])
def test_exception_off_the_projected_dates_changes_nothing(bins, family, day, change):
    Chore.objects.filter(pk=bins.pk).update(end_date=d("2026-12-16"))
    bins.refresh_from_db()
    expected = members_of(bins)
    exception(bins, day, member=family["mom"], **change)
    assert members_of(bins) == expected
    assert stored_of(bins)["2026-11-19"] == ("Ann", False, False)


@pytest.mark.django_db
def test_exception_on_a_stored_one_off_changes_nothing(family):
    c = Chore.objects.create(title="Garage", start_date=d("2026-11-17"), recurrence="once")
    ChoreOccurrence.objects.create(chore=c, date=d("2026-11-17"), points=10)
    exception(c, "2026-11-17", is_override=True, member=family["cat"])
    assert members_of(c) == {}


@pytest.mark.django_db
def test_cancel_wins_over_override_on_the_same_row(bins, family):
    exception(bins, "2026-12-03", is_cancelled=True, is_override=True, member=family["mom"])
    assert "2026-12-03" not in members_of(bins)


@pytest.mark.django_db
def test_projection_writes_nothing(bins, family):
    exception(bins, "2026-12-03", is_override=True, member=family["ann"])
    exception(bins, "2026-12-10", is_cancelled=True)
    with CaptureQueriesContext(connection) as queries:
        project_occurrences(d("2026-11-16"), d("2027-12-31"), TODAY)
        projected_occurrence(bins, d("2026-12-03"), TODAY)
    assert not [q["sql"] for q in queries if WRITE_SQL.match(q["sql"])]
    assert OccurrenceException.objects.count() == 2


# --- Query count (#30) --------------------------------------------------------------


def _count(fn):
    with CaptureQueriesContext(connection) as queries:
        fn()
    return len(queries)


@pytest.mark.django_db
@pytest.mark.parametrize("end", ["2026-11-22", "2027-11-30", "2036-12-31"])
def test_query_count_is_the_same_with_0_and_30_exceptions(family, end):
    chores = [
        Chore.objects.create(title=f"C{i}", start_date=d("2026-11-16"), recurrence="daily",
                             assignment_mode=mode, eligibility="kids", assigned_to=family["mom"])
        for i, mode in enumerate(["rotating", "fixed", "unassigned"])
    ]
    run = lambda: project_occurrences(d("2026-11-16"), d(end), TODAY)  # noqa: E731
    one = lambda: projected_occurrence(chores[0], d("2026-12-03"), TODAY)  # noqa: E731
    empty, empty_one = _count(run), _count(one)
    for i in range(30):
        c = chores[i % 3]
        exception(c, (d("2026-11-23") + datetime.timedelta(days=i)).isoformat(),
                  is_cancelled=i % 2 == 0, is_override=i % 2 == 1, member=family["ben"])
    assert _count(run) == empty
    assert _count(one) == empty_one


@pytest.mark.django_db
def test_exceptions_add_exactly_one_query(bins):
    with mock.patch.object(services, "_exceptions_in", return_value={}):
        without = _count(lambda: project_occurrences(d("2026-11-16"), d("2026-12-31"), TODAY))
    assert _count(lambda: project_occurrences(d("2026-11-16"), d("2026-12-31"), TODAY)) == without + 1


@pytest.mark.django_db
def test_exception_query_is_bounded_to_the_returned_window(bins):
    with CaptureQueriesContext(connection) as queries:
        project_occurrences(d("2026-11-01"), d("2026-12-31"), TODAY)
    [sql] = [q["sql"] for q in queries if "occurrenceexception" in q["sql"]]
    assert "2026-11-16" in sql and "2026-12-31" in sql  # max(start, current Monday), end


# --- Materialization agrees with projection ------------------------------------------


def _shown_on_18_nov(c):
    return members_of(c, start="2026-11-23", end="2026-12-06")


@pytest.mark.django_db
def test_example_a_materialized(bins, family):
    exception(bins, "2026-12-03", is_override=True, member=family["ann"])
    shown = _shown_on_18_nov(bins)
    open_week("2026-11-23")
    assert stored_of(bins)["2026-11-26"] == ("Ben", False, False) and state_of(bins) == "Ben"
    open_week("2026-11-30")
    assert stored_of(bins)["2026-12-03"] == ("Ann", True, False)
    assert state_of(bins) == "Cat"  # the rotation member, never the override
    assert {day: stored_of(bins)[day][0] for day in shown} == shown
    assert members_of(bins, start="2026-12-07", today=d("2026-12-01")) == {"2026-12-10": "Ann", "2026-12-17": "Ben"}
    assert not OccurrenceException.objects.exists()


@pytest.mark.django_db
def test_example_b_materialized(bins):
    exception(bins, "2026-12-03", is_override=True, member=None)
    shown = _shown_on_18_nov(bins)
    open_week("2026-11-23")
    open_week("2026-11-30")
    assert stored_of(bins)["2026-12-03"] == (None, True, False)
    assert state_of(bins) == "Cat"
    assert {day: stored_of(bins)[day][0] for day in shown} == shown
    assert members_of(bins, start="2026-12-07", today=d("2026-12-01")) == {"2026-12-10": "Ann", "2026-12-17": "Ben"}


@pytest.mark.django_db
def test_example_c_materialized_and_hidden_on_the_dashboard(bins):
    exception(bins, "2026-12-03", is_cancelled=True)
    shown = _shown_on_18_nov(bins)
    assert shown == {"2026-11-26": "Ben"}
    open_week("2026-11-23")
    open_week("2026-11-30")
    assert stored_of(bins)["2026-12-03"] == ("Cat", False, True)
    assert state_of(bins) == "Cat"
    assert members_of(bins, start="2026-12-07", today=d("2026-12-01")) == {"2026-12-10": "Ann", "2026-12-17": "Ben"}
    client = Client()
    with at("2026-12-01T12:00:00"):
        body = main(client.get("/dashboard/").content.decode())
    assert 'datetime="2026-11-26"' in body  # still overdue: the page is the real one
    assert 'datetime="2026-12-03"' not in body
    with at("2026-12-01T12:00:00"):
        week = main(client.get("/dashboard/", {"week": "2026-W49"}).content.decode())
    assert 'datetime="2026-12-03"' not in week


@pytest.mark.django_db
def test_materialize_consumes_exceptions_up_to_sunday_only(bins, family):
    exception(bins, "2026-11-12", is_cancelled=True)  # a week nobody opened
    exception(bins, "2026-11-26", is_override=True, member=family["mom"])
    later = exception(bins, "2026-12-03", is_override=True, member=family["cat"])
    open_week("2026-11-23")
    assert set(OccurrenceException.objects.values_list("pk", flat=True)) == {later.pk}
    assert stored_of(bins)["2026-11-26"] == ("Mom", True, False)
    assert state_of(bins) == "Ben"


@pytest.mark.django_db
def test_materialize_ignores_exceptions_off_the_rule(bins, family):
    exception(bins, "2026-11-25", is_override=True, member=family["mom"])  # a Wednesday
    open_week("2026-11-23")
    assert set(stored_of(bins)) == {"2026-11-19", "2026-11-26"}
    assert stored_of(bins)["2026-11-26"] == ("Ben", False, False)
    assert not OccurrenceException.objects.exists()


@pytest.mark.django_db
def test_fixed_and_one_off_materialized(family):
    fixed = Chore.objects.create(
        title="Laundry", start_date=d("2026-11-16"), recurrence="weekly", recurrence_day=6,
        assignment_mode="fixed", assigned_to=family["mom"],
    )
    one_off = Chore.objects.create(title="Garage", start_date=d("2026-11-28"), recurrence="once")
    exception(fixed, "2026-11-29", is_override=True, member=family["ben"])
    exception(one_off, "2026-11-28", is_override=True, member=family["cat"])
    open_week("2026-11-23")
    assert stored_of(fixed)["2026-11-29"] == ("Ben", True, False)
    assert stored_of(one_off) == {"2026-11-28": ("Cat", True, False)}
    assert not RotationState.objects.exists()


def _patched_projection(collide_on_calls, extra):
    real = project_occurrences
    calls = []

    def fake(start, end, today=None):
        calls.append(1)
        result = real(start, end, today=today)
        if len(calls) in collide_on_calls:
            result = sorted(result + [extra], key=lambda o: (o["date"], o["title"], o["chore_id"]))
        return result

    return mock.patch.object(services, "project_occurrences", side_effect=fake), calls


def _collision(c, day):
    return {"chore_id": c.pk, "title": c.title, "date": d(day), "due_time": None,
            "member_id": None, "points": c.points, "assignment_mode": c.assignment_mode,
            "is_override": False}


@pytest.mark.django_db
def test_double_collision_rolls_back_exceptions_too(bins, family):
    other = Chore.objects.create(title="Z", start_date=d("2026-11-23"), recurrence="once")
    ChoreOccurrence.objects.create(chore=other, date=d("2026-11-23"), points=10)
    exception(bins, "2026-11-26", is_override=True, member=family["mom"])
    exception(bins, "2026-11-12", is_cancelled=True)
    before = sorted(OccurrenceException.objects.values_list("chore_id", "date", "is_cancelled", "is_override", "member_id"))
    patch, calls = _patched_projection({1, 2}, _collision(other, "2026-11-23"))
    with patch, pytest.raises(IntegrityError):
        open_week("2026-11-23")
    assert len(calls) == 2
    assert sorted(OccurrenceException.objects.values_list(
        "chore_id", "date", "is_cancelled", "is_override", "member_id")) == before
    assert set(stored_of(bins)) == {"2026-11-19"} and state_of(bins) == "Ann"


@pytest.mark.django_db
def test_single_collision_retries_and_applies_exceptions_once(bins, family):
    other = Chore.objects.create(title="Z", start_date=d("2026-11-29"), recurrence="once")
    ChoreOccurrence.objects.create(chore=other, date=d("2026-11-29"), points=10)
    exception(bins, "2026-11-26", is_cancelled=True)
    patch, calls = _patched_projection({1}, _collision(other, "2026-11-29"))
    with patch:
        open_week("2026-11-23")
    assert len(calls) == 2
    assert stored_of(bins)["2026-11-26"] == ("Ben", False, True)
    assert state_of(bins) == "Ben"  # one turn, not two
    assert not OccurrenceException.objects.exists()


# --- Service helpers ------------------------------------------------------------------


@pytest.mark.django_db
def test_projected_occurrence_matches_project_occurrences(bins, family):
    exception(bins, "2026-12-03", is_override=True, member=family["mom"])
    for day in ["2026-11-26", "2026-12-03", "2026-12-10"]:
        [expected] = [o for o in project_occurrences(d(day), d(day), TODAY) if o["chore_id"] == bins.pk]
        assert projected_occurrence(bins, d(day), TODAY) == expected
    for day in ["2026-11-19", "2026-12-02", "2026-11-12", "2026-10-29"]:
        assert projected_occurrence(bins, d(day), TODAY) is None
    template = Chore.objects.create(title="T", start_date=d("2026-11-16"), recurrence="daily", is_template=True)
    assert projected_occurrence(template, d("2026-11-26"), TODAY) is None


@pytest.mark.django_db
def test_set_projected_member_creates_then_updates_one_row(bins, family):
    set_projected_member(bins, d("2026-12-03"), family["ann"].pk)
    set_projected_member(bins, d("2026-12-03"), None)
    e = OccurrenceException.objects.get()
    assert (e.chore_id, e.date, e.is_override, e.is_cancelled, e.member_id) == (bins.pk, d("2026-12-03"), True, False, None)
    assert set(stored_of(bins)) == {"2026-11-19"} and state_of(bins) == "Ann"


@pytest.mark.django_db
def test_set_projected_member_retries_a_concurrent_insert_once(bins, family):
    real = OccurrenceException.objects.update_or_create
    calls = []

    def racing(**kwargs):
        calls.append(1)
        if len(calls) == 1:  # another request inserted (chore, date) first
            OccurrenceException.objects.create(chore=bins, date=d("2026-12-03"), is_override=True)
            raise IntegrityError("unique_exception_per_chore_date")
        return real(**kwargs)

    with mock.patch.object(OccurrenceException.objects, "update_or_create", side_effect=racing):
        set_projected_member(bins, d("2026-12-03"), family["cat"].pk)
    assert len(calls) == 2
    assert OccurrenceException.objects.get().member_id == family["cat"].pk


# --- Other readers never see exceptions (#14, #15, #16, #17) ----------------------------


def _calendar_count(body, day):
    m = re.search(rf'aria-label="Show [^"]*">{day}</a>\s*(?:<span class="count" aria-label="[^"]*">(\d+)</span>)?', body)
    return int(m.group(1) or 0)


@pytest.mark.django_db
def test_calendar_follows_projection(bins, family):
    client = Client()

    def month():
        with at("2026-11-18T12:00:00"):
            return client.get("/calendar/", {"month": "2026-12"}).content.decode()

    assert (_calendar_count(month(), 3), _calendar_count(month(), 10)) == (1, 1)
    exception(bins, "2026-12-03", is_cancelled=True)
    exception(bins, "2026-12-10", is_override=True, member=family["mom"])
    with CaptureQueriesContext(connection) as queries:
        body = month()
    assert (_calendar_count(body, 3), _calendar_count(body, 10)) == (0, 1)
    assert not [q["sql"] for q in queries if WRITE_SQL.match(q["sql"])]


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["/contribution/", "/progress/", "/history/"])
def test_contribution_progress_history_ignore_exceptions(bins, family, url):
    ChoreOccurrence.objects.filter(chore=bins).update(status="done", completed_by=family["ann"])
    client = Client()

    def body():
        with at("2026-11-20T12:00:00"):
            return main(client.get(url).content.decode())

    before = body()
    exception(bins, "2026-11-26", is_override=True, member=family["cat"])
    exception(bins, "2026-12-03", is_cancelled=True)
    with CaptureQueriesContext(connection) as queries:
        after = body()
    assert after == before
    assert not any("occurrenceexception" in q["sql"] for q in queries)
