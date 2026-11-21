import datetime
import inspect
import re

import pytest
from django.contrib import admin
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from chores import services
from chores.models import Chore, ChoreOccurrence, Member, RotationState
from chores.services import advance_rotation, next_member, project_occurrences

WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
KEYS = {"chore_id", "title", "instructions", "date", "due_time", "member_id", "points", "assignment_mode", "is_override"}


def d(text):
    return datetime.date.fromisoformat(text)


def member(name, role="kid", order=0):
    return Member.objects.create(name=name, role=role, avatar="😀", order=order)


def chore(recurrence="daily", start_date="2026-11-16", mode="unassigned", **kwargs):
    return Chore.objects.create(
        title=kwargs.pop("title", "Dishes"), start_date=d(start_date), recurrence=recurrence,
        assignment_mode=mode, **kwargs,
    )


def stored(c, day, member=None, status="todo"):
    return ChoreOccurrence.objects.create(chore=c, date=d(day), member=member, points=c.points, status=status)


def project(start, end, today):
    return project_occurrences(d(start), d(end), today=d(today))


def dates(result, c=None):
    return [o["date"].isoformat() for o in result if c is None or o["chore_id"] == c.pk]


def who(result):
    return [(o["date"].isoformat(), o["member_id"]) for o in result]


def snapshot():
    return (
        Chore.objects.count(),
        ChoreOccurrence.objects.count(),
        sorted(RotationState.objects.values_list("id", "chore_id", "last_member_id")),
    )


# --- ChoreOccurrence model -------------------------------------------------


@pytest.mark.django_db
def test_occurrence_registered_in_admin():
    assert ChoreOccurrence in admin.site._registry


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


@pytest.mark.django_db
def test_occurrence_defaults_and_choices():
    occ = stored(chore(points=20), "2026-11-16")
    occ.refresh_from_db()
    assert occ.status == "todo"
    assert occ.is_override is False
    assert occ.points == 20
    assert occ.done_at is None and occ.due_time is None
    assert [c for c, _ in ChoreOccurrence.Status.choices] == ["todo", "done", "skipped"]


@pytest.mark.django_db
def test_unique_chore_and_date():
    c = chore()
    stored(c, "2026-11-16")
    with pytest.raises(IntegrityError), transaction.atomic():
        stored(c, "2026-11-16")
    stored(chore(title="Other"), "2026-11-16")  # other chore, same date: fine


@pytest.mark.django_db
def test_deleting_member_keeps_occurrence_with_null_member():
    ann = member("Ann")
    occ = stored(chore(), "2026-11-16", member=ann)
    ann.delete()
    occ.refresh_from_db()
    assert occ.member is None


@pytest.mark.django_db
def test_deleting_chore_deletes_occurrences():
    c = chore()
    stored(c, "2026-11-16")
    c.delete()
    assert ChoreOccurrence.objects.count() == 0


# --- Signature and output --------------------------------------------------


def test_signature():
    params = inspect.signature(project_occurrences).parameters
    assert list(params) == ["start", "end", "today"]
    assert params["today"].default is None


@pytest.mark.django_db
def test_today_defaults_to_localdate(monkeypatch):
    chore(start_date="2026-11-16")
    monkeypatch.setattr(services.timezone, "localdate", lambda: d("2026-11-18"))
    result = project_occurrences(d("2026-11-01"), d("2026-11-17"))
    assert dates(result) == ["2026-11-16", "2026-11-17"]


@pytest.mark.django_db
def test_output_keys_and_values():
    ann = member("Ann", "adult")
    c = chore("once", "2026-11-18", mode="fixed", assigned_to=ann, points=20,
              due_time=datetime.time(7, 30), title="Trash")
    [occ] = project("2026-11-16", "2026-11-22", "2026-11-16")
    assert set(occ) == KEYS
    assert occ == {
        "chore_id": c.pk, "title": "Trash", "instructions": "", "date": d("2026-11-18"),
        "due_time": datetime.time(7, 30), "member_id": ann.pk, "points": 20,
        "assignment_mode": "fixed", "is_override": False,
    }
    assert type(occ["date"]) is datetime.date


@pytest.mark.django_db
def test_due_time_none_when_not_set():
    chore("once", "2026-11-18")
    assert project("2026-11-18", "2026-11-18", "2026-11-16")[0]["due_time"] is None


