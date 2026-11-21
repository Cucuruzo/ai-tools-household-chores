"""Mark done, skip and undo (#12)."""
import datetime
import html
import importlib
import inspect
import re
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import services, views
from chores.models import Chore, ChoreOccurrence, RotationState
from chores.profile import SESSION_KEY
from chores.services import materialize_week, set_occurrence_status
from tests.test_dashboard import (
    UTC, WED, at, blank_csrf, chore, d, instant, member, no_writes, page, sections,
)
from tests.test_delete import HOSTILE, OVERFLOW, SAT_NOON, controls, hidden, row, scopes, weekly

HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def client():
    return Client()


# --- Helpers ---------------------------------------------------------------------


def toggle(client, pk, action=None, when=WED, tz=None, htmx=False, **data):
    if tz is not None:
        client.cookies["tz"] = tz
    if action is not None:
        data["action"] = action
    with at(when):
        return client.post(f"/dashboard/toggle/{pk}/", data, **(HTMX if htmx else {}))


def activate(client, m):
    assert client.post(f"/profile/{m.pk}/").status_code == 302


def once(title="Job", day="2026-11-18", **kwargs):
    """A one-off chore on ``day`` with its stored todo row."""
    c = chore(title, start_date=day, **kwargs)
    return row(c, day)


def fields(o):
    o.refresh_from_db()
    return {f.attname: getattr(o, f.attname) for f in ChoreOccurrence._meta.concrete_fields}


def everything():
    """Every stored value a toggle could change."""
    return (
        sorted(ChoreOccurrence.objects.values_list(
            "id", "chore_id", "date", "due_time", "member_id", "status", "done_at", "is_override",
            "points", "is_cancelled", "completed_by_id")),
        sorted(Chore.objects.values_list("id", "title", "points", "end_date", "is_template")),
        sorted(RotationState.objects.values_list("id", "chore_id", "last_member_id")),
    )


BLOCK = re.compile(r'<div class="card-block" id="card-(\d+)">.*?</div>', re.S)
STATUS_FORM = re.compile(r'<form class="status"([^>]*)>(.*?)</form>', re.S)


def blocks(body):
    """{occurrence pk: the whole card-block html}."""
    return {int(m.group(1)): m.group(0) for m in BLOCK.finditer(body)}


def actions(fragment):
    m = STATUS_FORM.search(fragment)
    if m is None:
        return None
    return re.findall(r'<button type="submit" name="action" value="([a-z]+)"', m.group(2))


def label(fragment):
    return re.search(r'<span class="status">(.*?)</span>', fragment).group(1)


def credit(fragment):
    m = re.search(r'<p class="credit">(.*?)</p>', fragment, re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))) if m else None


# --- Model and migration -----------------------------------------------------------


def test_migration_name_and_dependency():
    module = importlib.import_module("chores.migrations.0008_choreoccurrence_completed_by")
    migration = module.Migration
    assert migration.dependencies == [("chores", "0007_chore_end_date_choreoccurrence_is_cancelled")]
    [op] = migration.operations
    assert type(op).__name__ == "AddField"
    assert (op.model_name, op.name) == ("choreoccurrence", "completed_by")
    assert op.field.null and op.field.blank


@pytest.mark.django_db
def test_completed_by_defaults_to_null():
    o = once()
    o.refresh_from_db()
    assert o.completed_by is None
    assert o.done_at is None


def test_service_takes_now_and_reads_no_clock():
    params = list(inspect.signature(set_occurrence_status).parameters)
    assert params == ["occurrence", "action", "credited_member", "now"]
    assert "timezone.now" not in inspect.getsource(set_occurrence_status)


# --- The four transitions ------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("start, action, status, done", [
    ("todo", "done", "done", True),
    ("todo", "skip", "skipped", False),
    ("done", "undo", "todo", False),
    ("skipped", "undo", "todo", False),
])
def test_transitions_change_only_status_fields(client, start, action, status, done):
    mom, emma = member("Mom", role="adult"), member("Emma")
    c = weekly(points=20, due_time=datetime.time(18, 0), end_date=d("2027-01-01"))
    o = row(c, "2026-11-18", status=start, member=emma, is_override=True,
            due_time=datetime.time(17, 0), points=10, completed_by=mom if start == "done" else None)
    other = row(c, "2026-11-25")
    activate(client, mom)
    before, chores_before, other_before = fields(o), everything()[1], fields(other)
    count = ChoreOccurrence.objects.count()
    with mock.patch("chores.views.materialize_week") as materialize:
        response = toggle(client, o.pk, action)
    assert response.status_code == 302
    materialize.assert_not_called()
    after = fields(o)
    assert after.pop("status") == status
    assert after.pop("done_at") == (instant(WED) if done else None)
    assert after.pop("completed_by_id") == (mom.pk if done else None)
    for key in ("status", "done_at", "completed_by_id"):
        before.pop(key)
    assert after == before
    assert everything()[1] == chores_before
    assert fields(other) == other_before
    assert ChoreOccurrence.objects.count() == count


