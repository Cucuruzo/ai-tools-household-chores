from .models import Member

SESSION_KEY = "active_member_id"


def _parse_pk(raw):
    """Accept only genuine ints or strings of ASCII digits; reject bools, floats, None etc."""
    if type(raw) is int:
        return raw
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        return int(raw)
    return None


def get_active_member(request):
    """Return the active Member from the session, or None.

    A stale (deleted member) or invalid (non-integer) id is removed from the
    session so later requests don't keep looking it up.
    """
    if SESSION_KEY not in request.session:
        return None
    pk = _parse_pk(request.session[SESSION_KEY])
    member = Member.objects.filter(pk=pk).first() if pk is not None else None
    if member is None:
        del request.session[SESSION_KEY]
    return member


def set_active_member(request, member):
    request.session[SESSION_KEY] = member.pk
