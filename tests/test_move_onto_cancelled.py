"""Move an occurrence onto a later-week date that holds a cancelled exception (#53).

Examples use today = Wed 18 Nov 2026 (UTC). "Bins" is weekly on Thursday,
rotating kids Emma (1) and Leo (2), 10 pts. Thu 19 Nov is stored for Emma.
Projected: Thu 26 Leo, Thu 3 Dec Emma, Thu 10 Dec Leo, Thu 17 Dec Emma.
"""
import datetime
from unittest import mock

import pytest
from django.db import IntegrityError
from django.db.models.query import QuerySet
from django.test import Client

from chores import services
from chores.models import Chore, ChoreOccurrence, OccurrenceException, RotationState
from chores.services import (
    MoveRefused, cancel_projected_date, edit_projected_date, move_occurrence, move_projected_date,
    projected_occurrence, set_projected_member,
)
from tests.test_dashboard import at, d, no_writes
from tests.test_edit_scope import messages_of, url as edit_url, weekly
from tests.test_occurrence_move import date_errors, move
from tests.test_planned_move import (
    MESSAGE, SUN_NOON, TODAY, WED, WED_2, WED_25, WED_5, _count, bins, bins_cards, dashboard,
    exceptions_of, extra, family, follow, moved_bins, pmove, rotation, stored, who,
)
from tests.test_undo_delete import REFUSED, deleted, slot, state, undo_post, undone

pytestmark = pytest.mark.django_db

WED_9 = "2026-12-09T12:00:00"
WED_16 = "2026-12-16T12:00:00"


@pytest.fixture
def client():
    return Client()


def bins_deleted_thu_10(client):
    """Bins with Thu 10 Dec deleted on its own from the Planned card (#22).
    Returns the chore, the Thu 19 row and the Undo token."""
    c, o = bins()
    token = deleted(client, c, when=WED, date="2026-12-10", scope="this")
    assert exceptions_of(c)["2026-12-10"].is_cancelled
    return c, o, token


def shown(client, week, c):
    return [(x["date"], x["assignee"]) for x in bins_cards(dashboard(client, week), c)]


def become_current(client, *whens):
    for when in whens:
        dashboard(client, when=when)


# --- The worked examples ------------------------------------------------------------------------


def test_thu_3_moved_onto_the_deleted_thu_10(client, family):
    mom, emma, leo = family
    c, _, token = bins_deleted_thu_10(client)
    rows_before = list(ChoreOccurrence.objects.values())
    rotation_before = list(RotationState.objects.values())
    cancel_pk = exceptions_of(c)["2026-12-10"].pk

    response = pmove(client, c.pk, date="2026-12-03", target="2026-12-10", week="2026-W49")
    assert response.status_code == 302
    assert response["Location"] == "/dashboard/?week=2026-W49"
    assert messages_of(response) == ["Occurrence moved to Thu 10 Dec"]
    assert list(ChoreOccurrence.objects.values()) == rows_before
    assert list(RotationState.objects.values()) == rotation_before
    found = exceptions_of(c)
    assert set(found) == {"2026-12-03", "2026-12-10"}
    source, target = found["2026-12-03"], found["2026-12-10"]
    assert (source.is_cancelled, source.moved_from) == (True, None)
    assert target.pk != cancel_pk  # deleted, not un-cancelled
    assert (target.moved_from, target.is_override, target.is_cancelled, target.member_id,
            target.title, target.instructions, target.due_time, target.points, target.own_values) == (
        d("2026-12-03"), True, False, emma.pk, "Bins", "old", None, 10, "")

    assert MESSAGE.format("Occurrence moved to Thu 10 Dec") in follow(client, response)
    assert shown(client, "2026-W49", c) == []
    assert shown(client, "2026-W50", c) == [("Thu, Dec 10", "😀 Emma")]
    assert shown(client, "2026-W51", c) == [("Thu, Dec 17", "😀 Emma")]

    after = state()
    refused = undo_post(client, token, WED_5)
    assert refused.status_code == 400 and REFUSED in refused.content
    assert state() == after
    assert slot(client) is None

    become_current(client, WED_25, WED_2, WED_9, WED_16)
    assert stored(c) == {
        "2026-11-19": ("Emma", False, False, "Bins", "old", 10, None),
        "2026-11-26": ("Leo", False, False, "Bins", "old", 10, None),
        "2026-12-03": ("Emma", True, False, "Bins", "old", 10, None),
        "2026-12-10": ("Emma", False, True, "Bins", "old", 10, None),
        "2026-12-17": ("Emma", False, False, "Bins", "old", 10, None),
    }
    assert rotation(c) == "Emma"
    assert not OccurrenceException.objects.filter(chore=c, date__lte=d("2026-12-20")).exists()