@pytest.mark.django_db
def test_done_at_is_utc_under_viewer_zone(client):
    o = once(day="2026-11-22")
    assert toggle(client, o.pk, "done", when="2026-11-22T12:00:00", tz="Pacific/Kiritimati").status_code == 302
    o.refresh_from_db()
    assert o.done_at == datetime.datetime(2026, 11, 22, 12, tzinfo=UTC)
    assert o.done_at.utcoffset() == datetime.timedelta(0)


@pytest.mark.django_db
def test_last_action_value_wins(client):
    o = once()
    with at(WED):
        response = client.post(f"/dashboard/toggle/{o.pk}/", {"action": ["skip", "done"]})
    assert response.status_code == 302
    o.refresh_from_db()
    assert o.status == "done"


# --- Who gets the credit -----------------------------------------------------------


@pytest.mark.django_db
def test_credit_goes_to_active_profile_not_assignee(client):
    mom, emma = member("Mom", role="adult"), member("Emma")
    o = row(chore("Emma's", start_date="2026-11-18", mode="fixed", assigned_to=emma), "2026-11-18", member=emma)
    activate(client, mom)
    toggle(client, o.pk, "done")
    o.refresh_from_db()
    assert o.completed_by == mom
    assert o.member == emma


@pytest.mark.django_db
@pytest.mark.parametrize("assigned", [True, False])
def test_no_profile_is_not_credited(client, assigned):
    emma = member("Emma")
    o = row(chore(start_date="2026-11-18"), "2026-11-18", member=emma if assigned else None)
    response = toggle(client, o.pk, "done", htmx=True)
    o.refresh_from_db()
    assert (o.status, o.completed_by, o.member) == ("done", None, emma if assigned else None)
    assert credit(response.content.decode()) == "Done · not credited"


@pytest.mark.django_db
def test_stale_session_profile_counts_as_none(client):
    mom = member("Mom", role="adult")
    o = once()
    activate(client, mom)
    mom.delete()
    response = toggle(client, o.pk, "done")
    assert response.status_code == 302
    o.refresh_from_db()
    assert (o.status, o.completed_by) == ("done", None)
    assert SESSION_KEY not in client.session


@pytest.mark.django_db
def test_undo_needs_no_profile_or_the_same_one(client):
    mom, emma = member("Mom", role="adult"), member("Emma")
    o = once()
    activate(client, mom)
    toggle(client, o.pk, "done")
    activate(client, emma)
    toggle(client, o.pk, "undo")
    o.refresh_from_db()
    assert (o.status, o.done_at, o.completed_by) == ("todo", None, None)
    toggle(client, o.pk, "done")
    fresh = Client()
    assert toggle(fresh, o.pk, "undo").status_code == 302
    o.refresh_from_db()
    assert (o.status, o.done_at, o.completed_by) == ("todo", None, None)


# --- Rotation and points -------------------------------------------------------------


def _rotation_run(client, with_toggles):
    a, b = member("A", order=0), member("B", order=1)
    c = weekly("Rotate", weekday=None, recurrence="daily", start="2026-11-16",
               mode="rotating", eligibility="kids")
    page(client)  # materializes 2026-W47 with A/B turns
    rows = list(ChoreOccurrence.objects.filter(chore=c).order_by("date"))
    state = list(RotationState.objects.values_list("chore_id", "last_member_id"))
    if with_toggles:
        toggle(client, rows[0].pk, "skip")
        toggle(client, rows[1].pk, "done")
        toggle(client, rows[2].pk, "done")
        toggle(client, rows[2].pk, "undo")
        toggle(client, rows[3].pk, "skip")
        toggle(client, rows[3].pk, "undo")
    assert list(RotationState.objects.values_list("chore_id", "last_member_id")) == state
    new = materialize_week(d("2026-11-23"), today=d("2026-11-23"))
    names = [o.member.name for o in sorted(new, key=lambda o: o.date)]
    Chore.objects.filter(pk=c.pk).delete()
    a.delete()
    b.delete()
    return names