@pytest.mark.django_db
def test_sorted_by_date_then_title_then_id():
    b = chore("daily", title="b chore")
    a2 = chore("daily", title="Alpha")
    a1 = chore("daily", title="Alpha")
    result = project("2026-11-16", "2026-11-17", "2026-11-16")
    assert [(o["date"].isoformat(), o["title"], o["chore_id"]) for o in result] == [
        ("2026-11-16", "Alpha", a2.pk), ("2026-11-16", "Alpha", a1.pk), ("2026-11-16", "b chore", b.pk),
        ("2026-11-17", "Alpha", a2.pk), ("2026-11-17", "Alpha", a1.pk), ("2026-11-17", "b chore", b.pk),
    ]


@pytest.mark.django_db
def test_end_before_start_is_empty():
    chore("daily")
    assert project("2026-11-20", "2026-11-19", "2026-11-16") == []


@pytest.mark.django_db
def test_one_day_range():
    chore("daily")
    assert dates(project("2026-11-18", "2026-11-18", "2026-11-16")) == ["2026-11-18"]


@pytest.mark.django_db
def test_empty_range_with_no_matching_dates():
    chore("weekly", recurrence_day=0)
    assert project("2026-11-17", "2026-11-22", "2026-11-16") == []


@pytest.mark.django_db
def test_year_boundary_weekly():
    chore("weekly", "2026-12-01", recurrence_day=0)
    assert dates(project("2026-12-28", "2027-01-10", "2026-12-28")) == ["2026-12-28", "2027-01-04"]


@pytest.mark.django_db
def test_month_boundary_daily():
    chore("daily", "2026-11-01")
    assert dates(project("2026-11-29", "2026-12-02", "2026-11-25")) == [
        "2026-11-29", "2026-11-30", "2026-12-01", "2026-12-02"]


@pytest.mark.django_db
def test_no_writes():
    a, b = member("A", order=0), member("B", order=1)
    with_state = chore("daily", mode="rotating", eligibility="kids", title="With state")
    RotationState.objects.create(chore=with_state, last_member=a)
    stored(with_state, "2026-11-16", member=a)
    no_state = chore("daily", mode="rotating", eligibility="kids", title="No state")
    chore("weekly", recurrence_day=3, mode="fixed", assigned_to=b)
    before = snapshot()
    with CaptureQueriesContext(connection) as queries:
        result = project("2026-11-16", "2026-12-31", "2026-11-16")
    assert result
    assert snapshot() == before
    assert not [q["sql"] for q in queries if WRITE_SQL.match(q["sql"])]
    assert not RotationState.objects.filter(chore=no_state).exists()


# --- Which chores ----------------------------------------------------------


@pytest.mark.django_db
def test_templates_never_returned():
    Chore.objects.create(title="Tpl", start_date=d("2026-11-16"), recurrence="daily", is_template=True)
    # The 4 built-in templates (data migration) are also excluded.
    assert Chore.objects.filter(is_template=True).count() == 5
    assert project("2026-11-16", "2026-12-31", "2026-11-16") == []


@pytest.mark.django_db
def test_all_modes_and_recurrences_included():
    kid = member("K", order=0)
    combos = [(m, r) for m in ["fixed", "rotating", "unassigned"] for r in ["once", "daily", "weekly", "monthly"]]
    made = []
    for mode, rec in combos:
        made.append(chore(rec, "2026-11-16", mode=mode, title=f"{mode}-{rec}",
                          eligibility="kids" if mode == "rotating" else None,
                          assigned_to=kid if mode == "fixed" else None,
                          recurrence_day={"weekly": 0, "monthly": 16}.get(rec)))
    result = project("2026-11-16", "2026-11-16", "2026-11-16")
    assert {o["chore_id"] for o in result} == {c.pk for c in made}


# --- Projection window -----------------------------------------------------


@pytest.mark.django_db
def test_future_start_date_nothing_before_it():
    chore("daily", "2026-11-20")
    assert dates(project("2026-11-16", "2026-11-22", "2026-11-18")) == ["2026-11-20", "2026-11-21", "2026-11-22"]


@pytest.mark.django_db
def test_past_start_date_starts_from_current_monday():
    chore("daily", "2026-10-01")
    result = project("2026-11-01", "2026-11-22", "2026-11-18")
    assert dates(result) == [f"2026-11-{n}" for n in range(16, 23)]


