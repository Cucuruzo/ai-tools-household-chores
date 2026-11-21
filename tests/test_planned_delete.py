"""Delete a projected date from its Planned card (#22): markup and
``POST /chores/<pk>/delete/`` with ``date``. Today Wed 18 Nov 2026 (UTC),
"Bins" weekly on Thursday rotating over Ann, Ben and Cat, Thu 19 Nov stored
for Ann; projected 26 Nov Ben, 3 Dec Cat, 10 Dec Ann, 17 Dec Ben."""
import datetime
import html
import inspect
import re
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import services, views
from chores.models import Chore, ChoreOccurrence, Member, OccurrenceException, RotationState
from chores.services import project_occurrences
from tests.test_dashboard import at, blank_csrf, no_writes

WED = "2026-11-18T12:00:00"
MON_23 = "2026-11-23T12:00:00"
TUE_1 = "2026-12-01T12:00:00"
HTMX = {"HTTP_HX_REQUEST": "true"}
MAX_PK_PLUS = "99999999999999999999"
HIDDEN = re.compile(r'<input type="hidden" name="((?!csrf)[^"]+)" value="([^"]*)">')
SCOPES = re.compile(r'<button type="submit" name="scope" value="([^"]*)" class="contrast">([^<]*)</button>')
CANCEL = ('<button type="button" class="secondary" onclick="this.closest(\'details\').open = false;">'
          "Cancel</button>")


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


def make_bins(family, status="todo"):
    c = Chore.objects.create(
        title="Bins", start_date=d("2026-11-05"), recurrence="weekly", recurrence_day=3,
        assignment_mode="rotating", eligibility="kids",
    )
    done_at = datetime.datetime(2026, 11, 19, 9, tzinfo=datetime.timezone.utc) if status == "done" else None
    ChoreOccurrence.objects.create(chore=c, date=d("2026-11-19"), member=family["ann"], points=10,
                                   status=status, done_at=done_at)
    RotationState.objects.create(chore=c, last_member=family["ann"])
    return c


@pytest.fixture
def bins(family):
    return make_bins(family)


def rugs(**kwargs):
    """Weekly Thursday from Thu 26 Nov, nothing stored."""
    return Chore.objects.create(title="Rugs", start_date=d("2026-11-26"), recurrence="weekly",
                                recurrence_day=3, **kwargs)


def delete(client, chore_id, when=WED, tz=None, htmx=False, **data):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        return client.post(f"/chores/{chore_id}/delete/", data, **(HTMX if htmx else {}))


def dashboard(client, week, when=WED, tz=None, **params):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        response = client.get("/dashboard/", {"week": week, **params})
    assert response.status_code == 200
    return response.content.decode()


def calendar(client, month, when=WED):
    with at(when):
        response = client.get("/calendar/", {"month": month})
    assert response.status_code == 200
    return response.content.decode()


def day_cell(body, label):
    """The calendar cell of e.g. "Thu, Dec 3": ``(count, [dot titles])``."""
    m = re.search(rf'aria-label="Show {label} on the dashboard">\d+</a>(.*?)</td>', body, re.S)
    count = re.search(r'<span class="count"[^>]*>(\d+)</span>', m.group(1))
    titles = re.findall(r'<span class="dot dot-[a-z-]+" title="([^"]*)"', m.group(1))
    return (int(count.group(1)) if count else 0, [html.unescape(t) for t in titles])


def card_on(body, chore, day):
    m = re.search(rf'<article class="chore-card" id="planned-{chore.pk}-{day}">(.*?)</article>', body, re.S)
    return m and m.group(1)


def assignee(body, chore, day):
    inner = card_on(body, chore, day)
    p = re.search(r'<p class="assignee">(.*?)</p>', inner, re.S).group(1)
    return html.unescape(re.search(r'<span class="(?:name|unassigned)">(.*?)</span>', p).group(1))


def delete_after(body, chore, day):
    """The Planned card's <details>, which must follow its assign form."""
    m = re.search(
        rf'<article class="chore-card" id="planned-{chore.pk}-{day}">(?:(?!<article).)*?</article>\s*'
        r'<form class="assign"[^>]*>(?:(?!<article).)*?</form>\s*'
        r'(<details class="delete-confirm">(?:(?!<article).)*?</details>)', body, re.S)
    return m and m.group(1)


