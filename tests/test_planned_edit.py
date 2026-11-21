"""Edit a Planned date in a later week (#38): "This occurrence only" on D, and
"this and future" from D.

Examples use today = Wed 18 Nov 2026 (UTC), current week Mon 16 - Sun 22 Nov,
and the members Mom (adult, order 0), Emma (kid, 1) and Leo (kid, 2).
"Bins" is weekly on Thursday, rotating kids, start 1 Nov, 10 pts,
instructions "old", no due time. Thu 19 Nov is stored for Emma, so
RotationState is Emma. Projected: Thu 26 Nov Leo, Thu 3 Dec Emma, Thu 10 Dec
Leo, Thu 17 Dec Emma.
"""
import datetime
import html
import importlib
import inspect
import re

import pytest
from django.db import connection, migrations
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import services, undo
from chores.models import Chore, ChoreOccurrence, Member, OccurrenceException
from chores.services import (
    MAX_KEPT_DATES, FutureEditRefused, apply_future_edit, edit_projected_date,
    project_occurrences, projected_occurrence,
)
from tests.test_calendar import get as calendar_get
from tests.test_dashboard import at, chore, d, member, no_writes
from tests.test_edit_scope import HREF, NOTE, OCC_FORM, CHORE_FORM, SECTION, form_of, weekly
from tests.test_planned_move import (
    bins, bins_cards, dashboard, exceptions_of, follow, pmove, projected, rotation, stored, who,
)
from tests.test_projection import KEYS
from tests.test_toggle import HTMX
from tests.test_undo_delete import deleted, state, undone

pytestmark = pytest.mark.django_db

WED = "2026-11-18T12:00:00"
WED_5 = "2026-11-18T12:05:00"
WED_25 = "2026-11-25T12:00:00"
WED_2 = "2026-12-02T12:00:00"
WED_9 = "2026-12-09T12:00:00"
WED_16 = "2026-12-16T12:00:00"
WED_23 = "2026-12-23T12:00:00"
SUN_NOON = "2026-11-22T12:00:00"  # Mon 23 Nov 02:00 in Kiritimati (UTC+14)
KIRI = "Pacific/Kiritimati"
TODAY = d("2026-11-18")
MONDAY = d("2026-11-16")
SCHEDULE = "The schedule can only change from today. Use Edit from today."
MESSAGE = '<p class="message" role="status"><mark>{}</mark></p>'
HIDDEN = re.compile(r'<input type="hidden" name="((?!csrf)[^"]+)" value="([^"]*)">')
NONFIELD = re.compile(r'<ul class="errorlist nonfield"><li>(.*?)</li></ul>')


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def family():
    return member("Mom", "adult", 0), member("Emma", "kid", 1), member("Leo", "kid", 2)


# --- Helpers ---------------------------------------------------------------------


def url(c, query=""):
    return f"/chores/{c.pk}/edit/{query}"


def get(client, c, query="", when=WED, tz=None):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        return client.get(url(c, query))


def page(client, c, query="", **kwargs):
    response = get(client, c, query, **kwargs)
    assert response.status_code == 200, response.content[:500]
    return response.content.decode()


def post(client, c, data, when=WED, tz=None):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        return client.post(url(c), data)


def this(client, c, planned="2026-12-10", title="Bins", instructions="old", due_time="",
         points="10", when=WED, tz=None, **extra):
    data = {"scope": "this", "planned": planned, "title": title, "instructions": instructions,
            "due_time": due_time, "points": points, **extra}
    return post(client, c, data, when=when, tz=tz)


def future(client, c, planned="2026-12-10", when=WED, tz=None, extra=None, **overrides):
    c.refresh_from_db()
    data = form_of(c, **overrides)
    data["scope"] = "future"
    if planned is not None:
        data["planned"] = planned
    data.update(extra or {})
    return post(client, c, data, when=when, tz=tz)


def big_bins(client, c):
    """"This occurrence only" on Thu 10 Dec: "Bins big", 18:00, 20 pts."""
    response = this(client, c, title="Bins big", due_time="18:00", points="20", week="2026-W50")
    assert response.status_code == 302, response.content[:2000]
    return response


def shown(c, first, last, today="2026-11-18"):
    names = dict(Member.objects.values_list("pk", "name"))
    return [(str(p["date"]), p["title"], names.get(p["member_id"]), p["points"], p["due_time"],
             p["is_override"]) for p in projected(c, first, last, today)]


def exception_row(e):
    names = dict(Member.objects.values_list("pk", "name"))
    return (e.own_values, e.is_override, names.get(e.member_id), e.is_cancelled, e.title,
            e.instructions, e.points, e.due_time)