@pytest.mark.django_db
def test_rotation_untouched_against_control_run(client):
    with mock.patch("chores.services.advance_rotation", wraps=services.advance_rotation) as turns:
        toggled = _rotation_run(client, True)
    control = _rotation_run(Client(), False)
    assert toggled == control
    assert turns.call_count == 14  # only the two materializations took turns


@pytest.mark.django_db
def test_points_are_a_snapshot(client):
    c = chore("Ten", start_date="2026-11-18", points=10)
    page(client)  # materialized with 10 points
    o = ChoreOccurrence.objects.get(chore=c)
    Chore.objects.filter(pk=c.pk).update(points=20)
    for action in ("done", "undo", "done"):
        toggle(client, o.pk, action)
        o.refresh_from_db()
        assert o.points == 10


# --- Dates -----------------------------------------------------------------------


@pytest.mark.django_db
def test_done_early_from_upcoming(client):
    o = once(day="2026-11-20")
    assert [c["id"] for c in sections(page(client))["upcoming"]] == [o.pk]
    toggle(client, o.pk, "done")
    o.refresh_from_db()
    assert (o.status, o.done_at) == ("done", instant(WED))


@pytest.mark.django_db
@pytest.mark.parametrize("week", [None, "2026-W46"])
def test_past_week_overdue_done_from_both_views(client, week):
    o = once(day="2026-11-12", points=20)
    body = page(client, week)
    block = blocks(body)[o.pk]
    assert actions(block) == ["done", "skip"]
    assert label(block) == "Overdue"
    if week is None:
        assert [c["id"] for c in sections(body)["overdue"]] == [o.pk]
    data = hidden(STATUS_FORM.search(block).group(2))
    response = toggle(client, o.pk, "done", **data)
    assert response["Location"] == ("/dashboard/" if week is None else f"/dashboard/?week={week}")
    o.refresh_from_db()
    assert (o.status, o.done_at, o.points) == ("done", instant(WED), 20)
    # On reload it leaves the current Overdue section and shows on its own week.
    assert o.pk not in blocks(page(client))
    assert label(blocks(page(client, "2026-W46"))[o.pk]) == "Done"


@pytest.mark.django_db
def test_undo_past_week_done_is_overdue_again(client):
    o = row(chore(start_date="2026-11-12"), "2026-11-12", status="done")
    response = toggle(client, o.pk, "undo", htmx=True)
    assert label(response.content.decode()) == "Overdue"
    assert [c["id"] for c in sections(page(client))["overdue"]] == [o.pk]


@pytest.mark.django_db
def test_swapped_label_uses_viewer_zone(client):
    o = row(chore(start_date="2026-11-21"), "2026-11-21", status="done")
    response = toggle(client, o.pk, "undo", when=SAT_NOON, htmx=True)
    assert label(response.content.decode()) == "To do"
    toggle(client, o.pk, "done", when=SAT_NOON)
    response = toggle(client, o.pk, "undo", when=SAT_NOON, tz="Pacific/Kiritimati", htmx=True)
    assert label(response.content.decode()) == "Overdue"


# --- Deleted member ----------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("assignee_too", [False, True])
def test_deleted_credited_member(client, assignee_too):
    mom, emma = member("Mom", role="adult"), member("Emma")
    assignee = mom if assignee_too else emma
    o = row(chore(start_date="2026-11-18"), "2026-11-18", member=assignee)
    activate(client, mom)
    toggle(client, o.pk, "done")
    mom.delete()
    o.refresh_from_db()
    assert (o.status, o.done_at, o.completed_by) == ("done", instant(WED), None)
    block = blocks(page(client))[o.pk]
    assert credit(block) == "Done · not credited"
    response = toggle(client, o.pk, "undo", htmx=True)
    assert response.status_code == 200
    o.refresh_from_db()
    assert (o.status, o.done_at, o.completed_by) == ("todo", None, None)
    body = response.content.decode()
    assert label(body) == "To do"
    assert credit(body) is None
    [card] = sections(page(client))["today"]
    assert card["assignee"] == ("Unassigned" if assignee_too else "😀 Emma")


