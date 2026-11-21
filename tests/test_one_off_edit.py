"""Editing a one-off chore keeps its occurrence in sync (#35).

Examples use today = Wed 2026-11-18 (UTC), current week Mon 16 - Sun 22 Nov
(2026-W47), and the members Mom (adult, order 0), Emma (kid, 1) and Leo
(kid, 2). "Vacuum" is a one-off, fixed Mom, 10 pts, no due time, start Fri
20 Nov, with its stored todo row R (Mom). "Windows" is a one-off, fixed
Emma, 10 pts, start Wed 2 Dec (W49), with no row.
"""
import datetime
import inspect
import re
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.db import IntegrityError, connection
from django.db.models.query import QuerySet
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import services, views
from chores.models import Chore, ChoreOccurrence, Member, OccurrenceException, RotationState
from chores.services import OneOffEditRefused, apply_one_off_edit
from tests.test_contribution import pts, view
from tests.test_dashboard import WED, WRITE_SQL, at, cards, chore, d, main, member, page, sections, t
from tests.test_edit_scope import form_of
from tests.test_exceptions import _calendar_count

pytestmark = pytest.mark.django_db

MON_23 = "2026-11-23T09:00:00"
SUN_NOON_UTC = "2026-11-22T12:00:00"
HISTORY = "Undo Done or Skip on the dashboard before changing the date of this chore."
DELETED = "This chore was deleted, so it can only move to this week or earlier."


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def family():
    return member("Mom", "adult", 0), member("Emma", "kid", 1), member("Leo", "kid", 2)


# --- Helpers ---------------------------------------------------------------------


def nov(day):
    return f"2026-11-{day:02d}"


def stored(c, day, status="todo", who=None, **kwargs):
    return ChoreOccurrence.objects.create(
        chore=c, date=d(day), status=status, member=who, points=c.points, due_time=c.due_time, **kwargs)


def vacuum(family, **kwargs):
    mom = family[0]
    c = chore("Vacuum", start_date=nov(20), mode="fixed", assigned_to=mom, points=10, **kwargs)
    return c, stored(c, nov(20), who=mom)


def mop(family):
    """One-off, rotating kids, last_member Emma, R = 20 Nov Emma (not override)."""
    emma = family[1]
    c = chore("Mop", start_date=nov(20), mode="rotating", eligibility="kids")
    RotationState.objects.create(chore=c, last_member=emma)
    return c, stored(c, nov(20), who=emma)


def windows(family, **kwargs):
    kwargs.setdefault("mode", "fixed")
    if kwargs["mode"] == "fixed":
        kwargs.setdefault("assigned_to", family[1])
    return chore("Windows", start_date="2026-12-02", points=10, **kwargs)


def override(c, day, who):
    return OccurrenceException.objects.create(chore=c, date=d(day), is_override=True, member=who)


def post(client, c, when=WED, tz=None, extra=None, **overrides):
    """POST the edit form as the page would (current values + ``overrides``)."""
    c.refresh_from_db()
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        return client.post(f"/chores/{c.pk}/edit/", {**form_of(c, **overrides), **(extra or {})})


def edit(client, c, **kwargs):
    response = post(client, c, **kwargs)
    assert response.status_code == 302, response.content.decode()[:2000]
    c.refresh_from_db()
    return response


def rows(c):
    return [(o.date, o.member_id, o.is_override, o.status)
            for o in ChoreOccurrence.objects.filter(chore=c).order_by("date")]


def exceptions(c=None):
    qs = OccurrenceException.objects.all() if c is None else OccurrenceException.objects.filter(chore=c)
    return [(e.date, e.member_id, e.is_override, e.is_cancelled) for e in qs.order_by("date")]


def last_member(c):
    state = RotationState.objects.filter(chore=c).first()
    return state and state.last_member_id


def snapshot():
    return (
        list(Chore.objects.order_by("id").values()),
        list(ChoreOccurrence.objects.order_by("id").values()),
        list(RotationState.objects.order_by("id").values()),
        list(OccurrenceException.objects.order_by("id").values()),
    )


def fields(o):
    return ChoreOccurrence.objects.filter(pk=o.pk).values().first()


