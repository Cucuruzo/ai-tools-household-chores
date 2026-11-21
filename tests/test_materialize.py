import datetime
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction

from chores import services
from chores.models import Chore, ChoreOccurrence, Member, RotationState
from chores.services import materialize_week, project_occurrences

# Week Mon 2026-11-16 .. Sun 2026-11-22, today Wed 2026-11-18.
MONDAY = "2026-11-16"
TODAY = "2026-11-18"
WEEK = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22"]
FIELDS = ("chore_id", "date", "member_id", "due_time", "points", "status", "done_at", "is_override")


def d(text):
    return datetime.date.fromisoformat(text)


def member(name, role="kid", order=0):
    return Member.objects.create(name=name, role=role, avatar="😀", order=order)


def chore(recurrence="daily", start_date=MONDAY, mode="unassigned", **kwargs):
    return Chore.objects.create(
        title=kwargs.pop("title", "Dishes"), start_date=d(start_date), recurrence=recurrence,
        assignment_mode=mode, **kwargs,
    )


def rotating(title="Dishes", **kwargs):
    kwargs.setdefault("eligibility", "kids")
    return chore(mode="rotating", title=title, **kwargs)


def stored(c, day, member=None, **kwargs):
    return ChoreOccurrence.objects.create(chore=c, date=d(day), member=member, points=c.points, **kwargs)


def run(week_start=MONDAY, today=TODAY):
    return materialize_week(d(week_start), today=d(today))


def dates(rows, c=None):
    return [o.date.isoformat() for o in rows if c is None or o.chore_id == c.pk]


def who(rows, c=None):
    return [(o.date.isoformat(), o.member_id) for o in rows if c is None or o.chore_id == c.pk]


def stored_dates(c):
    return [x.isoformat() for x in ChoreOccurrence.objects.filter(chore=c).order_by("date").values_list("date", flat=True)]


def state_of(c):
    return RotationState.objects.filter(chore=c).values_list("last_member_id", flat=True).first()


def set_state(c, last):
    RotationState.objects.create(chore=c, last_member=last)


def rows_snapshot():
    return sorted(ChoreOccurrence.objects.values_list("id", *FIELDS))


def state_snapshot():
    return sorted(RotationState.objects.values_list("id", "chore_id", "last_member_id"))


@pytest.fixture
def abc(db):
    return member("A", order=0), member("B", order=1), member("C", order=2)


# --- Signature ----------------------------------------------------------------


@pytest.mark.django_db
def test_not_a_monday_raises_and_writes_nothing(abc):
    rotating()
    with pytest.raises(ValueError):
        materialize_week(d("2026-11-17"), today=d(TODAY))
    assert ChoreOccurrence.objects.count() == 0
    assert RotationState.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("week_start", ["2026-11-09", "2026-11-23"], ids=["past", "future"])
def test_monday_other_than_current_raises_and_writes_nothing(abc, week_start):
    rotating()
    with pytest.raises(ValueError):
        run(week_start)
    assert ChoreOccurrence.objects.count() == 0
    assert RotationState.objects.count() == 0


@pytest.mark.django_db
def test_today_defaults_to_localdate():
    c = chore()
    with mock.patch("chores.services.timezone.localdate", return_value=d(TODAY)):
        created = materialize_week(d(MONDAY))
    assert dates(created, c) == WEEK


@pytest.mark.django_db
def test_returns_created_rows_in_projection_order():
    bins = chore(title="Bins", recurrence="weekly", recurrence_day=2)
    alpha_1 = chore(title="Alpha")
    alpha_2 = chore(title="Alpha")  # same title: tie broken by chore_id
    created = run()
    assert all(isinstance(o, ChoreOccurrence) and o.pk for o in created)
    expected = []
    for day in WEEK:
        expected += [(day, alpha_1.pk), (day, alpha_2.pk)]
        if day == "2026-11-18":
            expected.append((day, bins.pk))
    assert [(o.date.isoformat(), o.chore_id) for o in created] == expected
    assert sorted(o.pk for o in created) == sorted(ChoreOccurrence.objects.values_list("pk", flat=True))


@pytest.mark.django_db
def test_call_that_creates_nothing_returns_empty_list():
    chore(recurrence="once", start_date="2026-11-30")
    assert run() == []


# --- What gets created -------------------------------------------------------------


