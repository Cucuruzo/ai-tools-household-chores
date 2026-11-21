"""Undo the latest chore delete (#24): snapshot, session slot and restore.

The prior state lives on the server, in one session slot per browser
(``request.session[SLOT]``); the page only carries the slot's random token.
Pure ORM, no clock read: the views pass ``now``.

A snapshot is the concrete fields (``attname``, ``id`` included) of the
chore, every ``ChoreOccurrence``, every ``OccurrenceException`` and its
``RotationState`` (or None), as JSON values: dates, times and datetimes are
ISO strings, FKs ``<field>_id`` integers.
"""
import datetime
import re
import secrets

from django.db import transaction
from django.db.models import Q

from .models import Chore, ChoreOccurrence, OccurrenceException, RotationState

SLOT = "chore_delete_undo"
WINDOW = datetime.timedelta(seconds=600)
TOKEN = re.compile(r"[0-9a-f]{32}")
# Keys each kind of slot carries besides v, token, created, kind and chore_id.
KINDS = {
    "cancel": ("occurrence_id",),
    "planned": ("date", "exception"),
    "trim": ("chore", "end_date_after", "rows", "exceptions"),
    "chore": ("chore", "occurrences", "exceptions", "rotation"),
}


class Refused(Exception):
    """The exact prior state can no longer be put back."""


# --- Snapshot ---------------------------------------------------------------------


def _json(value):
    if isinstance(value, (datetime.date, datetime.time)):  # datetime is a date
        return value.isoformat()
    return value


def fields(obj):
    """Every concrete field of ``obj`` as a JSON value, keyed by attname."""
    return {f.attname: _json(getattr(obj, f.attname)) for f in obj._meta.concrete_fields}


def snapshot(chore):
    """The whole stored state of ``chore``, read before a delete writes."""
    rotation = RotationState.objects.filter(chore_id=chore.pk).first()
    return {
        "chore": fields(chore),
        "occurrences": [fields(o) for o in ChoreOccurrence.objects.filter(chore_id=chore.pk).order_by("pk")],
        "exceptions": [fields(e) for e in OccurrenceException.objects.filter(chore_id=chore.pk).order_by("pk")],
        "rotation": fields(rotation) if rotation else None,
    }


def exception_on(chore, day):
    """The ``(chore, day)`` exception's fields, or None when there is none."""
    exception = OccurrenceException.objects.filter(chore_id=chore.pk, date=day).first()
    return fields(exception) if exception else None


def after_delete(snap):
    """Kind and data of a recurring "this and future" or one-off delete, read
    after its write, or None when the delete wrote nothing."""
    chore_id = snap["chore"]["id"]
    current = Chore.objects.filter(pk=chore_id).first()
    if current is None:
        return "chore", snap
    row_ids = set(ChoreOccurrence.objects.filter(chore_id=chore_id).values_list("pk", flat=True))
    exception_ids = set(OccurrenceException.objects.filter(chore_id=chore_id).values_list("pk", flat=True))
    data = {
        "chore": snap["chore"],
        "end_date_after": _json(current.end_date),
        "rows": [o for o in snap["occurrences"] if o["id"] not in row_ids],
        "exceptions": [e for e in snap["exceptions"] if e["id"] not in exception_ids],
    }
    if not data["rows"] and not data["exceptions"] and data["end_date_after"] == snap["chore"]["end_date"]:
        return None
    return "trim", data


def new_slot(kind, chore_id, now, data):
    """A fresh slot; ``now`` is stored as ISO-8601 UTC."""
    slot = {
        "v": 1,
        "token": secrets.token_hex(16),
        "created": now.astimezone(datetime.timezone.utc).isoformat(),
        "kind": kind,
        "chore_id": chore_id,
    }
    slot.update(data)
    return slot


# --- Reading the slot -------------------------------------------------------------


def created_at(slot):
    """The slot's aware ``created`` datetime, or None when the slot is malformed."""
    if not isinstance(slot, dict) or slot.get("v") != 1 or not isinstance(slot.get("token"), str):
        return None
    keys = KINDS.get(slot.get("kind"))
    if keys is None or not isinstance(slot.get("chore_id"), int) or any(k not in slot for k in keys):
        return None
    try:
        created = datetime.datetime.fromisoformat(slot.get("created"))
    except (TypeError, ValueError):
        return None
    return created if created.tzinfo is not None else None