def projected(chore, start="2026-11-16", end="2026-12-31", today=d("2026-11-18")):
    """``{date: member name or None}`` that projection gives for ``chore``."""
    names = dict(Member.objects.values_list("pk", "name"))
    return {o["date"].isoformat(): names.get(o["member_id"])
            for o in project_occurrences(d(start), d(end), today=today) if o["chore_id"] == chore.pk}


def db():
    return (
        sorted(OccurrenceException.objects.values_list("chore_id", "date", "is_cancelled", "is_override", "member_id")),
        sorted(ChoreOccurrence.objects.values_list(
            "id", "chore_id", "date", "member_id", "status", "is_override", "is_cancelled")),
        sorted(Chore.objects.values_list("id", "end_date")),
        sorted(RotationState.objects.values_list("chore_id", "last_member_id")),
    )


# --- Markup ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_recurring_planned_card_delete_markup(client, bins, family):
    body = blank_csrf(dashboard(client, "2026-W49", member=str(family["cat"].pk), day="4"))
    details = delete_after(body, bins, "2026-12-03")
    assert details is not None
    assert '<summary role="button" class="outline contrast">Delete</summary>' in details
    assert f'<form method="post" action="/chores/{bins.pk}/delete/">' in details
    assert 'name="csrfmiddlewaretoken"' in details
    assert HIDDEN.findall(details) == [
        ("date", "2026-12-03"), ("week", "2026-W49"), ("member", str(family["cat"].pk)), ("day", "4"),
    ]
    assert 'name="occurrence"' not in details
    assert SCOPES.findall(details) == [
        ("this", "This occurrence only"), ("future", "This and all future occurrences")]
    assert details.index("This and all future occurrences") < details.index(CANCEL)
    assert "Delete this chore?" not in details


@pytest.mark.django_db
def test_one_off_planned_card_delete_markup(client, family):
    c = Chore.objects.create(title="Garage", start_date=d("2026-12-05"), recurrence="once")
    details = delete_after(dashboard(client, "2026-W49"), c, "2026-12-05")
    assert "<p>Delete this chore?</p>" in details
    assert SCOPES.findall(details) == [("future", "Delete")]
    assert details.index("Delete this chore?") < details.index('value="future"') < details.index(CANCEL)
    assert HIDDEN.findall(details) == [("date", "2026-12-05"), ("week", "2026-W49")]


@pytest.mark.django_db
def test_hidden_filters_left_out_by_default(client, bins):
    details = delete_after(dashboard(client, "2026-W49"), bins, "2026-12-03")
    assert HIDDEN.findall(details) == [("date", "2026-12-03"), ("week", "2026-W49")]


@pytest.mark.django_db
def test_assign_form_still_follows_the_article_directly(client, bins):
    body = dashboard(client, "2026-W49")
    assert re.search(rf'id="planned-{bins.pk}-2026-12-03">.*?</article>\s*<form class="assign"', body, re.S)


@pytest.mark.django_db
def test_every_planned_card_has_one_delete(client, family):
    c = Chore.objects.create(title="Dishes", start_date=d("2026-11-16"), recurrence="daily")
    body = dashboard(client, "2026-W49")
    days = [f"2026-12-0{n}" for n in range(1, 7)] + ["2026-11-30"]
    assert all(delete_after(body, c, day) for day in days)
    assert body.count('<details class="delete-confirm">') == 7


@pytest.mark.django_db
def test_card_only_reply_has_no_details_or_form(client, bins, family):
    with at(WED):
        response = client.post(f"/dashboard/assign/planned/{bins.pk}/",
                               {"date": "2026-12-03", "assignee": str(family["ann"].pk)}, **HTMX)
    body = response.content.decode().strip()
    assert body.startswith(f'<article class="chore-card" id="planned-{bins.pk}-2026-12-03">')
    assert body.endswith("</article>") and "<details" not in body and "<form" not in body


@pytest.mark.django_db
def test_stored_card_keeps_occurrence_input(client, bins):
    body = dashboard(client, "2026-W47")
    o = bins.occurrences.get()
    m = re.search(rf'id="occurrence-{o.pk}">.*?</article>\s*<details class="delete-confirm">(.*?)</details>',
                  body, re.S)
    assert ("occurrence", str(o.pk)) in HIDDEN.findall(m.group(1))
    assert 'name="date"' not in m.group(1)


