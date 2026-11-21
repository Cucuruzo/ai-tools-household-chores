"""Move a Planned date into the current week (#51).

Examples use today = Wed 18 Nov 2026 (UTC), current week Mon 16 - Sun 22 Nov,
and the members Mom (adult, 0), Emma (kid, 1) and Leo (kid, 2). "Bins" is
weekly on Thursday, rotating kids, start 1 Nov, 10 pts, instructions "old".
Thu 19 Nov is stored for Emma, so RotationState is Emma. Projected: Thu 26
Leo, Thu 3 Dec Emma, Thu 10 Dec Leo.
"""
import datetime
import inspect
from unittest import mock

import pytest
from django.db import IntegrityError, connection, models
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import services
from chores.models import Chore, ChoreOccurrence, OccurrenceException, RotationState
from chores.services import (
    MoveRefused, cancel_projected_date, edit_projected_date, materialize_week, move_projected_date,
    projected_occurrence, set_projected_member,
)
from tests.test_dashboard import at, d, sections
from tests.test_edit_scope import daily, messages_of, row, rows_of, weekly
from tests.test_occurrence_move import move
from tests.test_planned_move import (
    MESSAGE, MONDAY, SUN_NOON, TODAY, WED, WED_2, WED_25, WED_5, _count, bins, bins_cards,
    dashboard, exceptions_of, extra, family, follow, pmove, rotation, stored,
)
from tests.test_undo_delete import REFUSED, deleted, slot, state, undo_post, undone

pytestmark = pytest.mark.django_db

WED_9 = "2026-12-09T12:00:00"
TAKEN = "This chore already has an occurrence on {}."


@pytest.fixture
def client():
    return Client()


def shown(client, week, c, **kwargs):
    return [(x["date"], x["assignee"]) for x in bins_cards(dashboard(client, week, **kwargs), c)]


def become_current(client, *whens):
    for when in whens:
        dashboard(client, when=when)


def unstored_bins():
    """Bins without a stored current week."""
    return weekly("Bins", 3, "2026-11-01", points=10, instructions="old",
                  mode="rotating", eligibility="kids")


# --- The worked examples ------------------------------------------------------------------------


def test_thu_3_moved_to_sat_21(client, family):
    mom, emma, leo = family
    c, o = bins()
    rotation_before = list(RotationState.objects.values())

    response = pmove(client, c.pk, date="2026-12-03", target="2026-11-21", week="2026-W49")
    assert response.status_code == 302
    assert response["Location"] == "/dashboard/?week=2026-W49"
    assert messages_of(response) == ["Occurrence moved to Sat 21 Nov"]
    assert list(RotationState.objects.values()) == rotation_before
    assert stored(c) == {
        "2026-11-19": ("Emma", False, False, "Bins", "old", 10, None),
        "2026-11-21": ("Emma", False, True, "Bins", "old", 10, None),
    }
    new = ChoreOccurrence.objects.get(chore=c, date=d("2026-11-21"))
    assert new.status == ChoreOccurrence.Status.TODO and new.member_id == emma.pk
    assert ChoreOccurrence.objects.get(pk=o.pk).date == d("2026-11-19")  # untouched
    [source] = OccurrenceException.objects.filter(chore=c)
    assert (source.date, source.is_cancelled, source.moved_from, source.own_values) == (
        d("2026-12-03"), True, None, "")

    assert MESSAGE.format("Occurrence moved to Sat 21 Nov") in follow(client, response)
    assert shown(client, None, c) == [("Thu, Nov 19", "😀 Emma"), ("Sat, Nov 21", "😀 Emma")]
    assert shown(client, "2026-W49", c) == []
    assert shown(client, "2026-W48", c) == [("Thu, Nov 26", "😀 Leo")]
    assert shown(client, "2026-W50", c) == [("Thu, Dec 10", "😀 Leo")]

    become_current(client, WED_25, WED_2, WED_9)
    later = stored(c)
    assert {k: later[k] for k in ("2026-11-26", "2026-12-03", "2026-12-10")} == {
        "2026-11-26": ("Leo", False, False, "Bins", "old", 10, None),
        "2026-12-03": ("Emma", True, False, "Bins", "old", 10, None),
        "2026-12-10": ("Leo", False, False, "Bins", "old", 10, None),
    }
    assert rotation(c) == "Leo"
    assert not OccurrenceException.objects.filter(chore=c).exists()


