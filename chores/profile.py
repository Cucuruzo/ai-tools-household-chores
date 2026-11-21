from .models import Member

# The header selection (#55): member pks in click order.
SESSION_KEY = "active_member_ids"
# The single active member of #12/#54, migrated into SESSION_KEY on read.
LEGACY_KEY = "active_member_id"
# Larger than any household; bounds the session and the pk__in list.
MAX_SELECTED = 50

_CACHE_ATTR = "_selected_members_cache"


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


def _session_state(session):
    """What the cache is keyed on: the stored list, or None when absent."""
    if LEGACY_KEY in session:
        return object()  # never equal: a legacy key always needs a read
    value = session.get(SESSION_KEY)
    return list(value) if type(value) is list else value


def _same_ids(stored, cleaned):
    """True when ``stored`` already is exactly ``cleaned`` (ints, same order)."""
    return (
        type(stored) is list and len(stored) == len(cleaned)
        and all(type(a) is int and a == b for a, b in zip(stored, cleaned))
    )


def get_selected_members(request):
    """The selected Members in family order, cleaning the session as it reads.

    A legacy ``active_member_id`` is migrated (the list wins when both are
    present). Non-list values, invalid, duplicate and stale entries and
    entries past MAX_SELECTED are dropped; an empty result removes the key.
    One query, cached on the request until the session value changes.
    """
    session = request.session
    cached = getattr(request, _CACHE_ATTR, None)
    if cached is not None and cached[0] == _session_state(session):
        return list(cached[1])
    has_legacy = LEGACY_KEY in session
    legacy = session.pop(LEGACY_KEY, None)
    present = SESSION_KEY in session or has_legacy
    raw = session[SESSION_KEY] if SESSION_KEY in session else [legacy]
    members = []
    if present:
        ids = []
        if type(raw) is list:
            for entry in raw:
                pk = _parse_pk(entry)
                if pk is not None and pk not in ids:
                    ids.append(pk)
                    if len(ids) == MAX_SELECTED:
                        break
        members = list(Member.objects.filter(pk__in=ids)) if ids else []
        found = {m.pk for m in members}
        cleaned = [pk for pk in ids if pk in found]
        if not cleaned:
            session.pop(SESSION_KEY, None)
        elif not _same_ids(session.get(SESSION_KEY), cleaned):
            session[SESSION_KEY] = cleaned
    setattr(request, _CACHE_ATTR, (_session_state(session), tuple(members)))
    return members


def get_active_member(request):
    """The member Done credits (#12): the selected one when exactly one is
    selected, else None (nobody, or several, are selected)."""
    members = get_selected_members(request)
    return members[0] if len(members) == 1 else None


def toggle_member(request, member):
    """Remove ``member`` from the selection, else append them (a no-op when
    MAX_SELECTED are already selected)."""
    get_selected_members(request)  # cleans the stored list first
    ids = list(request.session.get(SESSION_KEY, []))
    if member.pk in ids:
        ids.remove(member.pk)
    elif len(ids) < MAX_SELECTED:
        ids.append(member.pk)
    else:
        return
    if ids:
        request.session[SESSION_KEY] = ids
    else:
        request.session.pop(SESSION_KEY, None)
