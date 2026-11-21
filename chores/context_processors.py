from django.utils import timezone

from .models import Member
from .profile import get_selected_members


def profile(request):
    selected = get_selected_members(request)
    return {
        "members": list(Member.objects.all()),
        "selected_members": selected,
        "selected_ids": {m.pk for m in selected},
        "switcher_next": request.path,
    }


def clock(request):
    """Current local time in the active (viewer's) zone for the header (#26)."""
    return {"now_local": timezone.localtime()}