@pytest.mark.django_db
def test_range_entirely_before_current_monday_is_empty():
    chore("daily", "2026-10-01")
    assert project("2026-11-01", "2026-11-15", "2026-11-18") == []


@pytest.mark.django_db
def test_stored_date_never_returned():
    c = chore("daily")
    stored(c, "2026-11-16", status="done")
    assert "2026-11-16" not in dates(project("2026-11-16", "2026-11-18", "2026-11-16"))


@pytest.mark.django_db
def test_gap_before_latest_stored_not_returned():
    c = chore("daily")
    stored(c, "2026-11-16")
    stored(c, "2026-11-18")
    assert dates(project("2026-11-16", "2026-11-22", "2026-11-16")) == [
        "2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22"]


@pytest.mark.django_db
def test_stored_rows_of_other_chores_do_not_affect_window():
    other = chore("daily", title="Other")
    stored(other, "2026-11-20")
    chore("daily", title="Mine")
    result = project("2026-11-16", "2026-11-17", "2026-11-16")
    assert [o["title"] for o in result] == ["Mine", "Mine"]


@pytest.mark.django_db
def test_chore_added_mid_week_projects_from_current_monday():
    chore("daily", "2026-11-01")
    assert dates(project("2026-11-16", "2026-11-22", "2026-11-19"))[0] == "2026-11-16"


@pytest.mark.django_db
def test_chore_added_mid_week_with_later_start_date():
    chore("daily", "2026-11-19")
    assert dates(project("2026-11-16", "2026-11-22", "2026-11-19"))[0] == "2026-11-19"


# --- once ------------------------------------------------------------------


@pytest.mark.django_db
def test_once_in_range():
    chore("once", "2026-11-18")
    assert dates(project("2026-11-16", "2026-11-22", "2026-11-16")) == ["2026-11-18"]


@pytest.mark.django_db
def test_once_out_of_range():
    chore("once", "2026-11-25")
    assert project("2026-11-16", "2026-11-22", "2026-11-16") == []


@pytest.mark.django_db
def test_once_in_the_past_not_projected():
    chore("once", "2026-11-10")
    assert project("2026-11-01", "2026-11-22", "2026-11-18") == []


@pytest.mark.django_db
def test_once_with_stored_row_never_projected_again():
    c = chore("once", "2026-11-17")
    stored(c, "2026-11-17", status="done")
    c.start_date = d("2026-11-20")  # edited later (#19 case)
    c.save()
    assert project("2026-11-16", "2026-11-30", "2026-11-16") == []


# --- daily / weekly --------------------------------------------------------


@pytest.mark.django_db
def test_daily_every_day():
    chore("daily", "2026-11-16")
    assert dates(project("2026-11-16", "2026-11-22", "2026-11-16")) == [f"2026-11-{n}" for n in range(16, 23)]


@pytest.mark.django_db
def test_weekly_start_weekday_mismatch_goes_to_next_matching_day():
    chore("weekly", "2026-11-18", recurrence_day=0)  # Wed start, Mondays
    assert dates(project("2026-11-16", "2026-12-07", "2026-11-16")) == ["2026-11-23", "2026-11-30", "2026-12-07"]


@pytest.mark.django_db
def test_weekly_start_weekday_matches():
    chore("weekly", "2026-11-18", recurrence_day=2)
    assert dates(project("2026-11-16", "2026-12-02", "2026-11-16")) == ["2026-11-18", "2026-11-25", "2026-12-02"]


@pytest.mark.django_db
def test_weekly_sunday():
    chore("weekly", "2026-11-16", recurrence_day=6)
    assert dates(project("2026-11-16", "2026-11-29", "2026-11-16")) == ["2026-11-22", "2026-11-29"]


@pytest.mark.django_db
def test_weekly_null_day_uses_start_weekday():
    chore("weekly", "2026-11-19", recurrence_day=None)  # Thursday
    assert dates(project("2026-11-16", "2026-11-30", "2026-11-16")) == ["2026-11-19", "2026-11-26"]


# --- monthly ---------------------------------------------------------------