def test_thu_3_moved_to_today_shows_under_today(client, family):
    c, _ = bins()
    response = pmove(client, c.pk, date="2026-12-03", target="2026-11-18", week="2026-W49")
    assert messages_of(response) == ["Occurrence moved to Wed 18 Nov"]
    body = dashboard(client)
    today = [x for x in sections(body)["today"] if x["title"] == "Bins"]
    assert [(x["id"], x["assignee"]) for x in today] == [
        (ChoreOccurrence.objects.get(chore=c, date=TODAY).pk, "😀 Emma")]
    assert [x["title"] for x in sections(body)["upcoming"] if x["title"] == "Bins"] == ["Bins"]  # Thu 19
    assert ChoreOccurrence.objects.get(chore=c, date=TODAY).is_override
    assert rotation(c) == "Emma"


def test_back_onto_the_stored_source(client, family):
    mom, emma, leo = family
    c, o = bins()
    assert move(client, c, o, "2026-11-27", title="Bins Fri", points=20).status_code == 302
    e = OccurrenceException.objects.get(chore=c, date=d("2026-11-27"))
    assert (e.moved_from, e.title, e.points) == (d("2026-11-19"), "Bins Fri", 20)
    assert ChoreOccurrence.objects.get(pk=o.pk).is_cancelled

    response = pmove(client, c.pk, date="2026-11-27", target="2026-11-19", week="2026-W48")
    assert response["Location"] == "/dashboard/?week=2026-W48"
    assert messages_of(response)[-1:] == ["Occurrence moved to Thu 19 Nov"]
    assert not ChoreOccurrence.objects.filter(pk=o.pk).exists()  # deleted, not un-cancelled
    assert stored(c) == {"2026-11-19": ("Emma", False, True, "Bins Fri", "old", 20, None)}
    assert not OccurrenceException.objects.filter(chore=c).exists()  # off the rule: deleted
    assert shown(client, "2026-W48", c) == [("Thu, Nov 26", "😀 Leo")]
    assert shown(client, None, c) == [("Thu, Nov 19", "😀 Emma")]
    assert rotation(c) == "Emma"


# --- Refusals -----------------------------------------------------------------------------------


@pytest.mark.parametrize("target, message, setup", [
    ("2026-11-19", TAKEN.format("Thu 19 Nov"), None),
    ("2026-11-17", "Pick a day from Wed 18 Nov to Thu 31 Dec 2099.", None),
    ("2026-11-16", "Pick a day from Wed 18 Nov to Thu 31 Dec 2099.", None),
    ("2026-11-21", TAKEN.format("Sat 21 Nov"), "todo"),
    ("2026-11-21", TAKEN.format("Sat 21 Nov"), "done"),
    ("2026-11-21", TAKEN.format("Sat 21 Nov"), "skipped"),
    ("2026-11-21", TAKEN.format("Sat 21 Nov"), "done-cancelled"),
    ("2026-11-21", TAKEN.format("Sat 21 Nov"), "skipped-cancelled"),
], ids=["thu-19-row", "tue-17", "mon-16", "todo", "done", "skipped", "done-cancelled",
        "skipped-cancelled"])
def test_refusals_write_nothing(client, family, target, message, setup):
    c, _ = bins()
    if setup:
        row(c, target, setup.split("-")[0], is_cancelled=setup.endswith("cancelled"))
    before = state()
    response = pmove(client, c.pk, date="2026-12-03", target=target, week="2026-W49")
    assert response.status_code == 302
    assert response["Location"] == "/dashboard/?week=2026-W49"
    assert messages_of(response) == [message]
    assert state() == before
    assert MESSAGE.format(message) in follow(client, response)


def test_end_date_refusal_from_the_view(client, family):
    c, _ = bins()
    Chore.objects.filter(pk=c.pk).update(end_date=d("2026-11-26"))
    before = state()
    response = pmove(client, c.pk, date="2026-11-26", target="2026-11-17", week="2026-W48")
    assert messages_of(response) == ["Pick a day from Wed 18 Nov to Thu 26 Nov."]
    assert state() == before


def test_end_date_limits_a_current_week_target(family):
    c, _ = bins()
    Chore.objects.filter(pk=c.pk).update(end_date=d("2026-11-26"))
    c.refresh_from_db()
    before = state()
    with pytest.raises(MoveRefused) as refused:
        move_projected_date(c, d("2026-11-26"), d("2026-11-17"), TODAY)
    assert str(refused.value) == "Pick a day from Wed 18 Nov to Thu 26 Nov."
    assert state() == before
    assert move_projected_date(c, d("2026-11-26"), d("2026-11-21"), TODAY) is True
    assert ChoreOccurrence.objects.filter(chore=c, date=d("2026-11-21")).exists()