def on(body, title):
    """Cards titled ``title`` in a dashboard body: [(section, date, assignee, status)]."""
    found = []
    for key, listed in sections(body).items():
        found += [(key, c["date"], c["assignee"], c["status"]) for c in listed if c["title"] == title]
    return found


def week(client, week_param, title, when=WED, **kwargs):
    """Cards titled ``title`` on a past or future week: [(date, assignee, status)]."""
    body = page(client, week_param, when=when, **kwargs)
    return [(c["date"], c["assignee"], c["status"]) for c in cards(main(body)) if c["title"] == title]


def writes_to(queries, table):
    return [q["sql"] for q in queries if WRITE_SQL.match(q["sql"]) and f'"{table}"' in q["sql"]]


def error_page(response, message):
    assert response.status_code == 200
    errors = response.context["form"].errors
    assert errors.get("start_date") == [message]
    assert message.replace("'", "&#x27;") in response.content.decode()


def calendar_count(client, month, day):
    with at(WED):
        response = client.get("/calendar/", {"month": month})
    assert response.status_code == 200
    return _calendar_count(response.content.decode(), day)


# --- Date changes on a stored todo row -------------------------------------------------


def test_move_within_the_week_keeps_the_row(client, family):
    c, r = vacuum(family)
    edit(client, c, start_date=nov(21))
    o = ChoreOccurrence.objects.get()
    assert (o.pk, o.date, o.member_id, o.is_override) == (r.pk, d(nov(21)), family[0].pk, False)
    assert on(page(client), "Vacuum") == [("upcoming", "Sat, Nov 21", "😀 Mom", "To do")]


def test_move_keeps_is_override(client, family):
    c, r = vacuum(family)
    r.member, r.is_override = family[2], True
    r.save()
    edit(client, c, start_date=nov(21))
    assert rows(c) == [(d(nov(21)), family[2].pk, True, "todo")]


def test_move_earlier_in_the_week_is_overdue(client, family):
    c, r = vacuum(family)
    edit(client, c, start_date=nov(16))
    assert ChoreOccurrence.objects.get().pk == r.pk
    assert rows(c) == [(d(nov(16)), family[0].pk, False, "todo")]
    assert [s for s, *_ in on(page(client), "Vacuum")] == ["overdue"]


def test_move_into_a_past_week(client, family):
    c, r = vacuum(family)
    edit(client, c, start_date=nov(13))
    assert ChoreOccurrence.objects.get().pk == r.pk
    assert rows(c) == [(d(nov(13)), family[0].pk, False, "todo")]
    assert [s for s, *_ in on(page(client), "Vacuum")] == ["overdue"]
    assert week(client, "2026-W46", "Vacuum") == [("Fri, Nov 13", "😀 Mom", "Overdue")]


def test_move_into_a_future_week_deletes_and_reprojects(client, family):
    c, _ = vacuum(family)
    edit(client, c, start_date=nov(26))
    assert rows(c) == [] and exceptions() == []
    assert on(page(client), "Vacuum") == []
    assert week(client, "2026-W48", "Vacuum") == [("Thu, Nov 26", "😀 Mom", "Planned")]
    page(client, when=MON_23)
    assert rows(c) == [(d(nov(26)), family[0].pk, False, "todo")]


def test_future_week_override_row_keeps_its_member(client, family):
    mom, _, leo = family
    c, r = vacuum(family)
    r.member, r.is_override = leo, True
    r.save()
    edit(client, c, start_date=nov(26))
    assert rows(c) == []
    assert exceptions(c) == [(d(nov(26)), leo.pk, True, False)]
    assert week(client, "2026-W48", "Vacuum") == [("Thu, Nov 26", "😀 Leo", "Planned")]
    page(client, when=MON_23)
    assert rows(c) == [(d(nov(26)), leo.pk, True, "todo")]
    assert exceptions() == []