def test_template_comment_describes_planned_delete():
    from pathlib import Path

    source = (Path(views.__file__).parent / "templates/chores/_chore_card.html").read_text()
    comment = source.split("{% endcomment %}")[0]
    assert "#22" in comment and "hidden date" in comment


def test_projected_cards_set_recurring():
    assert "recurring=chore.recurrence != Chore.Recurrence.ONCE" in inspect.getsource(views._projected_cards)


# --- G: this occurrence only -------------------------------------------------------------


@pytest.mark.django_db
def test_g_this_only_writes_one_cancelled_exception(client, bins, family):
    before = db()
    spy, wrapper = no_writes()
    with wrapper:
        response = delete(client, bins.pk, date="2026-12-03", scope="this", week="2026-W49")
    assert (response.status_code, response["Location"]) == (302, "/dashboard/?week=2026-W49")
    e = OccurrenceException.objects.get()
    assert (e.chore_id, e.date, e.is_cancelled, e.is_override, e.member_id) == (
        bins.pk, d("2026-12-03"), True, False, None)
    tables = " ".join(spy.writes).lower()
    assert "choreoccurrence" not in tables and "rotationstate" not in tables
    assert db()[1:] == before[1:]
    # Every other date keeps its rotation member.
    assert projected(bins) == {"2026-11-26": "Ben", "2026-12-10": "Ann", "2026-12-17": "Ben",
                               "2026-12-24": "Cat", "2026-12-31": "Ann"}
    assert card_on(dashboard(client, "2026-W49"), bins, "2026-12-03") is None
    assert assignee(dashboard(client, "2026-W48"), bins, "2026-11-26") == "Ben"
    assert assignee(dashboard(client, "2026-W50"), bins, "2026-12-10") == "Ann"
    assert assignee(dashboard(client, "2026-W51"), bins, "2026-12-17") == "Ben"


@pytest.mark.django_db
def test_g_existing_override_stays_but_no_longer_matters(client, bins, family):
    OccurrenceException.objects.create(chore=bins, date=d("2026-12-03"), is_override=True, member=family["mom"])
    assert delete(client, bins.pk, date="2026-12-03", scope="this").status_code == 302
    e = OccurrenceException.objects.get()
    assert (e.is_cancelled, e.is_override, e.member_id) == (True, True, family["mom"].pk)
    assert "2026-12-03" not in projected(bins)
    dashboard(client, "2026-W48", when=MON_23)
    dashboard(client, "2026-W49", when=TUE_1)
    row = ChoreOccurrence.objects.get(chore=bins, date=d("2026-12-03"))
    assert (row.member_id, row.is_cancelled) == (family["cat"].pk, True)


@pytest.mark.django_db
def test_g_materialized_later_keeps_the_turn(client, bins, family):
    delete(client, bins.pk, date="2026-12-03", scope="this")
    dashboard(client, "2026-W48", when=MON_23)
    w49 = dashboard(client, "2026-W49", when=TUE_1)
    row = ChoreOccurrence.objects.get(chore=bins, date=d("2026-12-03"))
    assert (row.member_id, row.is_cancelled, row.status) == (family["cat"].pk, True, "todo")
    assert ChoreOccurrence.objects.get(chore=bins, date=d("2026-11-26")).member_id == family["ben"].pk
    assert RotationState.objects.get(chore=bins).last_member == family["cat"]
    assert not OccurrenceException.objects.exists()
    assert 'datetime="2026-12-03"' not in w49  # the current week does not show the cancelled date
    assert assignee(dashboard(client, "2026-W50", when=TUE_1), bins, "2026-12-10") == "Ann"


@pytest.mark.django_db
def test_g_calendar_follows(client, bins, family):
    Chore.objects.create(title="Garage", start_date=d("2026-12-03"), recurrence="once")
    before = day_cell(calendar(client, "2026-12"), "Thu, Dec 3")
    assert before[0] == 2 and any(t.startswith("Bins") for t in before[1])
    delete(client, bins.pk, date="2026-12-03", scope="this")
    after = day_cell(calendar(client, "2026-12"), "Thu, Dec 3")
    assert after[0] == 1 and not any(t.startswith("Bins") for t in after[1])
    assert any(t.startswith("Bins") for t in day_cell(calendar(client, "2026-12"), "Thu, Dec 10")[1])
    # Once current, the stored cancelled row is not on the calendar either.
    dashboard(client, "2026-W48", when=MON_23)
    current = day_cell(calendar(client, "2026-12", when=TUE_1), "Thu, Dec 3")
    assert current[0] == 1 and not any(t.startswith("Bins") for t in current[1])