def test_then_thu_10_moved_to_sat_12_leaves_a_cancel(client, family):
    mom, emma, leo = family
    c, _, _ = bins_deleted_thu_10(client)
    pmove(client, c.pk, date="2026-12-03", target="2026-12-10")
    moved = exceptions_of(c)["2026-12-10"]

    response = pmove(client, c.pk, date="2026-12-10", target="2026-12-12", week="2026-W50")
    assert messages_of(response)[-1:] == ["Occurrence moved to Sat 12 Dec"]
    found = exceptions_of(c)
    assert set(found) == {"2026-12-03", "2026-12-10", "2026-12-12"}
    assert (found["2026-12-12"].pk, found["2026-12-12"].moved_from, found["2026-12-12"].member_id) == (
        moved.pk, d("2026-12-03"), emma.pk)
    cancel = found["2026-12-10"]
    assert cancel.pk != moved.pk
    assert (cancel.is_cancelled, cancel.moved_from, cancel.is_override, cancel.member_id,
            cancel.own_values) == (True, None, False, None, "")
    assert shown(client, "2026-W50", c) == [("Sat, Dec 12", "😀 Emma")]
    assert who(c, "2026-11-30", "2026-12-20") == [("2026-12-12", "Emma"), ("2026-12-17", "Emma")]

    become_current(client, WED_25, WED_2, WED_9, WED_16)
    found = stored(c)
    assert found["2026-12-10"][:3] == ("Leo", True, False)
    assert found["2026-12-12"][:3] == ("Emma", False, True)
    assert found["2026-12-17"][:3] == ("Emma", False, False)
    assert rotation(c) == "Emma"


def test_extra_moved_back_onto_its_source(client, family):
    mom, emma, leo = family
    c, _ = moved_bins(client)  # Thu 3 -> Sat 5
    first = exceptions_of(c)["2026-12-05"]
    rows_before = list(ChoreOccurrence.objects.values())

    response = pmove(client, c.pk, date="2026-12-05", target="2026-12-03", week="2026-W49")
    assert messages_of(response)[-1:] == ["Occurrence moved to Thu 3 Dec"]
    found = exceptions_of(c)
    assert set(found) == {"2026-12-03"}  # off the rule, Sat 5 leaves nothing
    back = found["2026-12-03"]
    assert (back.pk, back.moved_from, back.member_id, back.is_cancelled, back.is_override) == (
        first.pk, d("2026-12-03"), emma.pk, False, True)
    assert shown(client, "2026-W49", c) == [("Thu, Dec 3", "😀 Emma")]
    assert list(ChoreOccurrence.objects.values()) == rows_before

    response = pmove(client, c.pk, date="2026-12-03", target="2026-12-04", week="2026-W49")
    assert messages_of(response)[-1:] == ["Occurrence moved to Fri 4 Dec"]
    found = exceptions_of(c)
    assert set(found) == {"2026-12-03", "2026-12-04"}
    assert (found["2026-12-04"].pk, found["2026-12-04"].moved_from) == (first.pk, d("2026-12-03"))
    assert (found["2026-12-03"].is_cancelled, found["2026-12-03"].moved_from) == (True, None)
    assert found["2026-12-03"].pk != first.pk
    assert shown(client, "2026-W49", c) == [("Fri, Dec 4", "😀 Emma")]
    assert who(c, "2026-12-07", "2026-12-20") == [("2026-12-10", "Leo"), ("2026-12-17", "Emma")]


