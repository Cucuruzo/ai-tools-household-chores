"""Assign a projected date from its Planned card (#31): markup and
``POST /dashboard/assign/planned/<chore_id>/``. Today Wed 18 Nov 2026 (UTC),
"Bins" weekly on Thursday rotating over Ann, Ben and Cat, Thu 19 Nov stored
for Ann; projected 26 Nov Ben, 3 Dec Cat, 10 Dec Ann, 17 Dec Ben."""
import datetime
import html
import re
from unittest import mock

import pytest
from django.db import IntegrityError, connection
from django.test import Client

from chores import services, views
from chores.models import Chore, ChoreOccurrence, Member, OccurrenceException, RotationState
from tests.test_dashboard import at, blank_csrf, cards, main, no_writes

WED = "2026-11-18T12:00:00"
HTMX = {"HTTP_HX_REQUEST": "true"}
ASSIGN_FORM = re.compile(r'<form class="assign"([^>]*)>(.*?)</form>', re.S)
OPTION = re.compile(r'<option value="([^"]*)"( selected)?>(.*?)</option>', re.S)
HIDDEN = re.compile(r'<input type="hidden" name="((?!csrf)[^"]+)" value="([^"]*)">')
MAX_PK_PLUS = "99999999999999999999"


def d(text):
    return datetime.date.fromisoformat(text)


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def family():
    make = lambda name, role, order: Member.objects.create(name=name, role=role, avatar="😀", order=order)  # noqa: E731
    return {"ann": make("Ann", "kid", 0), "ben": make("Ben", "kid", 1),
            "cat": make("Cat", "kid", 2), "mom": make("Mom", "adult", 3)}


@pytest.fixture
def bins(family):
    c = Chore.objects.create(
        title="Bins", start_date=d("2026-11-05"), recurrence="weekly", recurrence_day=3,
        assignment_mode="rotating", eligibility="kids",
    )
    ChoreOccurrence.objects.create(chore=c, date=d("2026-11-19"), member=family["ann"], points=10)
    RotationState.objects.create(chore=c, last_member=family["ann"])
    return c


def url(chore_id):
    return f"/dashboard/assign/planned/{chore_id}/"


def post(client, chore_id, when=WED, tz=None, htmx=True, **data):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        return client.post(url(chore_id), data, **(HTMX if htmx else {}))


def dashboard(client, week, when=WED, tz=None, **params):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        response = client.get("/dashboard/", {"week": week, **params})
    assert response.status_code == 200
    return response.content.decode()


def card_on(body, chore, day):
    """The Planned card with id ``planned-<chore>-<day>`` (its inner HTML)."""
    m = re.search(rf'<article class="chore-card" id="planned-{chore.pk}-{day}">(.*?)</article>', body, re.S)
    return m and m.group(1)


def assignee(body, chore, day):
    inner = card_on(body, chore, day)
    p = re.search(r'<p class="assignee">(.*?)</p>', inner, re.S).group(1)
    name = re.search(r'<span class="(?:name|unassigned)">(.*?)</span>', p).group(1)
    return html.unescape(name)


def form_after(body, chore, day):
    """The form that follows the card's ``</article>``."""
    m = re.search(
        rf'id="planned-{chore.pk}-{day}">.*?</article>\s*<form class="assign"([^>]*)>(.*?)</form>', body, re.S)
    return m and (m.group(1), m.group(2))


def db():
    return (
        sorted(OccurrenceException.objects.values_list("chore_id", "date", "is_cancelled", "is_override", "member_id")),
        sorted(ChoreOccurrence.objects.values_list("id", "chore_id", "date", "member_id", "status", "is_override")),
        sorted(RotationState.objects.values_list("chore_id", "last_member_id")),
    )