@pytest.mark.django_db
def test_g_second_this_only_is_404(client, bins):
    delete(client, bins.pk, date="2026-12-03", scope="this")
    before = db()
    assert delete(client, bins.pk, date="2026-12-03", scope="this").status_code == 404
    assert db() == before


@pytest.mark.django_db
def test_concurrent_insert_is_retried(bins):
    real = OccurrenceException.objects.update_or_create
    calls = []

    def racing(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            OccurrenceException.objects.create(chore=bins, date=d("2026-12-03"), is_override=True)
            raise IntegrityError("unique_exception_per_chore_date")
        return real(**kwargs)

    with mock.patch.object(OccurrenceException.objects, "update_or_create", side_effect=racing):
        services.cancel_projected_date(bins, d("2026-12-03"))
    assert len(calls) == 2 and OccurrenceException.objects.get().is_cancelled


def test_cancel_projected_date_reads_no_clock():
    source = inspect.getsource(services.cancel_projected_date)
    assert list(inspect.signature(services.cancel_projected_date).parameters) == ["chore", "day"]
    assert "localdate" not in source and "now(" not in source


# --- H, I: this and future -----------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["todo", "done"])
def test_h_i_this_and_future_keeps_19_nov(client, family, status):
    bins = make_bins(family, status=status)
    row = bins.occurrences.get()
    response = delete(client, bins.pk, date="2026-12-03", scope="future", week="2026-W49")
    assert (response.status_code, response["Location"]) == (302, "/dashboard/?week=2026-W49")
    bins.refresh_from_db()
    assert bins.end_date == d("2026-12-02")
    kept = ChoreOccurrence.objects.get(chore=bins)
    assert (kept.pk, kept.date, kept.member_id, kept.status) == (row.pk, d("2026-11-19"), family["ann"].pk, status)
    assert projected(bins) == {"2026-11-26": "Ben"}
    assert assignee(dashboard(client, "2026-W48"), bins, "2026-11-26") == "Ben"
    for week in ("2026-W49", "2026-W50", "2026-W51"):
        assert "Bins" not in dashboard(client, week)
    december = calendar(client, "2026-12")
    for label in ("Thu, Dec 3", "Thu, Dec 10", "Thu, Dec 17"):
        assert day_cell(december, label) == (0, [])


# --- J, K: nothing before D ---------------------------------------------------------------


@pytest.mark.django_db
def test_j_nothing_before_d_deletes_the_chore(client, family):
    c = rugs(assignment_mode="rotating", eligibility="kids")
    RotationState.objects.create(chore=c, last_member=family["ann"])
    OccurrenceException.objects.create(chore=c, date=d("2026-12-10"), is_override=True, member=family["cat"])
    assert delete(client, c.pk, date="2026-11-26", scope="future").status_code == 302
    assert not Chore.objects.filter(pk=c.pk).exists()
    assert not RotationState.objects.exists() and not OccurrenceException.objects.exists()


@pytest.mark.django_db
def test_j_from_10_dec_keeps_the_chore(client, family):
    c = rugs()
    assert delete(client, c.pk, date="2026-12-10", scope="future").status_code == 302
    c.refresh_from_db()
    assert c.end_date == d("2026-12-09")
    assert sorted(projected(c)) == ["2026-11-26", "2026-12-03"]


@pytest.mark.django_db
def test_k_cancelled_in_advance_does_not_count_as_before_d(client, family):
    c = rugs()
    assert delete(client, c.pk, date="2026-11-26", scope="this").status_code == 302
    assert delete(client, c.pk, date="2026-12-03", scope="future").status_code == 302
    assert not Chore.objects.filter(pk=c.pk).exists()
    assert not OccurrenceException.objects.exists()


# --- L: exceptions on or after D go ---------------------------------------------------------


@pytest.mark.django_db
def test_l_exceptions_from_d_go(client, bins, family):
    OccurrenceException.objects.create(chore=bins, date=d("2026-12-17"), is_override=True, member=family["ann"])
    OccurrenceException.objects.create(chore=bins, date=d("2026-11-26"), is_override=True, member=family["cat"])
    assert delete(client, bins.pk, date="2026-12-10", scope="future").status_code == 302
    assert list(OccurrenceException.objects.values_list("date", "member_id")) == [
        (d("2026-11-26"), family["cat"].pk)]
    bins.refresh_from_db()
    assert bins.end_date == d("2026-12-09")
    assert projected(bins) == {"2026-11-26": "Cat", "2026-12-03": "Cat"}
    assert assignee(dashboard(client, "2026-W48"), bins, "2026-11-26") == "Cat"
    assert assignee(dashboard(client, "2026-W49"), bins, "2026-12-03") == "Cat"


@pytest.mark.django_db
def test_l_stored_card_variant(client, bins, family):
    """This and future from the stored 19 Nov card removes a 3 Dec exception."""
    ChoreOccurrence.objects.create(chore=bins, date=d("2026-11-12"), member=family["cat"], points=10,
                                   status="done",
                                   done_at=datetime.datetime(2026, 11, 12, 9, tzinfo=datetime.timezone.utc))
    OccurrenceException.objects.create(chore=bins, date=d("2026-12-03"), is_override=True, member=family["mom"])
    stored = bins.occurrences.get(date=d("2026-11-19"))
    assert delete(client, bins.pk, occurrence=stored.pk, scope="future").status_code == 302
    bins.refresh_from_db()
    assert bins.end_date == d("2026-11-18")
    assert not OccurrenceException.objects.exists()
    assert list(bins.occurrences.values_list("date", flat=True)) == [d("2026-11-12")]


@pytest.mark.django_db
def test_delete_from_docstring_mentions_exceptions():
    assert "exceptions" in views._delete_from.__doc__.lower()


# --- One-off ----------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ["future", "this", ""])
def test_one_off_with_date_deletes_the_chore(client, family, scope):
    c = Chore.objects.create(title="Garage", start_date=d("2026-12-05"), recurrence="once")
    OccurrenceException.objects.create(chore=c, date=d("2026-12-05"), is_override=True, member=family["ann"])
    assert delete(client, c.pk, date="2026-12-05", scope=scope).status_code == 302
    assert not Chore.objects.filter(pk=c.pk).exists() and not OccurrenceException.objects.exists()


# --- Responses -------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ["this", "future"])
def test_no_javascript_302_with_filters_and_message(client, bins, family, scope):
    response = delete(client, bins.pk, date="2026-12-03", scope=scope,
                      week="2026-W49", member=str(family["cat"].pk), day="4")
    assert response.status_code == 302
    assert response["Location"] == f"/dashboard/?week=2026-W49&member={family['cat'].pk}&day=4"
    with at(WED):
        body = client.get(response["Location"]).content.decode()
    assert body.count('<p class="message" role="status"><mark>Chore deleted</mark></p>') == 1
    assert card_on(body, bins, "2026-12-03") is None


@pytest.mark.django_db
def test_viewing_the_future_week_writes_nothing(client, bins):
    writes = []
    for step in ("before", "this", "future"):
        if step == "this":
            delete(client, bins.pk, date="2026-12-03", scope="this")
        elif step == "future":
            delete(client, bins.pk, date="2026-12-10", scope="future")
        spy, wrapper = no_writes()
        with wrapper:
            dashboard(client, "2026-W49")
            dashboard(client, "2026-W50")
        writes += spy.writes
    assert not writes


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ["this", "future"])
def test_today_is_read_once(client, bins, scope):
    with mock.patch("chores.views.timezone.localdate", wraps=views.timezone.localdate) as spy_:
        response = delete(client, bins.pk, date="2026-12-10", scope=scope)
    assert response.status_code == 302 and spy_.call_count == 1


