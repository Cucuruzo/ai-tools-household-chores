from django.utils import timezone

from .models import Member
from .profile import get_active_member


def profile(request):
    return {
        "members": list(Member.objects.all()),
        "active_member": get_active_member(request),
    }


def clock(request):
    """Current local time in the active (viewer's) zone for the header (#26)."""
    return {"now_local": timezone.localtime()}