def test_back_on_its_source_is_stored_with_its_turn(client, family):
    c, _ = moved_bins(client)
    pmove(client, c.pk, date="2026-12-05", target="2026-12-03")
    [shown_] = [p for p in services.project_occurrences(d("2026-12-03"), d("2026-12-03"), TODAY)
                if p["chore_id"] == c.pk]
    become_current(client, WED_25, WED_2, WED_9)
    found = stored(c)
    assert found["2026-12-03"] == ("Emma", False, True, "Bins", "old", 10, None)
    assert found["2026-12-10"][:3] == ("Leo", False, False)
    assert rotation(c) == "Leo"
    row = ChoreOccurrence.objects.get(chore=c, date=d("2026-12-03"))
    assert (shown_["title"], shown_["member_id"], shown_["points"], shown_["due_time"]) == (
        row.title, row.member_id, row.points, row.due_time)


def test_stored_row_onto_the_deleted_thu_10(client, family):
    mom, emma, leo = family
    c, o, _ = bins_deleted_thu_10(client)
    response = move(client, c, o, "2026-12-10", title="Bins late", points=20)
    assert response.status_code == 302
    assert response["Location"] == edit_url(c)
    assert "occurrence=" not in response["Location"]
    assert messages_of(response) == ["Occurrence moved to Thu 10 Dec"]
    row = ChoreOccurrence.objects.get(pk=o.pk)
    assert (row.date, row.is_cancelled, row.title, row.points, row.member_id) == (
        d("2026-11-19"), True, "Bins", 10, emma.pk)
    assert ChoreOccurrence.objects.filter(chore=c).count() == 1
    [e] = OccurrenceException.objects.filter(chore=c)
    assert (e.date, e.moved_from, e.is_override, e.is_cancelled, e.member_id, e.title,
            e.instructions, e.due_time, e.points) == (
        d("2026-12-10"), d("2026-11-19"), True, False, emma.pk, "Bins late", "old", None, 20)
    assert rotation(c) == "Emma"
    assert shown(client, "2026-W50", c) == [("Thu, Dec 10", "😀 Emma")]

    become_current(client, WED_25, WED_2, WED_9)
    found = stored(c)
    assert found["2026-12-10"] == ("Emma", False, True, "Bins late", "old", 20, None)
    assert found["2026-11-26"][0] == "Leo" and found["2026-12-03"][0] == "Emma"
    assert rotation(c) == "Leo"  # Thu 10 took Leo's turn


# --- What gives way, what refuses ------------------------------------------------------------------


def test_cancelled_extra_off_the_rule_gives_way_and_takes_no_turn(client, family):
    mom, emma, leo = family
    c, _ = bins()
    gone = extra(c, "2026-12-12", moved_from="2026-12-17", is_cancelled=True)
    response = pmove(client, c.pk, date="2026-12-03", target="2026-12-12")
    assert messages_of(response) == ["Occurrence moved to Sat 12 Dec"]
    new = exceptions_of(c)["2026-12-12"]
    assert not OccurrenceException.objects.filter(pk=gone.pk).exists()
    assert (new.moved_from, new.member_id, new.is_cancelled) == (d("2026-12-03"), emma.pk, False)
    assert who(c, "2026-11-23", "2026-12-20") == [
        ("2026-11-26", "Leo"), ("2026-12-10", "Leo"), ("2026-12-12", "Emma"), ("2026-12-17", "Emma")]
    become_current(client, WED_25, WED_2, WED_9, WED_16)
    found = stored(c)
    assert found["2026-12-10"][:3] == ("Leo", False, False)
    assert found["2026-12-12"][:3] == ("Emma", False, True)
    assert found["2026-12-17"][:3] == ("Emma", False, False)
    assert rotation(c) == "Emma"


def test_cancel_on_a_date_that_left_the_rule_gives_way(client, family):
    mom, emma, leo = family
    c, o = bins()
    OccurrenceException.objects.create(chore=c, date=d("2026-12-09"), is_cancelled=True)
    response = pmove(client, c.pk, date="2026-12-03", target="2026-12-09")
    assert messages_of(response) == ["Occurrence moved to Wed 9 Dec"]
    assert exceptions_of(c)["2026-12-09"].moved_from == d("2026-12-03")
    assert who(c, "2026-12-07", "2026-12-13") == [("2026-12-09", "Emma"), ("2026-12-10", "Leo")]

    OccurrenceException.objects.create(chore=c, date=d("2026-12-16"), is_cancelled=True)
    response = move(client, c, o, "2026-12-16")
    assert response.status_code == 302
    assert exceptions_of(c)["2026-12-16"].moved_from == d("2026-11-19")