@pytest.mark.django_db
def test_same_today_for_check_and_delete_from(client, bins):
    seen = []
    real_check, real_delete = views.projected_occurrence, views._delete_from

    def check(chore, day, today):
        seen.append(today)
        return real_check(chore, day, today)

    def delete_from(chore, start, today):
        seen.append(today)
        return real_delete(chore, start, today)

    with mock.patch.object(views, "projected_occurrence", side_effect=check), \
            mock.patch.object(views, "_delete_from", side_effect=delete_from):
        assert delete(client, bins.pk, date="2026-12-10", scope="future").status_code == 302
    assert seen[0] == d("2026-11-18") and all(t is seen[0] for t in seen) and len(seen) >= 2


# --- Atomicity -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_failed_save_rolls_back_the_whole_delete(client, family):
    bins = make_bins(family, status="done")
    OccurrenceException.objects.create(chore=bins, date=d("2026-12-17"), is_override=True, member=family["ann"])
    OccurrenceException.objects.create(chore=bins, date=d("2026-11-26"), is_override=True, member=family["cat"])
    before = db()
    with mock.patch.object(Chore, "save", side_effect=RuntimeError("disk full")):
        with pytest.raises(RuntimeError):
            delete(client, bins.pk, date="2026-12-10", scope="future")
    assert db() == before


