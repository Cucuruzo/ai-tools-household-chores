"""Pure helpers for the weekly dashboard (#10).

Nothing here reads the clock or the database: ``today`` and ``now`` are
always passed in, so every rule can be tested with plain values.
"""
import dataclasses
import datetime
import re

ONE_DAY = datetime.timedelta(days=1)
ONE_WEEK = datetime.timedelta(days=7)

MIN_YEAR, MAX_YEAR = 2000, 2099
# ASCII digits only, uppercase W, two-digit week, nothing before or after.
_WEEK_RE = re.compile(r"([0-9]{4})-W([0-9]{2})")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

TODO, DONE, SKIPPED, PLANNED = "todo", "done", "skipped", "planned"
STATUS_LABELS = {TODO: "To do", DONE: "Done", SKIPPED: "Skipped", PLANNED: "Planned"}
OVERDUE_LABEL = "Overdue"

UNASSIGNED = "Unassigned"
NEEDS_ELIGIBLE = "Unassigned · needs eligible member"


# --- Weeks -------------------------------------------------------------------


def monday_of(day):
    return day - datetime.timedelta(days=day.weekday())


def parse_week(raw):
    """Monday of the ``YYYY-Www`` week in ``raw``, or None if it is not valid.

    Years outside 2000-2099, weeks the ISO year does not have, and anything
    that is not exactly the format (including non-ASCII digits, padding and
    NUL bytes) are rejected.
    """
    if not isinstance(raw, str):
        return None
    match = _WEEK_RE.fullmatch(raw)
    if match is None:
        return None
    year, week = int(match.group(1)), int(match.group(2))
    if not MIN_YEAR <= year <= MAX_YEAR:
        return None
    try:
        return datetime.date.fromisocalendar(year, week, 1)
    except ValueError:  # week 00, 54, or 53 in a 52-week year
        return None


def week_param(monday):
    """``YYYY-Www`` for the ISO week starting on ``monday``."""
    year, week, _ = monday.isocalendar()
    return f"{year}-W{week:02d}"


def in_range(monday):
    return MIN_YEAR <= monday.isocalendar()[0] <= MAX_YEAR


def _month_day(day):
    return f"{_MONTHS[day.month - 1]} {day.day}"


def week_range_text(monday):
    """"Nov 16 – 22, 2026", "Nov 30 – Dec 6, 2026" or "Dec 28, 2026 – Jan 3, 2027"."""
    sunday = monday + 6 * ONE_DAY
    if monday.year != sunday.year:
        return f"{_month_day(monday)}, {monday.year} – {_month_day(sunday)}, {sunday.year}"
    if monday.month != sunday.month:
        return f"{_month_day(monday)} – {_month_day(sunday)}, {sunday.year}"
    return f"{_month_day(monday)} – {sunday.day}, {sunday.year}"


def week_nav(monday, today):
    """Header data for the week starting on ``monday``, seen on ``today``."""
    current = monday_of(today)
    previous, following = monday - ONE_WEEK, monday + ONE_WEEK
    is_current = monday == current
    text = week_range_text(monday)
    return {
        "monday": monday,
        "sunday": monday + 6 * ONE_DAY,
        "is_current": is_current,
        "is_past": monday < current,
        "is_future": monday > current,
        "title": f"This Week ({text})" if is_current else text,
        "previous": week_param(previous) if in_range(previous) else None,
        "next": week_param(following) if in_range(following) else None,
    }


# --- Cards and grouping ------------------------------------------------------


@dataclasses.dataclass
class Card:
    """One chore on one date, stored (``pk`` set) or projected (``pk`` None)."""

    pk: object
    chore_id: int
    title: str
    date: datetime.date
    due_time: object
    points: int
    status: str  # todo, done, skipped or planned
    member: object = None
    assignee_note: str = UNASSIGNED
    overdue: bool = False

    @property
    def label(self):
        return OVERDUE_LABEL if self.overdue else STATUS_LABELS[self.status]

    @property
    def struck(self):
        return self.status in (DONE, SKIPPED)


def assignee_note(mode, has_member, is_override, pool_empty):
    """Text shown instead of a member (None when a member is shown).

    A rotating chore with nobody while its eligible pool is empty *now* needs
    an eligible member; once someone is eligible it is plain "Unassigned"
    (it then has to be assigned by hand, #13), as is an occurrence that was
    unassigned by hand (``is_override``).
    """
    if has_member:
        return None
    if mode == "rotating" and pool_empty and not is_override:
        return NEEDS_ELIGIBLE
    return UNASSIGNED


def _time_key(due_time):
    return (due_time is None, due_time or datetime.time.min)


def list_key(card):
    """Date, then due_time (no time last), then title, then id."""
    return (card.date, *_time_key(card.due_time), card.title, card.pk or 0, card.chore_id)


def today_key(card):
    return (*_time_key(card.due_time), card.title, card.pk or 0, card.chore_id)


def is_overdue(card, today, now):
    """A ``todo`` card dated before today, or today with a ``due_time``
    before the current local minute (plain wall-clock comparison)."""
    if card.status != TODO:
        return False
    if card.date < today:
        return True
    if card.date == today and card.due_time is not None:
        return (card.due_time.hour, card.due_time.minute) < (now.hour, now.minute)
    return False


def group_current_week(cards, today, now):
    """Split stored cards into the current week's sections.

    ``cards`` are the current week's stored rows plus ``todo`` rows from any
    earlier date. Returns a dict with overdue, today, upcoming and
    done_earlier lists, each already sorted.
    """
    monday = monday_of(today)
    sunday = monday + 6 * ONE_DAY
    overdue, todays, upcoming, done_earlier = [], [], [], []
    for card in cards:
        card.overdue = is_overdue(card, today, now)
        if card.overdue:
            overdue.append(card)
        elif card.date == today:
            todays.append(card)
        elif today < card.date <= sunday:
            upcoming.append(card)
        elif monday <= card.date < today and card.status in (DONE, SKIPPED):
            done_earlier.append(card)
    return {
        "overdue": sorted(overdue, key=list_key),
        "today": sorted(todays, key=today_key),
        "upcoming": sorted(upcoming, key=list_key),
        "done_earlier": sorted(done_earlier, key=list_key),
    }


def mark_past_overdue(cards):
    """On a past week every ``todo`` card is overdue."""
    for card in cards:
        card.overdue = card.status == TODO
    return cards