def test_future_week_rotating_keeps_its_member_and_turn(client, family):
    _, emma, leo = family
    c, _ = mop(family)
    edit(client, c, start_date=nov(26))
    assert rows(c) == []
    assert exceptions(c) == [(d(nov(26)), emma.pk, True, False)]
    assert last_member(c) == emma.pk
    assert week(client, "2026-W48", "Mop") == [("Thu, Nov 26", "😀 Emma", "Planned")]
    page(client, when=MON_23)
    assert rows(c) == [(d(nov(26)), emma.pk, True, "todo")]
    assert last_member(c) == leo.pk  # the date took its turn (#31)
    assert exceptions() == []


def test_future_week_with_assignment_change_keeps_no_exception(client, family):
    c, r = vacuum(family)
    r.member, r.is_override = family[2], True
    r.save()
    edit(client, c, start_date=nov(26), assigned_to=str(family[1].pk))
    assert rows(c) == [] and exceptions() == []
    assert week(client, "2026-W48", "Vacuum") == [("Thu, Nov 26", "😀 Emma", "Planned")]


def test_moving_back_consumes_the_exception(client, family):
    emma = family[1]
    c, _ = mop(family)
    edit(client, c, start_date=nov(26))
    edit(client, c, start_date=nov(21))
    assert rows(c) == [(d(nov(21)), emma.pk, True, "todo")]
    assert exceptions() == []


def test_double_submit_of_the_same_move(client, family):
    c, r = vacuum(family)
    data = form_of(c, start_date=nov(21))
    for _ in range(2):
        with at(WED):
            assert client.post(f"/chores/{c.pk}/edit/", data).status_code == 302
    assert list(ChoreOccurrence.objects.values_list("pk", "date")) == [(r.pk, d(nov(21)))]


# --- A one-off with no row ------------------------------------------------------------------


def test_no_row_moved_into_this_week_is_stored(client, family):
    c = windows(family, due_time=t("18:00"))
    with mock.patch("chores.services.materialize_one_off", wraps=services.materialize_one_off) as spy:
        edit(client, c, start_date=nov(21), points="20")
    spy.assert_called_once()
    o = ChoreOccurrence.objects.get()
    assert (o.date, o.member_id, o.due_time, o.points, o.is_override, o.status) == (
        d(nov(21)), family[1].pk, t("18:00"), 20, False, "todo")
    assert [(s, day) for s, day, *_ in on(page(client), "Windows")] == [("upcoming", "Sat, Nov 21")]


def test_no_row_moved_into_a_past_week(client, family):
    c = windows(family)
    edit(client, c, start_date=nov(10))
    assert rows(c) == [(d(nov(10)), family[1].pk, False, "todo")]
    assert [s for s, *_ in on(page(client), "Windows")] == ["overdue"]


def test_no_row_rotating_takes_one_turn(client, family):
    _, emma, leo = family
    c = windows(family, mode="rotating", eligibility="kids")
    RotationState.objects.create(chore=c, last_member=emma)
    edit(client, c, start_date=nov(21))
    assert rows(c) == [(d(nov(21)), leo.pk, False, "todo")]
    assert last_member(c) == leo.pk


def test_no_row_with_an_override_consumes_it(client, family):
    leo = family[2]
    c = windows(family)
    override(c, "2026-12-02", leo)
    edit(client, c, start_date=nov(21))
    assert rows(c) == [(d(nov(21)), leo.pk, True, "todo")]
    assert exceptions() == []
    assert not RotationState.objects.exists()


@pytest.mark.parametrize("with_override", [True, False])
def test_future_to_future_moves_the_exception(client, family, with_override):
    c = windows(family)
    if with_override:
        override(c, "2026-12-02", family[2])
    name = "Leo" if with_override else "Emma"
    assert week(client, "2026-W49", "Windows") == [("Wed, Dec 2", f"😀 {name}", "Planned")]
    edit(client, c, start_date="2026-12-09")
    assert rows(c) == []
    assert exceptions(c) == ([(d("2026-12-09"), family[2].pk, True, False)] if with_override else [])
    assert week(client, "2026-W49", "Windows") == []
    assert week(client, "2026-W50", "Windows") == [("Wed, Dec 9", f"😀 {name}", "Planned")]