# --- Repeats, conflicts and races ------------------------------------------------------


@pytest.mark.django_db
def test_repeat_done_keeps_first_values(client):
    mom, emma = member("Mom", role="adult"), member("Emma")
    o = once()
    activate(client, mom)
    toggle(client, o.pk, "done")
    first = fields(o)
    activate(client, emma)
    spy, wrapper = no_writes()
    with wrapper:
        plain = toggle(client, o.pk, "done", when="2026-11-18T15:00:00")
        partial = toggle(client, o.pk, "done", when="2026-11-18T15:00:00", htmx=True)
    assert spy.writes == []
    assert plain.status_code == 302 and plain["Location"] == "/dashboard/"
    assert partial.status_code == 200
    assert credit(partial.content.decode()) == "Done by 😀 Mom"
    assert fields(o) == first
    assert first["completed_by_id"] == mom.pk and first["done_at"] == instant(WED)


@pytest.mark.django_db
@pytest.mark.parametrize("status, action", [("skipped", "skip"), ("todo", "undo")])
def test_other_no_ops_write_nothing(client, status, action):
    o = row(chore(start_date="2026-11-18"), "2026-11-18", status=status)
    before = everything()
    spy, wrapper = no_writes()
    with wrapper:
        assert toggle(client, o.pk, action).status_code == 302
        assert toggle(client, o.pk, action, htmx=True).status_code == 200
    assert spy.writes == []
    assert everything() == before


@pytest.mark.django_db
@pytest.mark.parametrize("status, action", [("done", "skip"), ("skipped", "done")])
def test_conflict_is_400(client, status, action):
    o = row(chore(start_date="2026-11-18"), "2026-11-18", status=status)
    before = everything()
    for htmx in (False, True):
        response = toggle(client, o.pk, action, htmx=htmx)
        assert response.status_code == 400
        assert "undo" in response.content.decode().lower()
    assert everything() == before


@pytest.mark.django_db
def test_service_update_is_conditional():
    mom = member("Mom", role="adult")
    o = once()
    stale = ChoreOccurrence.objects.get(pk=o.pk)  # read as todo
    assert set_occurrence_status(o, "done", mom, instant(WED)) is True
    assert set_occurrence_status(stale, "done", None, instant(SAT_NOON)) is False
    o.refresh_from_db()
    assert (o.done_at, o.completed_by) == (instant(WED), mom)
    cancelled = once("Gone", day="2026-11-19")
    ChoreOccurrence.objects.filter(pk=cancelled.pk).update(is_cancelled=True)
    assert set_occurrence_status(cancelled, "skip", None, instant(WED)) is False
    cancelled.refresh_from_db()
    assert cancelled.status == "todo"


def _race(change):
    def lose(occurrence, action, credited_member, now):
        change(occurrence)
        return False
    return mock.patch("chores.views.set_occurrence_status", side_effect=lose)


@pytest.mark.django_db
@pytest.mark.parametrize("change, expected", [
    (lambda o: ChoreOccurrence.objects.filter(pk=o.pk).update(status="done"), 200),
    (lambda o: ChoreOccurrence.objects.filter(pk=o.pk).update(status="skipped"), 400),
    (lambda o: ChoreOccurrence.objects.filter(pk=o.pk).update(is_cancelled=True), 404),
    (lambda o: Chore.objects.filter(pk=o.chore_id).delete(), 404),
])
def test_lost_race(client, change, expected):
    o = once()
    with _race(change):
        response = toggle(client, o.pk, "done", htmx=True)
    assert response.status_code == expected
    if expected == 200:
        assert actions(response.content.decode()) == ["undo"]


@pytest.mark.django_db
def test_lost_race_plain_no_op_redirects(client):
    o = once()
    with _race(lambda o: ChoreOccurrence.objects.filter(pk=o.pk).update(status="done")):
        response = toggle(client, o.pk, "done", member="unassigned")
    assert (response.status_code, response["Location"]) == (302, "/dashboard/?member=unassigned")


# --- 404s and errors -------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["done", "skip", "undo"])
def test_cancelled_row_is_404(client, action):
    o = row(weekly(), "2026-11-18", is_cancelled=True)
    before = everything()
    assert toggle(client, o.pk, action).status_code == 404
    assert toggle(client, o.pk, action, htmx=True).status_code == 404
    assert everything() == before