def kept_row(member=None, override=False, title="Bins", points=10):
    return ("kept", override, member, False, title, "old", points, None)


def week_by_week(client, *whens):
    for when in whens:
        dashboard(client, when=when)


def _count(run):
    with CaptureQueriesContext(connection) as queries:
        run()
    return len(queries)


# --- Migration -------------------------------------------------------------------


def test_migration_0012_adds_only_own_values():
    module = importlib.import_module("chores.migrations.0012_occurrenceexception_own_values")
    migration = module.Migration
    assert migration.dependencies == [("chores", "0011_occurrenceexception_extra")]
    [operation] = migration.operations
    assert isinstance(operation, migrations.AddField)
    assert (operation.model_name, operation.name) == ("occurrenceexception", "own_values")
    field = operation.field
    assert (field.max_length, field.blank, field.default) == (6, True, "")
    assert list(field.choices) == [("edited", "Edited"), ("kept", "Kept")]


def test_own_values_defaults_to_empty(family):
    c, _ = bins()
    e = OccurrenceException.objects.create(chore=c, date=d("2026-12-03"), is_cancelled=True)
    assert e.own_values == ""


# --- Entry point: the Planned card link --------------------------------------------


def test_planned_card_links_with_planned(client, family):
    c, _ = bins()
    with at(WED):
        body = client.get("/dashboard/", {"week": "2026-W48"}).content.decode()
        filtered = client.get("/dashboard/", {"week": "2026-W48", "day": "4"}).content.decode()
    assert HREF.findall(body) == [f"/chores/{c.pk}/edit/?week=2026-W48&amp;planned=2026-11-26"]
    assert HREF.findall(filtered) == [f"/chores/{c.pk}/edit/?week=2026-W48&amp;day=4&amp;planned=2026-11-26"]


def test_htmx_swapped_planned_card_has_the_same_link(client, family):
    mom = family[0]
    c, _ = bins()
    with at(WED):
        fresh = HREF.findall(client.get("/dashboard/", {"week": "2026-W48", "day": "4"}).content.decode())
        swapped = client.post(f"/dashboard/assign/planned/{c.pk}/",
                              {"date": "2026-11-26", "assignee": str(mom.pk), "week": "2026-W48",
                               "day": "4"}, **HTMX)
    assert swapped.status_code == 200
    assert HREF.findall(swapped.content.decode()) == fresh
    assert "planned=2026-11-26" in fresh[0]


def test_one_off_planned_and_stored_cards_are_unchanged(client, family):
    one = chore("Once", start_date="2026-11-26")
    c, o = bins()
    with at(WED):
        w48 = client.get("/dashboard/", {"week": "2026-W48"}).content.decode()
        current = client.get("/dashboard/").content.decode()
    assert f"/chores/{one.pk}/edit/?week=2026-W48" in HREF.findall(w48)
    assert [html.unescape(h) for h in HREF.findall(current)] == [f"/chores/{c.pk}/edit/?occurrence={o.pk}"]


# --- The edit page anchored at a Planned date ----------------------------------------------


def test_get_shows_this_occurrence_only_without_a_date_field(client, family):
    c, _ = bins()
    spy_, wrapper = no_writes()
    with wrapper:
        body = page(client, c, "?week=2026-W50&planned=2026-12-10")
    assert [w for w in spy_.writes if "django_session" not in w] == []
    [section] = SECTION.findall(body)
    assert "<h2>This occurrence only</h2>" in section
    assert '<time datetime="2026-12-10">Thu 10 Dec</time>' in section
    [occ_form] = OCC_FORM.findall(body)
    for name, label in [("title", "Title"), ("instructions", "Instructions"),
                        ("due_time", "Due time"), ("points", "Points")]:
        assert f'<label for="occ_{name}">{label}</label>' in occ_form
        assert f'id="occ_{name}"' in occ_form
    assert 'id="occ_date"' not in body and 'name="date"' not in occ_form and ">Date<" not in occ_form
    assert 'value="Bins"' in occ_form and "\nold</textarea>" in occ_form
    assert '<option value="10" selected>' in occ_form
    assert dict(HIDDEN.findall(occ_form)) == {"planned": "2026-12-10", "week": "2026-W50"}
    [chore_form] = CHORE_FORM.findall(body)
    assert dict(HIDDEN.findall(chore_form)) == {"planned": "2026-12-10", "week": "2026-W50"}
    [note] = NOTE.findall(body)
    assert re.sub(r"<[^>]+>", "", note) == "Changes apply from Thu 10 Dec. Earlier occurrences keep their values."
    assert '<a id="edit-from-today" href="/chores/{}/edit/?week=2026-W50">Edit from today</a>'.format(c.pk) in body
    assert body.index('id="scope-note"') < body.index('id="edit-from-today"')
    assert f'name="next" value="/chores/{c.pk}/edit/?week=2026-W50&amp;planned=2026-12-10"' in body


