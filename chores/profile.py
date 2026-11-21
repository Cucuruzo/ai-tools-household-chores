from .models import Member

SESSION_KEY = "active_member_id"


def get_active_member(request):
    """Return the active Member from the session, or None.

    A stale (deleted member) or invalid (non-integer) id is removed from the
    session so later requests don't keep looking it up.
    """
    raw = request.session.get(SESSION_KEY)
    if raw is None:
        return None
    try:
        pk = int(raw)
    except (TypeError, ValueError):
        pk = None
    member = Member.objects.filter(pk=pk).first() if pk is not None else None
    if member is None:
        del request.session[SESSION_KEY]
    return member


def set_active_member(request, member):
    request.session[SESSION_KEY] = member.pk