def test_cancelled_own_values_date_gives_way(client, family):
    mom, emma, leo = family
    c, _ = bins()
    assert edit_projected_date(c, d("2026-12-10"), "Bins x", "new", datetime.time(9), 30, TODAY)
    cancel_projected_date(c, d("2026-12-10"))
    assert exceptions_of(c)["2026-12-10"].own_values == "edited"
    response = pmove(client, c.pk, date="2026-12-03", target="2026-12-10")
    assert messages_of(response) == ["Occurrence moved to Thu 10 Dec"]
    e = exceptions_of(c)["2026-12-10"]
    assert (e.moved_from, e.member_id, e.title, e.instructions, e.due_time, e.points, e.own_values) == (
        d("2026-12-03"), emma.pk, "Bins", "old", None, 10, "")


def test_38_edit_on_the_new_extra_keeps_it_an_extra(client, family):
    c, _, _ = bins_deleted_thu_10(client)
    pmove(client, c.pk, date="2026-12-03", target="2026-12-10")
    assert edit_projected_date(c, d("2026-12-10"), "Bins late", "old", None, 15, TODAY)
    e = exceptions_of(c)["2026-12-10"]
    assert (e.moved_from, e.own_values, e.title, e.points) == (d("2026-12-03"), "", "Bins late", 15)


def test_cancelled_override_gives_way_and_the_extra_keeps_the_source_member(client, family):
    mom, emma, leo = family
    c, _ = bins()
    set_projected_member(c, d("2026-12-10"), mom.pk)
    cancel_projected_date(c, d("2026-12-10"))
    pmove(client, c.pk, date="2026-12-03", target="2026-12-10")
    e = exceptions_of(c)["2026-12-10"]
    assert (e.member_id, e.moved_from) == (emma.pk, d("2026-12-03"))


def test_31_assign_on_the_extra_that_replaced_a_delete(client, family):
    mom, emma, leo = family
    c, _, _ = bins_deleted_thu_10(client)
    pmove(client, c.pk, date="2026-12-03", target="2026-12-10")
    before = exceptions_of(c)["2026-12-10"]
    with at(WED):
        response = client.post(f"/dashboard/assign/planned/{c.pk}/",
                               {"date": "2026-12-10", "assignee": str(leo.pk), "week": "2026-W50"})
    assert response.status_code == 302
    after = exceptions_of(c)["2026-12-10"]
    assert (after.pk, after.member_id, after.moved_from) == (before.pk, leo.pk, d("2026-12-03"))
    assert shown(client, "2026-W50", c) == [("Thu, Dec 10", "😀 Leo")]


def test_22_delete_of_the_extra_then_another_move_onto_it(client, family):
    mom, emma, leo = family
    c, _, _ = bins_deleted_thu_10(client)
    pmove(client, c.pk, date="2026-12-03", target="2026-12-10")
    token = deleted(client, c, when=WED, date="2026-12-10", scope="this")
    assert exceptions_of(c)["2026-12-10"].is_cancelled
    assert shown(client, "2026-W50", c) == []
    response = pmove(client, c.pk, date="2026-12-17", target="2026-12-10")
    assert messages_of(response)[-1:] == ["Occurrence moved to Thu 10 Dec"]
    found = exceptions_of(c)
    assert (found["2026-12-10"].moved_from, found["2026-12-10"].member_id) == (d("2026-12-17"), emma.pk)
    assert found["2026-12-17"].is_cancelled
    after = state()
    refused = undo_post(client, token, WED_5)
    assert refused.status_code == 400 and REFUSED in refused.content
    assert state() == after


def test_undo_first_then_the_move_is_refused(client, family):
    c, _, token = bins_deleted_thu_10(client)
    undone(client, token, when=WED_5)
    assert "2026-12-10" not in exceptions_of(c)
    before = state()
    response = pmove(client, c.pk, date="2026-12-03", target="2026-12-10")
    assert messages_of(response)[-1:] == ["This chore already has an occurrence on Thu 10 Dec."]
    assert state() == before