@pytest.mark.django_db
def test_created_rows_equal_projection(abc):
    a, b, _ = abc
    chore(title="Fixed", mode="fixed", assigned_to=a)
    rotating(title="Rot")
    rotating(title="RotWeekly", recurrence="weekly", recurrence_day=4)
    set_state(rotating(title="RotState", eligibility="everyone"), b)
    chore(title="Monthly", recurrence="monthly", recurrence_day=18)
    chore(title="Once", recurrence="once", start_date="2026-11-21")
    late = chore(title="Late")
    stored(late, "2026-11-19")
    projected = project_occurrences(d(MONDAY), d("2026-11-22"), today=d(TODAY))
    created = run()
    assert [(o.chore_id, o.date, o.member_id) for o in created] == [
        (o["chore_id"], o["date"], o["member_id"]) for o in projected
    ]


@pytest.mark.django_db
def test_created_row_fields(abc):
    c = chore(mode="fixed", assigned_to=abc[0], points=20, due_time=datetime.time(18, 30))
    no_time = chore(title="NoTime", recurrence="once")
    created = run()
    for o in created:
        o.refresh_from_db()
        assert o.status == ChoreOccurrence.Status.TODO
        assert o.done_at is None
        assert o.is_override is False
    assert {(o.member_id, o.due_time, o.points) for o in created if o.chore_id == c.pk} == {
        (abc[0].pk, datetime.time(18, 30), 20)
    }
    [once] = [o for o in created if o.chore_id == no_time.pk]
    assert once.due_time is None and once.points == 10 and once.member_id is None


@pytest.mark.django_db
def test_due_time_and_points_are_copied_not_linked():
    c = chore(points=10, due_time=datetime.time(8, 0))
    run()
    c.points = 20
    c.due_time = datetime.time(9, 0)
    c.save()
    run(today="2026-11-21")
    assert set(ChoreOccurrence.objects.values_list("points", "due_time")) == {(10, datetime.time(8, 0))}


@pytest.mark.django_db
def test_mixed_chores_in_one_week(abc):
    a = abc[0]
    fixed = chore(title="Fixed", mode="fixed", assigned_to=a)
    weekly = rotating(title="Weekly", recurrence="weekly", recurrence_day=4)
    monthly = chore(title="Monthly", recurrence="monthly", recurrence_day=18)
    once_in = chore(title="OnceIn", recurrence="once", start_date="2026-11-21")
    once_out = chore(title="OnceOut", recurrence="once", start_date="2026-11-25")
    created = run()
    assert who(created, fixed) == [(day, a.pk) for day in WEEK]
    assert who(created, weekly) == [("2026-11-20", a.pk)]
    assert who(created, monthly) == [("2026-11-18", None)]
    assert who(created, once_in) == [("2026-11-21", None)]
    assert dates(created, once_out) == []
    assert len(created) == 10


@pytest.mark.django_db
def test_templates_get_no_rows_and_no_state(abc):
    rotating(is_template=True)
    chore(title="FixedTpl", mode="fixed", assigned_to=abc[0], is_template=True)
    assert run() == []
    assert ChoreOccurrence.objects.count() == 0
    assert RotationState.objects.count() == 0


@pytest.mark.django_db
def test_templates_with_existing_state_are_untouched(abc):
    tpl = rotating(is_template=True)
    set_state(tpl, abc[0])
    run()
    assert state_of(tpl) == abc[0].pk
    assert ChoreOccurrence.objects.filter(chore=tpl).count() == 0


@pytest.mark.django_db
def test_no_chores_returns_empty_and_writes_nothing():
    assert run() == []
    assert ChoreOccurrence.objects.count() == 0
    assert RotationState.objects.count() == 0


# --- A week that is partly in the past ----------------------------------------------


@pytest.mark.django_db
def test_whole_week_including_past_days_is_materialized(abc):
    a, b, c = abc
    rot = rotating(start_date="2026-11-01")
    created = run()
    assert who(created, rot) == list(zip(WEEK, [a.pk, b.pk, c.pk, a.pk, b.pk, c.pk, a.pk]))
    past = ChoreOccurrence.objects.filter(chore=rot, date__lt=d(TODAY))
    assert sorted(past.values_list("date", "status")) == [
        (d("2026-11-16"), "todo"), (d("2026-11-17"), "todo"),
    ]


@pytest.mark.django_db
def test_no_back_fill_before_current_monday():
    c = chore(start_date="2026-11-01")
    run()
    assert stored_dates(c) == WEEK
    assert not ChoreOccurrence.objects.filter(date__lt=d(MONDAY)).exists()


@pytest.mark.django_db
def test_start_date_later_in_the_week():
    c = chore(start_date="2026-11-20")
    assert dates(run(), c) == ["2026-11-20", "2026-11-21", "2026-11-22"]


