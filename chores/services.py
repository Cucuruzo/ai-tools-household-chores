"""Rotation logic (_docs/architecture.md → Rotation Logic).

Invariant: only ``advance_rotation`` writes ``RotationState``. ``next_member``
is read-only, so projection (#8) can simulate turns in memory from the stored
state and get the same sequence that materialization (#9) will store.
"""
import calendar
import datetime

from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import dateformat, timezone

from .models import Chore, ChoreOccurrence, Member, OccurrenceException, RotationState

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

    Per-date exceptions (#31) are applied on read: a cancelled date is not
    returned, an override date gets the exception's member (None for
    Unassigned) and ``is_override=True``. Both still take their rotation
    turn, so the members of every other date are unchanged. An extra (#48,
    a date moved into a later week) is returned with its own title,
    instructions, due time, points and member, and ``is_override=True``; it
    takes no turn, except when it sits on a rule date, which it replaces.
    A rule date whose exception has ``own_values`` (#38, "edited" or
    "kept") gets that exception's title, instructions, due time and points
    the same way and keeps its turn; its member is the override member when
    ``is_override``, else the rotation member. Every dict has
    ``instructions``: the chore's, or the exception's own.

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
    return _project(start, end, _resolve_today(today), with_cancelled=False)


def _exceptions_in(first, end):
    """``{(chore_id, date): (is_cancelled, is_override, member_id, values, moved)}``
    for every exception dated in [first, end]: one query, whatever the range.
    ``values`` is None, or ``(title, instructions, due_time, points)`` for an
    extra (``moved_from`` set, #48) or a date with ``own_values`` (#38).
    ``moved`` is True only for an extra: only extras are shown off the rule."""
    if end < first:
        return {}
    rows = OccurrenceException.objects.filter(date__gte=first, date__lte=end).values_list(
        "chore_id", "date", "is_cancelled", "is_override", "member_id",
        "moved_from", "own_values", "title", "instructions", "due_time", "points",
    )
    return {
        (chore_id, day): (is_cancelled, is_override, member_id,
                          (title, instructions, due_time, points)
                          if moved_from is not None or own_values else None,
                          moved_from is not None)
        for chore_id, day, is_cancelled, is_override, member_id, moved_from, own_values, title,
        instructions, due_time, points in rows
    }


def _occurrence_dict(chore, day, member_id, is_override, extra):
    """One projected occurrence: the chore's values, or an extra's own (#48)."""
    title, instructions, due_time, points = chore.title, chore.instructions, chore.due_time, chore.points
    if extra is not None:
        title = extra[0] or title
        instructions, due_time = extra[1], extra[2]
        points = points if extra[3] is None else extra[3]
    return {
        "chore_id": chore.pk,
        "title": title,
        "instructions": instructions,
        "date": day,
        "due_time": due_time,
        "member_id": member_id,
        "points": points,
        "assignment_mode": chore.assignment_mode,
        "is_override": is_override,
    }


def _project(start, end, today, with_cancelled):
    """``project_occurrences`` for a resolved ``today``. With
    ``with_cancelled`` (materialization only), cancelled dates are returned
    too, with their rotation member, and every dict gets ``is_cancelled``,
    ``is_extra`` and ``takes_turn`` (False only for an extra off the rule)."""
    if end < start:
        return []

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
    monday = _monday_of(today)
    # Exceptions only matter on dates that can be returned; turns before
    # ``start`` are counted, and an exception never changes the count.
    exceptions = _exceptions_in(max(start, monday), end)
    extras = {}
    for (chore_id, day), exception in exceptions.items():
        if exception[4]:  # only ``moved_from`` puts a date on the list off the rule
            extras.setdefault(chore_id, []).append(day)

    results = []

    def add(occurrence, is_cancelled, is_extra, takes_turn):
        if with_cancelled:
            occurrence.update(is_cancelled=bool(is_cancelled), is_extra=is_extra, takes_turn=takes_turn)
        results.append(occurrence)

    for chore in Chore.objects.filter(is_template=False):
        latest_stored = latest.get(chore.pk)
        if latest_stored == datetime.date.max:
            continue  # nothing can come after it
        has_stored = latest_stored is not None
        first = _window_start(chore, latest_stored, today)
        last_day = _last_projectable_date(chore, end)
        from_day = max(first, start)
        rotating = chore.assignment_mode == Chore.AssignmentMode.ROTATING
        # A one-off ignores ``moved_from``: its rows act as plain exceptions.
        once = chore.recurrence == Chore.Recurrence.ONCE
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
            # The turn above is taken whatever the exception says (rule 7).
            is_cancelled, is_override, override_member_id, extra, _ = exceptions.get(
                (chore.pk, day), (False, False, None, None, False)
            )
            if once:  # a one-off ignores ``moved_from`` and ``own_values``
                extra = None
            if is_cancelled and not with_cancelled:
                continue
            if is_override and not is_cancelled:
                member_id = override_member_id
            # An extra, or a date with its own values (#38), on a rule date
            # replaces it, and the date keeps its turn.
            add(_occurrence_dict(chore, day, member_id, bool(is_override and not is_cancelled), extra),
                is_cancelled, extra is not None, True)
        if once:
            continue
        # Extras off the rule: own values and member, no turn (#48). A stored
        # row always wins, so nothing on or before the latest one is shown.
        low = max(start, monday) if latest_stored is None else max(start, monday, latest_stored + ONE_DAY)
        for day in extras.get(chore.pk, ()):
            if not low <= day <= last_day or on_rule(chore, day):
                continue
            is_cancelled, _, member_id, extra, _ = exceptions[(chore.pk, day)]
            if is_cancelled and not with_cancelled:
                continue
            add(_occurrence_dict(chore, day, member_id, not is_cancelled, extra),
                is_cancelled, True, False)
    results.sort(key=lambda o: (o["date"], o["title"], o["chore_id"]))
    return results