def test_unstored_one_off_already_due_gets_its_row_on_a_title_edit(client, family):
    c = chore("Late", start_date="2026-11-30", mode="fixed", assigned_to=family[0])
    edit(client, c, when="2026-11-30T09:00:00", title="Later")
    assert rows(c) == [(d("2026-11-30"), family[0].pk, False, "todo")]


def test_calendar_after_moving_windows_into_this_week(client, family):
    c = windows(family)
    assert calendar_count(client, "2026-12", 2) == 1
    edit(client, c, start_date=nov(21))
    assert calendar_count(client, "2026-12", 2) == 0
    assert calendar_count(client, "2026-11", 21) == 1


def test_other_exceptions_of_the_chore_are_deleted(client, family):
    c = windows(family)
    override(c, "2026-12-02", family[2])
    override(c, "2026-12-16", family[0])  # stale: not the old date
    edit(client, c, start_date="2026-12-09")
    assert exceptions() == [(d("2026-12-09"), family[2].pk, True, False)]


# --- Due time, points, title --------------------------------------------------------------


@pytest.mark.parametrize("is_override", [False, True])
def test_points_and_due_time_are_copied(client, family, is_override):
    c, r = vacuum(family)
    ChoreOccurrence.objects.filter(pk=r.pk).update(is_override=is_override)
    edit(client, c, points="20", due_time="18:30")
    o = ChoreOccurrence.objects.get()
    assert (o.pk, o.points, o.due_time, o.is_override, o.member_id) == (
        r.pk, 20, t("18:30"), is_override, family[0].pk)


def test_passed_due_time_today_is_overdue(client, family):
    c = chore("Today", start_date=nov(18), mode="fixed", assigned_to=family[0])
    stored(c, nov(18), who=family[0])
    assert [s for s, *_ in on(page(client), "Today")] == ["today"]
    edit(client, c, due_time="09:00")  # WED is 12:00 UTC
    assert [s for s, *_ in on(page(client), "Today")] == ["overdue"]


def test_title_only_writes_no_row(client, family):
    c, r = vacuum(family)
    before = fields(r)
    with CaptureQueriesContext(connection) as queries:
        edit(client, c, title="Hoover", instructions="Under the bed")
    writes = writes_to(queries, "chores_choreoccurrence")
    assert len(writes) == 1 and writes[0].startswith('UPDATE "chores_choreoccurrence" SET "title" = ')
    assert writes_to(queries, "chores_chore")  # the chore itself is saved
    assert fields(r) == {**before, "title": "Hoover", "instructions": "Under the bed"}
    assert [day for _, day, *_ in on(page(client), "Hoover")] == ["Fri, Nov 20"]


@pytest.mark.parametrize("setup", ["vacuum", "windows-override", "mop", "windows-due"])
def test_unchanged_resave_writes_nothing_but_the_chore(client, family, setup):
    if setup == "vacuum":
        c, _ = vacuum(family)
    elif setup == "mop":
        c, _ = mop(family)
    else:
        c = windows(family)
        override(c, "2026-12-02", family[2])
        if setup == "windows-due":  # a stored row on its due date
            c.start_date = d(nov(21))
            c.save()
            OccurrenceException.objects.all().delete()
            stored(c, nov(21), who=family[1])
    before = snapshot()[1:]
    with CaptureQueriesContext(connection) as queries:
        edit(client, c)
    for table in ("chores_choreoccurrence", "chores_occurrenceexception", "chores_rotationstate"):
        assert writes_to(queries, table) == []
    assert snapshot()[1:] == before


# --- Assignee ------------------------------------------------------------------------------


def test_fixed_to_fixed_emma(client, family):
    c, r = vacuum(family)
    edit(client, c, assigned_to=str(family[1].pk))
    assert rows(c) == [(d(nov(20)), family[1].pk, False, "todo")]
    assert ChoreOccurrence.objects.get().pk == r.pk


def test_fixed_to_unassigned(client, family):
    c, _ = vacuum(family)
    edit(client, c, assignment_mode="unassigned", assigned_to="")
    assert rows(c) == [(d(nov(20)), None, False, "todo")]


