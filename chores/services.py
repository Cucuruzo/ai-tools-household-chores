"""Rotation logic (_docs/architecture.md → Rotation Logic).

Invariant: only ``advance_rotation`` writes ``RotationState``. ``next_member``
is read-only, so projection (#8) can simulate turns in memory from the stored
state and get the same sequence that materialization (#9) will store.
"""
import calendar
import datetime

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import Chore, ChoreOccurrence, Member, RotationState

ONE_DAY = datetime.timedelta(days=1)

_ROLE_FOR_ELIGIBILITY = {
    Chore.Eligibility.KIDS: Member.Role.KID,
    Chore.Eligibility.ADULTS: Member.Role.ADULT,
}


def eligible_pool(chore):
    """Members eligible for a rotating chore, in rotation order (order, id)."""
    members = Member.objects.order_by("order", "id")
    if chore.eligibility == Chore.Eligibility.EVERYONE:
        return list(members)
    role = _ROLE_FOR_ELIGIBILITY.get(chore.eligibility)
    if role is None:  # NULL or unknown eligibility: nobody is eligible
        return []
    return list(members.filter(role=role))


def _rotation_key(member):
    """(order, id) of a member, using the current DB order when it still exists."""
    current = Member.objects.filter(pk=member.pk).values_list("order", flat=True).first()
    return (member.order if current is None else current, member.pk)


def next_member(chore, last_member):
    """Who gets the turn after ``last_member``. Never writes to the DB.

    Fixed chores always go to ``assigned_to``; unassigned chores to nobody.
    For rotating chores: the first pool member after ``last_member``'s
    (order, id), wrapping; the first in the pool when ``last_member`` is None;
    None when the pool is empty.
    """
    if chore.assignment_mode == Chore.AssignmentMode.FIXED:
        return chore.assigned_to
    if chore.assignment_mode != Chore.AssignmentMode.ROTATING:
        return None
    pool = eligible_pool(chore)
    if not pool:
        return None
    if last_member is None:
        return pool[0]
    key = _rotation_key(last_member)
    return next((m for m in pool if (m.order, m.pk) > key), pool[0])


def advance_rotation(chore):
    """Take one turn: store and return the next member.

    Only rotating chores with a non-empty pool touch ``RotationState`` (the
    row is created on the first such turn). Runs in a savepoint inside the
    caller's transaction, so a rollback there also undoes the turn.
    """
    if chore.assignment_mode != Chore.AssignmentMode.ROTATING:
        return next_member(chore, None)
    with transaction.atomic():
        state = (
            RotationState.objects.select_for_update()
            .select_related("last_member")
            .filter(chore=chore)
            .first()
        )
        member = next_member(chore, state.last_member if state else None)
        if member is None:
            return None  # empty pool: keep the stored last_member as is
        if state is None:
            RotationState.objects.create(chore=chore, last_member=member)
        else:
            state.last_member = member
            state.save(update_fields=["last_member"])
        return member


# --- Projection (#8) ---------------------------------------------------------


def _monday_of(day):
    return day - datetime.timedelta(days=day.weekday())


def _window_start(chore, latest_stored, today):
    """First date a chore may be projected on: the latest of its start_date,
    the day after its latest stored occurrence, and the current Monday
    (past weeks are never back-filled)."""
    candidates = [chore.start_date, _monday_of(today)]
    if latest_stored is not None:
        candidates.append(latest_stored + ONE_DAY)
    return max(candidates)


def _last_projectable_date(chore, end):
    """Last date a chore may be projected on. The single place for limits on
    a chore's end (#5 adds ``end_date`` here)."""
    return end


def _weekday(chore):
    day = chore.recurrence_day
    return day if day is not None and 0 <= day <= 6 else chore.start_date.weekday()


def _day_of_month(chore):
    day = chore.recurrence_day
    return day if day is not None and 1 <= day <= 31 else chore.start_date.day


def _rule_dates(chore, first, last, has_stored):
    """Dates in [first, last] that match the chore's recurrence rule."""
    if first > last:
        return
    recurrence = chore.recurrence
    if recurrence == Chore.Recurrence.ONCE:
        # Any stored occurrence means the one-off already happened (#19).
        if not has_stored and first <= chore.start_date <= last:
            yield chore.start_date
    elif recurrence == Chore.Recurrence.DAILY:
        day = first
        while day <= last:
            yield day
            day += ONE_DAY
    elif recurrence == Chore.Recurrence.WEEKLY:
        day = first + datetime.timedelta(days=(_weekday(chore) - first.weekday()) % 7)
        while day <= last:
            yield day
            day += datetime.timedelta(days=7)
    elif recurrence == Chore.Recurrence.MONTHLY:
        wanted = _day_of_month(chore)
        year, month = first.year, first.month
        while (year, month) <= (last.year, last.month):
            # Clamp to the month's length; the clamp never carries over.
            day = datetime.date(year, month, min(wanted, calendar.monthrange(year, month)[1]))
            if first <= day <= last:
                yield day
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def project_occurrences(start, end, today=None):
    """Occurrences that would happen in [start, end], without writing anything.

    Dates with a stored occurrence, and anything before a chore's window
    start, are never returned. Rotating chores simulate turns in memory with
    ``next_member`` from ``RotationState.last_member``, one turn per projected
    date from the window start, so the result matches what #9 will store.
    """
    if end < start:
        return []
    if today is None:
        today = timezone.localdate()

    latest = dict(
        ChoreOccurrence.objects.values("chore_id")
        .annotate(latest=Max("date"))
        .values_list("chore_id", "latest")
    )
    last_members = {
        state.chore_id: state.last_member
        for state in RotationState.objects.select_related("last_member")
    }

    results = []
    for chore in Chore.objects.filter(is_template=False):
        latest_stored = latest.get(chore.pk)
        first = _window_start(chore, latest_stored, today)
        # Rotating chores walk from the window start so earlier turns count.
        walk_from = first if chore.assignment_mode == Chore.AssignmentMode.ROTATING else max(first, start)
        last_day = _last_projectable_date(chore, end)
        last = last_members.get(chore.pk)
        for day in _rule_dates(chore, walk_from, last_day, latest_stored is not None):
            if chore.assignment_mode == Chore.AssignmentMode.FIXED:
                member_id = chore.assigned_to_id
            elif chore.assignment_mode == Chore.AssignmentMode.ROTATING:
                member = next_member(chore, last)
                if member is not None:  # empty pool: like advance_rotation, keep last
                    last = member
                member_id = member.pk if member else None
            else:
                member_id = None
            if day >= start:
                results.append({
                    "chore_id": chore.pk,
                    "title": chore.title,
                    "date": day,
                    "due_time": chore.due_time,
                    "member_id": member_id,
                    "points": chore.points,
                    "assignment_mode": chore.assignment_mode,
                })
    results.sort(key=lambda o: (o["date"], o["title"], o["chore_id"]))
    return results