def _taken(c, mom, kind):
    if kind == "override":
        set_projected_member(c, d("2026-12-10"), mom.pk)
        return "2026-12-10", "Thu 10 Dec"
    if kind == "edited":
        edit_projected_date(c, d("2026-12-10"), "Bins x", "old", None, 10, TODAY)
        return "2026-12-10", "Thu 10 Dec"
    if kind == "kept":
        OccurrenceException.objects.create(
            chore=c, date=d("2026-12-10"), own_values="kept", is_override=True, member=mom,
            title="Bins", instructions="old", points=10)
        return "2026-12-10", "Thu 10 Dec"
    if kind == "extra-on-rule":
        extra(c, "2026-12-10", moved_from="2026-12-08")
        return "2026-12-10", "Thu 10 Dec"
    extra(c, "2026-12-12", moved_from="2026-12-17")
    return "2026-12-12", "Sat 12 Dec"


KINDS = ["override", "edited", "kept", "extra-on-rule", "extra"]


@pytest.mark.parametrize("kind", KINDS)
def test_non_cancelled_target_still_refuses_from_the_planned_card(client, family, kind):
    mom, emma, leo = family
    c, _ = bins()
    target, text = _taken(c, mom, kind)
    before = state()
    response = pmove(client, c.pk, date="2026-12-03", target=target, week="2026-W49")
    assert response.status_code == 302
    assert messages_of(response) == [f"This chore already has an occurrence on {text}."]
    assert state() == before


@pytest.mark.parametrize("kind", KINDS)
def test_non_cancelled_target_still_refuses_from_the_edit_page(client, family, kind):
    mom, emma, leo = family
    c, o = bins()
    target, text = _taken(c, mom, kind)
    before = state()
    response = move(client, c, o, target)
    assert response.status_code == 200
    assert date_errors(response) == [f"This chore already has an occurrence on {text}."]
    assert state() == before


# --- Limits, retry, race ---------------------------------------------------------------------------


def test_range_limits_come_before_the_give_way(client, family):
    c, o = bins()
    cancel_projected_date(c, d("2026-12-10"))
    Chore.objects.filter(pk=c.pk).update(end_date=d("2026-12-04"))
    before = state()
    response = pmove(client, c.pk, date="2026-12-03", target="2026-12-10")
    assert messages_of(response) == ["Pick a day from Mon 23 Nov to Fri 4 Dec."]
    response = move(client, c, o, "2026-12-10")
    assert date_errors(response) == ["Pick a day from Wed 18 Nov to Fri 4 Dec."]
    assert state() == before


def test_kiritimati_vs_utc(client, family):
    c, _ = bins()
    cancel_projected_date(c, d("2026-11-26"))
    before = state()
    # Sun 22 12:00 UTC is Mon 23 in Kiritimati: Thu 26 is in the current week there.
    response = pmove(client, c.pk, date="2026-12-03", target="2026-11-26",
                     when=SUN_NOON, tz="Pacific/Kiritimati")
    assert messages_of(response) == ["Pick a day from Mon 30 Nov to Thu 31 Dec 2099."]
    assert state() == before
    response = pmove(client, c.pk, date="2026-12-03", target="2026-11-26", when=SUN_NOON, tz="UTC")
    assert messages_of(response)[-1:] == ["Occurrence moved to Thu 26 Nov"]
    assert exceptions_of(c)["2026-11-26"].moved_from == d("2026-12-03")


def test_concurrent_cancel_on_the_target_gives_way_on_the_retry(family):
    mom, emma, leo = family
    c, _ = bins()
    real = services._planned_move_attempt
    calls = []

    def concurrent(*args):
        calls.append(args)
        if len(calls) == 1:
            cancel_projected_date(c, d("2026-12-05"))  # a concurrent cancel on T
            raise IntegrityError("unique_exception_per_chore_date")
        return real(*args)

    with mock.patch("chores.services._planned_move_attempt", side_effect=concurrent):
        assert move_projected_date(c, d("2026-12-03"), d("2026-12-05"), TODAY) is True
    assert len(calls) == 2
    e = exceptions_of(c)["2026-12-05"]
    assert (e.moved_from, e.member_id, e.is_cancelled) == (d("2026-12-03"), emma.pk, False)


def test_lost_race_on_the_delete_refuses(family):
    c, o = bins()
    cancel_projected_date(c, d("2026-12-10"))
    before = state()
    with mock.patch.object(QuerySet, "delete", return_value=(0, {})):
        with pytest.raises(MoveRefused) as refused:
            move_projected_date(c, d("2026-12-03"), d("2026-12-10"), TODAY)
        assert str(refused.value) == "This chore already has an occurrence on Thu 10 Dec."
        with pytest.raises(MoveRefused):
            move_occurrence(o, d("2026-12-10"), None, 10, "Bins", "old", TODAY)
    assert state() == before