def test_edit_from_today_link_keeps_every_filter(client, family):
    emma = family[1]
    c, _ = bins()
    body = page(client, c, f"?week=2026-W50&member={emma.pk}&day=4&planned=2026-12-10")
    expected = f"/chores/{c.pk}/edit/?week=2026-W50&amp;member={emma.pk}&amp;day=4"
    assert f'<a id="edit-from-today" href="{expected}">' in body
    plain = page(client, c, "?planned=2026-12-10")
    assert f'<a id="edit-from-today" href="/chores/{c.pk}/edit/">' in plain
    assert "edit-from-today" not in page(client, c)


def test_projected_values_fill_the_form(client, family):
    c, _ = bins()
    big_bins(client, c)
    [occ_form] = OCC_FORM.findall(page(client, c, "?planned=2026-12-10"))
    assert 'value="Bins big"' in occ_form and 'value="18:00"' in occ_form
    assert '<option value="20" selected>' in occ_form


HOSTILE_PLANNED = ["2026-11-19", "2026-12-04", "2026-12-03", "abc", "", "2026-11-12", "2026-12-10T00:00",
                   "2026-02-30", "1999-12-02", "٢٠٢٦-١٢-١٠", "2026-12-10 ", "9" * 10_000]


@pytest.mark.parametrize("raw", HOSTILE_PLANNED, ids=lambda v: v[:12] or "empty")
def test_not_a_planned_date_is_404_on_get_and_post(client, family, raw):
    c, _ = bins()
    with at(WED):
        client.post(f"/chores/{c.pk}/delete/", {"date": "2026-12-03", "scope": "this"})  # cancelled
    before = state()
    assert get(client, c, "?planned=" + raw).status_code == 404
    assert this(client, c, planned=raw, title="X").status_code == 404
    assert future(client, c, planned=raw, title="X").status_code == 404
    assert state() == before


def test_occurrence_and_planned_is_400(client, family):
    c, o = bins()
    before = state()
    assert get(client, c, f"?occurrence={o.pk}&planned=2026-12-10").status_code == 400
    response = this(client, c, occurrence=str(o.pk), title="X")
    assert response.status_code == 400
    assert response.content == b"Give an occurrence or a planned date, not both"
    assert future(client, c, extra={"occurrence": str(o.pk)}, title="X").status_code == 400
    assert state() == before


def test_one_off_chore_ignores_planned(client, family):
    one = chore("Once", start_date="2026-12-10")
    body = page(client, one, "?planned=2026-12-10")
    assert "this-occurrence" not in body and "edit-from-today" not in body
    assert page(client, one, "?planned=abc")
    data = form_of(one, title="Once more")
    data.update(planned="abc")
    assert post(client, one, data).status_code == 302
    one.refresh_from_db()
    assert one.title == "Once more"


def test_template_and_hostile_pk_still_404(client, family):
    t = chore("Tpl", is_template=True, recurrence="weekly", recurrence_day=3)
    assert get(client, t, "?planned=2026-12-10").status_code == 404


# --- "This occurrence only" on D ---------------------------------------------------------


def test_this_occurrence_only_bins_example(client, family):
    c, _ = bins()
    response = big_bins(client, c)
    assert response["Location"] == f"/chores/{c.pk}/edit/?week=2026-W50&planned=2026-12-10"
    assert MESSAGE.format("Occurrence saved") in follow(client, response)
    [e] = OccurrenceException.objects.filter(chore=c)
    assert str(e.date) == "2026-12-10"
    assert exception_row(e) == ("edited", True, "Leo", False, "Bins big", "old", 20, datetime.time(18))
    assert e.moved_from is None
    assert ChoreOccurrence.objects.filter(chore=c).count() == 1 and rotation(c) == "Emma"
    assert [(x["title"], x["assignee"], x["time"], x["points"])
            for x in bins_cards(dashboard(client, "2026-W50"), c)] == [("Bins big", "😀 Leo", "18:00", "20 pts")]
    assert shown(c, "2026-11-23", "2026-12-20") == [
        ("2026-11-26", "Bins", "Leo", 10, None, False),
        ("2026-12-03", "Bins", "Emma", 10, None, False),
        ("2026-12-10", "Bins big", "Leo", 20, datetime.time(18), True),
        ("2026-12-17", "Bins", "Emma", 10, None, False),
    ]
    assert all(set(p) == KEYS for p in projected(c, "2026-11-23", "2026-12-20"))
    week_by_week(client, WED_25, WED_2, WED_9)
    assert stored(c)["2026-12-10"] == ("Leo", False, True, "Bins big", "old", 20, datetime.time(18))
    assert rotation(c) == "Leo"
    assert [(x["title"], x["assignee"], x["status"]) for x in bins_cards(dashboard(client, when=WED_9), c)
            if x["date"] == "Thu, Dec 10"] == [("Bins big", "😀 Leo", "To do")]
    assert not OccurrenceException.objects.filter(chore=c).exists()