@pytest.mark.django_db
def test_once_chore_earlier_in_current_week_is_created_as_todo():
    c = chore(recurrence="once", start_date=MONDAY)
    [row] = run()
    assert (row.chore_id, row.date.isoformat(), row.status) == (c.pk, MONDAY, "todo")


@pytest.mark.django_db
def test_once_chore_before_current_monday_is_not_created():
    chore(recurrence="once", start_date="2026-11-12")
    assert run() == []
    assert ChoreOccurrence.objects.count() == 0


# --- Who gets it ---------------------------------------------------------------------


@pytest.mark.django_db
def test_fixed_uses_current_assignee_and_no_state(abc):
    a, b, _ = abc
    c = chore(mode="fixed", assigned_to=a)
    c.assigned_to = b
    c.save()
    created = run()
    assert who(created, c) == [(day, b.pk) for day in WEEK]
    assert RotationState.objects.count() == 0


@pytest.mark.django_db
def test_fixed_without_assignee_is_null():
    c = chore(mode="fixed")
    assert who(run(), c) == [(day, None) for day in WEEK]
    assert RotationState.objects.count() == 0


@pytest.mark.django_db
def test_unassigned_is_null_and_no_state(abc):
    c = chore(mode="unassigned", eligibility="kids")
    assert who(run(), c) == [(day, None) for day in WEEK]
    assert RotationState.objects.count() == 0


@pytest.mark.django_db
def test_rotation_sequence_and_final_state(abc):
    a, b, c = abc
    rot = rotating()
    created = run()
    assert who(created, rot) == list(zip(WEEK, [a.pk, b.pk, c.pk, a.pk, b.pk, c.pk, a.pk]))
    assert state_of(rot) == a.pk


@pytest.mark.django_db
def test_advance_rotation_called_once_per_created_row(abc):
    rot = rotating()
    stored(rot, "2026-11-19")  # only 11-20 .. 11-22 are created
    with mock.patch.object(services, "advance_rotation", wraps=services.advance_rotation) as spy:
        created = run()
    assert dates(created, rot) == ["2026-11-20", "2026-11-21", "2026-11-22"]
    assert spy.call_count == 3
    assert all(call.args == (rot,) for call in spy.call_args_list)


@pytest.mark.django_db
def test_rotation_continues_from_state_not_from_hand_changed_member():
    a, b = member("A", order=0), member("B", order=1)
    rot = rotating(recurrence="weekly", recurrence_day=0)
    [first] = run()
    assert first.member_id == a.pk
    ChoreOccurrence.objects.filter(pk=first.pk).update(member=b, is_override=True)
    [second] = run("2026-11-23", today="2026-11-25")
    assert (second.date.isoformat(), second.member_id) == ("2026-11-23", b.pk)
    first.refresh_from_db()
    assert (first.member_id, first.is_override) == (b.pk, True)
    assert state_of(rot) == b.pk


@pytest.mark.django_db
def test_missed_weeks_use_no_turn():
    a, b = member("A", order=0), member("B", order=1)
    rot = rotating(recurrence="weekly", recurrence_day=0, start_date="2026-10-26")
    stored(rot, "2026-11-02", member=a)
    set_state(rot, a)
    created = run()
    assert who(created, rot) == [("2026-11-16", b.pk)]
    assert stored_dates(rot) == ["2026-11-02", "2026-11-16"]


@pytest.mark.django_db
@pytest.mark.parametrize("setup", ["only_adults", "no_members", "null_eligibility"])
def test_empty_pool_without_state_creates_null_rows(setup):
    eligibility = "kids"
    if setup == "only_adults":
        member("Mum", role="adult")
    elif setup == "null_eligibility":
        member("A")
        eligibility = None
    rot = rotating(eligibility=eligibility)
    assert who(run(), rot) == [(day, None) for day in WEEK]
    assert RotationState.objects.count() == 0


@pytest.mark.django_db
def test_empty_pool_with_state_keeps_last_member():
    mum = member("Mum", role="adult")
    rot = rotating(eligibility="kids")
    set_state(rot, mum)
    assert who(run(), rot) == [(day, None) for day in WEEK]
    assert state_snapshot() == [(RotationState.objects.get().pk, rot.pk, mum.pk)]


@pytest.mark.django_db
def test_pool_filled_later_without_state():
    member("Mum", role="adult")
    rot = rotating(recurrence="weekly", recurrence_day=0)
    [first] = run()
    assert first.member_id is None
    a = member("A", order=5)
    member("B", order=6)
    [second] = run("2026-11-23", today="2026-11-25")
    assert second.member_id == a.pk
    first.refresh_from_db()
    assert first.member_id is None