# --- Markup ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_planned_card_id_and_assign_form(client, bins, family):
    body = blank_csrf(dashboard(client, "2026-W49", member=str(family["cat"].pk), day="4"))
    assert card_on(body, bins, "2026-12-03") is not None
    attrs, inner = form_after(body, bins, "2026-12-03")
    target = url(bins.pk)
    assert attrs == (f' method="post" action="{target}" hx-post="{target}" hx-trigger="change"'
                     f' hx-target="#planned-{bins.pk}-2026-12-03" hx-swap="outerHTML"')
    assert 'name="csrfmiddlewaretoken"' in inner
    assert HIDDEN.findall(inner) == [
        ("date", "2026-12-03"), ("week", "2026-W49"), ("member", str(family["cat"].pk)), ("day", "4"),
    ]
    assert f'<label for="assign-planned-{bins.pk}-2026-12-03">Assigned to</label>' in inner
    assert f'<select id="assign-planned-{bins.pk}-2026-12-03" name="assignee">' in inner
    assert [(v, bool(s), html.unescape(t)) for v, s, t in OPTION.findall(inner)] == [
        ("", False, "Unassigned"),
        (str(family["ann"].pk), False, "😀 Ann"), (str(family["ben"].pk), False, "😀 Ben"),
        (str(family["cat"].pk), True, "😀 Cat"), (str(family["mom"].pk), False, "😀 Mom"),
    ]
    assert "<noscript><button type=\"submit\">Assign</button></noscript>" in inner


@pytest.mark.django_db
def test_planned_card_has_no_status_or_delete_and_keeps_its_text(client, bins):
    body = main(dashboard(client, "2026-W49"))
    assert '<form class="status"' not in body and "delete-confirm" not in body and "card-block" not in body
    [card] = cards(body)
    assert (card["id"], card["title"], card["date"], card["points"], card["status"]) == (
        None, "Bins", "Thu, Dec 3", "10 pts", "Planned")
    assert len(ASSIGN_FORM.findall(body)) == 1


@pytest.mark.django_db
def test_hidden_filters_left_out_by_default(client, bins):
    _, inner = form_after(dashboard(client, "2026-W49"), bins, "2026-12-03")
    assert HIDDEN.findall(inner) == [("date", "2026-12-03"), ("week", "2026-W49")]


@pytest.mark.django_db
def test_stored_cards_keep_their_ids(client, bins):
    body = dashboard(client, "2026-W47")
    o = ChoreOccurrence.objects.get(chore=bins, date=d("2026-11-19"))
    assert f'<div class="card-block" id="card-{o.pk}">' in body
    assert f'<article class="chore-card" id="occurrence-{o.pk}">' in body
    assert "planned-" not in body


# --- Writing ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("who, shown", [("ann", "Ann"), ("mom", "Mom"), (None, "Unassigned")])
def test_assign_writes_one_exception_and_swaps_the_card(client, bins, family, who, shown):
    value = "" if who is None else str(family[who].pk)
    spy, wrapper = no_writes()
    with wrapper:
        response = post(client, bins.pk, date="2026-12-03", assignee=value)
    assert response.status_code == 200
    body = response.content.decode()
    assert body.strip().startswith(f'<article class="chore-card" id="planned-{bins.pk}-2026-12-03">')
    assert body.strip().endswith("</article>") and "<form" not in body
    assert assignee(body, bins, "2026-12-03") == shown
    e = OccurrenceException.objects.get()
    assert (e.chore_id, e.date, e.is_override, e.is_cancelled, e.member_id) == (
        bins.pk, d("2026-12-03"), True, False, family[who].pk if who else None)
    tables = " ".join(spy.writes).lower()
    assert "choreoccurrence" not in tables and "rotationstate" not in tables
    # Every other date keeps its rotation member (example A / B).
    page = dashboard(client, "2026-W48") + dashboard(client, "2026-W50") + dashboard(client, "2026-W51")
    assert [assignee(page, bins, day) for day in ("2026-11-26", "2026-12-10", "2026-12-17")] == ["Ben", "Ann", "Ben"]
    assert assignee(dashboard(client, "2026-W49"), bins, "2026-12-03") == shown


@pytest.mark.django_db
def test_same_member_writes_nothing(client, bins, family):
    before = db()
    response = post(client, bins.pk, date="2026-12-03", assignee=str(family["cat"].pk))
    assert response.status_code == 200 and db() == before


@pytest.mark.django_db
def test_unassigned_to_unassigned_writes_nothing(client, family):
    c = Chore.objects.create(title="Garage", start_date=d("2026-12-05"), recurrence="once")
    response = post(client, c.pk, date="2026-12-05", assignee="")
    assert response.status_code == 200 and not OccurrenceException.objects.exists()