def test_equal_values_write_nothing(client, family):
    c, _ = bins()
    spy_, wrapper = no_writes()
    with wrapper:
        response = this(client, c)
    assert response.status_code == 302
    assert [w for w in spy_.writes if "django_session" not in w] == []
    assert not OccurrenceException.objects.exists()


def test_posted_date_is_ignored(client, family):
    c, _ = bins()
    response = this(client, c, title="Bins big", date="2026-12-12")
    assert response.status_code == 302
    assert set(exceptions_of(c)) == {"2026-12-10"}
    assert this(client, c, title="Bins bigger", date="abc").status_code == 302
    assert exceptions_of(c)["2026-12-10"].title == "Bins bigger"


def test_invalid_values_rerender_without_writing(client, family):
    c, _ = bins()
    before = state()
    response = this(client, c, title="", points="7")
    assert response.status_code == 200
    [occ_form] = OCC_FORM.findall(response.content.decode())
    assert "errorlist" in occ_form and 'id="occ_date"' not in occ_form
    assert state() == before


def test_this_only_on_an_extra_changes_only_its_values(client, family):
    c, _ = bins()
    assert pmove(client, c.pk, date="2026-12-03", target="2026-12-05").status_code == 302
    before = exceptions_of(c)["2026-12-05"]
    assert this(client, c, planned="2026-12-05", title="Bins Sat", points="5").status_code == 302
    after = exceptions_of(c)["2026-12-05"]
    assert (after.pk, after.moved_from, after.own_values, after.is_override, after.member_id) == (
        before.pk, before.moved_from, "", True, before.member_id)
    assert (after.title, after.points) == ("Bins Sat", 5)


def test_this_only_on_a_31_override_keeps_its_member(client, family):
    mom = family[0]
    c, _ = bins()
    with at(WED):
        client.post(f"/dashboard/assign/planned/{c.pk}/", {"date": "2026-12-10", "assignee": str(mom.pk)})
    assert this(client, c, title="Bins big").status_code == 302
    assert exception_row(exceptions_of(c)["2026-12-10"])[:3] == ("edited", True, "Mom")


def test_this_only_on_a_kept_date_freezes_its_member(client, family):
    c, _ = bins()
    assert future(client, c, title="Recycling").status_code == 302
    assert this(client, c, planned="2026-11-26", title="Bins old").status_code == 302
    assert exception_row(exceptions_of(c)["2026-11-26"]) == (
        "edited", True, "Leo", False, "Bins old", "old", 10, None)


def test_service_refuses_what_is_not_a_planned_date(family):
    c, _ = bins()
    one = chore("Once", start_date="2026-12-10")
    for target, day in [(c, "2026-11-19"), (c, "2026-12-04"), (c, "2026-11-12"), (one, "2026-12-10")]:
        assert edit_projected_date(target, d(day), "X", "", None, 10, TODAY) is False
    assert not OccurrenceException.objects.exists()
    assert edit_projected_date(c, d("2026-12-10"), "X", "old", None, 10, TODAY) is True
    assert exceptions_of(c)["2026-12-10"].own_values == "edited"


# --- "This and future" from D: values -------------------------------------------------------