def projected_occurrence(chore, day, today):
    """The dict ``project_occurrences(day, day, today)`` gives for ``chore``
    on ``day``, or None when ``day`` is not a projected date of it (stored,
    off the rule, outside the window, a template, or cancelled in advance).
    Constant query count."""
    return next(
        (o for o in project_occurrences(day, day, today=today) if o["chore_id"] == chore.pk),
        None,
    )


def set_projected_member(chore, day, member_id):
    """Override the member of projected date ``day`` (#31): create or update
    the one ``(chore, day)`` exception with ``is_override=True`` and
    ``member_id`` (None for Unassigned). Writes nothing else. A concurrent
    insert that hits the unique constraint is retried once, as an update."""
    for attempt in range(2):
        try:
            with transaction.atomic():
                OccurrenceException.objects.update_or_create(
                    chore=chore, date=day,
                    defaults={"is_override": True, "member_id": member_id},
                )
            return
        except IntegrityError:
            if attempt:
                raise


def cancel_projected_date(chore, day):
    """Delete projected date ``day`` on its own (#22): create or update the
    one ``(chore, day)`` exception with ``is_cancelled=True``. An override on
    the same row stays but no longer matters. Writes nothing else: the date
    still takes its rotation turn (rule 7). A concurrent insert that hits the
    unique constraint is retried once, as an update."""
    for attempt in range(2):
        try:
            with transaction.atomic():
                OccurrenceException.objects.update_or_create(
                    chore=chore, date=day, defaults={"is_cancelled": True},
                )
            return
        except IntegrityError:
            if attempt:
                raise


# --- Materialization (#9) ----------------------------------------------------


_ROTATION_MEMBER = object()


def create_occurrence(chore, day, override_member_id=_ROTATION_MEMBER, is_cancelled=False):
    """Store one occurrence of ``chore`` on ``day`` with the right member.

    The member comes from ``advance_rotation``, which takes (and stores) one
    rotation turn for rotating chores and returns the fixed assignee or None
    otherwise. ``due_time``, ``points``, ``title`` and ``instructions`` are
    copied from the chore now (the title is a snapshot, #39).
    An exception (#31) still takes that turn: ``override_member_id`` (a pk
    or None) replaces the member and sets ``is_override``; ``is_cancelled``
    stores the row cancelled, with the rotation member.
    Raises ``IntegrityError`` if ``(chore, day)`` is already stored; the
    caller's ``atomic`` block then also rolls back the turn.
    """
    member = advance_rotation(chore)
    override = override_member_id is not _ROTATION_MEMBER
    return ChoreOccurrence.objects.create(
        chore=chore,
        date=day,
        member_id=override_member_id if override else getattr(member, "pk", None),
        due_time=chore.due_time,
        points=chore.points,
        title=chore.title,
        instructions=chore.instructions,
        status=ChoreOccurrence.Status.TODO,
        is_override=override,
        is_cancelled=is_cancelled,
    )


def materialize_one_off(chore, today):
    """Store a new one-off chore's occurrence now when it is due by this Sunday (#19).

    Returns the row, or None for a template, a repeating chore or a start
    date in a later week (``materialize_week`` stores it when that week is
    current). Call it inside the ``atomic`` block that saved the chore.
    """
    if chore.is_template or chore.recurrence != Chore.Recurrence.ONCE:
        return None
    if chore.start_date > _monday_of(today) + 6 * ONE_DAY:
        return None
    return create_occurrence(chore, chore.start_date)