@pytest.mark.django_db
def test_deleted_occurrence_and_template_row_are_404(client):
    o = once()
    pk = o.pk
    Chore.objects.filter(pk=o.chore_id).delete()
    assert toggle(client, pk, "done").status_code == 404
    template = chore("Template", start_date="2026-11-18", is_template=True)
    t_row = row(template, "2026-11-18")
    before = everything()
    for action in ("done", "skip", "undo"):
        assert toggle(client, t_row.pk, action).status_code == 404
    assert everything() == before


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["0", OVERFLOW, "9223372036854775808", "424242", "abc", "-1", "1%00"])
def test_bad_pk_is_404(client, path):
    once()
    before = everything()
    with at(WED):
        response = client.post(f"/dashboard/toggle/{path}/", {"action": "done"})
    assert response.status_code == 404
    assert everything() == before


@pytest.mark.django_db
@pytest.mark.parametrize("value", [None, "", "DONE", "delete", " done", "done ", "x" * 10000, HOSTILE])
def test_bad_action_is_400_never_echoed(client, value):
    o = once()
    before = everything()
    for htmx in (False, True):
        response = toggle(client, o.pk, value, htmx=htmx)
        assert response.status_code == 400
        body = response.content.decode()
        assert len(body) < 100
        if value:
            assert value not in body
            assert html.escape(value) not in body
    assert everything() == before


@pytest.mark.django_db
def test_error_order(client):
    o = row(chore(start_date="2026-11-18"), "2026-11-18", status="done")
    assert toggle(client, 424242, "bad").status_code == 404  # pk before action
    assert toggle(client, o.pk, "bad").status_code == 400
    assert toggle(client, o.pk, "skip").status_code == 400  # conflict


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "put", "delete", "patch"])
def test_non_post_is_405(client, method):
    o = once()
    before = everything()
    assert getattr(client, method)(f"/dashboard/toggle/{o.pk}/").status_code == 405
    assert everything() == before


@pytest.mark.django_db
def test_post_without_csrf_is_403():
    o = once()
    before = everything()
    strict = Client(enforce_csrf_checks=True)
    with at(WED):
        response = strict.post(f"/dashboard/toggle/{o.pk}/", {"action": "done"}, **HTMX)
    assert response.status_code == 403
    assert everything() == before


@pytest.mark.django_db
def test_no_messages_on_any_path(client):
    o = once()
    responses = [
        toggle(client, o.pk, "done", htmx=True),
        toggle(client, o.pk, "done", htmx=True),
        toggle(client, o.pk, "skip", htmx=True),
        toggle(client, o.pk, "bad", htmx=True),
        toggle(client, o.pk, "undo"),
        toggle(client, o.pk, "skip"),
        toggle(client, 424242, "done"),
    ]
    for response in responses:
        assert list(get_messages(response.wsgi_request)) == []
    with at(WED):
        after = client.get("/dashboard/")
    assert list(get_messages(after.wsgi_request)) == []
    assert 'class="message"' not in after.content.decode()


# --- Card markup -----------------------------------------------------------------------


@pytest.fixture
def week_of_cards(db):
    kid = member("Kid")
    return {
        "overdue_old": once("Old", day="2026-11-12"),
        "today": once("Today", day="2026-11-18"),
        "upcoming": once("Later", day="2026-11-20"),
        "done_earlier": row(chore("Done", start_date="2026-11-17"), "2026-11-17", status="done",
                            completed_by=kid),
        "skipped": row(chore("Skipped", start_date="2026-11-18"), "2026-11-18", status="skipped"),
        "future": once("Future", day="2026-11-25"),
    }


@pytest.mark.django_db
def test_form_buttons_per_status_and_section(client, week_of_cards):
    rows = week_of_cards
    body = page(client)
    found = sections(body)
    assert [c["id"] for c in found["overdue"]] == [rows["overdue_old"].pk]
    assert [c["id"] for c in found["done-earlier"]] == [rows["done_earlier"].pk]
    current = blocks(body)
    for key in ("overdue_old", "today", "upcoming"):
        assert actions(current[rows[key].pk]) == ["done", "skip"], key
    for key in ("done_earlier", "skipped"):
        assert actions(current[rows[key].pk]) == ["undo"], key
    assert actions(blocks(page(client, "2026-W46"))[rows["overdue_old"].pk]) == ["done", "skip"]
    assert actions(blocks(page(client, "2026-W48"))[rows["future"].pk]) == ["done", "skip"]


