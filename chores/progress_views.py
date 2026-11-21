from django.db.models import Q
from django.db.models.functions import Lower
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from . import progress as stats
from .models import Chore, Member

_UNITS = {"daily": "day", "weekly": "week", "monthly": "month"}
_LABELS = {kind: (icon, label) for kind, icon, label in stats.BADGES}


@require_GET
def progress(request):
    now = timezone.localtime()  # one clock read, viewer's zone (#26)
    today = timezone.localdate(now)
    streaks = stats.chore_streaks(today)
    badges = stats.member_badges(today)

    chores = (
        Chore.objects.filter(pk__in=[pk for pk, n in streaks.items() if n >= 1], is_template=False)
        .exclude(recurrence=Chore.Recurrence.ONCE)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .order_by(Lower("title"), "pk")
    )
    streak_rows = []
    for chore in chores:
        count = streaks[chore.pk]
        unit = _UNITS[chore.recurrence] + ("" if count == 1 else "s")
        streak_rows.append({"chore": chore, "count": count, "unit": unit})

    badge_rows = [
        {
            "member": member,
            "badges": [(kind, *_LABELS[kind]) for kind in badges.get(member.pk, [])],
        }
        for member in Member.objects.all()
    ]
    return render(request, "chores/progress.html", {"streak_rows": streak_rows, "badge_rows": badge_rows})
