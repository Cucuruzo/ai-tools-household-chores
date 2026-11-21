"""History of completed and skipped chores (#17). Read-only, full page, no HTMX."""
import itertools
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from . import dashboard as dash
from .models import ChoreOccurrence
from .views import _dashboard_filters, _shown_occurrences

PAGE_SIZE = 50
MAX_PAGE_DIGITS = 6


def _page_number(raw):
    """Page number from the raw ``page`` value; the last page is ``None``.

    Checked before any ``int()``: only ASCII digits count, and a long run of
    digits means "the last page" without converting it.
    """
    if not (isinstance(raw, str) and raw.isascii() and raw.isdigit()):
        return 1
    if len(raw) > MAX_PAGE_DIGITS:
        return None
    return int(raw) or 1


def _history_url(member, page):
    """Canonical URL from normalized values only: ``member`` then ``page``."""
    params = []
    if member is not None:
        params.append(("member", member))
    if page > 1:
        params.append(("page", page))
    return reverse("history") + (f"?{urlencode(params)}" if params else "")


@require_GET
def history(request):
    _, member, _ = _dashboard_filters(request.GET)
    if member == dash.ALL_FILTER:  # history has no header selection to override
        member = None
    all_rows = _shown_occurrences().filter(
        status__in=[ChoreOccurrence.Status.DONE, ChoreOccurrence.Status.SKIPPED]
    )
    rows = all_rows
    if member == dash.UNASSIGNED_FILTER:
        rows = rows.filter(member__isnull=True)
    elif member is not None:
        rows = rows.filter(Q(member_id=member) | Q(completed_by_id=member))
    rows = rows.select_related("chore", "member", "completed_by").order_by(
        "-date", "status", "title", "id"
    )
    paginator = Paginator(rows, PAGE_SIZE)
    number = _page_number(request.GET.get("page"))
    page = paginator.get_page(paginator.num_pages if number is None else number)
    groups = [
        (day, list(items))
        for day, items in itertools.groupby(page.object_list, key=lambda o: o.date)
    ]
    here = _history_url(member, page.number)
    return render(request, "chores/history.html", {
        "groups": groups,
        "page": page,
        "paginator": paginator,
        "filter_member": "" if member is None else member,
        "never_any": paginator.count == 0 and not all_rows.exists(),
        "newer_url": _history_url(member, page.number - 1) if page.has_previous() else None,
        "older_url": _history_url(member, page.number + 1) if page.has_next() else None,
        "switcher_next": here,
    })
