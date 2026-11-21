import datetime
import re

import pytest
from django.contrib import admin
from django.core.management import call_command
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from chores.models import Chore, Member, RotationState
from chores.services import advance_rotation, eligible_pool, next_member

WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


def member(name, role="kid", order=0):
    return Member.objects.create(name=name, role=role, avatar="😀", order=order)


def chore(mode="rotating", eligibility="kids", **kwargs):
    return Chore.objects.create(
        title="Dishes", start_date=datetime.date(2026, 3, 2),
        assignment_mode=mode, eligibility=eligibility, **kwargs,
    )


def state_of(c):
    return RotationState.objects.filter(chore=c).first()


def set_state(c, last):
    return RotationState.objects.create(chore=c, last_member=last)


def snapshot():
    return (
        Member.objects.count(),
        Chore.objects.count(),
        sorted(RotationState.objects.values_list("id", "chore_id", "last_member_id")),
    )


@pytest.fixture
def abc(db):
    return member("A", order=0), member("B", order=1), member("C", order=2)


# --- Model -----------------------------------------------------------------


@pytest.mark.django_db
def test_rotation_state_registered_in_admin():
    assert RotationState in admin.site._registry


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


@pytest.mark.django_db
def test_creating_a_chore_creates_no_state():
    chore()
    assert RotationState.objects.count() == 0


def test_first_advance_creates_the_row(abc):
    c = chore()
    advance_rotation(c)
    assert state_of(c).last_member == abc[0]


def test_deleting_chore_deletes_state(abc):
    c = chore()
    advance_rotation(c)
    c.delete()
    assert RotationState.objects.count() == 0


def test_deleting_last_member_keeps_row_with_null(abc):
    c = chore()
    advance_rotation(c)
    abc[0].delete()
    state = RotationState.objects.get()
    assert state.chore_id == c.pk
    assert state.last_member is None


# --- next_member is read-only ----------------------------------------------


@pytest.mark.parametrize("with_state", [False, True])
def test_next_member_never_writes(abc, with_state):
    c = chore()
    if with_state:
        set_state(c, abc[1])
    before = snapshot()
    with CaptureQueriesContext(connection) as queries:
        next_member(c, abc[1])
        next_member(c, None)
    assert snapshot() == before
    assert not [q["sql"] for q in queries if WRITE_SQL.match(q["sql"])]
    assert RotationState.objects.filter(chore=c).exists() is with_state


def test_advance_rotation_runs_in_callers_transaction(abc):
    c = chore()
    set_state(c, abc[0])
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            assert advance_rotation(c) == abc[1]
            raise RuntimeError("caller rolls back")
    assert state_of(c).last_member == abc[0]


def test_advance_rotation_first_turn_rolled_back_leaves_no_row(abc):
    c = chore()
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            advance_rotation(c)
            raise RuntimeError
    assert state_of(c) is None


# --- Pool ------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "eligibility, expected",
    [("kids", ["K1", "K2"]), ("adults", ["Ad1", "Ad2"]), ("everyone", ["K1", "Ad1", "K2", "Ad2"])],
)
def test_pool_by_eligibility(eligibility, expected):
    member("K1", "kid", 0)
    member("Ad1", "adult", 1)
    member("K2", "kid", 2)
    member("Ad2", "adult", 3)
    assert [m.name for m in eligible_pool(chore(eligibility=eligibility))] == expected


@pytest.mark.django_db
def test_pool_order_is_order_then_id():
    z = member("Z", order=5)
    y = member("Y", order=1)
    x = member("X", order=5)
    assert eligible_pool(chore()) == [y, z, x]


@pytest.mark.django_db
@pytest.mark.parametrize("eligibility", [None, "", "pets"])
def test_null_or_unknown_eligibility_is_empty_pool(eligibility):
    member("A")
    c = chore(eligibility=eligibility)
    assert eligible_pool(c) == []
    assert next_member(c, None) is None
    assert advance_rotation(c) is None
    assert state_of(c) is None


# --- Rules for rotating chores ---------------------------------------------


def test_no_last_member_gives_first(abc):
    assert next_member(chore(), None) == abc[0]


def test_next_after_a_and_b(abc):
    a, b, c_ = abc
    ch = chore()
    assert next_member(ch, a) == b
    assert next_member(ch, b) == c_


def test_wraps_after_last(abc):
    a, _, c_ = abc
    assert next_member(chore(), c_) == a


def test_ineligible_last_member_goes_to_next_by_order(abc):
    a, b, c_ = abc
    ch = chore()
    Member.objects.filter(pk=b.pk).update(role="adult")
    assert next_member(ch, b) == c_


def test_ineligible_last_member_wraps(abc):
    a, _, c_ = abc
    ch = chore()
    Member.objects.filter(pk=c_.pk).update(role="adult")
    assert next_member(ch, c_) == a


@pytest.mark.django_db
def test_pool_of_one_always_same():
    a = member("A", order=0)
    outsider = member("Grown", "adult", order=1)
    ch = chore()
    assert next_member(ch, None) == a
    assert next_member(ch, a) == a
    assert next_member(ch, outsider) == a
    assert [advance_rotation(ch) for _ in range(3)] == [a, a, a]


@pytest.mark.django_db
def test_empty_pool_only_adults():
    dad = member("Dad", "adult")
    ch = chore()
    assert next_member(ch, None) is None
    assert next_member(ch, dad) is None


@pytest.mark.django_db
def test_empty_pool_no_members():
    assert next_member(chore(eligibility="everyone"), None) is None


@pytest.mark.django_db
def test_member_inserted_in_middle_joins_next_turn():
    a = member("A", order=0)
    member("C", order=2)
    ch = chore()
    b = member("B", order=1)
    assert next_member(ch, a) == b