@pytest.mark.django_db
def test_pool_filled_later_continues_from_stored_last_member():
    a, b = member("A", order=0), member("B", order=1)
    rot = rotating(recurrence="weekly", recurrence_day=0)
    set_state(rot, a)
    Member.objects.update(role="adult")
    [first] = run()
    assert first.member_id is None
    assert state_of(rot) == a.pk
    Member.objects.update(role="kid")
    [second] = run("2026-11-23", today="2026-11-25")
    assert second.member_id == b.pk
    first.refresh_from_db()
    assert first.member_id is None


@pytest.mark.django_db
def test_rotating_chores_are_independent(abc):
    a, b, _ = abc
    one = rotating(title="One", recurrence="weekly", recurrence_day=0)
    two = rotating(title="Two", recurrence="weekly", recurrence_day=0)
    created = run()
    assert who(created, one) == [(MONDAY, a.pk)]
    assert who(created, two) == [(MONDAY, a.pk)]
    assert state_of(one) == a.pk and state_of(two) == a.pk


# --- Never modify what is stored ---------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["done", "skipped", "override"])
def test_existing_rows_are_never_changed(abc, kind):
    a, b, _ = abc
    rot = rotating(points=20, due_time=datetime.time(7, 0))
    extra = {
        "done": {"status": "done", "done_at": datetime.datetime(2026, 11, 18, 9, 0, tzinfo=datetime.timezone.utc)},
        "skipped": {"status": "skipped"},
        "override": {"is_override": True},
    }[kind]
    existing = ChoreOccurrence.objects.create(
        chore=rot, date=d(TODAY), member=b, points=5, due_time=datetime.time(6, 0), **extra
    )
    before = ChoreOccurrence.objects.filter(pk=existing.pk).values_list(*FIELDS).get()
    created = run()
    assert existing.pk not in [o.pk for o in created]
    assert ChoreOccurrence.objects.filter(chore=rot, date=d(TODAY)).count() == 1
    assert ChoreOccurrence.objects.filter(pk=existing.pk).values_list(*FIELDS).get() == before
    assert dates(created, rot) == ["2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22"]


@pytest.mark.django_db
def test_gaps_before_latest_stored_row_are_not_filled():
    c = chore()
    stored(c, "2026-11-16")
    stored(c, "2026-11-18")
    created = run()
    assert dates(created, c) == ["2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22"]
    assert not ChoreOccurrence.objects.filter(chore=c, date=d("2026-11-17")).exists()


@pytest.mark.django_db
def test_later_dated_existing_row_is_respected():
    c = chore()
    existing = stored(c, "2026-11-20")
    created = run()
    assert dates(created, c) == ["2026-11-21", "2026-11-22"]
    assert stored_dates(c) == ["2026-11-20", "2026-11-21", "2026-11-22"]
    assert ChoreOccurrence.objects.filter(chore=c, date=d("2026-11-20")).get().pk == existing.pk


# --- Idempotency and repeat runs ----------------------------------------------------


@pytest.mark.django_db
def test_second_run_changes_nothing(abc):
    rotating(title="Rot")
    chore(title="Fixed", mode="fixed", assigned_to=abc[1])
    chore(title="Monthly", recurrence="monthly", recurrence_day=18)
    assert run()
    rows, states = rows_snapshot(), state_snapshot()
    assert run() == []
    assert rows_snapshot() == rows
    assert state_snapshot() == states


@pytest.mark.django_db
def test_later_run_in_same_week_creates_nothing_new(abc):
    rotating(title="Rot")
    chore(title="Daily")
    chore(title="Once", recurrence="once", start_date="2026-11-21")
    run()
    rows, states = rows_snapshot(), state_snapshot()
    assert run(today="2026-11-21") == []
    assert rows_snapshot() == rows
    assert state_snapshot() == states


@pytest.mark.django_db
@pytest.mark.parametrize(
    "start_date, expected",
    [(MONDAY, WEEK), ("2026-11-19", WEEK[3:])],
    ids=["start-monday", "start-thursday"],
)
def test_chore_added_mid_week_is_picked_up(abc, start_date, expected):
    a, b, c = abc
    old = rotating(title="Old")
    run()
    rows, old_state = rows_snapshot(), state_of(old)
    new = rotating(title="New", start_date=start_date)
    created = run()
    assert dates(created) == expected
    assert {o.chore_id for o in created} == {new.pk}
    turns = [a.pk, b.pk, c.pk, a.pk, b.pk, c.pk, a.pk][: len(expected)]
    assert who(created, new) == list(zip(expected, turns))
    assert state_of(new) == turns[-1]
    assert state_of(old) == old_state
    assert sorted(r for r in rows_snapshot() if r[1] == old.pk) == rows


