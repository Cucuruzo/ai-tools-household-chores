"""Rotation logic (_docs/architecture.md → Rotation Logic).

Invariant: only ``advance_rotation`` writes ``RotationState``. ``next_member``
is read-only, so projection (#8) can simulate turns in memory from the stored
state and get the same sequence that materialization (#9) will store.
"""
import calendar
import datetime

from django.db import IntegrityError, transaction
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


def _resolve_today(today):
    """The only clock read in this module: ``today`` or the local date."""
    return timezone.localdate() if today is None else today


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
    if chore.end_date is not None:
        return min(end, chore.end_date)
    return end


def _weekday(chore):
    day = chore.recurrence_day
    return day if day is not None and 0 <= day <= 6 else chore.start_date.weekday()


def _day_of_month(chore):
    day = chore.recurrence_day
    return day if day is not None and 1 <= day <= 31 else chore.start_date.day


def _first_weekly(chore, first, last):
    """First date in [first, last] on the chore's weekday, or None."""
    offset = (_weekday(chore) - first.weekday()) % 7
    return None if (last - first).days < offset else first + datetime.timedelta(days=offset)


def _monthly_date(chore, year, month):
    # Clamp to the month's length; the clamp never carries over.
    return datetime.date(year, month, min(_day_of_month(chore), calendar.monthrange(year, month)[1]))


def _rule_dates(chore, first, last, has_stored):
    """Dates in [first, last] that match the chore's recurrence rule.

    Never steps past ``last``, so ranges ending on ``date.max`` do not
    overflow (#30).
    """
    if first > last:
        return
    recurrence = chore.recurrence
    if recurrence == Chore.Recurrence.ONCE:
        # Any stored occurrence means the one-off already happened (#19).
        if not has_stored and first <= chore.start_date <= last:
            yield chore.start_date
    elif recurrence in (Chore.Recurrence.DAILY, Chore.Recurrence.WEEKLY):
        step = 1 if recurrence == Chore.Recurrence.DAILY else 7
        day = first if step == 1 else _first_weekly(chore, first, last)
        while day is not None:
            yield day
            day = day + datetime.timedelta(days=step) if (last - day).days >= step else None
    elif recurrence == Chore.Recurrence.MONTHLY:
        year, month = first.year, first.month
        while (year, month) <= (last.year, last.month):
            day = _monthly_date(chore, year, month)
            if first <= day <= last:
                yield day
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _count_rule_dates(chore, first, last, has_stored):
    """How many dates ``_rule_dates`` yields, in constant time."""
    if first > last:
        return 0
    recurrence = chore.recurrence
    if recurrence == Chore.Recurrence.ONCE:
        return int(not has_stored and first <= chore.start_date <= last)
    if recurrence == Chore.Recurrence.DAILY:
        return (last - first).days + 1
    if recurrence == Chore.Recurrence.WEEKLY:
        day = _first_weekly(chore, first, last)
        return 0 if day is None else (last - day).days // 7 + 1
    if recurrence == Chore.Recurrence.MONTHLY:
        count = (last.year - first.year) * 12 + last.month - first.month + 1
        count -= _monthly_date(chore, first.year, first.month) < first
        count -= _monthly_date(chore, last.year, last.month) > last
        return count
    return 0


def _rotation_pools(members):
    """``eligible_pool`` for every eligibility, from one (order, id) list of
    all members, so projection needs no query per chore or per date."""
    pools = {Chore.Eligibility.EVERYONE: members}
    for eligibility, role in _ROLE_FOR_ELIGIBILITY.items():
        pools[eligibility] = [m for m in members if m.role == role]
    return pools


def _first_turn_index(pool, last, current_order):
    """Index in ``pool`` of ``next_member(chore, last)``; ``pool`` is non-empty.

    Same rule as ``next_member``: ``last``'s current order when it still
    exists (``_rotation_key``), the order stored on the object otherwise.
    """
    if last is None:
        return 0
    key = (current_order.get(last.pk, last.order), last.pk)
    return next((i for i, m in enumerate(pool) if (m.order, m.pk) > key), 0)