@pytest.mark.django_db
def test_projected_cards_have_no_form_or_wrapper(client):
    weekly("Projected", weekday=2)
    body = page(client, "2026-W48")
    [card] = sections(body)["week"]
    assert card["id"] is None and card["status"] == "Planned"
    assert "card-block" not in body
    assert '<form class="status"' not in body


@pytest.mark.django_db
def test_block_order_and_form_markup(client):
    o = row(weekly(), "2026-11-18")
    block = blocks(page(client))[o.pk]
    assert re.fullmatch(
        rf'<div class="card-block" id="card-{o.pk}">\s*'
        rf'<article class="chore-card" id="occurrence-{o.pk}">.*?</article>\s*'
        r'<details class="delete-confirm">.*?</details>\s*'
        r'<form class="status" [^>]*>.*?</form>\s*</div>', block, re.S)
    attrs = STATUS_FORM.search(block).group(1)
    url = f"/dashboard/toggle/{o.pk}/"
    assert attrs == (f' method="post" action="{url}" hx-post="{url}" '
                     f'hx-target="#card-{o.pk}" hx-swap="outerHTML"')
    form = STATUS_FORM.search(block).group(2)
    assert 'name="csrfmiddlewaretoken"' in form
    assert '<button type="submit" name="action" value="done">Done</button>' in form
    assert '<button type="submit" name="action" value="skip" class="secondary">Skip</button>' in form
    assert controls(page(client))[o.pk]  # the #5 regex still finds its Delete


@pytest.mark.django_db
def test_status_form_hidden_filter_inputs(client):
    mom = member("Mom", role="adult")
    past = row(chore(start_date="2026-11-12"), "2026-11-12", member=mom)
    now = once("Now")

    def inputs(body, pk):
        return hidden(STATUS_FORM.search(blocks(body)[pk]).group(2))

    assert inputs(page(client), now.pk) == {}
    assert inputs(page(client), past.pk) == {}
    assert inputs(page(client, "2026-W46", data={"member": str(mom.pk), "day": "4"}), past.pk) == {
        "week": "2026-W46", "member": str(mom.pk), "day": "4"}
    assert inputs(page(client, data={"member": "unassigned"}), now.pk) == {"member": "unassigned"}


@pytest.mark.django_db
def test_credit_line(client):
    bo = member("<b>Bo</b>", avatar="🐱")
    credited = row(chore("A", start_date="2026-11-18"), "2026-11-18", status="done", completed_by=bo)
    nobody = row(chore("B", start_date="2026-11-18"), "2026-11-18", status="done")
    todo = once("C")
    skipped = row(chore("D", start_date="2026-11-18"), "2026-11-18", status="skipped")
    body = page(client)
    found = {c["id"]: c for c in sections(body)["today"]}
    assert credit(found[credited.pk]["html"]) == "Done by 🐱 <b>Bo</b>"
    assert "&lt;b&gt;Bo&lt;/b&gt;" in found[credited.pk]["html"]
    assert "<b>Bo</b>" not in body
    assert credit(found[nobody.pk]["html"]) == "Done · not credited"
    assert found[credited.pk]["status"] == found[nobody.pk]["status"] == "Done"
    assert credit(found[todo.pk]["html"]) is None
    assert credit(found[skipped.pk]["html"]) is None
    # Credit sits after the date/points/status line, inside the <article>.
    assert re.search(r'<span class="status">Done</span>\s*</small>\s*</p>\s*<p class="credit">',
                     found[credited.pk]["html"])


@pytest.mark.django_db
def test_done_cards_do_not_add_queries(client):
    def count(n):
        for i in range(n):
            m = member(f"M{i}")
            row(chore(f"Q{i}", start_date="2026-11-18"), "2026-11-18", status="done", completed_by=m)
        page(client)  # materialize first
        with CaptureQueriesContext(connection) as queries:
            page(client)
        ChoreOccurrence.objects.all().delete()
        Chore.objects.filter(is_template=False).delete()
        return len(queries)

    assert count(1) == count(4)


# --- HTMX response -------------------------------------------------------------------