def test_fixed_to_rotating_everyone_takes_one_turn(client, family):
    c, _ = vacuum(family)
    RotationState.objects.create(chore=c, last_member=family[1])
    edit(client, c, assignment_mode="rotating", eligibility="everyone", assigned_to="")
    assert rows(c) == [(d(nov(20)), family[2].pk, False, "todo")]
    assert last_member(c) == family[2].pk


def test_rotating_with_an_empty_pool_stays_unassigned(client):
    member("Emma", "kid", 1), member("Leo", "kid", 2)
    c = chore("Vacuum", start_date=nov(20))
    stored(c, nov(20))
    edit(client, c, assignment_mode="rotating", eligibility="adults")
    assert rows(c) == [(d(nov(20)), None, False, "todo")]
    assert not RotationState.objects.exists()


def test_card_override_survives_title_and_points_edits(client, family):
    mom, emma, leo = family
    c, r = vacuum(family)
    r.member, r.is_override = leo, True
    r.save()
    edit(client, c, title="Hoover", points="20")
    assert rows(c) == [(d(nov(20)), leo.pk, True, "todo")]
    edit(client, c, assigned_to=str(emma.pk))
    assert rows(c) == [(d(nov(20)), emma.pk, False, "todo")]


def test_assignment_change_deletes_an_unstored_override(client, family):
    mom = family[0]
    c = windows(family)
    override(c, "2026-12-02", family[2])
    edit(client, c, assigned_to=str(mom.pk))
    assert exceptions() == []
    assert week(client, "2026-W49", "Windows") == [("Wed, Dec 2", "😀 Mom", "Planned")]


# --- Done, skipped, cancelled: history is never rewritten ------------------------------------


@pytest.mark.parametrize("status", ["done", "skipped"])
def test_history_row_is_never_written(client, family, status):
    mom, emma, _ = family
    c = chore("Vacuum", start_date=nov(16), mode="fixed", assigned_to=mom, points=10)
    r = stored(c, nov(16), status=status, who=mom)
    if status == "done":
        r.done_at = datetime.datetime(2026, 11, 16, 9, tzinfo=datetime.timezone.utc)
        r.completed_by = mom
        r.save()
    before = fields(r)
    edit(client, c, points="20", due_time="18:30", title="Hoover", assigned_to=str(emma.pk))
    assert (c.points, c.due_time, c.title, c.assigned_to_id) == (20, t("18:30"), "Hoover", emma.pk)
    assert fields(r) == before
    if status == "done":
        assert pts(view(client, "week"))[f"contribution-member-{mom.pk}"] == 10


@pytest.mark.parametrize("status, cancelled", [("done", False), ("skipped", False), ("todo", True)])
def test_date_change_on_a_history_row_is_a_form_error(client, family, status, cancelled):
    c, r = vacuum(family)
    ChoreOccurrence.objects.filter(pk=r.pk).update(status=status, is_cancelled=cancelled)
    before = snapshot()
    error_page(post(client, c, start_date=nov(21), points="20"), HISTORY)
    assert snapshot() == before


def test_race_row_done_after_clean_is_refused_by_the_service(family):
    c, r = vacuum(family)
    old = {name: getattr(c, name) for name in services.RULE_FIELDS + services.ASSIGNMENT_FIELDS}
    ChoreOccurrence.objects.filter(pk=r.pk).update(status="done")
    before = snapshot()
    c.start_date, c.points = d(nov(21)), 20
    with pytest.raises(OneOffEditRefused) as refused:
        apply_one_off_edit(c, old, d(nov(18)))
    assert str(refused.value) == HISTORY
    assert snapshot() == before


def test_race_in_the_view_rolls_back_and_shows_the_error(client, family):
    c, r = vacuum(family)
    real = views.apply_one_off_edit

    def racing(*args):
        ChoreOccurrence.objects.filter(pk=r.pk).update(status="done")  # after clean
        return real(*args)

    before = snapshot()
    with mock.patch.object(views, "apply_one_off_edit", side_effect=racing) as service:
        response = post(client, c, start_date=nov(21), points="20", title="Hoover")
    service.assert_called_once()
    error_page(response, HISTORY)
    # The simulated concurrent write ran in the same transaction, so it is
    # rolled back with the edit: the chore and the row are as before.
    assert snapshot() == before