def test_daily_chore_refuses_every_day_of_this_week(client, family):
    c = daily(points=10)
    before = state()
    for day in range(18, 23):
        response = pmove(client, c.pk, date="2026-12-01", target=f"2026-11-{day}")
        assert messages_of(response)[-1:] == [TAKEN.format(services._day_text(d(f"2026-11-{day}"), TODAY))]
        assert state() == before  # the materialization is rolled back too


def test_refusal_rolls_the_materialization_back(client, family):
    c = unstored_bins()
    assert not ChoreOccurrence.objects.exists()
    before = state()
    response = pmove(client, c.pk, date="2026-12-03", target="2026-11-19")
    assert messages_of(response) == [TAKEN.format("Thu 19 Nov")]
    assert state() == before
    assert not ChoreOccurrence.objects.exists() and not RotationState.objects.exists()
    response = pmove(client, c.pk, date="2026-12-03", target="2026-11-17")
    assert messages_of(response)[-1:] == ["Pick a day from Wed 18 Nov to Thu 31 Dec 2099."]
    assert state() == before


def test_source_rule_is_unchanged(client, family):
    c, _ = bins()
    before = state()
    for date in ["2026-11-19", "2026-11-21", "2026-11-22", "2026-12-04"]:
        assert pmove(client, c.pk, date=date, target="2026-11-20").status_code == 404
    assert state() == before


# --- Planned dates with exceptions ----------------------------------------------------------------


def test_31_override_source_gives_its_member(client, family):
    mom, emma, leo = family
    c, _ = bins()
    set_projected_member(c, d("2026-11-26"), mom.pk)
    response = pmove(client, c.pk, date="2026-11-26", target="2026-11-20")
    assert messages_of(response) == ["Occurrence moved to Fri 20 Nov"]
    assert stored(c)["2026-11-20"] == ("Mom", False, True, "Bins", "old", 10, None)
    source = exceptions_of(c)["2026-11-26"]
    assert (source.is_cancelled, source.is_override, source.member_id) == (True, True, mom.pk)
    assert rotation(c) == "Emma"
    become_current(client, WED_25)
    assert stored(c)["2026-11-26"][:2] == ("Leo", True)  # the source keeps its turn
    assert rotation(c) == "Leo"


def test_38_edited_source_gives_its_values(client, family):
    mom, emma, leo = family
    c, _ = bins()
    assert edit_projected_date(c, d("2026-12-03"), "Bins+", "new", datetime.time(18), 15, TODAY)
    assert exceptions_of(c)["2026-12-03"].own_values == "edited"
    response = pmove(client, c.pk, date="2026-12-03", target="2026-11-22")
    assert messages_of(response) == ["Occurrence moved to Sun 22 Nov"]
    assert stored(c)["2026-11-22"] == ("Emma", False, True, "Bins+", "new", 15, datetime.time(18))
    source = exceptions_of(c)["2026-12-03"]
    assert (source.is_cancelled, source.own_values) == (True, "edited")
    assert shown(client, "2026-W49", c) == []


def test_extra_off_the_rule_is_deleted(client, family):
    mom, emma, leo = family
    c, _ = bins()
    pmove(client, c.pk, date="2026-12-03", target="2026-12-05")  # #48: an extra on Sat 5
    response = pmove(client, c.pk, date="2026-12-05", target="2026-11-20")
    assert messages_of(response)[-1:] == ["Occurrence moved to Fri 20 Nov"]
    assert stored(c)["2026-11-20"] == ("Emma", False, True, "Bins", "old", 10, None)
    found = exceptions_of(c)
    assert set(found) == {"2026-12-03"} and found["2026-12-03"].is_cancelled
    assert shown(client, "2026-W49", c) == []


def test_extra_on_a_rule_date_is_cancelled(client, family):
    mom, emma, leo = family
    c, _ = bins()
    extra(c, "2026-11-26", member=mom, moved_from="2026-11-24", title="X", points=5)
    response = pmove(client, c.pk, date="2026-11-26", target="2026-11-21")
    assert messages_of(response) == ["Occurrence moved to Sat 21 Nov"]
    assert stored(c)["2026-11-21"] == ("Mom", False, True, "X", "old", 5, None)
    source = exceptions_of(c)["2026-11-26"]
    assert (source.is_cancelled, source.moved_from) == (True, d("2026-11-24"))
    become_current(client, WED_25)
    assert stored(c)["2026-11-26"][:2] == ("Leo", True)
    assert rotation(c) == "Leo"


