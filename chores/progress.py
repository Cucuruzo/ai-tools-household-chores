"""Streaks and badges (#16), computed on read from ``ChoreOccurrence`` rows.

Nothing is stored and nothing is written. ``today`` is always passed in and
this module never reads the clock; the viewer's zone is applied with
``timezone.localdate(value)`` on a given value only.
"""
import collections
import datetime

from django.utils import timezone

from .models import ChoreOccurrence
from .views import _shown_occurrences

STREAK_7, HERO, COMPLETED_50 = "streak_7", "hero", "completed_50"

# (type, icon, label) in the fixed display order.
BADGES = (
    (STREAK_7, "🔥", "7 in a Row"),
    (HERO, "🦸", "Chore Hero"),
    (COMPLETED_50, "⭐", "50 Chores Completed"),
)

STREAK_TARGET = 7
HERO_TARGET = 10
COMPLETED_TARGET = 50

_DONE = ChoreOccurrence.Status.DONE
_SKIPPED = ChoreOccurrence.Status.SKIPPED


def _rows():
    """One query: counted rows, each chore's rows oldest ``date`` first."""
    return _shown_occurrences().order_by("chore_id", "date").values_list(
        "chore_id", "date", "status", "completed_by_id", "done_at"
    )


def _walk(rows, today):
    """Yield ``(chore_id, counter, row)`` for each row, oldest first per chore.

    ``done`` adds 1; ``skipped`` and a ``todo`` before ``today`` reset the
    counter; a ``todo`` today or later changes nothing.
    """
    current, counter = None, 0
    for row in rows:
        chore_id, date, status = row[0], row[1], row[2]
        if chore_id != current:
            current, counter = chore_id, 0
        if status == _DONE:
            counter += 1
        elif status == _SKIPPED or date < today:
            counter = 0
        yield chore_id, counter, row


def chore_streaks(today):
    """``{chore_pk: streak}`` for every non-template chore with a counted row.

    A chore missing from the result has streak 0.
    """
    streaks = {}
    for chore_id, counter, _row in _walk(_rows(), today):
        streaks[chore_id] = counter
    return streaks


def member_badges(today):
    """``{member_pk: [badge types]}`` for credited members, in ``BADGES`` order."""
    streak_7, completed = set(), collections.Counter()
    weekly = collections.Counter()
    for _chore_id, counter, row in _walk(_rows(), today):
        _c, _date, status, credited, done_at = row
        if status != _DONE or credited is None:
            continue
        completed[credited] += 1
        if counter >= STREAK_TARGET:
            streak_7.add(credited)
        if done_at is not None:
            local = timezone.localdate(done_at)
            weekly[(credited, local - datetime.timedelta(days=local.weekday()))] += 1
    hero = {member for (member, _monday), n in weekly.items() if n >= HERO_TARGET}
    result = {}
    for member in completed:
        earned = []
        if member in streak_7:
            earned.append(STREAK_7)
        if member in hero:
            earned.append(HERO)
        if completed[member] >= COMPLETED_TARGET:
            earned.append(COMPLETED_50)
        result[member] = earned
    return result