@pytest.fixture
def deleted_vacuum(family):
    """Deleted with history (#5: end_date = start - 1), then Undo on the card (#12)."""
    c, r = vacuum(family, end_date=d(nov(19)))
    return c, r


@pytest.mark.parametrize("day", [nov(21), nov(13)])
def test_deleted_one_off_moves_this_week_or_earlier(client, deleted_vacuum, day):
    c, r = deleted_vacuum
    edit(client, c, start_date=day)
    assert list(ChoreOccurrence.objects.values_list("pk", "date")) == [(r.pk, d(day))]
    assert c.end_date == d(nov(19))


def test_deleted_one_off_cannot_move_into_a_future_week(client, deleted_vacuum):
    c, _ = deleted_vacuum
    before = snapshot()
    error_page(post(client, c, start_date=nov(26)), DELETED)
    assert snapshot() == before


def test_two_crafted_rows_only_save_the_chore(client, family):
    c, r = vacuum(family)
    other = stored(c, nov(21), who=family[0])
    before = [fields(r), fields(other)]
    edit(client, c, start_date=nov(26), points="20", assigned_to=str(family[1].pk))
    assert (c.start_date, c.points) == (d(nov(26)), 20)
    assert [fields(r), fields(other)] == before
    assert exceptions() == []


# --- Once <-> repeating (#20) ------------------------------------------------------------------


def test_once_to_repeating_without_rows_leaves_exceptions(client, family):
    c = windows(family)
    override(c, "2026-12-02", family[2])
    edit(client, c, recurrence="daily")
    assert c.recurrence == "daily"
    assert rows(c) == []
    assert exceptions() == [(d("2026-12-02"), family[2].pk, True, False)]


def test_switch_daily_to_once_without_rows_follows_35(client):
    # Moved here from test_edit_scope.py::test_switch_without_rows_is_a_plain_save.
    c = chore("Job", start_date="2026-11-10", recurrence="daily")
    response = post(client, c, recurrence="once")
    assert response.status_code == 302
    c.refresh_from_db()
    assert (c.recurrence, c.start_date) == ("once", d("2026-11-10"))
    assert [(o.date, o.status) for o in ChoreOccurrence.objects.all()] == [(d("2026-11-10"), "todo")]
    assert [s for s, *_ in on(page(client), "Job")] == ["overdue"]


def test_switch_daily_to_once_in_a_future_week(client):
    c = chore("Job", start_date="2026-11-10", recurrence="daily")
    edit(client, c, recurrence="once", start_date="2026-12-02")
    assert not ChoreOccurrence.objects.exists()
    assert week(client, "2026-W49", "Job") == [("Wed, Dec 2", "Unassigned", "Planned")]


# --- Viewer's zone (#26) ---------------------------------------------------------------------


def test_new_service_code_does_not_read_the_clock():
    for fn in (services.apply_one_off_edit, services._one_off_edit_attempt):
        source = inspect.getsource(fn)
        for forbidden in ("timezone", "datetime.now", "date.today", "localdate", "_resolve_today"):
            assert forbidden not in source


def test_service_uses_the_given_today(family):
    c = windows(family)
    old = {name: getattr(c, name) for name in services.RULE_FIELDS + services.ASSIGNMENT_FIELDS}
    c.start_date = d("2026-11-26")
    with mock.patch("django.utils.timezone.now", side_effect=AssertionError("clock read")):
        apply_one_off_edit(c, old, d("2026-11-23"))
    assert rows(c) == [(d("2026-11-26"), family[1].pk, False, "todo")]


@pytest.mark.parametrize("tz, today", [("Pacific/Kiritimati", "2026-11-23"), ("UTC", "2026-11-22")])
def test_view_passes_the_viewers_today(client, family, tz, today):
    c, _ = vacuum(family)
    with mock.patch.object(views, "apply_one_off_edit", wraps=views.apply_one_off_edit) as spy:
        edit(client, c, when=SUN_NOON_UTC, tz=tz, title="Hoover")
    assert spy.call_args.args[2] == d(today)