def test_future_from_thu_10_keeps_earlier_values(client, family):
    c, _ = bins()
    response = future(client, c, title="Recycling", points="20", week="2026-W50")
    assert response.status_code == 302, response.content[:2000]
    assert response["Location"] == f"/chores/{c.pk}/edit/?week=2026-W50&planned=2026-12-10"
    assert MESSAGE.format("Chore saved") in follow(client, response)
    c.refresh_from_db()
    assert (c.title, c.points, c.start_date) == ("Recycling", 20, d("2026-11-01"))
    found = exceptions_of(c)
    assert set(found) == {"2026-11-26", "2026-12-03"}
    assert [exception_row(found[k]) for k in sorted(found)] == [kept_row(), kept_row()]
    assert shown(c, "2026-11-23", "2026-12-20") == [
        ("2026-11-26", "Bins", "Leo", 10, None, False),
        ("2026-12-03", "Bins", "Emma", 10, None, False),
        ("2026-12-10", "Recycling", "Leo", 20, None, False),
        ("2026-12-17", "Recycling", "Emma", 20, None, False),
    ]
    assert stored(c)["2026-11-19"][3:6] == ("Bins", "old", 10)  # the current week is untouched
    # A later "this and future" from today with 5 pts clears both kept dates.
    assert future(client, c, planned=None, points="5").status_code == 302
    assert exceptions_of(c) == {}
    assert [(day, title, points) for day, title, _, points, _, _ in shown(c, "2026-11-23", "2026-12-20")] == [
        ("2026-11-26", "Recycling", 5), ("2026-12-03", "Recycling", 5),
        ("2026-12-10", "Recycling", 5), ("2026-12-17", "Recycling", 5)]


def test_kept_dates_are_stored_as_follower_rows(client, family):
    c, _ = bins()
    future(client, c, title="Recycling", points="20")
    week_by_week(client, WED_25)
    assert stored(c)["2026-11-26"] == ("Leo", False, False, "Bins", "old", 10, None)
    # A follower row: a later edit from today rewrites it.
    future(client, c, planned=None, when=WED_25, points="5")
    assert stored(c)["2026-11-26"][3:6] == ("Recycling", "old", 5)


def test_earlier_future_edit_clears_later_kept_values(client, family):
    c, _ = bins()
    future(client, c, title="Recycling", points="20")
    assert future(client, c, planned="2026-12-03", title="Paper").status_code == 302
    assert set(exceptions_of(c)) == {"2026-11-26"}
    assert [(day, title, points) for day, title, _, points, _, _ in shown(c, "2026-11-23", "2026-12-13")] == [
        ("2026-11-26", "Bins", 10), ("2026-12-03", "Paper", 20), ("2026-12-10", "Paper", 20)]


def test_no_value_or_assignment_change_pins_nothing(client, family):
    c, _ = bins()
    before = state()
    response = future(client, c)
    assert response.status_code == 302
    assert state() == before


def test_value_change_is_compared_with_the_stored_chore(family):
    c, _ = bins()
    old = {name: getattr(c, name) for name in services.RULE_FIELDS + services.ASSIGNMENT_FIELDS}
    c.instructions = "new"
    apply_future_edit(c, old, d("2026-12-10"), TODAY)
    assert [exception_row(e) for e in OccurrenceException.objects.order_by("date")] == [kept_row(), kept_row()]


# --- "This and future" from D: assignment -------------------------------------------------


def test_future_from_thu_10_assignment_change_everyone_example(client, family):
    c, _ = bins()
    assert future(client, c, eligibility="everyone").status_code == 302
    found = exceptions_of(c)
    assert {k: exception_row(v)[:4] for k, v in found.items()} == {
        "2026-11-26": ("", True, "Leo", False), "2026-12-03": ("", True, "Emma", False)}
    expected = [("2026-11-26", "Leo"), ("2026-12-03", "Emma"), ("2026-12-10", "Emma"),
                ("2026-12-17", "Leo"), ("2026-12-24", "Mom")]
    assert who(c, "2026-11-23", "2026-12-27") == expected
    # Emma has Thu 3 and Thu 10 back to back (accepted cost), and that is what gets stored.
    week_by_week(client, WED_25, WED_2, WED_9, WED_16, WED_23)
    rows = stored(c)
    assert [(day, rows[day][0]) for day, _ in expected] == expected
    assert [rows[day][2] for day, _ in expected] == [True, True, False, False, False]


def test_value_and_assignment_change_pins_both(client, family):
    c, _ = bins()
    assert future(client, c, title="Recycling", eligibility="everyone").status_code == 302
    found = exceptions_of(c)
    assert exception_row(found["2026-11-26"]) == kept_row("Leo", override=True)
    assert exception_row(found["2026-12-03"]) == kept_row("Emma", override=True)
    # From today later: kept overrides keep their member, only own_values goes.
    assert future(client, c, planned=None, points="5").status_code == 302
    found = exceptions_of(c)
    assert {k: exception_row(v)[:3] for k, v in found.items()} == {
        "2026-11-26": ("", True, "Leo"), "2026-12-03": ("", True, "Emma")}
    assert [(day, title) for day, title, *_ in shown(c, "2026-11-23", "2026-12-06")] == [
        ("2026-11-26", "Recycling"), ("2026-12-03", "Recycling")]