def _store_extra(chore, o):
    """Store one extra (#48), or one date with its own values (#38), of the
    week, or nothing; returns the row or None.

    Not cancelled: a turn only when it sits on a rule date, then one todo
    row with its own member, title, instructions, due time and points, and
    the projected ``is_override`` (True for an extra or an "edited" date,
    False for a "kept" date that is not an override, so it stays a follower
    row). Cancelled on a rule date: a cancelled row that takes the turn.
    Cancelled off the rule: nothing.
    """
    if o["is_cancelled"]:
        if o["takes_turn"]:
            return create_occurrence(chore, o["date"], is_cancelled=True)
        return None
    if o["takes_turn"]:
        advance_rotation(chore)
    return ChoreOccurrence.objects.create(
        chore=chore, date=o["date"], member_id=o["member_id"], due_time=o["due_time"],
        points=o["points"], title=o["title"], instructions=o["instructions"],
        status=ChoreOccurrence.Status.TODO, is_override=o["is_override"],
    )


def _materialize_attempt(week_start, today):
    sunday = week_start + 6 * ONE_DAY
    with transaction.atomic():
        projected = project_occurrences(week_start, sunday, today=today)
        # Dates cancelled in advance are hidden from projection but are still
        # stored (cancelled) and still take their turn. Extras (#48) need
        # ``takes_turn``, so they come from the second projection too, and so
        # do dates with their own values (#38), stored by ``_store_extra``.
        if OccurrenceException.objects.filter(
            Q(is_cancelled=True) | Q(moved_from__isnull=False) | ~Q(own_values=""),
            date__gte=week_start, date__lte=sunday,
        ).exists():
            special = [
                o for o in _project(week_start, sunday, today, with_cancelled=True)
                if o["is_cancelled"] or o["is_extra"]
            ]
            keys = {(o["chore_id"], o["date"]) for o in special}
            projected = [o for o in projected if (o["chore_id"], o["date"]) not in keys] + special
            projected.sort(key=lambda o: (o["date"], o["title"], o["chore_id"]))
        chores = Chore.objects.select_related("assigned_to").in_bulk(
            {o["chore_id"] for o in projected}
        )
        # Sorted by date first, so each rotating chore takes its turns in
        # date order. Exceptions are applied, then consumed.
        created = []
        for o in projected:
            chore = chores[o["chore_id"]]
            if o.get("is_extra"):
                row = _store_extra(chore, o)
                if row is not None:
                    created.append(row)
            elif o.get("is_cancelled"):
                created.append(create_occurrence(chore, o["date"], is_cancelled=True))
            elif o.get("is_override"):
                created.append(create_occurrence(chore, o["date"], override_member_id=o["member_id"]))
            else:
                created.append(create_occurrence(chore, o["date"]))
        OccurrenceException.objects.filter(date__lte=sunday).delete()
        return created