@pytest.mark.django_db
def test_monthly_31_clamps_without_carry_over():
    chore("monthly", "2027-01-01", recurrence_day=31)
    assert dates(project("2027-01-01", "2027-04-30", "2027-01-01")) == [
        "2027-01-31", "2027-02-28", "2027-03-31", "2027-04-30"]


@pytest.mark.django_db
@pytest.mark.parametrize("day, expected", [(29, "2027-02-28"), (30, "2027-02-28"), (31, "2027-02-28")])
def test_monthly_end_of_february_non_leap(day, expected):
    chore("monthly", "2027-01-01", recurrence_day=day)
    assert dates(project("2027-02-01", "2027-02-28", "2027-02-01")) == [expected]


@pytest.mark.django_db
@pytest.mark.parametrize("day", [29, 30, 31])
def test_monthly_end_of_february_leap(day):
    chore("monthly", "2028-01-01", recurrence_day=day)
    assert dates(project("2028-02-01", "2028-02-29", "2028-02-01")) == ["2028-02-29"]


@pytest.mark.django_db
def test_monthly_29_across_leap_and_non_leap():
    chore("monthly", "2027-01-01", recurrence_day=29)
    result = dates(project("2027-01-01", "2028-03-31", "2027-01-01"))
    assert "2027-02-28" in result and "2028-02-29" in result
    assert "2027-03-29" in result and "2028-03-29" in result
    assert len(result) == 15


@pytest.mark.django_db
def test_monthly_31_start_in_february():
    chore("monthly", "2027-02-10", recurrence_day=31)
    assert dates(project("2027-02-01", "2027-03-31", "2027-02-01")) == ["2027-02-28", "2027-03-31"]


@pytest.mark.django_db
def test_monthly_day_before_start_date_skips_to_next_month():
    chore("monthly", "2026-11-20", recurrence_day=15)
    assert dates(project("2026-11-16", "2027-01-31", "2026-11-16")) == ["2026-12-15", "2027-01-15"]


@pytest.mark.django_db
def test_monthly_year_boundary():
    chore("monthly", "2026-11-01", recurrence_day=1)
    assert dates(project("2026-11-16", "2027-02-01", "2026-11-16")) == ["2026-12-01", "2027-01-01", "2027-02-01"]


@pytest.mark.django_db
def test_monthly_null_day_uses_start_day_of_month():
    chore("monthly", "2026-11-17", recurrence_day=None)
    assert dates(project("2026-11-16", "2027-01-31", "2026-11-16")) == ["2026-11-17", "2026-12-17", "2027-01-17"]


@pytest.mark.django_db
def test_monthly_null_day_from_31st_clamps():
    chore("monthly", "2026-10-31", recurrence_day=None)
    assert dates(project("2026-11-02", "2026-12-31", "2026-11-02")) == ["2026-11-30", "2026-12-31"]


# --- Who gets it -----------------------------------------------------------


@pytest.mark.django_db
def test_fixed_with_assignee():
    ann = member("Ann", "adult")
    chore("daily", mode="fixed", assigned_to=ann)
    assert {o["member_id"] for o in project("2026-11-16", "2026-11-18", "2026-11-16")} == {ann.pk}


@pytest.mark.django_db
def test_fixed_without_assignee():
    chore("daily", mode="fixed")
    assert {o["member_id"] for o in project("2026-11-16", "2026-11-18", "2026-11-16")} == {None}


@pytest.mark.django_db
def test_unassigned_has_no_member():
    member("Ann")
    chore("daily", mode="unassigned")
    assert {o["member_id"] for o in project("2026-11-16", "2026-11-18", "2026-11-16")} == {None}


@pytest.mark.django_db
def test_rotation_starts_from_state_not_occurrence_member():
    a, b = member("A", order=0), member("B", order=1)
    c = chore("daily", "2026-11-01", mode="rotating", eligibility="kids")
    RotationState.objects.create(chore=c, last_member=a)
    stored(c, "2026-11-17", member=b)  # hand-changed from A to B (#13)
    result = project("2026-11-20", "2026-11-22", "2026-11-17")
    assert who(result) == [("2026-11-20", b.pk), ("2026-11-21", a.pk), ("2026-11-22", b.pk)]