@pytest.mark.django_db
def test_htmx_response_is_one_block(client):
    o = row(weekly(), "2026-11-18")
    response = toggle(client, o.pk, "done", htmx=True)
    assert response.status_code == 200
    body = response.content.decode().strip()
    assert body.startswith(f'<div class="card-block" id="card-{o.pk}">')
    assert body.endswith("</div>")
    assert "<html" not in body and "<header" not in body and "profile-switcher" not in body
    assert "<main" not in body and "dashboard-filters" not in body
    assert body.count("<article") == 1
    assert body.count('<details class="delete-confirm">') == 1
    assert body.count('<form class="status"') == 1
    assert actions(body) == ["undo"]


@pytest.mark.django_db
@pytest.mark.parametrize("recurring", [True, False])
def test_delete_scopes_follow_the_swap(client, recurring):
    c = weekly() if recurring else chore(start_date="2026-11-18")
    o = row(c, "2026-11-18")
    todo_scopes = ["this", "future"] if recurring else ["future"]
    assert scopes(controls(page(client))[o.pk]) == todo_scopes
    for action, expected in (("done", ["future"]), ("undo", todo_scopes),
                             ("skip", ["future"]), ("undo", todo_scopes)):
        body = toggle(client, o.pk, action, htmx=True).content.decode()
        assert scopes(controls(body)[o.pk]) == expected, action
        assert actions(body) == (["undo"] if action != "undo" else ["done", "skip"])


@pytest.mark.django_db
@pytest.mark.parametrize("week, data", [
    (None, {}),
    ("2026-W46", {"day": "4"}),
    ("2026-W48", {}),
    (None, {"member": "unassigned"}),
])
def test_swapped_block_equals_full_page_block(client, week, data):
    mom = member("Mom", role="adult")
    day = {None: "2026-11-18", "2026-W46": "2026-11-12", "2026-W48": "2026-11-25"}[week]
    o = row(weekly(weekday=d(day).weekday()), day)
    if "member" not in data:
        ChoreOccurrence.objects.filter(pk=o.pk).update(member=mom)
    activate(client, mom)
    params = dict(data, **({"week": week} if week else {}))
    form = STATUS_FORM.search(blocks(page(client, week, data=data))[o.pk]).group(2)
    assert hidden(form) == params
    for action in ("done", "undo", "skip", "undo"):
        swapped = toggle(client, o.pk, action, htmx=True, **params).content.decode()
        full = blocks(page(client, week, data=data))[o.pk]
        assert blank_csrf(swapped).strip() == blank_csrf(full).strip(), action


# --- No-JS redirect -------------------------------------------------------------------


@pytest.mark.django_db
def test_redirect_examples(client):
    mom, emma = member("Mom", role="adult"), member("Emma")
    o = once()
    cases = [
        ({"week": "2026-W46", "member": str(mom.pk), "day": "4"},
         f"/dashboard/?week=2026-W46&member={mom.pk}&day=4"),
        ({}, "/dashboard/"),
        ({"member": str(emma.pk)}, f"/dashboard/?member={emma.pk}"),
    ]
    for data, expected in cases:
        for action in ("done", "undo"):
            response = toggle(client, o.pk, action, **data)
            assert (response.status_code, response["Location"]) == (302, expected)


@pytest.mark.django_db
def test_redirect_from_old_overdue_card_goes_to_current_week(client):
    o = once(day="2026-11-12")
    form = STATUS_FORM.search(blocks(page(client))[o.pk]).group(2)
    response = toggle(client, o.pk, "done", **hidden(form))
    assert response["Location"] == "/dashboard/"


@pytest.mark.django_db
@pytest.mark.parametrize("extra", [
    {"week": "2026-W99"}, {"week": HOSTILE}, {"member": "abc"}, {"member": "deleted"}, {"day": "9"},
    {"week": "//evil.example"}, {"member": "https://evil.example"},
])
def test_redirect_drops_bad_values(client, extra):
    if extra.get("member") == "deleted":
        gone = member("Gone")
        extra = {"member": str(gone.pk)}
        gone.delete()
    o = once()
    response = toggle(client, o.pk, "done", **extra)
    assert (response.status_code, response["Location"]) == (302, "/dashboard/")


@pytest.mark.django_db
def test_chore_delete_uses_shared_redirect():
    source = inspect.getsource(views.chore_delete)
    assert source.rstrip().endswith("return _dashboard_redirect(request.POST)")
    assert "dashboard_query" not in source