def test_cancelled_kept_date_keeps_its_cancel_when_cleared(client, family):
    c, _ = bins()
    future(client, c, title="Recycling")
    with at(WED):
        client.post(f"/chores/{c.pk}/delete/", {"date": "2026-11-26", "scope": "this"})
    future(client, c, planned=None, points="5")
    e = exceptions_of(c)["2026-11-26"]
    assert (e.is_cancelled, e.own_values) == (True, "")
    assert set(exceptions_of(c)) == {"2026-11-26"}


# --- Refusals ------------------------------------------------------------------------------


@pytest.mark.parametrize("change", [{"recurrence_day": "4"}, {"recurrence": "daily", "recurrence_day": ""},
                                    {"start_date": "2026-11-05"},
                                    {"recurrence": "monthly", "recurrence_day": "10"}])
def test_rule_change_from_a_planned_date_is_refused(client, family, change):
    c = weekly("Bins", 3, "2026-11-01", mode="rotating", eligibility="kids", points=10, instructions="old")
    before = state()
    response = future(client, c, title="Recycling", **change)
    assert response.status_code == 200
    [chore_form] = CHORE_FORM.findall(response.content.decode())
    assert NONFIELD.findall(chore_form) == [SCHEDULE]
    assert state() == before
    assert not ChoreOccurrence.objects.exists()  # the current week was not materialized


def test_switch_to_once_from_a_planned_date_is_refused(client, family):
    c = weekly("Bins", 3, "2026-11-01", points=10)
    before = state()
    response = future(client, c, recurrence="once", recurrence_day="")
    assert response.status_code == 200
    assert NONFIELD.findall(response.content.decode()) == [SCHEDULE]
    assert state() == before


def test_service_raises_for_a_rule_change(family):
    c, _ = bins()
    old = {name: getattr(c, name) for name in services.RULE_FIELDS + services.ASSIGNMENT_FIELDS}
    c.recurrence_day = 4
    before = state()
    with pytest.raises(FutureEditRefused, match="^The schedule can only change from today"):
        apply_future_edit(c, old, d("2026-12-10"), TODAY)
    assert state() == before


def test_366_date_cap(client, family):
    assert MAX_KEPT_DATES == 366
    c = chore("Dishes", start_date="2026-11-01", recurrence="daily", points=10)
    before = state()
    response = future(client, c, planned="2027-12-10", title="Plates")
    assert response.status_code == 200
    assert NONFIELD.findall(response.content.decode()) == [
        "More than 366 planned dates come before Fri 10 Dec 2027. Pick an earlier date, or use Edit from today."]
    assert state() == before
    assert not ChoreOccurrence.objects.exists()
    response = future(client, c, planned="2027-11-23", title="Plates")
    assert response.status_code == 302
    kept = OccurrenceException.objects.filter(chore=c)
    assert kept.count() == 365 and set(kept.values_list("own_values", flat=True)) == {"kept"}
    assert (min(kept.values_list("date", flat=True)), max(kept.values_list("date", flat=True))) == (
        d("2026-11-23"), d("2027-11-22"))


# --- Extras, edited dates and overrides ---------------------------------------------------


def test_extras_edited_and_overrides_are_never_written_by_future(client, family):
    mom = family[0]
    c, _ = bins()
    assert pmove(client, c.pk, date="2026-12-03", target="2026-12-05").status_code == 302
    this(client, c, planned="2026-11-26", title="Bins early")
    this(client, c, planned="2026-12-24", title="Bins xmas")
    with at(WED):
        client.post(f"/dashboard/assign/planned/{c.pk}/", {"date": "2026-12-17", "assignee": str(mom.pk)})
    untouched = ["2026-11-26", "2026-12-03", "2026-12-05", "2026-12-24"]
    before = {k: undo.fields(v) for k, v in exceptions_of(c).items()}
    assert set(before) == {*untouched, "2026-12-17"}
    override = before.pop("2026-12-17")
    # From Thu 10 (the override on Thu 17 is on/after D), then from today.
    assert future(client, c, planned="2026-12-10", title="Recycling", eligibility="everyone").status_code == 302
    assert future(client, c, planned=None, points="5").status_code == 302
    after = exceptions_of(c)
    assert {k: undo.fields(after[k]) for k in untouched} == before
    assert undo.fields(after["2026-12-17"]) == override
    assert shown(c, "2026-12-05", "2026-12-05")[0][1] == "Bins"
    # From Thu 31: the override on Thu 17 comes before D, so it keeps the
    # values it showed ("kept"); its member is never written.
    assert future(client, c, planned="2026-12-31", title="Paper", eligibility="kids").status_code == 302
    after = exceptions_of(c)
    assert {k: undo.fields(after[k]) for k in untouched} == before
    assert exception_row(after["2026-12-17"]) == ("kept", True, "Mom", False, "Recycling", "old", 5, None)