@pytest.mark.django_db
def test_repeat_is_a_no_op_and_reassign_updates_the_same_row(client, bins, family):
    post(client, bins.pk, date="2026-12-03", assignee=str(family["ann"].pk))
    first = db()
    spy, wrapper = no_writes()
    with wrapper:
        post(client, bins.pk, date="2026-12-03", assignee=str(family["ann"].pk))
    assert db() == first and not spy.writes
    post(client, bins.pk, date="2026-12-03", assignee=str(family["cat"].pk))
    assert OccurrenceException.objects.get().member_id == family["cat"].pk  # an override to the rotation member


@pytest.mark.django_db
def test_override_to_unassigned_never_needs_eligible_member(client, family):
    c = Chore.objects.create(title="Feed fish", start_date=d("2026-11-16"), recurrence="weekly",
                             recurrence_day=3, assignment_mode="rotating", eligibility="adults")
    Member.objects.filter(role="adult").delete()
    assert assignee(dashboard(client, "2026-W49"), c, "2026-12-03") == "Unassigned · needs eligible member"
    post(client, c.pk, date="2026-12-03", assignee=str(family["ann"].pk))  # not eligible: still allowed
    body = post(client, c.pk, date="2026-12-03", assignee="").content.decode()
    assert assignee(body, c, "2026-12-03") == "Unassigned"
    assert assignee(dashboard(client, "2026-W49"), c, "2026-12-03") == "Unassigned"


@pytest.mark.django_db
def test_deleting_the_member_shows_unassigned(client, bins, family):
    post(client, bins.pk, date="2026-12-03", assignee=str(family["mom"].pk))
    family["mom"].delete()
    assert assignee(dashboard(client, "2026-W49"), bins, "2026-12-03") == "Unassigned"