@pytest.mark.django_db
def test_ties_broken_by_id():
    a = member("A", order=0)
    d = member("D", order=0)
    assert a.pk < d.pk
    ch = chore()
    assert next_member(ch, a) == d
    assert next_member(ch, d) == a


def test_order_change_takes_effect_next_turn(abc):
    a, b, _ = abc
    ch = chore()
    Member.objects.filter(pk=a.pk).update(order=5)
    # The caller's stale instance still says order=0; the current order counts.
    assert next_member(ch, a) == b


def test_order_change_with_advance(abc):
    a, b, _ = abc
    ch = chore()
    advance_rotation(ch)  # A
    Member.objects.filter(pk=a.pk).update(order=5)
    assert advance_rotation(ch) == b


# --- advance_rotation ------------------------------------------------------


def test_four_turns_wrap(abc):
    a, b, c_ = abc
    ch = chore()
    assert [advance_rotation(ch) for _ in range(4)] == [a, b, c_, a]
    assert state_of(ch).last_member == a
    assert RotationState.objects.count() == 1


@pytest.mark.django_db
def test_empty_pool_creates_no_row():
    member("Dad", "adult")
    ch = chore()
    assert advance_rotation(ch) is None
    assert state_of(ch) is None


def test_empty_pool_keeps_existing_state_and_resumes(abc):
    a, b, c_ = abc
    ch = chore()
    advance_rotation(ch)
    advance_rotation(ch)  # B
    Member.objects.update(role="adult")
    assert advance_rotation(ch) is None
    assert state_of(ch).last_member == b
    Member.objects.update(role="kid")
    assert advance_rotation(ch) == c_


@pytest.mark.django_db
def test_fixed_returns_assignee_without_state():
    ann = member("Ann", "adult")
    ch = chore(mode="fixed", eligibility=None, assigned_to=ann)
    assert advance_rotation(ch) == ann
    assert next_member(ch, None) == ann
    assert RotationState.objects.count() == 0


@pytest.mark.django_db
def test_fixed_without_assignee_returns_none():
    member("Ann")
    ch = chore(mode="fixed", eligibility=None)
    assert advance_rotation(ch) is None
    assert RotationState.objects.count() == 0


def test_fixed_does_not_change_existing_state(abc):
    a, b, _ = abc
    ch = chore()
    set_state(ch, b)
    ch.assignment_mode = "fixed"
    ch.assigned_to = a
    ch.save()
    assert advance_rotation(ch) == a
    assert state_of(ch).last_member == b


def test_unassigned_returns_none_without_state(abc):
    ch = chore(mode="unassigned", eligibility=None)
    assert advance_rotation(ch) is None
    assert RotationState.objects.count() == 0
    set_state(ch, abc[1])
    assert advance_rotation(ch) is None
    assert state_of(ch).last_member == abc[1]


def test_mode_change_keeps_state(abc):
    a, b, c_ = abc
    ch = chore()
    advance_rotation(ch)
    advance_rotation(ch)  # B
    ch.assignment_mode, ch.assigned_to = "fixed", a
    ch.save()
    advance_rotation(ch)
    ch.assignment_mode, ch.assigned_to = "rotating", None
    ch.save()
    assert advance_rotation(ch) == c_


@pytest.mark.django_db
def test_eligibility_change_keeps_last_member():
    k1 = member("K1", "kid", 0)
    ad1 = member("Ad1", "adult", 1)
    member("K2", "kid", 2)
    ad2 = member("Ad2", "adult", 3)
    ch = chore(eligibility="kids")
    assert advance_rotation(ch) == k1
    ch.eligibility = "adults"
    ch.save()
    assert advance_rotation(ch) == ad1  # first adult after K1's (0, id)
    assert advance_rotation(ch) == ad2
    ch.eligibility = "kids"
    ch.save()
    assert advance_rotation(ch) == k1  # nothing after Ad2 → wrap


@pytest.mark.django_db
def test_eligibility_change_wraps():
    ad1 = member("Ad1", "adult", 0)
    member("K1", "kid", 1)
    k2 = member("K2", "kid", 2)
    ch = chore(eligibility="kids")
    set_state(ch, k2)
    ch.eligibility = "adults"
    ch.save()
    assert advance_rotation(ch) == ad1


def test_advance_signature_takes_only_the_chore(abc):
    import inspect

    assert list(inspect.signature(advance_rotation).parameters) == ["chore"]
    with CaptureQueriesContext(connection) as queries:
        advance_rotation(chore())
    assert not [q for q in queries if "occurrence" in q["sql"].lower()]


def test_turns_depend_only_on_call_count(abc):
    a, b, c_ = abc
    first, second = chore(), chore()
    for _ in range(2):
        advance_rotation(first)
    for _ in range(2):
        advance_rotation(second)
    assert state_of(first).last_member == state_of(second).last_member == b


# --- Projection and materialization agree ----------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("start", [None, "A", "B", "C", "X", "deleted"])
def test_next_member_simulation_matches_advance(start):
    a = member("A", order=0)
    member("X", "adult", order=1)  # in the DB but not in the kids pool
    b = member("B", order=1)
    member("C", order=3)
    ghost = member("Ghost", order=2)
    ch = chore()
    if start == "deleted":
        set_state(ch, ghost)
        ghost.delete()
    else:
        ghost.delete()
        if start:
            set_state(ch, Member.objects.get(name=start))

    state = state_of(ch)
    last = state.last_member if state else None
    before = snapshot()
    simulated = []
    for _ in range(4):
        last = next_member(ch, last)
        simulated.append(last)
    assert snapshot() == before  # projection wrote nothing

    stored = [advance_rotation(ch) for _ in range(4)]
    assert simulated == stored
    assert state_of(ch).last_member == stored[-1]
    assert a in stored and b in stored