@pytest.mark.django_db
def test_failure_after_cancel_rolls_back(client, bins):
    seen = []
    real = views.cancel_projected_date

    def inside(*args):
        real(*args)
        seen.append(OccurrenceException.objects.count())
        raise RuntimeError("after the write")

    with mock.patch.object(views, "cancel_projected_date", side_effect=inside):
        with pytest.raises(RuntimeError):
            delete(client, bins.pk, date="2026-12-03", scope="this")
    assert seen == [1]
    assert not OccurrenceException.objects.exists()


# --- Viewer's zone (#26) -----------------------------------------------------------------------


@pytest.mark.django_db
def test_kiritimati_then_utc(family):
    noon = "2026-11-22T12:00:00"  # Kiritimati: Mon 23 Nov; UTC: Sun 22 Nov
    bins = make_bins(family)
    kiri, utc = Client(), Client()
    dashboard(kiri, "2026-W48", when=noon, tz="Pacific/Kiritimati")  # materializes W48
    assert ChoreOccurrence.objects.filter(chore=bins, date=d("2026-11-26")).exists()
    before = db()
    for scope in ("this", "future"):
        response = delete(utc, bins.pk, when=noon, tz="UTC", date="2026-11-26", scope=scope)
        assert response.status_code == 404
    assert db() == before


# --- Hostile input -------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("chore_id", [MAX_PK_PLUS, "0", "424242"])
def test_unknown_chore_is_404(client, bins, chore_id):
    before = db()
    assert delete(client, chore_id, date="2026-12-03", scope="this").status_code == 404
    assert db() == before


@pytest.mark.django_db
def test_template_is_404(client, family):
    t = Chore.objects.create(title="T", start_date=d("2026-11-16"), recurrence="daily", is_template=True)
    assert delete(client, t.pk, date="2026-12-03", scope="this").status_code == 404
    assert Chore.objects.filter(pk=t.pk).exists() and not OccurrenceException.objects.exists()


@pytest.mark.django_db
def test_get_is_405(client, bins):
    with at(WED):
        assert client.get(f"/chores/{bins.pk}/delete/", {"date": "2026-12-03"}).status_code == 405


@pytest.mark.django_db
@pytest.mark.parametrize("occurrence, day", [
    ("stored", "2026-12-03"), ("abc", "x"), ("", ""), (MAX_PK_PLUS, "2026-12-3"), ("stored", "2026-11-19"),
])
def test_occurrence_and_date_is_400(client, bins, occurrence, day):
    if occurrence == "stored":
        occurrence = str(bins.occurrences.get().pk)
    before = db()
    for scope in ("this", "future"):
        response = delete(client, bins.pk, occurrence=occurrence, date=day, scope=scope)
        assert (response.status_code, response.content) == (400, b"Give an occurrence or a date, not both")
    assert db() == before


BAD_DATES = ["", "2026-12-3", "2026-12-03T00:00", "2026-02-30", "٢٠٢٦-١٢-٠٣",
             "1999-12-02", "2100-01-07", "2026-12-03\x00", "9" * 10000]


@pytest.mark.django_db
@pytest.mark.parametrize("raw", BAD_DATES)
@pytest.mark.parametrize("scope", ["this", "future"])
def test_malformed_date_is_400(client, bins, raw, scope):
    before = db()
    response = delete(client, bins.pk, date=raw, scope=scope)
    assert (response.status_code, response.content) == (400, b"Invalid date")
    assert db() == before