def project_occurrences(start, end, today=None):
    """Occurrences that would happen in [start, end], without writing anything.

    Dates with a stored occurrence, and anything before a chore's window
    start, are never returned. Rotating chores simulate the turns that
    ``next_member`` would give from ``RotationState.last_member``, one turn
    per projected date from the window start, so the result matches what #9
    will store. The simulation runs in memory on pools loaded once per call:
    after the first turn the rotation just cycles through the pool, so turn
    ``k`` is ``pool[(first + k) % len(pool)]`` and the turns before ``start``
    are counted, not walked. The query count does not depend on the range
    (#30).
    """
    if end < start:
        return []
    today = _resolve_today(today)

    latest = dict(
        ChoreOccurrence.objects.values("chore_id")
        .annotate(latest=Max("date"))
        .values_list("chore_id", "latest")
    )
    last_members = {
        state.chore_id: state.last_member
        for state in RotationState.objects.select_related("last_member")
    }
    members = list(Member.objects.order_by("order", "id"))
    pools = _rotation_pools(members)
    current_order = {m.pk: m.order for m in members}

    results = []
    for chore in Chore.objects.filter(is_template=False):
        latest_stored = latest.get(chore.pk)
        if latest_stored == datetime.date.max:
            continue  # nothing can come after it
        has_stored = latest_stored is not None
        first = _window_start(chore, latest_stored, today)
        last_day = _last_projectable_date(chore, end)
        from_day = max(first, start)
        rotating = chore.assignment_mode == Chore.AssignmentMode.ROTATING
        if rotating:
            pool = pools.get(chore.eligibility, [])
            # Turns taken in [first, start) count, so the walk from the
            # window start is not needed (empty pool: no member, keep last).
            turn = 0
            if pool:
                turn = _first_turn_index(pool, last_members.get(chore.pk), current_order)
                if start > first:
                    turn += _count_rule_dates(chore, first, min(last_day, start - ONE_DAY), has_stored)
        for day in _rule_dates(chore, from_day, last_day, has_stored):
            if chore.assignment_mode == Chore.AssignmentMode.FIXED:
                member_id = chore.assigned_to_id
            elif rotating:
                member_id = pool[turn % len(pool)].pk if pool else None
                turn += 1
            else:
                member_id = None
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


# --- Materialization (#9) ----------------------------------------------------


def create_occurrence(chore, day):
    """Store one occurrence of ``chore`` on ``day`` with the right member.

    The member comes from ``advance_rotation``, which takes (and stores) one
    rotation turn for rotating chores and returns the fixed assignee or None
    otherwise. ``due_time`` and ``points`` are copied from the chore now.
    Raises ``IntegrityError`` if ``(chore, day)`` is already stored; the
    caller's ``atomic`` block then also rolls back the turn.
    """
    member = advance_rotation(chore)
    return ChoreOccurrence.objects.create(
        chore=chore,
        date=day,
        member=member,
        due_time=chore.due_time,
        points=chore.points,
        status=ChoreOccurrence.Status.TODO,
        is_override=False,
    )


def _materialize_attempt(week_start, today):
    with transaction.atomic():
        projected = project_occurrences(week_start, week_start + 6 * ONE_DAY, today=today)
        chores = Chore.objects.select_related("assigned_to").in_bulk(
            {o["chore_id"] for o in projected}
        )
        # Projection is sorted by date first, so each rotating chore takes
        # its turns in date order.
        return [create_occurrence(chores[o["chore_id"]], o["date"]) for o in projected]


def materialize_week(week_start, today=None):
    """Store the current week's projected occurrences and return the new rows.

    Only the current week (the Monday of ``today``'s week) may be
    materialized. Existing rows are never touched: projection already skips
    them. A concurrent run that stores a row first makes an insert hit the
    unique ``(chore, date)`` constraint; the attempt is rolled back (rotation
    turns included) and retried once from a fresh projection.
    """
    today = _resolve_today(today)
    if week_start.weekday() != 0:
        raise ValueError(f"week_start {week_start} is not a Monday")
    if week_start != _monday_of(today):
        raise ValueError(f"week_start {week_start} is not the current Monday")
    try:
        return _materialize_attempt(week_start, today)
    except IntegrityError:
        return _materialize_attempt(week_start, today)


# --- Done / skip / undo (#12) ---------------------------------------------------

_TARGET_STATUS = {
    "done": ChoreOccurrence.Status.DONE,
    "skip": ChoreOccurrence.Status.SKIPPED,
    "undo": ChoreOccurrence.Status.TODO,
}


def set_occurrence_status(occurrence, action, credited_member, now):
    """Apply ``action`` ("done", "skip" or "undo") to ``occurrence``.

    One conditional update on the status that was read, so a concurrent
    change or a cancellation since then makes it match nothing. Only
    ``status``, ``done_at`` and ``completed_by`` are written; rotation,
    assignment and points never change. ``credited_member`` (a Member or
    None) and ``now`` (the aware UTC instant the caller read once; this
    module never reads the clock itself) are used only for "done". Returns
    True when the row was written, False when the update matched 0 rows.
    """
    done = action == "done"
    updated = ChoreOccurrence.objects.filter(
        pk=occurrence.pk, status=occurrence.status, is_cancelled=False
    ).update(
        status=_TARGET_STATUS[action],
        done_at=now if done else None,
        completed_by=credited_member if done else None,
    )
    return updated == 1