# --- #22 delete, #24 undo, #43, #48 ---------------------------------------------------------


def test_22_delete_and_24_undo_of_an_edited_date(client, family):
    c, _ = bins()
    big_bins(client, c)
    before = undo.fields(exceptions_of(c)["2026-12-10"])
    snap = state()
    token = deleted(client, c, when=WED, date="2026-12-10", scope="this")
    e = exceptions_of(c)["2026-12-10"]
    assert (e.is_cancelled, e.own_values) == (True, "edited")
    assert bins_cards(dashboard(client, "2026-W50"), c) == []
    undone(client, token, when=WED_5)
    assert undo.fields(exceptions_of(c)["2026-12-10"]) == before
    assert state() == snap


def test_22_future_delete_and_undo_with_kept_dates(client, family):
    c, _ = bins()
    future(client, c, title="Recycling")
    snap = state()
    token = deleted(client, c, when=WED, date="2026-12-03", scope="future")
    assert set(exceptions_of(c)) == {"2026-11-26"}
    undone(client, token, when=WED_5)
    assert state() == snap


def test_43_deleting_the_last_edited_date_deletes_the_chore(client, family):
    c, o = bins()
    Chore.objects.filter(pk=c.pk).update(end_date=d("2026-12-10"))
    ChoreOccurrence.objects.filter(pk=o.pk).update(is_cancelled=True)
    follow(client, big_bins(client, c))
    for day in ("2026-11-26", "2026-12-03"):
        with at(WED):
            response = client.post(f"/chores/{c.pk}/delete/", {"date": day, "scope": "this"})
        follow(client, response)
    snap = state()
    assert snap["OccurrenceException"] and any(
        e["own_values"] == "edited" for e in snap["OccurrenceException"].values())
    token = deleted(client, c, when=WED, date="2026-12-10", scope="this")
    assert not Chore.objects.filter(pk=c.pk).exists()
    undone(client, token, when=WED_5)
    assert state() == snap


@pytest.mark.parametrize("kind", ["edited", "kept"])
def test_48_moving_an_edited_or_kept_date(client, family, kind):
    c, _ = bins()
    if kind == "edited":
        this(client, c, planned="2026-12-03", title="Bins big", points="20")
    else:
        future(client, c, planned="2026-12-10", title="Recycling", points="20")
    shown_before = projected_occurrence(c, d("2026-12-03"), TODAY)
    assert pmove(client, c.pk, date="2026-12-03", target="2026-12-05").status_code == 302
    found = exceptions_of(c)
    assert found["2026-12-03"].is_cancelled
    moved = found["2026-12-05"]
    assert moved.moved_from == d("2026-12-03") and moved.own_values == ""
    after = projected_occurrence(c, d("2026-12-05"), TODAY)
    assert {k: after[k] for k in ("title", "points", "member_id", "instructions", "due_time")} == {
        k: shown_before[k] for k in ("title", "points", "member_id", "instructions", "due_time")}


# --- Viewing writes nothing, query counts, clock ---------------------------------------------


def test_viewing_future_weeks_and_calendar_writes_nothing(client, family):
    c, _ = bins()
    big_bins(client, c)
    future(client, c, planned="2026-12-17", title="Recycling")
    spy_, wrapper = no_writes()
    with wrapper:
        for week in ["2026-W48", "2026-W49", "2026-W50", "2026-W51", "2026-W52"]:
            dashboard(client, week)
        with at(WED):
            assert calendar_get(client, "2026-12").status_code == 200
        page(client, c, "?planned=2026-12-24")
    assert [w for w in spy_.writes if "django_session" not in w] == []


def _others_with_own_values(count):
    for i in range(count):
        other = weekly(f"Other {i}", 5, "2026-11-02", points=10)
        OccurrenceException.objects.create(chore=other, date=d("2026-12-05"), own_values="edited",
                                           is_override=True, title=f"Other {i} big", points=20)