# --- Rotation and materialization ---------------------------------------------------------------


def test_the_move_takes_no_rotation_turn(family):
    c, _ = bins()
    with mock.patch("chores.services.advance_rotation") as advance, \
            mock.patch("chores.services.create_occurrence") as create:
        assert move_projected_date(c, d("2026-12-03"), d("2026-11-21"), TODAY) is True
    assert advance.call_count == 0 and create.call_count == 0
    assert ChoreOccurrence.objects.filter(chore=c, date=d("2026-11-21")).exists()


def test_only_materialize_week_writes_rotation(client, family):
    c = unstored_bins()
    with mock.patch("chores.services.advance_rotation", wraps=services.advance_rotation) as advance:
        response = pmove(client, c.pk, date="2026-12-03", target="2026-11-21")
    assert messages_of(response) == ["Occurrence moved to Sat 21 Nov"]
    assert advance.call_count == 1  # Thu 19, from materialize_week
    assert stored(c) == {
        "2026-11-19": ("Emma", False, False, "Bins", "old", 10, None),
        "2026-11-21": ("Emma", False, True, "Bins", "old", 10, None),
    }
    assert rotation(c) == "Emma"


def test_current_week_is_materialized_correctly_afterwards(client, family):
    mom, emma, leo = family
    c = unstored_bins()
    other = weekly("Plants", 5, "2026-11-01", points=5, mode="rotating", eligibility="kids")
    set_projected_member(c, d("2026-11-19"), mom.pk)  # an exception of this week
    pmove(client, c.pk, date="2026-12-03", target="2026-11-20")
    assert stored(c) == {
        "2026-11-19": ("Mom", False, True, "Bins", "old", 10, None),
        "2026-11-20": ("Emma", False, True, "Bins", "old", 10, None),
    }
    assert ChoreOccurrence.objects.filter(chore=other, date=d("2026-11-21")).exists()
    assert not OccurrenceException.objects.filter(date__lte=d("2026-11-22")).exists()
    before = state()
    assert materialize_week(MONDAY, today=TODAY) == []
    dashboard(client)
    assert state() == before
    assert projected_occurrence(c, d("2026-11-26"), TODAY)["member_id"] == leo.pk


def test_later_week_target_does_not_materialize(client, family):
    c = unstored_bins()
    response = pmove(client, c.pk, date="2026-12-03", target="2026-12-05")
    assert messages_of(response) == ["Occurrence moved to Sat 5 Dec"]
    assert not ChoreOccurrence.objects.exists() and not RotationState.objects.exists()


# --- Interactions with the new row ----------------------------------------------------------------


def _moved(client):
    c, o = bins()
    pmove(client, c.pk, date="2026-12-03", target="2026-11-21")
    return c, o, ChoreOccurrence.objects.get(chore=c, date=d("2026-11-21"))


def test_22_delete_and_24_undo_of_the_new_row(client, family):
    c, _, new = _moved(client)
    before = state()
    token = deleted(client, c, when=WED, occurrence=new.pk, scope="this")
    assert slot(client)["kind"] == "cancel"
    assert ChoreOccurrence.objects.get(pk=new.pk).is_cancelled
    assert shown(client, None, c) == [("Thu, Nov 19", "😀 Emma")]
    undone(client, token, when=WED_5)
    assert state() == before


def test_live_undo_of_a_deleted_cancelled_target_is_refused(client, family):
    c, o = bins()
    token = deleted(client, c, when=WED, occurrence=o.pk, scope="this")
    assert slot(client)["kind"] == "cancel"
    response = pmove(client, c.pk, date="2026-12-03", target="2026-11-19")
    assert messages_of(response)[-1:] == ["Occurrence moved to Thu 19 Nov"]
    assert not ChoreOccurrence.objects.filter(pk=o.pk).exists()
    after = state()
    refused = undo_post(client, token, WED_5)
    assert refused.status_code == 400 and REFUSED in refused.content
    assert state() == after
    assert slot(client) is None