@pytest.mark.django_db
def test_last_date_value_wins(client, bins):
    with at(WED):
        bad_last = client.post(f"/chores/{bins.pk}/delete/", {"date": ["2026-12-03", "x"], "scope": "this"})
        assert bad_last.status_code == 400 and not OccurrenceException.objects.exists()
        good_last = client.post(f"/chores/{bins.pk}/delete/", {"date": ["x", "2026-12-03"], "scope": "this"})
    assert good_last.status_code == 302
    assert OccurrenceException.objects.get().date == d("2026-12-03")


@pytest.mark.django_db
@pytest.mark.parametrize("day", [
    "2026-11-12",  # a past date
    "2026-11-19",  # current week, already stored
    "2026-12-02",  # a Wednesday: off the rule
    "2026-12-24",  # after end_date
    "2026-10-29",  # before start_date
    "2026-12-10",  # cancelled in advance
])
@pytest.mark.parametrize("scope", ["this", "future"])
def test_not_a_projected_date_is_404(client, bins, day, scope):
    Chore.objects.filter(pk=bins.pk).update(end_date=d("2026-12-20"))
    OccurrenceException.objects.create(chore=bins, date=d("2026-12-10"), is_cancelled=True)
    before = db()
    response = delete(client, bins.pk, date=day, scope=scope)
    assert response.status_code == 404 and db() == before


@pytest.mark.django_db
@pytest.mark.parametrize("day", ["2026-11-17", "2026-12-05"])
def test_one_off_with_a_row_is_404(client, day):
    c = Chore.objects.create(title="Garage", start_date=d(day), recurrence="once")
    ChoreOccurrence.objects.create(chore=c, date=d(day), points=10)
    before = db()
    assert delete(client, c.pk, date=day, scope="future").status_code == 404 and db() == before


@pytest.mark.django_db
@pytest.mark.parametrize("scope", [None, "", "THIS", "all", "x" * 10000])
def test_bad_scope_is_400(client, bins, scope):
    data = {"date": "2026-12-03"}
    if scope is not None:
        data["scope"] = scope
    before = db()
    response = delete(client, bins.pk, **data)
    assert (response.status_code, response.content) == (400, b"scope must be 'this' or 'future'")
    assert db() == before


@pytest.mark.django_db
def test_this_without_occurrence_or_date_is_still_400(client, bins):
    before = db()
    response = delete(client, bins.pk, scope="this")
    assert (response.status_code, response.content) == (400, b"Only a to-do occurrence can be deleted on its own")
    assert db() == before


# --- Query count (#30) ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_projection_query_count_ignores_cancelled_exceptions(family):
    c = Chore.objects.create(title="Dishes", start_date=d("2026-11-16"), recurrence="daily")
    counts = []
    for n in (0, 20):
        for i in range(n):
            OccurrenceException.objects.create(chore=c, date=d("2026-11-23") + datetime.timedelta(days=i),
                                               is_cancelled=True)
        with CaptureQueriesContext(connection) as queries:
            result = project_occurrences(d("2026-11-23"), d("2026-12-31"), today=d("2026-11-18"))
        counts.append(len(queries))
        assert len(result) == 39 - n
    assert counts[0] == counts[1]


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ["this", "future"])
def test_delete_query_count_ignores_exceptions(client, family, scope):
    counts = []
    for n in (1, 20):
        Chore.objects.all().delete()
        c = Chore.objects.create(title="Dishes", start_date=d("2026-11-16"), recurrence="daily")
        for i in range(n):  # around D = 1 Dec, never on it
            day = d("2026-11-26") + datetime.timedelta(days=i if i < 5 else i + 1)
            OccurrenceException.objects.create(chore=c, date=day, is_override=True, member=family["ann"])
        with CaptureQueriesContext(connection) as queries:
            assert delete(client, c.pk, date="2026-12-01", scope=scope).status_code == 302
        counts.append(len(queries))
    assert counts[0] == counts[1]


@pytest.mark.django_db
def test_dashboard_query_count_with_1_and_7_planned_cards(client, family):
    counts = []
    for start in ("2026-12-06", "2026-11-30"):
        Chore.objects.all().delete()
        Chore.objects.create(title="Dishes", start_date=d(start), recurrence="daily")
        with CaptureQueriesContext(connection) as queries:
            body = dashboard(client, "2026-W49")
        counts.append(len(queries))
        assert body.count('<details class="delete-confirm">') == (1 if start == "2026-12-06" else 7)
    assert counts[0] == counts[1]


# --- Migrations ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", "chores", check=True, dry_run=True, verbosity=0)