@pytest.mark.django_db
def test_rotation_counts_turns_before_range_start():
    a, b, c_ = member("A", order=0), member("B", order=1), member("C", order=2)
    chore("daily", "2026-11-16", mode="rotating", eligibility="kids")
    # Mon A, Tue B, Wed C, Thu A, ...
    assert who(project("2026-11-18", "2026-11-19", "2026-11-16")) == [("2026-11-18", c_.pk), ("2026-11-19", a.pk)]


@pytest.mark.django_db
def test_rotation_across_range_boundary_is_consistent():
    member("A", order=0), member("B", order=1), member("C", order=2)
    chore("daily", "2026-11-16", mode="rotating", eligibility="kids")
    whole = who(project("2026-11-16", "2026-11-29", "2026-11-16"))
    first = who(project("2026-11-16", "2026-11-22", "2026-11-16"))
    second = who(project("2026-11-23", "2026-11-29", "2026-11-16"))
    assert first + second == whole


@pytest.mark.django_db
def test_rotation_no_back_fill():
    a, b = member("A", order=0), member("B", order=1)
    c = chore("weekly", "2026-10-01", mode="rotating", eligibility="kids", recurrence_day=0)
    stored(c, "2026-11-02", member=a)
    RotationState.objects.create(chore=c, last_member=a)
    result = project("2026-11-16", "2026-11-30", "2026-11-18")
    assert who(result) == [("2026-11-16", b.pk), ("2026-11-23", a.pk), ("2026-11-30", b.pk)]


@pytest.mark.django_db
def test_rotation_past_start_no_state_first_is_current_monday_to_a():
    a, b = member("A", order=0), member("B", order=1)
    chore("daily", "2026-10-28", mode="rotating", eligibility="kids")  # 3 weeks before today
    result = project("2026-11-01", "2026-11-22", "2026-11-18")
    assert result[0]["date"] == d("2026-11-16")
    assert result[0]["member_id"] == a.pk
    assert result[1]["member_id"] == b.pk


@pytest.mark.django_db
@pytest.mark.parametrize("eligibility", ["kids", None])
def test_rotation_empty_pool(eligibility):
    dad = member("Dad", "adult")
    c = chore("daily", mode="rotating", eligibility=eligibility)
    RotationState.objects.create(chore=c, last_member=dad)
    result = project("2026-11-16", "2026-11-18", "2026-11-16")
    assert len(result) == 3
    assert {o["member_id"] for o in result} == {None}
    assert RotationState.objects.get(chore=c).last_member == dad


@pytest.mark.django_db
@pytest.mark.parametrize("start_with", [None, "A", "B", "C", "Adult"])
@pytest.mark.parametrize("recurrence, day", [("daily", None), ("weekly", 3), ("monthly", 18)])
def test_agrees_with_advance_rotation_for_current_week(start_with, recurrence, day):
    member("A", order=0)
    member("Adult", "adult", order=1)
    member("B", order=1)
    member("C", order=4)
    c = chore(recurrence, "2026-11-01", mode="rotating", eligibility="kids", recurrence_day=day)
    if start_with:
        RotationState.objects.create(chore=c, last_member=Member.objects.get(name=start_with))
    projected = [o["member_id"] for o in project("2026-11-16", "2026-11-22", "2026-11-18")]
    assert projected
    stored_members = [getattr(advance_rotation(c), "pk", None) for _ in projected]
    assert projected == stored_members


@pytest.mark.django_db
def test_simulation_matches_next_member_chain():
    a, b = member("A", order=0), member("B", order=1)
    c = chore("daily", "2026-11-16", mode="rotating", eligibility="kids")
    last, expected = None, []
    for _ in range(7):
        last = next_member(c, last)
        expected.append(last.pk)
    assert [o["member_id"] for o in project("2026-11-16", "2026-11-22", "2026-11-16")] == expected


# --- Timezone --------------------------------------------------------------


def test_service_reads_no_clock_besides_today_default():
    source = inspect.getsource(services)
    assert "datetime.now" not in source
    assert "timezone.now" not in source
    assert source.count("timezone.localdate()") == 1


# --- In-memory rotation agrees with next_member (#30) ----------------------


def next_member_chain(c, days):
    """The old per-date walk: one ``next_member`` call per date, from the
    stored ``RotationState.last_member``."""
    state = RotationState.objects.select_related("last_member").filter(chore=c).first()
    last, chain = (state.last_member if state else None), []
    for _ in days:
        m = next_member(c, last)
        if m is not None:
            last = m
        chain.append(getattr(m, "pk", None))
    return chain