@pytest.mark.django_db
def test_concurrent_insert_is_retried_not_a_500(client, bins, family):
    real = OccurrenceException.objects.update_or_create
    calls = []

    def racing(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            OccurrenceException.objects.create(chore=bins, date=d("2026-12-03"), is_override=True)
            raise IntegrityError("unique_exception_per_chore_date")
        return real(**kwargs)

    with mock.patch.object(OccurrenceException.objects, "update_or_create", side_effect=racing):
        response = post(client, bins.pk, date="2026-12-03", assignee=str(family["ann"].pk))
    assert response.status_code == 200 and len(calls) == 2
    assert OccurrenceException.objects.get().member_id == family["ann"].pk


@pytest.mark.django_db
def test_check_and_write_run_in_one_transaction(client, bins, family):
    seen = []
    real = views.set_projected_member

    def inside(*args):
        seen.append(connection.in_atomic_block)
        real(*args)
        raise RuntimeError("after the write")

    with mock.patch.object(views, "set_projected_member", side_effect=inside):
        with pytest.raises(RuntimeError):
            post(client, bins.pk, date="2026-12-03", assignee=str(family["ann"].pk))
    assert seen == [True]
    assert not OccurrenceException.objects.exists()  # rolled back with the check


# --- Responses -------------------------------------------------------------------------


@pytest.mark.django_db
def test_member_filter_still_matching_swaps_the_card(client, bins, family):
    response = post(client, bins.pk, date="2026-12-03", assignee=str(family["ann"].pk),
                    week="2026-W49", member=str(family["ann"].pk))
    assert "HX-Retarget" not in response
    assert response.content.decode().strip().startswith('<article class="chore-card"')


@pytest.mark.django_db
def test_member_filter_no_longer_matching_retargets_the_list(client, bins, family):
    response = post(client, bins.pk, date="2026-12-03", assignee=str(family["ann"].pk),
                    week="2026-W49", member=str(family["cat"].pk), day="4")
    assert response.status_code == 200
    assert (response["HX-Retarget"], response["HX-Reswap"]) == ("#dashboard-list", "outerHTML")
    body = response.content.decode()
    assert body.strip().startswith('<div id="dashboard-list">')
    assert card_on(body, bins, "2026-12-03") is None and 'id="no-chores"' not in body
    assert "Nothing here" in body


@pytest.mark.django_db
def test_unassigned_filter_retargets_after_assigning(client, family):
    c = Chore.objects.create(title="Garage", start_date=d("2026-12-05"), recurrence="once")
    response = post(client, c.pk, date="2026-12-05", assignee=str(family["cat"].pk),
                    week="2026-W49", member="unassigned")
    assert response["HX-Retarget"] == "#dashboard-list"


@pytest.mark.django_db
def test_no_javascript_redirects_with_filters_and_shows_the_member(client, bins, family):
    response = post(client, bins.pk, htmx=False, date="2026-12-03", assignee=str(family["ann"].pk),
                    week="2026-W49", member=str(family["ann"].pk), day="4")
    assert response.status_code == 302
    assert response["Location"] == f"/dashboard/?week=2026-W49&member={family['ann'].pk}&day=4"
    with at(WED):
        body = client.get(response["Location"]).content.decode()
    assert assignee(body, bins, "2026-12-03") == "Ann"


@pytest.mark.django_db
def test_no_javascript_without_filters(client, bins, family):
    response = post(client, bins.pk, htmx=False, date="2026-12-03", assignee="", week="2026-W49")
    assert (response.status_code, response["Location"]) == (302, "/dashboard/?week=2026-W49")


@pytest.mark.django_db
def test_shown_equals_stored_through_the_ui(bins, family):
    """Example A end to end: assign on 18 Nov, open W48 on 23 Nov, W49 on 1 Dec."""
    client = Client()
    post(client, bins.pk, date="2026-12-03", assignee=str(family["ann"].pk))
    shown = {day: assignee(dashboard(client, week), bins, day)
             for week, day in (("2026-W48", "2026-11-26"), ("2026-W49", "2026-12-03"))}
    dashboard(client, "2026-W48", when="2026-11-23T12:00:00")
    dashboard(client, "2026-W49", when="2026-12-01T12:00:00")
    stored = {o.date.isoformat(): o.member.name for o in ChoreOccurrence.objects.filter(
        chore=bins, date__gte=d("2026-11-26")).select_related("member")}
    assert stored == shown == {"2026-11-26": "Ben", "2026-12-03": "Ann"}
    assert RotationState.objects.get(chore=bins).last_member == family["cat"]
    assert not OccurrenceException.objects.exists()


@pytest.mark.django_db
def test_viewing_future_weeks_writes_nothing(client, bins, family):
    post(client, bins.pk, date="2026-12-03", assignee=str(family["ann"].pk))
    OccurrenceException.objects.create(chore=bins, date=d("2026-12-10"), is_cancelled=True)
    spy, wrapper = no_writes()
    with wrapper:
        dashboard(client, "2026-W49")
        dashboard(client, "2026-W50")
        with at(WED):
            client.get("/calendar/", {"month": "2026-12"})
    assert not spy.writes


# --- Viewer's zone (#26) ---------------------------------------------------------------


@pytest.mark.django_db
def test_kiritimati_then_utc(family):
    noon = "2026-11-22T12:00:00"  # Kiritimati: Mon 23 Nov 02:00; UTC: Sun 22 Nov
    c = Chore.objects.create(title="Bins", start_date=d("2026-11-05"), recurrence="weekly",
                             recurrence_day=3, assignment_mode="rotating", eligibility="kids")
    ChoreOccurrence.objects.create(chore=c, date=d("2026-11-19"), member=family["ann"], points=10)
    RotationState.objects.create(chore=c, last_member=family["ann"])
    kiri, utc = Client(), Client()
    # W48 is not stored yet: the 26 Nov date is still projected, in Kiritimati's week.
    response = post(kiri, c.pk, when=noon, tz="Pacific/Kiritimati", date="2026-11-26",
                    assignee=str(family["cat"].pk))
    assert response.status_code == 200
    assert OccurrenceException.objects.get().member_id == family["cat"].pk
    # A Kiritimati dashboard view materializes W48 (and consumes the exception).
    dashboard(kiri, "2026-W48", when=noon, tz="Pacific/Kiritimati")
    row = ChoreOccurrence.objects.get(chore=c, date=d("2026-11-26"))
    assert (row.member_id, row.is_override) == (family["cat"].pk, True)
    before = db()
    response = post(utc, c.pk, when=noon, tz="UTC", date="2026-11-26", assignee=str(family["ben"].pk))
    assert response.status_code == 404
    assert db() == before


# --- Hostile input -----------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("chore_id", [MAX_PK_PLUS, "0", "424242"])
def test_unknown_chore_is_404(client, bins, family, chore_id):
    before = db()
    response = post(client, chore_id, date="2026-12-03", assignee=str(family["ann"].pk))
    assert response.status_code == 404 and db() == before


@pytest.mark.django_db
def test_template_chore_is_404(client, family):
    t = Chore.objects.create(title="T", start_date=d("2026-11-16"), recurrence="daily", is_template=True)
    assert post(client, t.pk, date="2026-12-03", assignee="").status_code == 404
    assert not OccurrenceException.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["abc", "-1", "1.5", "٣"])