@pytest.mark.parametrize("action, status", [("done", "done"), ("skip", "skipped")])
def test_12_done_and_skip_on_the_new_row(client, family, action, status):
    c, _, new = _moved(client)
    with at(WED):
        response = client.post(f"/dashboard/toggle/{new.pk}/", {"action": action})
    assert response.status_code in (200, 302)
    assert ChoreOccurrence.objects.get(pk=new.pk).status == status
    with at(WED):
        client.post(f"/dashboard/toggle/{new.pk}/", {"action": "undo"})
    assert ChoreOccurrence.objects.get(pk=new.pk).status == "todo"


def test_13_assign_on_the_new_row(client, family):
    mom, emma, leo = family
    c, _, new = _moved(client)
    with at(WED):
        response = client.post(f"/dashboard/assign/{new.pk}/", {"assignee": str(mom.pk)})
    assert response.status_code in (200, 302)
    assert ChoreOccurrence.objects.get(pk=new.pk).member_id == mom.pk
    assert rotation(c) == "Emma"


def test_53_another_date_onto_the_cancelled_source(client, family):
    mom, emma, leo = family
    c, _, _ = _moved(client)
    response = pmove(client, c.pk, date="2026-12-10", target="2026-12-03", week="2026-W50")
    assert messages_of(response)[-1:] == ["Occurrence moved to Thu 3 Dec"]
    found = exceptions_of(c)
    assert set(found) == {"2026-12-03", "2026-12-10"}
    assert (found["2026-12-03"].moved_from, found["2026-12-03"].member_id) == (d("2026-12-10"), leo.pk)
    assert shown(client, "2026-W49", c) == [("Thu, Dec 3", "😀 Leo")]


def test_22_cancel_of_a_planned_date_then_move_into_this_week(client, family):
    c, _ = bins()
    cancel_projected_date(c, d("2026-12-03"))
    before = state()
    assert pmove(client, c.pk, date="2026-12-03", target="2026-11-21").status_code == 404
    assert state() == before
    response = pmove(client, c.pk, date="2026-12-10", target="2026-11-21")
    assert messages_of(response) == ["Occurrence moved to Sat 21 Nov"]
    assert stored(c)["2026-11-21"][0] == "Leo"
    assert exceptions_of(c)["2026-12-03"].is_cancelled and exceptions_of(c)["2026-12-10"].is_cancelled


# --- Races and retries ----------------------------------------------------------------------------


def test_lost_race_on_a_cancelled_target_refuses(client, family):
    c, _ = bins()
    held = row(c, "2026-11-21", is_cancelled=True)
    before = state()
    real = models.QuerySet.delete
    calls = []

    def undo_first(queryset):
        if not calls:
            calls.append(1)  # a concurrent Undo un-cancels the row first
            ChoreOccurrence.objects.filter(pk=held.pk).update(is_cancelled=False)
        return real(queryset)

    with mock.patch.object(models.QuerySet, "delete", undo_first):
        response = pmove(client, c.pk, date="2026-12-03", target="2026-11-21")
    assert messages_of(response) == [TAKEN.format("Sat 21 Nov")]
    assert state() == before


def test_cancelled_target_gives_way(client, family):
    c, _ = bins()
    held = row(c, "2026-11-21", is_cancelled=True)
    assert messages_of(pmove(client, c.pk, target="2026-11-21")) == ["Occurrence moved to Sat 21 Nov"]
    assert not ChoreOccurrence.objects.filter(pk=held.pk).exists()
    assert not stored(c)["2026-11-21"][1]


def test_concurrent_row_on_the_target_refuses_on_the_retry(family):
    c, _ = bins()
    real = services._planned_move_attempt
    calls = []

    def concurrent(*args):
        calls.append(args)
        if len(calls) == 1:
            ChoreOccurrence.objects.create(chore=c, date=d("2026-11-21"), points=10)
            raise IntegrityError("unique_occurrence_per_chore_date")
        return real(*args)

    with mock.patch("chores.services._planned_move_attempt", side_effect=concurrent):
        with pytest.raises(MoveRefused) as refused:
            move_projected_date(c, d("2026-12-03"), d("2026-11-21"), TODAY)
    assert str(refused.value) == TAKEN.format("Sat 21 Nov")
    assert len(calls) == 2
    assert not exceptions_of(c)


