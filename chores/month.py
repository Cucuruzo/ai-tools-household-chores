"""Pure helpers for the monthly calendar (#14) and the ``?date=`` pre-fill.

Nothing here reads the clock or the database: ``today`` is always passed in.
Months are represented by their first day (a ``datetime.date``).
"""
import calendar
import datetime
import re

from . import dashboard as dash

WINDOW_MONTHS = 12
MAX_DOTS = 3

# ASCII digits only; fullmatch also rejects a trailing newline.
_MONTH_RE = re.compile(r"([0-9]{4})-([0-9]{2})")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
WEEKDAY_HEADERS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

OVERDUE = "overdue"
DOT_LABELS = {
    dash.TODO: "To do", OVERDUE: "Overdue", dash.DONE: "Done",
    dash.SKIPPED: "Skipped", dash.PLANNED: "Planned",
}


def first_of_month(day):
    return day.replace(day=1)


def shift_month(first, months):
    index = first.year * 12 + (first.month - 1) + months
    return datetime.date(index // 12, index % 12 + 1, 1)


def last_of_month(first):
    return first.replace(day=calendar.monthrange(first.year, first.month)[1])


def window(today):
    """First days of the earliest and latest month that may be shown."""
    current = first_of_month(today)
    return shift_month(current, -WINDOW_MONTHS), shift_month(current, WINDOW_MONTHS)


def month_param(first):
    return f"{first.year:04d}-{first.month:02d}"


def parse_month(raw, today):
    """First day of the month in ``raw`` (``YYYY-MM``), or None.

    Anything but exactly ``YYYY-MM`` with month 01-12, and any month outside
    the window around ``today``'s month, gives None.
    """
    if not isinstance(raw, str):
        return None
    match = _MONTH_RE.fullmatch(raw)
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12 or not 1 <= year <= 9999:
        return None
    first = datetime.date(year, month, 1)
    low, high = window(today)
    return first if low <= first <= high else None


def parse_date(raw):
    """The date in ``raw`` if it is exactly ``YYYY-MM-DD`` (2000-2099), else None."""
    if not isinstance(raw, str) or _DATE_RE.fullmatch(raw) is None:
        return None
    try:
        day = datetime.date.fromisoformat(raw)
    except ValueError:
        return None
    return day if dash.MIN_YEAR <= day.year <= dash.MAX_YEAR else None


def month_title(first):
    return f"{MONTH_NAMES[first.month - 1]} {first.year}"


def grid_weeks(first):
    """Monday-first weeks (lists of 7 dates) covering the month, no empty rows."""
    return calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)


def month_nav(first, today):
    """Header data for the month starting on ``first``, seen on ``today``."""
    current = first_of_month(today)
    low, high = window(today)
    previous, following = shift_month(first, -1), shift_month(first, 1)
    return {
        "first": first,
        "last": last_of_month(first),
        "title": month_title(first),
        "is_current": first == current,
        "is_past": first < current,
        "previous": month_param(previous) if previous >= low else None,
        "next": month_param(following) if following <= high else None,
    }


def dot_status(status, day, today):
    """Dot class for a stored row (``todo`` before today is overdue)."""
    if status == dash.TODO and day < today:
        return OVERDUE
    return status


def day_cell(entries):
    """Count and dots for one day from ``(status, due_time, title, chore_id)``."""
    ordered = sorted(entries, key=lambda e: (e[1] is None, e[1] or datetime.time.min, e[2], e[3]))
    count = len(ordered)
    dots = [
        {"status": s, "title": title, "label": DOT_LABELS[s]}
        for s, _, title, _ in ordered[:MAX_DOTS]
    ]
    return {"count": count, "dots": dots, "more": max(0, count - MAX_DOTS)}


def day_label(day):
    """"Wed, Nov 18" for accessible link names."""
    return f"{WEEKDAY_HEADERS[day.weekday()]}, {MONTH_NAMES[day.month - 1][:3]} {day.day}"