def test_non_digits_never_reach_the_view(client, bins, path):
    with at(WED):
        assert client.post(f"/dashboard/assign/planned/{path}/", {"date": "2026-12-03"}).status_code == 404


@pytest.mark.django_db
def test_get_is_405(client, bins):
    with at(WED):
        assert client.get(url(bins.pk)).status_code == 405


BAD_DATES = [None, "", "2026-12-3", "2026-12-03T00:00", "2026-02-30", "٢٠٢٦-١٢-٠٣",
             "1999-12-02", "2100-01-07", "2026-12-03\x00", "9" * 10000]


@pytest.mark.django_db
@pytest.mark.parametrize("raw", BAD_DATES)
def test_malformed_date_is_400(client, bins, family, raw):
    data = {"assignee": str(family["ann"].pk)}
    if raw is not None:
        data["date"] = raw
    before = db()
    response = post(client, bins.pk, **data)
    assert (response.status_code, response.content) == (400, b"Invalid date")
    assert db() == before


@pytest.mark.django_db
def test_last_date_value_wins(client, bins, family):
    with at(WED):
        bad_last = client.post(url(bins.pk), {"date": ["2026-12-03", "x"], "assignee": ""}, **HTMX)
        good_last = client.post(url(bins.pk), {"date": ["x", "2026-12-03"], "assignee": ""}, **HTMX)
    assert (bad_last.status_code, good_last.status_code) == (400, 200)


@pytest.mark.django_db
@pytest.mark.parametrize("day", [
    "2026-11-12",  # a past date
    "2026-11-19",  # current week, already stored
    "2026-12-02",  # a Wednesday: off the rule
    "2026-12-24",  # after end_date
    "2026-10-29",  # before start_date
    "2026-12-10",  # cancelled in advance (#22)
])
def test_not_a_projected_date_is_404(client, bins, family, day):
    Chore.objects.filter(pk=bins.pk).update(end_date=d("2026-12-20"))
    OccurrenceException.objects.create(chore=bins, date=d("2026-12-10"), is_cancelled=True)
    before = db()
    response = post(client, bins.pk, date=day, assignee=str(family["mom"].pk))
    assert response.status_code == 404 and db() == before


@pytest.mark.django_db
def test_stored_one_off_is_404(client, family):
    c = Chore.objects.create(title="Garage", start_date=d("2026-11-17"), recurrence="once")
    ChoreOccurrence.objects.create(chore=c, date=d("2026-11-17"), points=10)
    before = db()
    assert post(client, c.pk, date="2026-11-17", assignee="").status_code == 404 and db() == before


@pytest.mark.django_db
def test_missing_assignee_is_400(client, bins):
    before = db()
    response = post(client, bins.pk, date="2026-12-03")
    assert (response.status_code, response.content) == (400, b"Choose who to assign") and db() == before


@pytest.mark.django_db
@pytest.mark.parametrize("raw", ["abc", "-1", "٣", "1" * 10000, "424242", MAX_PK_PLUS])
def test_unknown_member_is_400(client, bins, raw):
    before = db()
    response = post(client, bins.pk, date="2026-12-03", assignee=raw)
    assert (response.status_code, response.content) == (400, b"Unknown member") and db() == before


def test_service_takes_today_and_reads_no_clock():
    import inspect

    assert list(inspect.signature(services.projected_occurrence).parameters) == ["chore", "day", "today"]
    assert "localdate" not in inspect.getsource(services.projected_occurrence)
    assert "localdate" not in inspect.getsource(services.set_projected_member)