def test_services_write_no_rotation_and_no_occurrence(family):
    c, o = bins()
    cancel_projected_date(c, d("2026-12-10"))
    cancel_projected_date(c, d("2026-12-17"))
    rows_before = list(ChoreOccurrence.objects.values())
    rotation_before = list(RotationState.objects.values())
    with mock.patch("chores.services.advance_rotation") as advance, \
            mock.patch("chores.services.create_occurrence") as create:
        assert move_projected_date(c, d("2026-12-03"), d("2026-12-10"), TODAY) is True
        assert move_projected_date(c, d("2026-12-10"), d("2026-12-12"), TODAY) is True
        assert move_occurrence(o, d("2026-12-17"), None, 10, "Bins", "old", TODAY) is True
    assert advance.call_count == 0 and create.call_count == 0
    assert list(RotationState.objects.values()) == rotation_before
    rows_before[0]["is_cancelled"] = True  # the stored-row path only sets is_cancelled
    assert list(ChoreOccurrence.objects.values()) == rows_before


def test_viewing_future_weeks_writes_nothing(client, family):
    c, _, _ = bins_deleted_thu_10(client)
    pmove(client, c.pk, date="2026-12-03", target="2026-12-10")
    pmove(client, c.pk, date="2026-12-10", target="2026-12-12")
    spy_, wrapper = no_writes()
    with wrapper:
        for week in ["2026-W49", "2026-W50", "2026-W51"]:
            dashboard(client, week)
    assert [w for w in spy_.writes if "django_session" not in w] == []


def test_projected_target_after_the_give_way_is_the_extra(family):
    mom, emma, leo = family
    c, _ = bins()
    cancel_projected_date(c, d("2026-12-10"))
    assert projected_occurrence(c, d("2026-12-10"), TODAY) is None
    move_projected_date(c, d("2026-12-03"), d("2026-12-10"), TODAY)
    p = projected_occurrence(c, d("2026-12-10"), TODAY)
    assert (p["member_id"], p["is_override"]) == (emma.pk, True)


# --- Query count and hostile input -------------------------------------------------------------------


def _move_queries(client, others):
    Chore.objects.all().delete()
    c, o = bins()
    cancel_projected_date(c, d("2026-12-10"))
    cancel_projected_date(c, d("2026-12-17"))
    for i in range(others):
        other = weekly(f"Other {i}", 3, "2026-11-02", points=10)
        cancel_projected_date(other, d("2026-12-10"))
        cancel_projected_date(other, d("2026-12-17"))
        extra(other, "2026-12-12", moved_from="2026-12-03")
    services.materialize_week(d("2026-11-16"), today=TODAY)
    planned = _count(lambda: pmove(client, c.pk, date="2026-12-03", target="2026-12-10"))
    again = _count(lambda: pmove(client, c.pk, date="2026-12-10", target="2026-12-12"))
    stored_move = _count(lambda: move(client, c, o, "2026-12-17"))
    assert exceptions_of(c)["2026-12-12"].moved_from == d("2026-12-03")
    assert exceptions_of(c)["2026-12-17"].moved_from == d("2026-11-19")
    return planned, again, stored_move


def test_queries_do_not_depend_on_other_chores(client, family):
    assert _move_queries(client, 0) == _move_queries(client, 10)


@pytest.mark.parametrize("bad", ["<script>", "x" * 10_000, "2026-W99", "-1", "2026-12-10'--", "\x00"])
def test_hostile_input_never_500(client, family, bad):
    c, o, _ = bins_deleted_thu_10(client)
    before = state()
    response = pmove(client, c.pk, date="2026-12-03", target=bad, week=bad, member=bad, day=bad)
    assert response.status_code in (302, 400)
    response = pmove(client, c.pk, date=bad, target="2026-12-10")
    assert response.status_code in (302, 400, 404)
    response = move(client, c, o, bad)
    assert response.status_code in (200, 400)
    assert state() == before
    response = pmove(client, c.pk, date="2026-12-03", target="2026-12-10", week=bad, member=bad, day=bad)
    assert response.status_code == 302
    assert exceptions_of(c)["2026-12-10"].moved_from == d("2026-12-03")
