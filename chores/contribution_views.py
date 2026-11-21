"""Points and contribution view (#15): who was credited, this week or month."""
import datetime

from django.db.models import Count, Sum
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.views.decorators.http import require_GET

from . import dashboard as dash
from .models import Member
from .views import _is_htmx, _shown_occurrences

WEEK_URL = "/contribution/"
MONTH_URL = "/contribution/?period=month"


def parse_period(raw):
    """"month" for exactly ``month``; every other value is the week."""
    return "month" if raw == "month" else "week"


def period_days(period, today):
    """First and last day of the period containing ``today``."""
    if period == "month":
        first = today.replace(day=1)
        following = (first + datetime.timedelta(days=32)).replace(day=1)
        return first, following - datetime.timedelta(days=1)
    monday = dash.monday_of(today)
    return monday, monday + datetime.timedelta(days=6)


def period_limits(first, last):
    """Aware local midnights of ``first`` and of the day after ``last``."""
    def midnight(day):
        return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))

    return midnight(first), midnight(last + datetime.timedelta(days=1))


def percentages(points):
    """Whole-number shares that sum to 100 (largest remainder), or all 0.

    Equal remainders go to the line that comes first.
    """
    total = sum(points)
    if total <= 0:
        return [0] * len(points)
    floors = [p * 100 // total for p in points]
    remainders = [p * 100 % total for p in points]
    order = sorted(range(len(points)), key=lambda i: (-remainders[i], i))
    for i in order[: 100 - sum(floors)]:
        floors[i] += 1
    return floors


def _lines(members, sums):
    """Member lines in member order, then the Not credited line if it has rows."""
    lines = [
        {"member": m, "pk": m.pk, **sums.get(m.pk, {"points": 0, "done": 0})}
        for m in members
    ]
    if None in sums:
        lines.append({"member": None, "pk": None, **sums[None]})
    total = sum(line["points"] for line in lines)
    for line, share in zip(lines, percentages([line["points"] for line in lines])):
        line["share"] = share
    return lines, total


@require_GET
def contribution(request):
    # One clock read per request, in the viewer's zone (#26).
    now = timezone.localtime()
    today = timezone.localdate(now)
    period = parse_period(request.GET.get("period"))
    first, last = period_days(period, today)
    start, end = period_limits(first, last)
    rows = (
        _shown_occurrences()
        .filter(status="done", done_at__gte=start, done_at__lt=end)
        .order_by()
        .values("completed_by")
        .annotate(points=Sum("points"), done=Count("pk"))
    )
    sums = {r["completed_by"]: {"points": r["points"] or 0, "done": r["done"]} for r in rows}
    members = list(Member.objects.all())
    lines, total = _lines(members, sums)
    own_url = MONTH_URL if period == "month" else WEEK_URL
    context = {
        "period": period,
        "heading_week": dash.week_range_text(first) if period == "week" else "",
        "heading_month": first,
        "lines": lines,
        "has_members": bool(members),
        "total_points": total,
        "total_done": sum(line["done"] for line in lines),
        "bar_max": total or 1,
        "week_url": WEEK_URL,
        "month_url": MONTH_URL,
        "switcher_next": own_url,
    }
    partial = _is_htmx(request) and request.headers.get("HX-History-Restore-Request") != "true"
    if partial:
        response = render(request, "chores/_contribution.html", context)
        response.content += render_to_string(
            "chores/_profile_switcher.html",
            {"switcher_oob": True, "switcher_next": own_url},
            request,
        ).encode()
    else:
        response = render(request, "chores/contribution.html", context)
    patch_vary_headers(response, ["HX-Request", "HX-History-Restore-Request"])
    return response