def assert_agrees_over_60_days(*chores):
    # Window starts on Monday 2026-11-16; the range starts later so the
    # turns before it are counted, and covers 60 days.
    result = project("2026-11-20", "2027-01-18", "2026-11-18")
    for c in chores:
        walked = [d("2026-11-16") + datetime.timedelta(days=n) for n in range(64)]
        expected = next_member_chain(c, walked)[4:]
        assert [o["member_id"] for o in result if o["chore_id"] == c.pk] == expected
        assert len(expected) == 60


@pytest.mark.django_db
def test_agrees_with_next_member_when_member_moved_after_state():
    a, b, c_ = member("A", order=0), member("B", order=1), member("C", order=2)
    c = chore("daily", "2026-11-16", mode="rotating", eligibility="kids")
    RotationState.objects.create(chore=c, last_member=b)
    b.order = 5  # moved to the end after the state was written
    b.save()
    assert_agrees_over_60_days(c)
    assert project("2026-11-16", "2026-11-17", "2026-11-16")[0]["member_id"] == a.pk  # after B (now last)


@pytest.mark.django_db
def test_agrees_with_next_member_when_last_member_deleted():
    member("A", order=0), member("C", order=2)
    b = member("B", order=1)
    c = chore("daily", "2026-11-16", mode="rotating", eligibility="kids")
    RotationState.objects.create(chore=c, last_member=b)
    b.delete()
    assert_agrees_over_60_days(c)


@pytest.mark.django_db
def test_first_turn_after_deleted_member_object_uses_its_stored_order():
    a, c_ = member("A", order=0), member("C", order=2)
    b = member("B", order=1)
    c = chore("daily", mode="rotating", eligibility="kids")
    stale = Member.objects.get(pk=b.pk)
    Member.objects.filter(pk=b.pk).delete()
    members = list(Member.objects.order_by("order", "id"))
    pool = services._rotation_pools(members)["kids"]
    index = services._first_turn_index(pool, stale, {m.pk: m.order for m in members})
    assert pool[index] == next_member(c, stale) == c_


@pytest.mark.django_db
def test_agrees_with_next_member_for_empty_pool():
    dad = member("Dad", "adult")
    c = chore("daily", "2026-11-16", mode="rotating", eligibility="kids")
    RotationState.objects.create(chore=c, last_member=dad)
    assert_agrees_over_60_days(c)
    assert RotationState.objects.get(chore=c).last_member == dad


@pytest.mark.django_db
def test_agrees_with_next_member_for_kids_adults_and_everyone():
    member("K1", "kid", order=0)
    mom = member("Mom", "adult", order=1)
    member("K2", "kid", order=1)
    member("Dad", "adult", order=3)
    member("K3", "kid", order=4)
    kids = chore("daily", "2026-11-16", mode="rotating", eligibility="kids", title="Kids")
    adults = chore("daily", "2026-11-16", mode="rotating", eligibility="adults", title="Adults")
    everyone = chore("daily", "2026-11-16", mode="rotating", eligibility="everyone", title="All")
    RotationState.objects.create(chore=kids, last_member=mom)  # not in the kids pool
    RotationState.objects.create(chore=everyone, last_member=mom)
    assert_agrees_over_60_days(kids, adults, everyone)


@pytest.mark.django_db
@pytest.mark.parametrize("recurrence, day", [("weekly", 3), ("monthly", 31), ("once", None)])
def test_agrees_with_next_member_for_other_recurrences(recurrence, day):
    a, b, c_ = member("A", order=0), member("B", order=1), member("C", order=2)
    c = chore(recurrence, "2026-11-19", mode="rotating", eligibility="kids", recurrence_day=day)
    RotationState.objects.create(chore=c, last_member=b)
    whole = project("2026-11-16", "2027-06-30", "2026-11-18")
    expected = next_member_chain(c, whole)
    for start in ["2026-11-16", "2026-12-24", "2027-03-01"]:
        part = [o["member_id"] for o in project(start, "2027-06-30", "2026-11-18")]
        assert part == expected[len(expected) - len(part):]