def test_scoped_switch_passes_scope_today(client):
    c = chore("Job", start_date="2026-11-10", recurrence="daily")
    with mock.patch.object(views, "apply_one_off_edit", wraps=views.apply_one_off_edit) as spy:
        edit(client, c, when=SUN_NOON_UTC, tz="Pacific/Kiritimati", recurrence="once",
             extra={"scope": "future"})
    assert spy.call_args.args[2] == d("2026-11-23")


@pytest.mark.parametrize("tz", ["Pacific/Kiritimati", "UTC"])
def test_stored_row_moved_to_25_nov_by_zone(client, family, tz):
    c, r = vacuum(family)
    edit(client, c, when=SUN_NOON_UTC, tz=tz, start_date="2026-11-25")
    if tz == "Pacific/Kiritimati":  # local Mon 23 Nov: W48 is current
        assert list(ChoreOccurrence.objects.values_list("pk", "date")) == [(r.pk, d("2026-11-25"))]
    else:  # Sun 22 Nov: W48 is a future week
        assert rows(c) == []
        assert week(client, "2026-W48", "Vacuum", when=SUN_NOON_UTC, tz="UTC") == [
            ("Wed, Nov 25", "😀 Mom", "Planned")]


@pytest.mark.parametrize("tz, created", [("Pacific/Kiritimati", True), ("UTC", False)])
def test_unstored_moved_to_26_nov_by_zone(client, family, tz, created):
    c = windows(family)
    edit(client, c, when=SUN_NOON_UTC, tz=tz, start_date="2026-11-26")
    assert rows(c) == ([(d("2026-11-26"), family[1].pk, False, "todo")] if created else [])


# --- Atomicity ---------------------------------------------------------------------------------


def test_chore_is_locked_inside_one_transaction(family):
    c, _ = vacuum(family)
    old = {name: getattr(c, name) for name in services.RULE_FIELDS + services.ASSIGNMENT_FIELDS}
    c.start_date = d(nov(26))
    locked = []
    real = QuerySet.select_for_update

    def spy(qs, *args, **kwargs):
        locked.append((qs.model, connection.in_atomic_block))
        return real(qs, *args, **kwargs)

    with mock.patch.object(QuerySet, "select_for_update", autospec=True, side_effect=spy):
        apply_one_off_edit(c, old, d(nov(18)))
    assert locked[0] == (Chore, True)
    assert all(inside for _, inside in locked)