def materialize_week(week_start, today=None):
    """Store the current week's projected occurrences and return the new rows.

    Only the current week (the Monday of ``today``'s week) may be
    materialized. Existing rows are never touched: projection already skips
    them. Exceptions of the week (#31) are stored as override or cancelled
    rows, each taking its rotation turn; an extra (#48) is stored by
    ``_store_extra`` with its own values, taking a turn only on a rule date.
    Every exception dated on or before that Sunday is deleted in the same
    transaction. A concurrent run that stores a row first makes an insert hit the
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


# --- Edit scope (#20) -----------------------------------------------------------

# Fields of a chore that decide which dates it falls on, and who gets them.
RULE_FIELDS = ("start_date", "recurrence", "recurrence_day")
ASSIGNMENT_FIELDS = ("assignment_mode", "eligibility", "assigned_to_id")
# Values a projected date can carry on its own exception (#38).
VALUE_FIELDS = ("title", "instructions", "due_time", "points")
# "This and future" from a Planned date keeps at most this many dates before it.
MAX_KEPT_DATES = 366


class FutureEditRefused(Exception):
    """A "this and future" edit from a Planned date that can't be saved (#38).

    ``str(error)`` is the non-field error shown above the chore form.
    """

    SCHEDULE = "The schedule can only change from today. Use Edit from today."


def on_rule(chore, day):
    """Whether ``day`` is one of ``chore``'s dates, as ``_rule_dates`` would
    yield it: on or after ``start_date``, not after ``end_date``, and on the
    weekday / day of month (clamped to the month length). Read-only."""
    if day < chore.start_date:
        return False
    if chore.end_date is not None and day > chore.end_date:
        return False
    recurrence = chore.recurrence
    if recurrence == Chore.Recurrence.DAILY:
        return True
    if recurrence == Chore.Recurrence.WEEKLY:
        return day.weekday() == _weekday(chore)
    if recurrence == Chore.Recurrence.MONTHLY:
        return day == _monthly_date(chore, day.year, day.month)
    return False  # once: not a repeating rule


def update_occurrence(occurrence, due_time, points, title, instructions):
    """"This occurrence only": set one todo row's ``due_time``, ``points``,
    ``title`` and ``instructions`` (#20, #39).

    One conditional update on the row, its chore, ``status="todo"`` and
    ``is_cancelled=False``; it also sets ``is_override``. Nothing is written
    when all four values equal the ones read. Returns False only when the
    update matched 0 rows (the row changed since it was read), True otherwise.
    """
    if (occurrence.due_time == due_time and occurrence.points == points
            and occurrence.title == title and occurrence.instructions == instructions):
        return True
    updated = ChoreOccurrence.objects.filter(
        pk=occurrence.pk,
        chore_id=occurrence.chore_id,
        status=ChoreOccurrence.Status.TODO,
        is_cancelled=False,
    ).update(due_time=due_time, points=points, title=title,
             instructions=instructions, is_override=True)
    return updated == 1


def _keep_planned_dates(chore, day, today, values_changed, assignment_changed):
    """Pin the projected dates of ``chore`` in [next Monday, ``day`` - 1] as
    they show now (#38). Call it before the chore's new values are saved.

    The current week is materialized first. When a value changed, each date
    gets the values it shows and ``own_values="kept"`` (an extra, or a date
    that already has ``own_values``, keeps what it has). When an assignment
    changed, each date that is not yet an override gets ``is_override=True``
    and the member it shows. New exceptions go in one ``bulk_create``.
    """
    monday = _monday_of(today)
    first, last = monday + 7 * ONE_DAY, day - ONE_DAY
    materialize_week(monday, today=today)
    shown = [o for o in _project(first, last, today, with_cancelled=False) if o["chore_id"] == chore.pk]
    existing = {
        e.date: e for e in OccurrenceException.objects.select_for_update().filter(
            chore_id=chore.pk, date__gte=first, date__lte=last)
    }
    new, changed = [], []
    for o in shown:
        exception = existing.get(o["date"])
        if exception is None:
            exception = OccurrenceException(chore_id=chore.pk, date=o["date"])
        dirty = False
        if values_changed and exception.moved_from is None and not exception.own_values:
            exception.own_values = "kept"
            for name in VALUE_FIELDS:
                setattr(exception, name, o[name])
            dirty = True
        if assignment_changed and not exception.is_override:
            exception.is_override, exception.member_id = True, o["member_id"]
            dirty = True
        if exception.pk is None:
            new.append(exception)
        elif dirty:
            changed.append(exception)
    if changed:
        OccurrenceException.objects.bulk_update(
            changed, ["own_values", *VALUE_FIELDS, "is_override", "member_id"])
    if new:
        OccurrenceException.objects.bulk_create(new)


def _clear_kept(chore, day):
    """The chore's "kept" exceptions dated on or after ``day`` give way (#38):
    one that is neither an override nor cancelled is deleted, any other gets
    ``own_values=""``."""
    kept = OccurrenceException.objects.filter(chore_id=chore.pk, own_values="kept", date__gte=day)
    kept.filter(is_override=False, is_cancelled=False).delete()
    kept.update(own_values="")


def _future_edit_attempt(chore, old, day, today):
    with transaction.atomic():
        stored = Chore.objects.select_for_update().filter(pk=chore.pk).first()
        rule_changed = any(getattr(chore, f) != old[f] for f in RULE_FIELDS)
        assignment_changed = any(getattr(chore, f) != old[f] for f in ASSIGNMENT_FIELDS)
        if day > _monday_of(today) + 6 * ONE_DAY:
            # From a Planned date (#38): the rule stays, earlier dates are pinned.
            if rule_changed:
                raise FutureEditRefused(FutureEditRefused.SCHEDULE)
            values_changed = stored is not None and any(
                getattr(chore, f) != getattr(stored, f) for f in VALUE_FIELDS)
            if values_changed or assignment_changed:
                base = chore if stored is None else stored
                first = max(_monday_of(today) + 7 * ONE_DAY, base.start_date)
                last = _last_projectable_date(base, day - ONE_DAY)
                if _count_rule_dates(base, first, last, True) > MAX_KEPT_DATES:
                    raise FutureEditRefused(
                        f"More than {MAX_KEPT_DATES} planned dates come before {_day_text(day, today)}. "
                        "Pick an earlier date, or use Edit from today.")
                _keep_planned_dates(base, day, today, values_changed, assignment_changed)
        elif rule_changed:
            chore.start_date = max(chore.start_date, day)
        _clear_kept(chore, day)
        chore.save()

        followers = list(
            ChoreOccurrence.objects.select_for_update().filter(
                chore=chore,
                date__gte=day,
                status=ChoreOccurrence.Status.TODO,
                is_cancelled=False,
                is_override=False,
            )
        )
        fill = []
        if rule_changed:
            monday = _monday_of(today)
            first, sunday = max(day, monday), monday + 6 * ONE_DAY
            taken = set(
                ChoreOccurrence.objects.filter(
                    chore=chore, date__gte=first, date__lte=sunday
                ).values_list("date", flat=True)
            )
            g = first
            while g <= sunday:
                if g not in taken and on_rule(chore, g):
                    fill.append(g)
                g += ONE_DAY

        # One pass in date order, so rotation turns follow the calendar.
        # Follower and fill dates never coincide (fill skips stored dates).
        steps = [(o.date, o) for o in followers] + [(g, None) for g in fill]
        steps.sort(key=lambda step: step[0])
        for when, row in steps:
            if row is None:
                create_occurrence(chore, when)
            elif rule_changed and not on_rule(chore, when):
                row.delete()  # a todo row is not history; the turn is not given back
            else:
                row.due_time = chore.due_time
                row.points = chore.points
                fields = ["due_time", "points"]
                for name in ("title", "instructions"):
                    if getattr(row, name) != getattr(chore, name):
                        setattr(row, name, getattr(chore, name))
                        fields.append(name)
                if assignment_changed:
                    row.member = advance_rotation(chore)
                    fields.append("member")
                row.save(update_fields=fields)
        return chore


def apply_future_edit(chore, old, day, today):
    """"This and future" from cutoff ``day`` (D), editing ``chore`` in place.

    ``chore`` holds the new values (``form.save(commit=False)``); ``old`` maps
    ``RULE_FIELDS`` and ``ASSIGNMENT_FIELDS`` to their values before the
    edit. ``today`` is the caller's local date; this never reads the clock.

    In one transaction: the chore is saved (``start_date`` clamped to at least
    D when a rule field changed, D up to this Sunday); follower rows (todo,
    not cancelled, not override, dated D or later) off the new rule are
    deleted, the others get
    the new due time and points, the chore's title and instructions when
    they differ (#39), and a new member from ``advance_rotation``
    when an assignment field changed; after a rule change, the current week
    is filled from ``max(D, current Monday)`` with ``create_occurrence``.
    Nothing dated before D, and no done, skipped, cancelled or override row,
    is written. In every such edit the chore's "kept" exceptions dated D or
    later give way (``_clear_kept``, #38).

    D after this Sunday (a Planned date, #38): a rule change raises
    ``FutureEditRefused`` and writes nothing. When a value field (compared
    with the chore as stored) or an assignment field changed, the projected
    dates in [next Monday, D - 1] are pinned as they show now
    (``_keep_planned_dates``, after materializing the current week); more
    than ``MAX_KEPT_DATES`` rule dates there raise ``FutureEditRefused``
    instead, before anything is written. Extras and "edited" exceptions are
    never written, and an override always keeps its member; ``RotationState``
    is written only by that materialization.

    A concurrent materialization that stores a fill date first makes the
    attempt roll back (rotation turns included); it is retried
    once from a fresh read, as ``materialize_week`` does.
    """
    try:
        return _future_edit_attempt(chore, old, day, today)
    except IntegrityError:
        return _future_edit_attempt(chore, old, day, today)


# --- Editing one Planned date (#38) ----------------------------------------------


def edit_projected_date(chore, day, title, instructions, due_time, points, today):
    """"This occurrence only" on projected date ``day`` (D) of a recurring chore (#38).

    ``today`` is the caller's local date; this never reads the clock. In one
    transaction, after locking the chore: returns False when D is not a
    projected date after this Sunday (nothing written). Values equal to what
    D shows write nothing. An extra (#48) only gets the four values. Any
    other date's ``(chore, D)`` exception, created when there is none, gets
    the four values and ``own_values="edited"``, plus ``is_override=True``
    and the member D shows when it was not yet an override (a #31 override
    keeps its member). No ChoreOccurrence or RotationState is written. An
    ``IntegrityError`` (a concurrent insert of the same exception) rolls the
    attempt back and it is retried once from a fresh read.
    """
    posted = {"title": title, "instructions": instructions, "due_time": due_time, "points": points}
    for attempt in range(2):
        try:
            with transaction.atomic():
                locked = Chore.objects.select_for_update().filter(pk=chore.pk, is_template=False).first()
                if (locked is None or locked.recurrence == Chore.Recurrence.ONCE
                        or day <= _monday_of(today) + 6 * ONE_DAY):
                    return False
                projected = projected_occurrence(locked, day, today)
                if projected is None:
                    return False
                if all(projected[name] == posted[name] for name in VALUE_FIELDS):
                    return True
                exception = OccurrenceException.objects.select_for_update().filter(
                    chore_id=locked.pk, date=day).first()
                if exception is None:
                    OccurrenceException.objects.create(
                        chore=locked, date=day, own_values="edited", is_override=True,
                        member_id=projected["member_id"], **posted)
                    return True
                for name in VALUE_FIELDS:
                    setattr(exception, name, posted[name])
                fields = list(VALUE_FIELDS)
                if exception.moved_from is None:  # an extra only changes its four values
                    exception.own_values = "edited"
                    fields.append("own_values")
                    if not exception.is_override:  # a #31 override keeps its member
                        exception.is_override, exception.member_id = True, projected["member_id"]
                        fields += ["is_override", "member_id"]
                exception.save(update_fields=fields)
                return True
        except IntegrityError:
            if attempt:
                raise


# --- Editing a one-off (#35) ------------------------------------------------------


class OneOffEditRefused(Exception):
    """A one-off edit that would rewrite history or hide a deleted chore.

    ``str(error)`` is the message shown on ``start_date``.
    """

    HISTORY = "Undo Done or Skip on the dashboard before changing the date of this chore."
    DELETED = "This chore was deleted, so it can only move to this week or earlier."


def _one_off_edit_attempt(chore, old, today):
    with transaction.atomic():
        Chore.objects.select_for_update().filter(pk=chore.pk).first()
        rows = list(ChoreOccurrence.objects.select_for_update().filter(chore=chore)[:2])
        old_date, new_date = old["start_date"], chore.start_date
        due = new_date <= _monday_of(today) + 6 * ONE_DAY
        row = rows[0] if len(rows) == 1 else None
        frozen = row is not None and (row.status != ChoreOccurrence.Status.TODO or row.is_cancelled)
        if frozen and new_date != old_date:
            raise OneOffEditRefused(OneOffEditRefused.HISTORY)
        if row is not None and not frozen and not due and chore.end_date is not None:
            raise OneOffEditRefused(OneOffEditRefused.DELETED)
        chore.save()
        if len(rows) > 1 or frozen:
            return chore  # crafted data or history: only the chore is saved

        assignment_changed = any(getattr(chore, f) != old[f] for f in ASSIGNMENT_FIELDS)
        exceptions = list(OccurrenceException.objects.select_for_update().filter(chore=chore))
        exception = next((e for e in exceptions if e.date == old_date), None)
        if exception is not None and (assignment_changed or not exception.is_override
                                      or exception.is_cancelled):
            exception = None  # the form's latest choice wins over a card assign
        # Every other exception goes, so none comes back if the date moves back.
        stale = [e.pk for e in exceptions if e is not exception]
        if stale:
            OccurrenceException.objects.filter(pk__in=stale).delete()

        if row is not None and due:
            fields = []
            for name, value in (("date", new_date), ("due_time", chore.due_time),
                                ("points", chore.points), ("title", chore.title),
                                ("instructions", chore.instructions)):
                if getattr(row, name) != value:
                    setattr(row, name, value)
                    fields.append(name)
            if assignment_changed:
                member_id = getattr(advance_rotation(chore), "pk", None)
                if row.member_id != member_id or row.is_override:
                    row.member_id, row.is_override = member_id, False
                    fields += ["member", "is_override"]
            if fields:
                row.save(update_fields=fields)
            if exception is not None:
                exception.delete()
        elif row is not None:
            # A stored row in a future week would be a "To do" card on a week
            # that has not started: delete it and project the date again.
            keep = not assignment_changed and (
                row.is_override or chore.assignment_mode == Chore.AssignmentMode.ROTATING
            )
            row.delete()  # a todo row is not history; the turn is not given back
            if exception is not None:
                exception.delete()
            if keep:
                OccurrenceException.objects.create(
                    chore=chore, date=new_date, is_override=True, member_id=row.member_id
                )
        elif due:
            created = materialize_one_off(chore, today)
            if exception is not None:
                # As materialize_week does: the date took its turn, the
                # exception's member replaces it, and the exception is used up.
                created.member_id, created.is_override = exception.member_id, True
                created.save(update_fields=["member", "is_override"])
                exception.delete()
        elif exception is not None and exception.date != new_date:
            exception.date = new_date
            exception.save(update_fields=["date"])
        return chore


def apply_one_off_edit(chore, old, today):
    """Save an edit of a chore that is ``once`` after the save (#35).

    ``chore`` holds the new values (``form.save(commit=False)``); ``old`` maps
    ``RULE_FIELDS`` and ``ASSIGNMENT_FIELDS`` to their values before the
    edit. ``today`` is the caller's local date; this never reads the clock.

    In one transaction, after locking the chore: a todo row dated by this
    Sunday gets the new date, due time, points, title and instructions (and
    a new member, with
    ``is_override=False``, after an assignment change); a todo row moved into
    a future week is deleted, keeping its member with an override exception
    when it was an override or the chore rotates. Without a row, a date due
    by Sunday is stored with ``materialize_one_off`` (consuming the old
    date's override), and a future date takes that override along. Done,
    skipped and cancelled rows are never written; a date change on them, or
    a deleted chore's row moved into a future week, raises
    ``OneOffEditRefused`` and writes nothing. Equal values write nothing.
    An ``IntegrityError`` (a concurrent store of the same date) rolls the
    attempt back, turns included, and it is retried once from a fresh read.
    """
    try:
        return _one_off_edit_attempt(chore, old, today)
    except IntegrityError:
        return _one_off_edit_attempt(chore, old, today)


# --- Moving one occurrence (#40) ----------------------------------------------------


class MoveRefused(Exception):
    """A "this occurrence only" date that is outside the limits.

    ``str(error)`` is the message shown under the Date field.
    """


# The dashboard's last week ends here (``dashboard.MAX_YEAR``).
LAST_MOVE_DAY = datetime.date(2099, 12, 31)


def _day_text(day, today):
    """``D j M``, plus `` Y`` when ``day`` is not in ``today``'s year."""
    return dateformat.format(day, "D j M" if day.year == today.year else "D j M Y")


def _range_refused(first, last, today):
    return MoveRefused(f"Pick a day from {_day_text(first, today)} to {_day_text(last, today)}.")


def _taken_refused(day, today):
    return MoveRefused(f"This chore already has an occurrence on {_day_text(day, today)}.")


def _later_target_taken(chore, target, today):
    """Whether a later-week ``target`` holds a row, any exception or a
    projected date of ``chore`` (#48). Constant query count."""
    return (
        ChoreOccurrence.objects.filter(chore_id=chore.pk, date=target).exists()
        or OccurrenceException.objects.filter(chore_id=chore.pk, date=target).exists()
        or projected_occurrence(chore, target, today) is not None
    )


def _move_attempt(occurrence, target, due_time, points, title, instructions, today):
    with transaction.atomic():
        materialize_week(_monday_of(today), today=today)
        chore = Chore.objects.select_for_update().filter(pk=occurrence.chore_id).first()
        row = ChoreOccurrence.objects.select_for_update().filter(
            pk=occurrence.pk, chore_id=occurrence.chore_id,
            status=ChoreOccurrence.Status.TODO, is_cancelled=False,
        ).first()
        if chore is None or row is None:
            transaction.set_rollback(True)  # nothing is written, materialization included
            return False
        if chore.end_date is not None and chore.end_date < today:
            raise MoveRefused("This chore has ended, so this occurrence can't move.")
        last = LAST_MOVE_DAY if chore.end_date is None else min(LAST_MOVE_DAY, chore.end_date)
        if not today <= target <= last:
            raise _range_refused(today, last, today)
        if target > _monday_of(today) + 6 * ONE_DAY:
            # A later week (#48): the row is its own tombstone, plus an extra.
            if _later_target_taken(chore, target, today):
                raise _taken_refused(target, today)
            ChoreOccurrence.objects.filter(pk=row.pk).update(is_cancelled=True)
            OccurrenceException.objects.create(
                chore_id=chore.pk, date=target, moved_from=row.date, is_override=True,
                member_id=row.member_id, title=title, instructions=instructions,
                due_time=due_time, points=points,
            )
            return True
        # A cancelled todo row on the target gives way (#49): it is deleted, not
        # un-cancelled, so the moved row keeps its pk. A lost race deletes 0 rows.
        held = ChoreOccurrence.objects.select_for_update().filter(
            chore_id=chore.pk, date=target).first()
        if held is not None:
            deleted, _ = ChoreOccurrence.objects.filter(
                pk=held.pk, is_cancelled=True, status=ChoreOccurrence.Status.TODO,
            ).delete()
            if not deleted:
                raise _taken_refused(target, today)
        source = row.date
        # The update comes first: it frees (chore, source) for the tombstone.
        ChoreOccurrence.objects.filter(pk=row.pk).update(
            date=target, due_time=due_time, points=points, title=title,
            instructions=instructions, is_override=True,
        )
        ChoreOccurrence.objects.create(
            chore_id=chore.pk, date=source, member_id=row.member_id,
            due_time=row.due_time, points=row.points, title=row.title,
            instructions=row.instructions, status=ChoreOccurrence.Status.TODO,
            is_override=False, is_cancelled=True,
        )
        return True


def move_occurrence(occurrence, target, due_time, points, title, instructions, today):
    """"This occurrence only" with a new date (#40): move one todo row to ``target``.

    ``today`` is the caller's local date; this never reads the clock. In one
    transaction: the current week is materialized, the chore and then the row
    (todo, not cancelled) are locked, and ``target`` must be in
    ``[today, min(31 Dec 2099, end_date)]``; otherwise ``MoveRefused`` is
    raised and nothing is written. A ``target`` after this Sunday (#48) must
    hold no row, exception or projected date of the chore; the row then only
    gets ``is_cancelled=True`` and an extra exception is created on
    ``target`` (``moved_from`` the row's date, its member, the posted values,
    ``is_override=True``, no turn). Up to this Sunday, ``target`` must hold
    no row of this chore except a cancelled todo row (#49), which is deleted
    first (0 rows deleted, a lost race, refuses), and one UPDATE sets the row's date, due time, points,
    title, instructions and ``is_override=True`` (same pk, same member, no
    rotation turn), then a cancelled todo tombstone is stored on the old
    date with the row's old member, due time, points, title and
    instructions. Returns False when the locked re-read finds no todo,
    non-cancelled row (a lost race), True after the move. An
    ``IntegrityError`` (a concurrent insert on the target or source date)
    rolls the attempt back and it is retried once from a fresh read.
    """
    try:
        return _move_attempt(occurrence, target, due_time, points, title, instructions, today)
    except IntegrityError:
        return _move_attempt(occurrence, target, due_time, points, title, instructions, today)


# --- Moving a Planned date into a later week (#48) -------------------------------------


def _planned_move_attempt(chore, source, target, today):
    with transaction.atomic():
        chore = Chore.objects.select_for_update().filter(pk=chore.pk, is_template=False).first()
        sunday = _monday_of(today) + 6 * ONE_DAY
        if chore is None or source <= sunday:
            return False
        projected = projected_occurrence(chore, source, today)
        if projected is None:
            return False
        if target == source:
            return True
        last = LAST_MOVE_DAY if chore.end_date is None else min(LAST_MOVE_DAY, chore.end_date)
        if not sunday < target <= last:
            raise _range_refused(sunday + ONE_DAY, last, today)
        if _later_target_taken(chore, target, today):
            raise _taken_refused(target, today)
        exception = OccurrenceException.objects.select_for_update().filter(
            chore_id=chore.pk, date=source).first()
        if exception is not None and exception.moved_from is not None:
            exception.date = target  # an extra moves again; moved_from stays
            exception.save(update_fields=["date"])
            return True
        OccurrenceException.objects.update_or_create(
            chore=chore, date=source, defaults={"is_cancelled": True},
        )
        OccurrenceException.objects.create(
            chore=chore, date=target, moved_from=source, is_override=True,
            member_id=projected["member_id"], title=projected["title"],
            instructions=projected["instructions"], due_time=projected["due_time"],
            points=projected["points"],
        )
        return True


def move_projected_date(chore, source, target, today):
    """Move projected date ``source`` of ``chore`` to ``target`` in a later week (#48).

    ``today`` is the caller's local date; this never reads the clock. In one
    transaction, after locking the chore: returns False when ``source`` is
    not a projected date after this Sunday (nothing written). ``target``
    equal to ``source`` writes nothing. ``target`` must be in
    ``[next Monday, min(end_date, 31 Dec 2099)]`` and hold no row, exception
    or projected date of the chore, else ``MoveRefused`` (nothing written).
    Then ``source`` is cancelled as ``cancel_projected_date`` does, and an
    extra is created on ``target`` with ``source``'s projected member,
    title, instructions, due time and points, ``moved_from=source`` and
    ``is_override=True``. An extra that moves again only gets the new date.
    No ChoreOccurrence or RotationState is written. An ``IntegrityError``
    rolls the attempt back and it is retried once from a fresh read.
    """
    try:
        return _planned_move_attempt(chore, source, target, today)
    except IntegrityError:
        return _planned_move_attempt(chore, source, target, today)