@pytest.mark.django_db
def test_far_week_matches_walking_every_week_from_now():
    member("A", order=0), member("B", order=1), member("C", order=2)
    chore("daily", "2026-11-16", mode="rotating", eligibility="kids")
    chore("weekly", "2026-11-16", mode="rotating", eligibility="kids", recurrence_day=2, title="W")
    walked = who(project("2026-11-16", "2099-12-27", "2026-11-18"))
    assert who(project("2099-12-21", "2099-12-27", "2026-11-18")) == walked[-8:]


@pytest.mark.django_db
def test_no_writes_for_far_future_range():
    a, b = member("A", order=0), member("B", order=1)
    c = chore("daily", mode="rotating", eligibility="kids")
    RotationState.objects.create(chore=c, last_member=a)
    stored(c, "2026-11-16", member=a)
    before = snapshot()
    with CaptureQueriesContext(connection) as queries:
        assert project("2126-11-01", "2126-11-30", "2026-11-18")
    assert snapshot() == before
    assert not [q["sql"] for q in queries if WRITE_SQL.match(q["sql"])]


# --- Query count (#30) -----------------------------------------------------


def query_count(start, end, today="2026-11-18"):
    # CaptureQueriesContext keeps at most 9000 queries, so a per-date cost
    # would look constant once capped; count every execute as well.
    executed = []
    with CaptureQueriesContext(connection) as queries, \
            connection.execute_wrapper(lambda run, *args: executed.append(1) or run(*args)):
        project(start, end, today)
    assert len(queries) == len(executed) < 20
    return len(queries)


def household():
    a, b = member("A", order=0), member("B", order=1)
    member("C", order=2)
    chore("daily", "2026-11-16", mode="rotating", eligibility="kids", title="Daily")
    chore("weekly", "2026-11-16", mode="rotating", eligibility="kids", recurrence_day=4, title="Weekly")
    chore("monthly", "2026-11-16", mode="fixed", assigned_to=a, recurrence_day=20, title="Monthly")


@pytest.mark.django_db
def test_query_count_does_not_depend_on_range():
    household()
    counts = {
        rng: query_count(*rng)
        for rng in [
            ("2026-11-16", "2026-11-22"),
            ("2027-11-01", "2027-11-30"),
            ("2099-12-21", "2099-12-27"),
            ("2026-11-16", "2126-11-15"),  # 100 years
        ]
    }
    assert len(set(counts.values())) == 1, counts


# Queries added per extra rotating chore: pools are loaded once per call.
MAX_QUERIES_PER_ADDED_CHORE = 0


@pytest.mark.django_db
def test_query_count_per_added_rotating_chore_is_bounded():
    member("A", order=0), member("B", order=1), member("C", order=2)
    chore("daily", "2026-11-16", mode="rotating", eligibility="kids", title="Daily 1")
    one = query_count("2099-12-21", "2099-12-27")
    for n in range(2, 6):
        chore("daily", "2026-11-16", mode="rotating", eligibility="kids", title=f"Daily {n}")
    five = query_count("2099-12-21", "2099-12-27")
    assert five - one <= 4 * MAX_QUERIES_PER_ADDED_CHORE


# --- No overflow near date.max (#30) ---------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("recurrence, day", [("daily", None), ("weekly", 4), ("monthly", 31)])
@pytest.mark.parametrize("mode", ["unassigned", "rotating"])
def test_range_ending_on_date_max(recurrence, day, mode):
    member("A", order=0)
    c = chore(recurrence, "2026-11-16", mode=mode, recurrence_day=day,
              eligibility="kids" if mode == "rotating" else None)
    result = project_occurrences(d("9999-12-25"), datetime.date.max, today=d("9999-12-27"))
    assert isinstance(result, list)
    assert "9999-12-31" in dates(result, c)
    if recurrence == "daily":
        assert dates(result, c) == [f"9999-12-{n}" for n in range(27, 32)]  # from Monday 12-27


@pytest.mark.django_db
def test_single_day_range_on_date_max():
    c = chore("daily", "2026-11-16")
    result = project_occurrences(datetime.date.max, datetime.date.max, today=d("9999-12-27"))
    assert dates(result, c) == ["9999-12-31"]


@pytest.mark.django_db
def test_stored_occurrence_on_date_max_does_not_overflow():
    c = chore("daily", "2026-11-16")
    stored(c, "9999-12-31")
    assert project_occurrences(d("9999-12-25"), datetime.date.max, today=d("9999-12-27")) == []