@pytest.mark.parametrize("target, setup", [
    ("advance_rotation", "rotating"), ("create_occurrence", "no-row"),
])
def test_runtime_error_leaves_everything_as_before(client, family, target, setup):
    if setup == "rotating":
        c, _ = vacuum(family)
        RotationState.objects.create(chore=c, last_member=family[1])
        override(c, "2026-12-30", family[2])  # a stale exception that would be deleted
        changes = {"assignment_mode": "rotating", "eligibility": "everyone", "assigned_to": ""}
    else:
        c = windows(family)
        override(c, "2026-12-02", family[2])
        changes = {"start_date": nov(21)}
    before = snapshot()
    with mock.patch(f"chores.services.{target}", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            post(client, c, title="Renamed", points="20", **changes)
    assert snapshot() == before


def test_integrity_error_is_retried_once(client, family):
    _, emma, leo = family
    c = windows(family, mode="rotating", eligibility="kids")
    RotationState.objects.create(chore=c, last_member=emma)
    real = services.create_occurrence
    calls = []

    def concurrent(chore_, day, *args, **kwargs):
        calls.append(day)
        result = real(chore_, day, *args, **kwargs)  # takes a turn, then "collides"
        if len(calls) == 1:
            raise IntegrityError("unique_occurrence_per_chore_date")
        return result

    with mock.patch("chores.services.create_occurrence", side_effect=concurrent):
        edit(client, c, start_date=nov(21))
    assert len(calls) == 2
    assert rows(c) == [(d(nov(21)), leo.pk, False, "todo")]
    assert last_member(c) == leo.pk  # the rolled-back turn is not counted


def test_second_integrity_error_leaves_everything_as_before(client, family):
    c = windows(family)
    before = snapshot()
    with mock.patch("chores.services.create_occurrence", side_effect=IntegrityError("taken")) as create:
        with pytest.raises(IntegrityError):
            post(client, c, start_date=nov(21), title="Renamed")
    assert create.call_count == 2
    assert snapshot() == before


# --- Hostile input -------------------------------------------------------------------------------


@pytest.mark.parametrize("changes", [
    {"start_date": ""}, {"start_date": "2026-02-30"}, {"start_date": "2026-11-2x"},
    {"start_date": "٢٠٢٦-١١-٢١"}, {"start_date": "2026-11-21\x00"}, {"start_date": "9" * 10_000},
    {"due_time": "25:00"}, {"points": "7"},
], ids=["empty", "feb-30", "2x", "arabic", "nul", "long", "due-25", "points-7"])
def test_hostile_input_is_a_form_error(client, family, changes):
    c, _ = vacuum(family)
    before = snapshot()
    response = post(client, c, **changes)
    assert response.status_code == 200
    assert set(response.context["form"].errors) == set(changes)
    assert snapshot() == before


@pytest.mark.parametrize("day", ["0001-01-01", "9999-12-31"])
@pytest.mark.parametrize("setup", ["vacuum", "mop", "windows"])
def test_extreme_dates(client, family, day, setup):
    if setup == "windows":
        c = windows(family)
    else:
        c, _ = vacuum(family) if setup == "vacuum" else mop(family)
    edit(client, c, start_date=day)
    if day == "0001-01-01":
        assert [o[0] for o in rows(c)] == [datetime.date.min]
    else:
        assert rows(c) == []
        assert [e[0] for e in exceptions(c)] == ([datetime.date.max] if setup == "mop" else [])
    page(client)
    assert calendar_count(client, "2026-11", 18) >= 0


@pytest.mark.parametrize("raw", ["garbage", "other-row", "99999999999999999999"])
def test_scope_and_occurrence_are_ignored(client, family, raw):
    c, r = vacuum(family)
    other_chore = chore("Other", start_date=nov(19), recurrence="daily")
    other = stored(other_chore, nov(19))
    other_before = fields(other)
    occurrence = str(other.pk) if raw == "other-row" else raw
    edit(client, c, start_date=nov(26), extra={"scope": raw, "occurrence": occurrence})
    assert rows(c) == []
    assert ChoreOccurrence.objects.filter(pk=r.pk).count() == 0
    assert fields(other) == other_before


@pytest.mark.parametrize("pk", ["0", "99999999999999999999", "template", "missing"])
def test_bad_chore_pk_is_404(client, family, pk):
    if pk == "template":
        pk = str(chore("Tpl", is_template=True).pk)
    elif pk == "missing":
        pk = "424242"
    with at(WED):
        response = client.post(f"/chores/{pk}/edit/", {"title": "x"})
    assert response.status_code == 404


def test_post_without_csrf_is_403(family):
    c, _ = vacuum(family)
    strict = Client(enforce_csrf_checks=True)
    with at(WED):
        response = strict.post(f"/chores/{c.pk}/edit/", form_of(c, start_date=nov(21)))
    assert response.status_code == 403
    assert rows(c) == [(d(nov(20)), family[0].pk, False, "todo")]


# --- No JavaScript -------------------------------------------------------------------------------


def test_plain_post_redirects_with_filters_and_shows_the_card(client, family):
    c, _ = vacuum(family)
    response = edit(client, c, start_date=nov(21), extra={"week": "2026-W47"})
    assert response["Location"] == f"/chores/{c.pk}/edit/?week=2026-W47"
    with at(WED):
        follow = client.get(response["Location"])
    body = follow.content.decode()
    assert body.count("Chore saved") == 1
    back = re.search(r'href="(/dashboard/\?week=2026-W47)"', body).group(1)
    with at(WED):
        dashboard = client.get(back).content.decode()
    assert on(dashboard, "Vacuum") == [("upcoming", "Sat, Nov 21", "😀 Mom", "To do")]
    with at(WED):
        assert [str(m) for m in get_messages(client.get(back).wsgi_request)] == []