def is_live(slot, now):
    created = created_at(slot)
    return created is not None and now - created <= WINDOW


def live_token(slot, now):
    """The token to show next to "Chore deleted", or None."""
    return slot["token"] if is_live(slot, now) else None


# --- Restore ----------------------------------------------------------------------


def _instance(model, data):
    """``model`` built from snapshot ``data``. An FK (other than to the chore
    itself) whose row is gone is NULL, as ``SET_NULL`` would have made it."""
    values = {}
    for f in model._meta.concrete_fields:
        value = data[f.attname]
        if value is not None:
            value = f.to_python(value)
            if (f.is_relation and f.attname != "chore_id"
                    and not f.related_model._default_manager.filter(pk=value).exists()):
                value = None
        values[f.attname] = value
    return model(**values)


def _stored(model, chore_id, items):
    """Whether any id or ``(chore, date)`` pair of ``items`` is stored."""
    if not items:
        return False
    ids = [i["id"] for i in items]
    dates = [i["date"] for i in items]
    return model.objects.filter(Q(pk__in=ids) | Q(chore_id=chore_id, date__in=dates)).exists()


def _restore_cancel(slot):
    row = ChoreOccurrence.objects.select_for_update().filter(
        pk=slot["occurrence_id"], chore_id=slot["chore_id"],
        is_cancelled=True, status=ChoreOccurrence.Status.TODO,
    ).first()
    if row is None:
        raise Refused
    row.is_cancelled = False
    row.save(update_fields=["is_cancelled"])


def _restore_planned(slot):
    prior = slot["exception"]
    current = OccurrenceException.objects.select_for_update().filter(
        chore_id=slot["chore_id"], date=datetime.date.fromisoformat(slot["date"]), is_cancelled=True,
    ).first()
    if current is None:
        raise Refused
    if prior is None:
        if current.is_override:
            raise Refused
        current.delete()
        return
    expected = fields(_instance(OccurrenceException, prior))
    if fields(current) != dict(expected, is_cancelled=True):
        raise Refused
    current.is_cancelled = prior["is_cancelled"]
    current.save(update_fields=["is_cancelled"])


def _restore_trim(slot):
    chore_id = slot["chore_id"]
    current = Chore.objects.select_for_update().filter(pk=chore_id).first()
    if current is None:
        raise Refused
    expected = fields(_instance(Chore, slot["chore"]))
    now = fields(current)
    if (dict(now, end_date=None) != dict(expected, end_date=None)
            or now["end_date"] != slot["end_date_after"]
            or _stored(ChoreOccurrence, chore_id, slot["rows"])
            or _stored(OccurrenceException, chore_id, slot["exceptions"])):
        raise Refused
    current.end_date = expected["end_date"] and datetime.date.fromisoformat(expected["end_date"])
    current.save(update_fields=["end_date"])
    ChoreOccurrence.objects.bulk_create([_instance(ChoreOccurrence, r) for r in slot["rows"]])
    OccurrenceException.objects.bulk_create([_instance(OccurrenceException, e) for e in slot["exceptions"]])


def _restore_chore(slot):
    chore_id = slot["chore_id"]
    rotation = slot["rotation"]
    if (Chore.objects.filter(pk=chore_id).exists()
            or ChoreOccurrence.objects.filter(pk__in=[o["id"] for o in slot["occurrences"]]).exists()
            or OccurrenceException.objects.filter(pk__in=[e["id"] for e in slot["exceptions"]]).exists()
            or (rotation is not None and RotationState.objects.filter(pk=rotation["id"]).exists())):
        raise Refused
    _instance(Chore, slot["chore"]).save(force_insert=True)
    ChoreOccurrence.objects.bulk_create([_instance(ChoreOccurrence, o) for o in slot["occurrences"]])
    OccurrenceException.objects.bulk_create([_instance(OccurrenceException, e) for e in slot["exceptions"]])
    if rotation is not None:
        _instance(RotationState, rotation).save(force_insert=True)


_RESTORE = {
    "cancel": _restore_cancel,
    "planned": _restore_planned,
    "trim": _restore_trim,
    "chore": _restore_chore,
}


def restore(slot):
    """Put back what the slot's delete changed, in one transaction, or raise
    ``Refused`` (nothing written). Every check runs before the first write.
    An ``IntegrityError`` propagates after the whole block rolled back."""
    with transaction.atomic():
        _RESTORE[slot["kind"]](slot)
