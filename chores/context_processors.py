from .models import Member
from .profile import get_active_member


def profile(request):
    return {
        "members": list(Member.objects.all()),
        "active_member": get_active_member(request),
    }