def test_projection_queries_do_not_depend_on_own_values(family):
    c, _ = bins()

    def counts():
        return (_count(lambda: project_occurrences(d("2026-11-16"), d("2027-03-31"), TODAY)),
                _count(lambda: projected_occurrence(c, d("2026-12-10"), TODAY)))

    none = counts()
    _others_with_own_values(10)
    assert {p["title"] for p in project_occurrences(d("2026-12-05"), d("2026-12-05"), TODAY)} == {
        f"Other {i} big" for i in range(10)}
    assert counts() == none


def _view_queries(client, others):
    Chore.objects.all().delete()
    c, _ = bins()
    _others_with_own_values(others)
    week = _count(lambda: dashboard(client, "2026-W49"))
    edit_page = _count(lambda: page(client, c, "?planned=2026-12-10"))
    return week, edit_page


def test_view_queries_do_not_depend_on_own_values(client, family):
    assert _view_queries(client, 0) == _view_queries(client, 10)


def test_own_values_off_the_rule_and_on_a_one_off_are_ignored(family):
    c, _ = bins()
    OccurrenceException.objects.create(chore=c, date=d("2026-12-04"), own_values="edited",
                                       is_override=True, title="Off", points=20)
    one = chore("Once", start_date="2026-12-10", points=10)
    OccurrenceException.objects.create(chore=one, date=d("2026-12-10"), own_values="edited",
                                       title="Once big", points=20)
    assert projected(c, "2026-12-04", "2026-12-04") == []
    [p] = projected(one, "2026-12-10", "2026-12-10")
    assert (p["title"], p["points"]) == ("Once", 10)


def test_services_never_read_the_clock():
    for fn in (services.edit_projected_date, services._keep_planned_dates, services._clear_kept,
               services._future_edit_attempt, services.apply_future_edit, services._store_extra,
               services._exceptions_in, services._project, services._materialize_attempt):
        source = inspect.getsource(fn)
        assert "timezone" not in source and "_resolve_today" not in source
        assert "localdate" not in source and "now(" not in source


def test_kiritimati_vs_utc(client, family):
    c, _ = bins()
    # Sun 22 12:00 UTC is Mon 23 in Kiritimati: Thu 26 is in the current week there.
    assert get(client, c, "?planned=2026-11-26", when=SUN_NOON, tz=KIRI).status_code == 404
    assert get(client, c, "?planned=2026-11-26", when=SUN_NOON, tz="UTC").status_code == 200
    # From Thu 3 Dec: in Kiritimati the week of Thu 26 is current, so it is stored, not kept.
    assert future(client, c, planned="2026-12-03", when=SUN_NOON, tz=KIRI, title="Recycling").status_code == 302
    assert exceptions_of(c) == {}
    assert stored(c)["2026-11-26"][3] == "Bins"
    c2 = weekly("Rugs", 3, "2026-11-01", points=10)
    assert future(client, c2, planned="2026-12-03", when=SUN_NOON, tz="UTC", title="Mats").status_code == 302
    assert set(exceptions_of(c2)) == {"2026-11-26"}
    assert not ChoreOccurrence.objects.filter(chore=c2, date=d("2026-11-26")).exists()


def test_no_js_plain_posts_with_csrf(family):
    c, _ = bins()
    csrf_client = Client(enforce_csrf_checks=True)
    body = page(csrf_client, c, "?week=2026-W50&planned=2026-12-10")
    [occ_form] = OCC_FORM.findall(body)
    assert "hx-" not in occ_form
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', occ_form).group(1)
    data = dict(HIDDEN.findall(occ_form))
    data.update({"csrfmiddlewaretoken": token, "scope": "this", "title": "Bins big",
                 "instructions": "old", "due_time": "18:00", "points": "20"})
    with at(WED):
        response = csrf_client.post(url(c), data)
    assert response.status_code == 302
    followed = follow(csrf_client, response)
    assert MESSAGE.format("Occurrence saved") in followed
    assert exceptions_of(c)["2026-12-10"].own_values == "edited"
    [chore_form] = CHORE_FORM.findall(followed)
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', chore_form).group(1)
    data = form_of(c, title="Recycling")
    data.update(dict(HIDDEN.findall(chore_form)))
    data.update({"csrfmiddlewaretoken": token, "scope": "future"})
    with at(WED):
        response = csrf_client.post(url(c), data)
    assert response.status_code == 302
    assert response["Location"] == f"/chores/{c.pk}/edit/?week=2026-W50&planned=2026-12-10"
    assert set(exceptions_of(c)) == {"2026-11-26", "2026-12-03", "2026-12-10"}