def test_integrity_error_rolls_the_attempt_back_and_is_retried(family):
    c, _ = bins()
    held = row(c, "2026-11-21", is_cancelled=True)
    real_create = ChoreOccurrence.objects.create
    calls = []

    def failing(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            assert not ChoreOccurrence.objects.filter(pk=held.pk).exists()  # deleted in the attempt
            raise IntegrityError("unique_occurrence_per_chore_date")
        return real_create(**kwargs)

    with mock.patch.object(ChoreOccurrence.objects, "create", side_effect=failing):
        assert move_projected_date(c, d("2026-12-03"), d("2026-11-21"), TODAY) is True
    assert len(calls) == 2
    assert set(exceptions_of(c)) == {"2026-12-03"}
    assert rows_of(c)[21].is_override and not rows_of(c)[21].is_cancelled


def test_second_integrity_error_leaves_everything_as_before(family):
    c, _ = bins()
    before = state()
    with mock.patch.object(ChoreOccurrence.objects, "create", side_effect=IntegrityError("x")):
        with pytest.raises(IntegrityError):
            move_projected_date(c, d("2026-12-03"), d("2026-11-21"), TODAY)
    assert state() == before


# --- Query count, hostile input, clock ------------------------------------------------------------


def _queries(client, others):
    Chore.objects.all().delete()
    c, _ = bins()
    row(c, "2026-11-21", is_cancelled=True)
    for i in range(others):
        o = weekly(f"Other {i}", i % 7, "2026-11-02", points=10)
        extra(o, "2026-12-05", moved_from="2026-11-30")
    materialize_week(MONDAY, today=TODAY)
    count = _count(lambda: pmove(client, c.pk, target="2026-11-21"))
    assert stored(c)["2026-11-21"][2] is True
    return count


def test_query_count_is_constant(client, family):
    assert _queries(client, 0) == _queries(client, 10)


@pytest.mark.parametrize("target", ["2026-11-21", "abc", "", "x" * 10_000, "<script>", "2026-11-2"])
def test_hostile_input_never_500(client, family, target):
    c, _ = bins()
    for bad in ["<script>", "x" * 10_000, "2026-W99", "-1", "99999999999999999999"]:
        response = pmove(client, c.pk, target=target, week=bad, member=bad, day=bad)
        assert response.status_code in (302, 404)
    for date in ["<script>", "x" * 10_000, "2026-11-21", "-1"]:
        assert pmove(client, c.pk, date=date, target="2026-11-21").status_code in (400, 404)


def test_kiritimati_vs_utc(client, family):
    c, _ = bins()
    before = state()
    # Sun 22 12:00 UTC is Mon 23 in Kiritimati: Sun 22 is already past there,
    # while in UTC it is today.
    response = pmove(client, c.pk, date="2026-12-03", target="2026-11-22",
                     when=SUN_NOON, tz="Pacific/Kiritimati")
    assert messages_of(response) == ["Pick a day from Mon 23 Nov to Thu 31 Dec 2099."]
    assert state() == before
    response = pmove(client, c.pk, date="2026-12-03", target="2026-11-22", when=SUN_NOON, tz="UTC")
    assert messages_of(response)[-1:] == ["Occurrence moved to Sun 22 Nov"]
    assert stored(c)["2026-11-22"][:3] == ("Emma", False, True)


def test_kiritimati_current_week_is_its_own(client, family):
    c, _ = bins()
    # Sat 28 is a later week in UTC but this week in Kiritimati (Mon 23 - Sun 29).
    response = pmove(client, c.pk, date="2026-12-03", target="2026-11-28",
                     when=SUN_NOON, tz="Pacific/Kiritimati")
    assert messages_of(response) == ["Occurrence moved to Sat 28 Nov"]
    assert stored(c)["2026-11-26"][:2] == ("Leo", False)  # Kiritimati's week was materialized
    assert stored(c)["2026-11-28"][:3] == ("Emma", False, True)
    assert rotation(c) == "Leo"


def test_services_never_read_the_clock():
    for fn in (services._into_this_week, services._planned_move_attempt, services.move_projected_date):
        source = inspect.getsource(fn)
        assert "timezone" not in source and "_resolve_today" not in source
        assert "localdate" not in source and "now(" not in source


def test_view_reads_today_once(client, family):
    c, _ = bins()
    with mock.patch("chores.views.timezone.localdate", wraps=services.timezone.localdate) as localdate:
        pmove(client, c.pk, target="2026-11-21")
    assert localdate.call_count == 1


def test_redirect_keeps_the_posted_filters(client, family):
    mom, emma, leo = family
    c, _ = bins()
    response = pmove(client, c.pk, target="2026-11-21", week="2026-W49", member=str(emma.pk), day="4")
    assert response["Location"] == f"/dashboard/?week=2026-W49&member={emma.pk}&day=4"