# --- Transaction and concurrency ----------------------------------------------------


@pytest.mark.django_db
def test_failure_partway_rolls_back_everything(abc):
    a, b, _ = abc
    fresh = rotating(title="A-fresh")  # no state yet: advance_rotation creates it
    known = rotating(title="B-known")
    set_state(known, a)                # has state: advance_rotation updates it
    rows, states = rows_snapshot(), state_snapshot()
    real_save = ChoreOccurrence.save
    calls = []

    def failing_save(self, *args, **kwargs):
        calls.append(self.date)
        if len(calls) == 3:
            raise RuntimeError("boom")
        return real_save(self, *args, **kwargs)

    with mock.patch.object(ChoreOccurrence, "save", autospec=True, side_effect=failing_save):
        with pytest.raises(RuntimeError, match="boom"):
            run()
    assert len(calls) == 3  # both chores had already taken turns
    assert rows_snapshot() == rows
    assert state_snapshot() == states
    assert state_of(fresh) is None and state_of(known) == a.pk


def _with_collision(c, day):
    """Projection entry for an already stored (chore, date), as a concurrent
    run would have seen it before the other request stored that row."""
    return {
        "chore_id": c.pk, "title": c.title, "date": d(day), "due_time": c.due_time,
        "member_id": None, "points": c.points, "assignment_mode": c.assignment_mode,
    }


def _patched_projection(extra, collide_on_calls):
    real = project_occurrences
    calls = []

    def fake(start, end, today=None):
        calls.append(1)
        result = real(start, end, today=today)
        if len(calls) in collide_on_calls:
            result = sorted(result + [extra], key=lambda o: (o["date"], o["title"], o["chore_id"]))
        return result

    return mock.patch.object(services, "project_occurrences", side_effect=fake), calls


@pytest.mark.django_db
def test_collision_is_rolled_back_and_retried_once(abc):
    a, b, c = abc
    rot = rotating(title="A-rot")
    other = chore(title="B-fixed", mode="fixed", assigned_to=b)
    existing = stored(other, TODAY, member=c, status="done")
    before = ChoreOccurrence.objects.filter(pk=existing.pk).values_list(*FIELDS).get()
    patch, calls = _patched_projection(_with_collision(other, TODAY), collide_on_calls={1})
    with patch:
        created = run()  # rot's 11-16 and 11-17 turns are taken, then 11-18 collides
    assert len(calls) == 2
    assert ChoreOccurrence.objects.filter(chore=other, date=d(TODAY)).count() == 1
    assert ChoreOccurrence.objects.filter(pk=existing.pk).values_list(*FIELDS).get() == before
    assert who(created, rot) == list(zip(WEEK, [a.pk, b.pk, c.pk, a.pk, b.pk, c.pk, a.pk]))
    assert dates(created, other) == WEEK[3:]
    assert ChoreOccurrence.objects.count() == 1 + 7 + 4
    assert state_of(rot) == [o for o in created if o.chore_id == rot.pk][-1].member_id


@pytest.mark.django_db
def test_double_collision_raises_and_writes_nothing(abc):
    a = abc[0]
    rot = rotating(title="A-rot")
    known = rotating(title="C-known")
    set_state(known, a)
    other = chore(title="B-fixed", mode="fixed", assigned_to=a)
    stored(other, TODAY)
    rows, states = rows_snapshot(), state_snapshot()
    patch, calls = _patched_projection(_with_collision(other, TODAY), collide_on_calls={1, 2})
    with patch:
        with pytest.raises(IntegrityError):
            run()
    assert len(calls) == 2
    assert rows_snapshot() == rows
    assert state_snapshot() == states
    assert state_of(rot) is None


@pytest.mark.django_db
def test_works_inside_callers_transaction(abc):
    rotating()

    class Abort(Exception):
        pass

    with pytest.raises(Abort):
        with transaction.atomic():
            assert len(run()) == 7
            assert ChoreOccurrence.objects.count() == 7
            raise Abort
    assert ChoreOccurrence.objects.count() == 0
    assert RotationState.objects.count() == 0


# --- Housekeeping -----------------------------------------------------------------


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)
