from .models import Member

SESSION_KEY = "active_member_id"


# Primary keys are signed 64-bit; larger ints make the DB driver raise OverflowError.
MAX_PK = 2**63 - 1


def is_valid_pk(pk):
    return type(pk) is int and 1 <= pk <= MAX_PK


def _parse_pk(raw):
    """Accept only genuine ints or strings of ASCII digits within 1..MAX_PK.

    Bools, floats, None, out-of-range values and anything else are rejected.
    A digit string too long to be a pk (more than ``len(str(MAX_PK))`` digits
    once leading zeros are stripped) is rejected before ``int()`` sees it, as
    Python 3.11+ raises ValueError for strings over 4300 digits.
    """
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        digits = raw.lstrip("0")
        if len(digits) > len(str(MAX_PK)):
            return None
        raw = int(digits) if digits else 0
    return raw if is_valid_pk(raw) else None


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


def clear_active_member(request):
    request.session.pop(SESSION_KEY, None)
