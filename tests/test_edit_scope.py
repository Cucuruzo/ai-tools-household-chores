"""Edit scope for recurring chores: this occurrence / this and future (#20).

Examples use today = Wed 2026-11-18 (UTC), current week Mon 16 - Sun 22 Nov,
and the members Mom (adult, order 0), Emma (kid, 1) and Leo (kid, 2).
"""
import datetime
import html
import inspect
import re
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.db import IntegrityError
from django.test import Client

from chores import progress, services
from chores.models import Chore, ChoreOccurrence, RotationState
from chores.profile import MAX_PK
from chores.services import apply_future_edit, on_rule, project_occurrences, update_occurrence
from tests.test_chores import payload
from tests.test_dashboard import WED, at, chore, d, member, no_writes, occ, page
from tests.test_toggle import HTMX, activate

SAT_NOON = "2026-11-21T12:00:00"
SUN_NOON = "2026-11-22T12:00:00"
NOTE = re.compile(r'<p id="scope-note">(.*?)</p>', re.S)
SECTION = re.compile(r'<section id="this-occurrence">(.*?)</section>', re.S)
OCC_FORM = re.compile(r'<form id="occurrence-form"[^>]*>(.*?)</form>', re.S)
CHORE_FORM = re.compile(r'<form id="chore-form"[^>]*>(.*?)</form>', re.S)
HREF = re.compile(r'<p class="title">\s*<strong><a href="([^"]*)"')
FUTURE_BUTTON = '<button type="submit" name="scope" value="future">Save this and future occurrences</button>'
THIS_BUTTON = '<button type="submit" name="scope" value="this">Save this occurrence</button>'
HOSTILE_OCCURRENCES = ["abc", "", "0", "-1", "3.0", "٣", "99999999999999999999", "9" * 10_000]
HOSTILE_SCOPES = ["", "THIS", "all", "<script>alert(1)</script>", "x" * 10_000]


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def family():
    return member("Mom", "adult", 0), member("Emma", "kid", 1), member("Leo", "kid", 2)


# --- Helpers ---------------------------------------------------------------------


def daily(title="Dishes", start="2026-11-01", **kwargs):
    return chore(title, start_date=start, recurrence="daily", **kwargs)


def weekly(title, weekday, start, **kwargs):
    return chore(title, start_date=start, recurrence="weekly", recurrence_day=weekday, **kwargs)


def row(c, day, status="todo", member=None, points=None, due=None, **kwargs):
    if status == "done":
        kwargs.setdefault("done_at", datetime.datetime(2026, 11, 16, 9, tzinfo=datetime.timezone.utc))
    return ChoreOccurrence.objects.create(
        chore=c, date=d(day), status=status, member=member,
        points=c.points if points is None else points,
        due_time=datetime.time.fromisoformat(due) if due else c.due_time, **kwargs)


def nov(day):
    return f"2026-11-{day:02d}"


def dishes(family):
    """Daily "Dishes", start 1 Nov, fixed Mom, 10 pts, rows 16-22 Nov."""
    mom, emma, _ = family
    c = daily(mode="fixed", assigned_to=mom, points=10)
    rows = {
        16: row(c, nov(16), "done", mom),
        17: row(c, nov(17), "skipped", mom),
        18: row(c, nov(18), member=mom),
        19: row(c, nov(19), member=emma, is_override=True),
        20: row(c, nov(20), member=mom),
        21: row(c, nov(21), "done", mom),
        22: row(c, nov(22), member=mom),
    }
    return c, rows


def form_of(c, **overrides):
    """The chore form as the edit page would post it, with ``overrides``."""
    data = payload(
        title=c.title, instructions=c.instructions, start_date=c.start_date.isoformat(),
        due_time=c.due_time.strftime("%H:%M") if c.due_time else "",
        assignment_mode=c.assignment_mode, eligibility=c.eligibility or "",
        assigned_to=str(c.assigned_to_id) if c.assigned_to_id else "",
        recurrence=c.recurrence,
        recurrence_day="" if c.recurrence_day is None else str(c.recurrence_day),
        points=str(c.points),
    )
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def url(c):
    return f"/chores/{c.pk}/edit/"


def get(client, c, query="", when=WED, tz=None):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        return client.get(url(c) + query)


def body(client, c, query="", **kwargs):
    response = get(client, c, query, **kwargs)
    assert response.status_code == 200
    return response.content.decode()


def post(client, c, data, when=WED, tz=None, **headers):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        return client.post(url(c), data, **headers)


def future(client, c, occurrence=None, when=WED, tz=None, **overrides):
    data = form_of(c, **overrides)
    data["scope"] = "future"
    if occurrence is not None:
        data["occurrence"] = str(occurrence.pk)
    response = post(client, c, data, when=when, tz=tz)
    assert response.status_code == 302, response.content.decode()[:2000]
    return response


def this(client, c, occurrence, due_time="", points="10", **extra):
    data = {"scope": "this", "occurrence": str(occurrence.pk), "due_time": due_time, "points": points}
    data.update(extra)
    return post(client, c, data)


def snapshot():
    return (
        list(ChoreOccurrence.objects.order_by("id").values()),
        list(Chore.objects.order_by("id").values()),
        list(RotationState.objects.order_by("id").values()),
    )


def rows_of(c):
    """``{day of November: row}`` (all test dates are in November 2026)."""
    return {o.date.day: o for o in ChoreOccurrence.objects.filter(chore=c).order_by("date")}


def values(o):
    return ChoreOccurrence.objects.filter(pk=o.pk).values().first()


def projected(c, first, last, today="2026-11-18"):
    return [p for p in project_occurrences(d(first), d(last), today=d(today)) if p["chore_id"] == c.pk]


def messages_of(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def hidden(fragment):
    found = dict(re.findall(r'<input type="hidden" name="([a-z]+)" value="([^"]*)">', fragment))
    found.pop("csrfmiddlewaretoken", None)
    return found


# --- Entry points: the card link -----------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["todo", "done", "skipped"])
def test_recurring_stored_card_link_carries_occurrence(client, status):
    c = weekly("Trash", 2, "2026-11-04")
    o = row(c, nov(18), status)
    text = page(client)
    assert [html.unescape(h) for h in HREF.findall(text)] == [f"/chores/{c.pk}/edit/?occurrence={o.pk}"]


@pytest.mark.django_db
def test_recurring_card_link_after_filters_uses_amp(client, family):
    emma = family[1]
    c = daily()
    o = row(c, nov(12), member=emma)
    text = page(client, week="2026-W46", data={"member": emma.pk, "day": "4"})
    expected = f"/chores/{c.pk}/edit/?week=2026-W46&amp;member={emma.pk}&amp;day=4&amp;occurrence={o.pk}"
    assert HREF.findall(text) == [expected]


@pytest.mark.django_db
def test_htmx_swapped_card_has_the_same_link(client, family):
    emma = family[1]
    c = daily()
    o = row(c, nov(12), member=emma)
    data = {"week": "2026-W46", "member": str(emma.pk), "day": "4"}
    fresh = HREF.findall(page(client, week="2026-W46", data={"member": emma.pk, "day": "4"}))
    with at(WED):
        toggled = client.post(f"/dashboard/toggle/{o.pk}/", {**data, "action": "done"}, **HTMX)
        client.post(f"/dashboard/toggle/{o.pk}/", {**data, "action": "undo"})
        assigned = client.post(f"/dashboard/assign/{o.pk}/", {**data, "assignee": str(emma.pk)}, **HTMX)
    assert toggled.status_code == assigned.status_code == 200
    assert HREF.findall(toggled.content.decode()) == fresh
    assert HREF.findall(assigned.content.decode()) == fresh
    assert f"occurrence={o.pk}" in fresh[0]


@pytest.mark.django_db
def test_one_off_and_projected_cards_keep_the_plain_link(client):
    one = occ("Once", nov(18)).chore
    assert HREF.findall(page(client)) == [f"/chores/{one.pk}/edit/"]
    weekly_chore = weekly("Trash", 3, "2026-11-04")
    assert HREF.findall(page(client, week="2026-W48")) == [f"/chores/{weekly_chore.pk}/edit/?week=2026-W48"]
    assert HREF.findall(page(client, week="2026-W48", data={"day": "4"})) == [
        f"/chores/{weekly_chore.pk}/edit/?week=2026-W48&amp;day=4"]


# --- Entry points: occurrence validation and D ------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("raw", HOSTILE_OCCURRENCES, ids=lambda v: v[:12] or "empty")
def test_hostile_occurrence_404_on_get_and_post(client, raw):
    c = daily()
    row(c, nov(18))
    before = snapshot()
    assert get(client, c, "?occurrence=" + raw).status_code == 404
    assert post(client, c, {**form_of(c, points="20"), "occurrence": raw}).status_code == 404
    assert post(client, c, {"scope": "this", "occurrence": raw, "points": "5"}).status_code == 404
    assert snapshot() == before


@pytest.mark.django_db
def test_missing_other_chores_and_cancelled_rows_404(client):
    c = daily()
    other = row(daily("Other"), nov(18))
    cancelled = row(c, nov(19), is_cancelled=True)
    before = snapshot()
    for pk in ("999999", str(other.pk), str(cancelled.pk)):
        assert get(client, c, "?occurrence=" + pk).status_code == 404
        assert post(client, c, {**form_of(c, points="20"), "occurrence": pk}).status_code == 404
    assert snapshot() == before


@pytest.mark.django_db
def test_repeated_occurrence_takes_the_last_value(client):
    c = daily()
    o = row(c, nov(20))
    text = body(client, c, f"?occurrence=abc&occurrence={o.pk}")
    assert '<time datetime="2026-11-20">Fri 20 Nov</time>' in NOTE.search(text).group(1)


@pytest.mark.django_db
def test_template_and_overflow_pks_stay_404(client):
    template = Chore.objects.create(title="T", start_date=d("2026-11-01"), recurrence="daily", is_template=True)
    with at(WED):
        assert client.get(f"/chores/{template.pk}/edit/?occurrence=1").status_code == 404
        assert client.post(f"/chores/{template.pk}/edit/", {"scope": "this"}).status_code == 404
        assert client.get(f"/chores/{MAX_PK + 1}/edit/").status_code == 404


# --- Edit page markup ---------------------------------------------------------------------


@pytest.mark.django_db
def test_page_without_occurrence_has_note_with_today_and_no_section(client):
    c = daily()
    text = body(client, c)
    assert ('<p id="scope-note">Changes apply from <time datetime="2026-11-18">Wed 18 Nov</time>. '
            "Earlier occurrences keep their values.</p>") in text
    form = CHORE_FORM.search(text).group(1)
    assert form.index('id="scope-note"') < form.index(FUTURE_BUTTON)
    assert "Save chore" not in text
    assert 'id="this-occurrence"' not in text
    assert "occurrence" not in hidden(form)


@pytest.mark.django_db
def test_page_for_a_todo_row(client):
    c = daily(points=10)
    o = row(c, nov(20), due="07:15", points=20)
    text = body(client, c, f"?occurrence={o.pk}&week=2026-W46&day=4")
    assert '<time datetime="2026-11-20">Fri 20 Nov</time>' in NOTE.search(text).group(1)
    assert hidden(CHORE_FORM.search(text).group(1)) == {"week": "2026-W46", "day": "4", "occurrence": str(o.pk)}
    section = SECTION.search(text).group(1)
    assert text.index('<section id="this-occurrence">') < text.index('<form id="chore-form"')
    assert "<h2>This occurrence only</h2>" in section
    assert '<time datetime="2026-11-20">Fri 20 Nov</time>' in section
    assert f'<form id="occurrence-form" method="post" action="/chores/{c.pk}/edit/">' in section
    form = OCC_FORM.search(section).group(1)
    assert 'name="csrfmiddlewaretoken"' in form
    assert hidden(form) == {"occurrence": str(o.pk), "week": "2026-W46", "day": "4"}
    assert re.search(r'<input type="time" name="due_time" value="07:15"[^>]*id="occ_due_time"', form)
    select = re.search(r'<select name="points" id="occ_points"[^>]*>(.*?)</select>', form, re.S).group(1)
    assert re.findall(r'<option value="(\d+)"', select) == ["5", "10", "20"]
    assert re.findall(r'<option value="(\d+)" selected', select) == ["20"]
    assert THIS_BUTTON in form


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["done", "skipped"])
def test_page_for_a_finished_row(client, status):
    c = daily()
    o = row(c, nov(17), status)
    text = body(client, c, f"?occurrence={o.pk}")
    section = SECTION.search(text).group(1).strip()
    assert section == f"<p>This occurrence is {status}. Undo it on the dashboard to change it on its own.</p>"
    assert 'id="occurrence-form"' not in text
    assert '<time datetime="2026-11-17">Tue 17 Nov</time>' in NOTE.search(text).group(1)
    assert FUTURE_BUTTON in text


@pytest.mark.django_db
def test_member_filter_hidden_on_occurrence_form(client, family):
    c = daily()
    o = row(c, nov(20))
    text = body(client, c, f"?member=unassigned&occurrence={o.pk}")
    assert hidden(OCC_FORM.search(text).group(1)) == {"occurrence": str(o.pk), "member": "unassigned"}


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["", "?occurrence=abc&scope=this", "?occurrence=1"])
def test_one_off_page_is_unchanged(client, query):
    c = chore("Once", start_date=nov(18))
    row(c, nov(18))
    text = body(client, c, query)
    assert 'id="scope-note"' not in text and 'id="this-occurrence"' not in text
    assert '<button type="submit">Save chore</button>' in text
    assert "occurrence" not in hidden(CHORE_FORM.search(text).group(1))


@pytest.mark.django_db
@pytest.mark.parametrize("extra", [{"scope": "garbage", "occurrence": "abc"}, {"scope": "this"}, {}])
def test_one_off_post_ignores_scope_and_occurrence(client, extra):
    c = chore("Once", start_date=nov(18))
    o = row(c, nov(18))
    before = values(o)
    response = post(client, c, {**form_of(c, title="Renamed", points="20"), **extra})
    assert response.status_code == 302
    assert response["Location"] == url(c)
    c.refresh_from_db()
    assert (c.title, c.points) == ("Renamed", 20)
    assert values(o) == before
    assert ChoreOccurrence.objects.count() == 1


# --- POST: scope values --------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("scope", HOSTILE_SCOPES, ids=lambda v: v[:12] or "empty")
def test_bad_scope_400_nothing_written(client, scope):
    c = daily()
    o = row(c, nov(20))
    before = snapshot()
    response = post(client, c, {**form_of(c, points="20"), "scope": scope, "occurrence": str(o.pk)})
    assert response.status_code == 400
    assert response.content.decode() == "scope must be 'this' or 'future'"
    assert snapshot() == before


@pytest.mark.django_db
def test_missing_scope_means_future(client):
    c = daily()
    o = row(c, nov(20))
    response = post(client, c, form_of(c, points="20"))
    assert response.status_code == 302
    assert values(o)["points"] == 20


@pytest.mark.django_db
def test_this_without_occurrence_400(client):
    c = daily()
    row(c, nov(20))
    before = snapshot()
    response = post(client, c, {"scope": "this", "points": "5"})
    assert response.status_code == 400
    assert response.content.decode() == "Choose an occurrence"
    assert snapshot() == before


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["done", "skipped"])
def test_this_on_finished_row_400(client, status):
    c = daily()
    o = row(c, nov(17), status)
    before = snapshot()
    response = this(client, c, o, points="5")
    assert response.status_code == 400
    assert response.content.decode() == "Undo first"
    assert snapshot() == before


# --- This occurrence only ---------------------------------------------------------------------


@pytest.mark.django_db
def test_this_occurrence_example(client, family):
    mom = family[0]
    c = daily(points=10)
    rows = {day: row(c, nov(day)) for day in range(16, 23)}
    others = [values(o) for day, o in rows.items() if day != 20]
    chore_before = list(Chore.objects.filter(pk=c.pk).values())
    states_before = list(RotationState.objects.values())
    response = this(client, c, rows[20], due_time="07:30", points="5")
    assert response.status_code == 302
    assert response["Location"] == f"/chores/{c.pk}/edit/?occurrence={rows[20].pk}"
    assert messages_of(response) == ["Occurrence saved"]
    o = values(rows[20])
    assert (o["due_time"], o["points"], o["is_override"]) == (datetime.time(7, 30), 5, True)
    assert [values(o) for day, o in rows.items() if day != 20] == others
    assert list(Chore.objects.filter(pk=c.pk).values()) == chore_before
    assert list(RotationState.objects.values()) == states_before
    text = page(client)
    card = re.search(rf'id="occurrence-{rows[20].pk}">(.*?)</article>', text, re.S).group(1)
    assert '<span class="due-time">07:30</span>' in card
    assert '<span class="points">5 pts</span>' in card
    # Marked done, it counts 5 points on /contribution/ (#15).
    activate(client, mom)
    with at(WED):
        client.post(f"/dashboard/toggle/{rows[20].pk}/", {"action": "done"})
        contribution = client.get("/contribution/").content.decode()
    line = re.search(rf'id="contribution-member-{mom.pk}">(.*?)</li>', contribution, re.S).group(1)
    assert '<span class="points">5 points' in line


@pytest.mark.django_db
def test_this_occurrence_redirect_keeps_filters(client, family):
    emma = family[1]
    c = daily()
    o = row(c, nov(20))
    response = this(client, c, o, points="5", week="2026-W46", member=str(emma.pk), day="4")
    assert response["Location"] == (
        f"/chores/{c.pk}/edit/?week=2026-W46&member={emma.pk}&day=4&occurrence={o.pk}")
    follow = client.get(response["Location"])
    assert "Occurrence saved" in follow.content.decode()


@pytest.mark.django_db
def test_this_occurrence_does_not_change_projection(client):
    c = daily()
    o = row(c, nov(20))
    before = projected(c, "2026-11-16", "2026-12-06")
    this(client, c, o, due_time="07:30", points="20")
    assert projected(c, "2026-11-16", "2026-12-06") == before


@pytest.mark.django_db
def test_this_occurrence_ignores_other_fields(client):
    c = daily(title="Dishes")
    o = row(c, nov(20))
    chore_before = list(Chore.objects.filter(pk=c.pk).values())
    this(client, c, o, points="20", title="Hacked", recurrence="weekly", recurrence_day="1",
         start_date="2030-01-01", date="2026-11-25", member="1")
    assert list(Chore.objects.filter(pk=c.pk).values()) == chore_before
    after = values(o)
    assert (after["date"], after["points"], after["member_id"]) == (d(nov(20)), 20, None)


@pytest.mark.django_db
def test_this_occurrence_unchanged_values_write_nothing(client):
    c = daily()
    o = row(c, nov(20), due="08:00", points=10)
    before = snapshot()
    spy, wrapper = no_writes()
    with wrapper:
        response = this(client, c, o, due_time="08:00", points="10")
    assert response.status_code == 302
    assert [sql for sql in spy.writes if "chores_" in sql] == []
    assert snapshot() == before
    assert values(o)["is_override"] is False


@pytest.mark.django_db
def test_this_occurrence_blank_due_time_clears_it(client):
    c = daily()
    o = row(c, nov(20), due="08:00")
    this(client, c, o, due_time="", points="10")
    assert (values(o)["due_time"], values(o)["is_override"]) == (None, True)


@pytest.mark.django_db
@pytest.mark.parametrize("points", ["15", "abc", ""])
def test_occurrence_form_invalid_points(client, points):
    c = daily()
    o = row(c, nov(20))
    before = snapshot()
    response = this(client, c, o, points=points)
    assert response.status_code == 200
    form = OCC_FORM.search(response.content.decode()).group(1)
    assert 'class="errorlist"' in form
    assert snapshot() == before


@pytest.mark.django_db
def test_occurrence_form_invalid_time(client):
    c = daily()
    o = row(c, nov(20))
    before = snapshot()
    response = this(client, c, o, due_time="25:99", points="10")
    assert response.status_code == 200
    assert 'class="errorlist"' in OCC_FORM.search(response.content.decode()).group(1)
    assert snapshot() == before


def _race(change):
    """Patch the view's update so ``change(row pk)`` happens just before it."""
    real = update_occurrence

    def racing(occurrence, due_time, points):
        change(occurrence.pk)
        return real(occurrence, due_time, points)

    return mock.patch("chores.views.update_occurrence", side_effect=racing)


@pytest.mark.django_db
@pytest.mark.parametrize("change, code", [
    (lambda pk: ChoreOccurrence.objects.filter(pk=pk).update(is_cancelled=True), 404),
    (lambda pk: ChoreOccurrence.objects.filter(pk=pk).delete(), 404),
    (lambda pk: ChoreOccurrence.objects.filter(pk=pk).update(status="done"), 400),
    (lambda pk: ChoreOccurrence.objects.filter(pk=pk).update(status="skipped"), 400),
], ids=["cancelled", "deleted", "done", "skipped"])
def test_this_occurrence_race(client, change, code):
    c = daily()
    o = row(c, nov(20))
    with _race(change):
        response = this(client, c, o, due_time="07:30", points="5")
    assert response.status_code == code
    if code == 400:
        assert response.content.decode() == "Undo first"
        assert (values(o)["points"], values(o)["is_override"]) == (10, False)


@pytest.mark.django_db
def test_update_occurrence_service():
    c = daily()
    o = row(c, nov(20))
    assert update_occurrence(o, datetime.time(9), 20) is True
    assert (values(o)["due_time"], values(o)["points"], values(o)["is_override"]) == (datetime.time(9), 20, True)
    ChoreOccurrence.objects.filter(pk=o.pk).update(status="done")
    assert update_occurrence(o, datetime.time(10), 5) is False


# --- This and future: worked examples ---------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("cutoff", [18, None], ids=["from-18th-card", "from-edit-page"])
def test_values_only_from_the_18th(client, family, cutoff):
    c, rows = dishes(family)
    frozen = {day: values(rows[day]) for day in (16, 17, 19, 21)}
    response = future(client, c, rows[cutoff] if cutoff else None, points="20", due_time="18:00")
    assert response["Location"] == url(c)
    assert messages_of(response) == ["Chore saved"]
    for day in (18, 20, 22):
        o = values(rows[day])
        assert (o["points"], o["due_time"], o["member_id"]) == (20, datetime.time(18), family[0].pk)
    assert {day: values(rows[day]) for day in (16, 17, 19, 21)} == frozen
    assert all((p["points"], p["due_time"]) == (20, datetime.time(18))
               for p in projected(c, "2026-11-23", "2026-12-06"))
    c.refresh_from_db()
    assert c.start_date == d("2026-11-01")
    assert ChoreOccurrence.objects.filter(chore=c).count() == 7


@pytest.mark.django_db
def test_values_only_from_the_20th(client, family):
    c, rows = dishes(family)
    future(client, c, rows[20], points="20", due_time="18:00")
    assert [values(rows[day])["points"] for day in range(16, 23)] == [10, 10, 10, 10, 20, 10, 20]


@pytest.mark.django_db
def test_rotation_continues_on_the_same_state(client, family):
    mom, emma, leo = family
    c = daily("Feed cat", mode="rotating", eligibility="kids")
    rows = {day: row(c, nov(day), member=emma if day % 2 == 0 else leo) for day in range(16, 23)}
    state = RotationState.objects.create(chore=c, last_member=emma)
    before = {day: values(rows[day]) for day in range(16, 20)}
    future(client, c, rows[20], eligibility="everyone")
    assert [values(rows[day])["member_id"] for day in (20, 21, 22)] == [leo.pk, mom.pk, emma.pk]
    assert {day: values(rows[day]) for day in range(16, 20)} == before
    assert list(RotationState.objects.values("id", "chore_id", "last_member_id")) == [
        {"id": state.pk, "chore_id": c.pk, "last_member_id": emma.pk}]
    assert projected(c, "2026-11-23", "2026-11-23")[0]["member_id"] == leo.pk


@pytest.mark.django_db
def test_assignment_change_to_fixed_and_unassigned(client, family):
    mom, emma, leo = family
    c = daily("Feed cat", mode="rotating", eligibility="kids")
    rows = {day: row(c, nov(day), member=emma) for day in (20, 21)}
    future(client, c, rows[20], assignment_mode="fixed", assigned_to=str(leo.pk), eligibility="")
    assert [values(rows[day])["member_id"] for day in (20, 21)] == [leo.pk, leo.pk]
    c.refresh_from_db()
    future(client, c, rows[20], assignment_mode="unassigned", assigned_to="")
    assert [values(rows[day])["member_id"] for day in (20, 21)] == [None, None]
    assert not RotationState.objects.exists()


@pytest.mark.django_db
def test_rotation_with_empty_pool_unassigns(client, family):
    emma, leo = family[1], family[2]
    c = daily("Feed cat", mode="rotating", eligibility="kids")
    rows = {day: row(c, nov(day), member=emma) for day in (20, 21)}
    RotationState.objects.create(chore=c, last_member=leo)
    c.refresh_from_db()
    for m in family:
        if m.role == "adult":
            m.delete()
    future(client, c, rows[20], eligibility="adults")
    assert [values(rows[day])["member_id"] for day in (20, 21)] == [None, None]
    assert RotationState.objects.get().last_member_id == leo.pk


@pytest.mark.django_db
def test_no_assignment_change_keeps_members(client, family):
    mom, emma, leo = family
    c = daily("Feed cat", mode="rotating", eligibility="kids")
    rows = {day: row(c, nov(day), member=emma if day % 2 == 0 else leo) for day in range(18, 23)}
    RotationState.objects.create(chore=c, last_member=emma)
    future(client, c, rows[18], points="20")
    assert [values(rows[day])["member_id"] for day in range(18, 23)] == [emma.pk, leo.pk, emma.pk, leo.pk, emma.pk]
    assert RotationState.objects.get().last_member_id == emma.pk


@pytest.mark.django_db
def test_weekday_change_from_the_edit_page(client, family):
    mom = family[0]
    c = weekly("Bins", 0, "2026-11-02", mode="fixed", assigned_to=mom)
    done9 = row(c, nov(9), "done", mom)
    overdue = row(c, nov(16), member=mom)
    before = [values(done9), values(overdue)]
    future(client, c, recurrence_day="3")
    c.refresh_from_db()
    assert (c.start_date, c.recurrence_day) == (d("2026-11-18"), 3)
    rows = rows_of(c)
    assert sorted(rows) == [9, 16, 19]
    assert [values(done9), values(overdue)] == before
    assert (rows[19].member_id, rows[19].status) == (mom.pk, "todo")
    assert [p["date"] for p in projected(c, "2026-11-16", "2026-12-06")] == [d("2026-11-26"), d("2026-12-03")]


@pytest.mark.django_db
def test_weekday_change_from_the_16th_card(client, family):
    mom = family[0]
    c = weekly("Bins", 0, "2026-11-02", mode="fixed", assigned_to=mom)
    row(c, nov(9), "done", mom)
    overdue = row(c, nov(16), member=mom)
    future(client, c, overdue, recurrence_day="3")
    c.refresh_from_db()
    assert c.start_date == d("2026-11-16")
    assert sorted(rows_of(c)) == [9, 19]
    assert rows_of(c)[19].member_id == mom.pk


@pytest.mark.django_db
def test_daily_to_weekly_saturday(client, family):
    c, rows = dishes(family)
    kept = {day: values(rows[day]) for day in (16, 17, 19, 21)}
    future(client, c, rows[18], recurrence="weekly", recurrence_day="5")
    assert sorted(rows_of(c)) == [16, 17, 19, 21]
    assert {day: values(rows[day]) for day in (16, 17, 19, 21)} == kept
    assert projected(c, "2026-11-16", "2026-12-06")[0]["date"] == d("2026-11-28")


@pytest.mark.django_db
def test_gap_fill_weekly_friday_to_daily(client, family):
    emma = family[1]
    c = weekly("Water plants", 4, "2026-10-30", mode="fixed", assigned_to=emma, points=5)
    r13 = row(c, nov(13), member=emma)
    r20 = row(c, nov(20), member=emma)
    ChoreOccurrence.objects.filter(pk__in=[r13.pk, r20.pk]).update(points=10)  # show the rewrite
    future(client, c, r13, recurrence="daily", recurrence_day="")
    rows = rows_of(c)
    assert sorted(rows) == [13, 16, 17, 18, 19, 20, 21, 22]
    assert all((o.member_id, o.points, o.status) == (emma.pk, 5, "todo") for o in rows.values())
    assert (rows[13].pk, rows[20].pk) == (r13.pk, r20.pk)


@pytest.mark.django_db
def test_fill_and_followers_take_turns_in_date_order(client, family):
    mom, emma, leo = family
    c = weekly("Feed cat", 4, "2026-10-30", mode="rotating", eligibility="kids")
    follower = row(c, nov(20), member=emma)
    RotationState.objects.create(chore=c, last_member=emma)
    future(client, c, recurrence="daily", recurrence_day="", eligibility="everyone")
    rows = rows_of(c)
    assert rows[20].pk == follower.pk
    assert [rows[day].member_id for day in range(18, 23)] == [leo.pk, mom.pk, emma.pk, leo.pk, mom.pk]
    assert RotationState.objects.get().last_member_id == mom.pk


@pytest.mark.django_db
def test_pause_by_moving_the_start_date(client, family):
    c, rows = dishes(family)
    future(client, c, start_date="2026-12-01")
    c.refresh_from_db()
    assert c.start_date == d("2026-12-01")
    assert sorted(rows_of(c)) == [16, 17, 19, 21]
    assert projected(c, "2026-11-16", "2026-12-06")[0]["date"] == d("2026-12-01")


@pytest.mark.django_db
def test_start_date_moved_earlier_is_clamped(client):
    c = daily(start="2026-11-10")
    future(client, c, start_date="2026-10-01")
    c.refresh_from_db()
    assert c.start_date == d("2026-11-18")
    assert sorted(rows_of(c)) == [18, 19, 20, 21, 22]


@pytest.mark.django_db
def test_ended_chore_is_not_filled(client, family):
    c = weekly("Bins", 0, "2026-11-02", mode="fixed", assigned_to=family[0], end_date=d("2026-11-18"))
    future(client, c, recurrence_day="3")
    c.refresh_from_db()
    assert c.end_date == d("2026-11-18")
    assert not ChoreOccurrence.objects.filter(chore=c).exists()
    assert projected(c, "2026-11-16", "2027-01-31") == []


@pytest.mark.django_db
def test_no_rule_change_no_fill_and_no_clamp(client):
    c = daily(start="2026-11-10")
    future(client, c, points="20")
    c.refresh_from_db()
    assert c.start_date == d("2026-11-10")
    assert not ChoreOccurrence.objects.filter(chore=c).exists()
    # Unstored dates of this week are projected with the new values.
    assert [(p["date"], p["points"]) for p in projected(c, "2026-11-16", "2026-11-17")] == [
        (d("2026-11-16"), 20), (d("2026-11-17"), 20)]


@pytest.mark.django_db
def test_fill_skips_cancelled_dates_and_past_weeks(client, family):
    c = weekly("Bins", 0, "2026-11-02", mode="fixed", assigned_to=family[0])
    row(c, nov(19), is_cancelled=True)
    future(client, c, recurrence="daily", recurrence_day="")
    assert sorted(rows_of(c)) == [18, 19, 20, 21, 22]
    assert rows_of(c)[19].is_cancelled is True


@pytest.mark.django_db
def test_rows_before_d_and_frozen_rows_are_never_touched(client, family):
    mom, emma, leo = family
    c = daily("Feed cat", mode="rotating", eligibility="kids")
    rows = {
        12: row(c, nov(12), member=emma),  # todo, before D
        15: row(c, nov(15), "done", leo),
        20: row(c, nov(20), "done", emma),
        21: row(c, nov(21), "skipped", leo),
        22: row(c, nov(22), member=leo, is_cancelled=True),
        23: row(c, "2026-11-23", member=mom, is_override=True),
    }
    before = {day: values(o) for day, o in rows.items()}
    future(client, c, recurrence="weekly", recurrence_day="1", eligibility="everyone", points="20",
           due_time="06:00", title="Renamed")
    assert {day: values(o) for day, o in rows.items()} == before


@pytest.mark.django_db
def test_deleted_follower_turn_is_not_given_back(client, family):
    mom, emma, leo = family
    c = daily("Feed cat", mode="rotating", eligibility="kids")
    row(c, nov(18), member=emma)
    row(c, nov(19), member=leo)
    RotationState.objects.create(chore=c, last_member=leo)
    future(client, c, recurrence="weekly", recurrence_day="3")  # only Thu 19 stays on the rule
    assert sorted(rows_of(c)) == [19]
    assert rows_of(c)[19].member_id == leo.pk  # no assignment change: member stays
    assert RotationState.objects.get().last_member_id == leo.pk  # not rewound


@pytest.mark.django_db
def test_chore_fields_saved_in_place(client, family):
    mom = family[0]
    template = Chore.objects.create(title="T", start_date=d("2026-01-01"), is_template=True)
    c = daily(template_source=template, end_date=d("2026-12-31"))
    response = future(client, c, title="Wash", instructions="Hot", due_time="19:00",
                      assignment_mode="fixed", assigned_to=str(mom.pk), points="5",
                      save_as_template="on")
    assert messages_of(response) == ["Chore saved and template added"]
    c.refresh_from_db()
    assert (c.title, c.instructions, c.due_time, c.assignment_mode, c.assigned_to_id, c.points) == (
        "Wash", "Hot", datetime.time(19), "fixed", mom.pk, 5)
    assert (c.template_source_id, c.end_date, c.is_template, c.is_builtin) == (
        template.pk, d("2026-12-31"), False, False)
    assert Chore.objects.filter(is_template=True, is_builtin=False, title="Wash").exists()
    assert Chore.objects.filter(is_template=False).count() == 1


@pytest.mark.django_db
def test_future_redirect_keeps_filters_without_occurrence(client, family):
    emma = family[1]
    c = daily()
    o = row(c, nov(20))
    data = {**form_of(c, points="20"), "scope": "future", "occurrence": str(o.pk),
            "week": "2026-W46", "member": str(emma.pk), "day": "4"}
    response = post(client, c, data)
    assert response["Location"] == f"/chores/{c.pk}/edit/?week=2026-W46&member={emma.pk}&day=4"


@pytest.mark.django_db
def test_invalid_future_form_shows_the_occurrence_section_again(client):
    c = daily()
    o = row(c, nov(20))
    before = snapshot()
    data = {**form_of(c, title=""), "scope": "future", "occurrence": str(o.pk)}
    response = post(client, c, data)
    assert response.status_code == 200
    text = response.content.decode()
    assert 'class="errorlist"' in CHORE_FORM.search(text).group(1)
    assert 'id="occurrence-form"' in SECTION.search(text).group(1)
    assert '<time datetime="2026-11-20">Fri 20 Nov</time>' in NOTE.search(text).group(1)
    assert snapshot() == before


# --- Atomicity and retry -------------------------------------------------------------------


@pytest.mark.django_db
def test_any_error_rolls_everything_back(client, family):
    c = daily("Feed cat", mode="rotating", eligibility="kids")
    rows = {day: row(c, nov(day), member=family[1]) for day in (18, 19, 20)}
    RotationState.objects.create(chore=c, last_member=family[1])
    before = snapshot()
    real = services.advance_rotation
    calls = []

    def failing(chore_):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("boom")
        return real(chore_)

    with mock.patch("chores.services.advance_rotation", side_effect=failing):
        with pytest.raises(RuntimeError):
            future(client, c, rows[18], eligibility="everyone", recurrence_day="", points="20",
                   title="Renamed", start_date="2026-11-05")
    assert snapshot() == before


@pytest.mark.django_db
def test_integrity_error_on_fill_is_retried_once(client, family):
    mom, emma, leo = family
    c = weekly("Feed cat", 0, "2026-11-02", mode="rotating", eligibility="kids")
    RotationState.objects.create(chore=c, last_member=leo)
    real = services.create_occurrence
    calls = []

    def concurrent(chore_, day):
        calls.append(day)
        result = real(chore_, day)  # takes a turn, then the insert "collides"
        if len(calls) == 2:
            raise IntegrityError("unique_occurrence_per_chore_date")
        return result

    with mock.patch("chores.services.create_occurrence", side_effect=concurrent):
        future(client, c, recurrence="daily", recurrence_day="")
    # 18-22 filled once; the rolled-back attempt's turns are not counted.
    rows = rows_of(c)
    assert sorted(rows) == [18, 19, 20, 21, 22]
    assert [rows[day].member_id for day in range(18, 23)] == [emma.pk, leo.pk, emma.pk, leo.pk, emma.pk]
    assert RotationState.objects.get().last_member_id == emma.pk
    assert len(calls) == 2 + 5


@pytest.mark.django_db
def test_second_integrity_error_leaves_everything_as_before(client, family):
    c = weekly("Bins", 0, "2026-11-02", mode="fixed", assigned_to=family[0])
    row(c, nov(9), "done", family[0])
    before = snapshot()
    with mock.patch("chores.services.create_occurrence", side_effect=IntegrityError("taken")) as create:
        with pytest.raises(IntegrityError):
            future(client, c, recurrence_day="3")
    assert create.call_count == 2
    assert snapshot() == before


# --- Viewer's time zone (#26) ----------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("tz, changed, label", [
    (None, [21, 22], ("2026-11-21", "Sat 21 Nov")),
    ("Pacific/Kiritimati", [22], ("2026-11-22", "Sun 22 Nov")),
    ("Pacific/Pago_Pago", [21, 22], ("2026-11-21", "Sat 21 Nov")),
], ids=["no-cookie", "kiritimati", "pago-pago"])
def test_d_is_today_in_the_viewers_zone(client, tz, changed, label):
    c = daily()
    rows = {day: row(c, nov(day)) for day in range(16, 23)}
    note = NOTE.search(body(client, c, when=SAT_NOON, tz=tz)).group(1)
    assert f'<time datetime="{label[0]}">{label[1]}</time>' in note
    future(client, c, when=SAT_NOON, tz=tz, points="20")
    assert [day for day, o in rows.items() if values(o)["points"] == 20] == changed


@pytest.mark.django_db
def test_kiritimati_fill_uses_that_viewers_week(client):
    c = weekly("Bins", 0, "2026-11-02")
    future(client, c, when=SUN_NOON, tz="Pacific/Kiritimati", recurrence_day="2")
    assert [o.date for o in ChoreOccurrence.objects.filter(chore=c)] == [d("2026-11-25")]
    c.refresh_from_db()
    assert c.start_date == d("2026-11-23")


# --- Once <-> repeating ---------------------------------------------------------------------


SWITCH = "A chore that already has occurrences can&#x27;t switch between Once and a repeating schedule."


@pytest.mark.django_db
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("old, new", [("daily", "once"), ("once", "daily"), ("once", "weekly")])
def test_switch_with_stored_rows_is_a_form_error(client, old, new, cancelled):
    c = chore("Job", start_date="2026-11-10", recurrence=old)
    row(c, nov(10), "done" if not cancelled else "todo", is_cancelled=cancelled)
    before = snapshot()
    response = post(client, c, {**form_of(c, recurrence=new, recurrence_day="1"), "scope": "future"})
    assert response.status_code == 200
    errors = response.context["form"].errors
    assert list(errors) == ["recurrence"]
    assert SWITCH in response.content.decode()
    assert snapshot() == before


@pytest.mark.django_db
@pytest.mark.parametrize("old, new", [("daily", "once"), ("once", "daily")])
def test_switch_without_rows_is_a_plain_save(client, old, new):
    c = chore("Job", start_date="2026-11-10", recurrence=old)
    response = post(client, c, form_of(c, recurrence=new))
    assert response.status_code == 302
    c.refresh_from_db()
    assert (c.recurrence, c.start_date) == (new, d("2026-11-10"))
    assert not ChoreOccurrence.objects.exists()


@pytest.mark.django_db
def test_repeating_to_repeating_with_rows_is_allowed(client):
    c = daily()
    row(c, nov(10), "done")
    assert post(client, c, form_of(c, recurrence="weekly", recurrence_day="1")).status_code == 302


# --- Other screens after an edit ---------------------------------------------------------------


@pytest.mark.django_db
def test_dashboard_and_history_after_a_rename(client, family):
    c, rows = dishes(family)
    future(client, c, rows[20], title="Wash up", points="20")
    text = page(client)
    for day, o in rows.items():
        card = re.search(rf'id="occurrence-{o.pk}">(.*?)</article>', text, re.S).group(1)
        assert "Wash up" in card
        assert f'<span class="points">{20 if day in (20, 22) else 10} pts</span>' in card
    with at(WED):
        history = client.get("/history/").content.decode()
    assert "Dishes" not in history and "Wash up" in history
    assert values(rows[16])["points"] == 10 and values(rows[16])["status"] == "done"


@pytest.mark.django_db
def test_calendar_shows_stored_rows_and_the_new_projection(client, family):
    c, rows = dishes(family)
    future(client, c, rows[18], recurrence="weekly", recurrence_day="5")
    with at(WED):
        text = client.get("/calendar/").content.decode()
    cells = re.findall(r'<a class="day-number"[^>]*>(\d+)</a>(.*?)</td>', text, re.S)
    days = sorted(int(day) for day, inner in cells if 'title="Dishes · ' in inner)
    assert days == [16, 17, 19, 21, 28]  # the stored rows plus the new rule's Sat 28
    assert sorted(rows_of(c)) == [16, 17, 19, 21]
    assert [p["date"] for p in projected(c, "2026-11-01", "2026-11-30")] == [d("2026-11-28")]


@pytest.mark.django_db
def test_streak_continues_across_a_recurrence_change(client):
    c = daily(start="2026-11-10")
    for day in range(10, 16):
        row(c, nov(day), "done")
    future(client, c, recurrence="weekly", recurrence_day="2")  # Wed 18 is filled
    o = ChoreOccurrence.objects.get(chore=c, date=d(nov(18)))
    ChoreOccurrence.objects.filter(pk=o.pk).update(status="done")
    assert progress.chore_streaks(d(nov(18)))[c.pk] == 7


@pytest.mark.django_db
def test_viewing_other_weeks_after_an_edit_writes_nothing(client, family):
    c, rows = dishes(family)
    future(client, c, rows[18], recurrence="weekly", recurrence_day="5")
    before = snapshot()
    for week in ("2026-W48", "2026-W46", "2026-W52"):
        page(client, week=week)
    assert snapshot() == before


# --- Without JavaScript ---------------------------------------------------------------------


@pytest.mark.django_db
def test_both_forms_work_as_plain_posts(client):
    c = daily()
    o = row(c, nov(20))
    text = body(client, c, f"?occurrence={o.pk}")
    assert "hx-" not in OCC_FORM.search(text).group(0)
    first = this(client, c, o, due_time="07:30", points="5")
    assert "Occurrence saved" in client.get(first["Location"]).content.decode()
    second = future(client, c, o, points="20")
    landed = client.get(second["Location"]).content.decode()
    assert "Chore saved" in landed
    assert values(o)["points"] == 5  # now an override row, frozen


# --- Service rules and clock ----------------------------------------------------------------


@pytest.mark.django_db
def test_on_rule():
    weekly_mon = weekly("Bins", 0, "2026-11-02", end_date=d("2026-11-30"))
    assert on_rule(weekly_mon, d("2026-11-09"))
    assert not on_rule(weekly_mon, d("2026-11-10"))
    assert not on_rule(weekly_mon, d("2026-10-26"))  # before start
    assert not on_rule(weekly_mon, d("2026-12-07"))  # after end
    monthly = chore("Rent", start_date="2026-01-31", recurrence="monthly", recurrence_day=31)
    assert on_rule(monthly, d("2027-02-28"))  # clamped to the month length
    assert not on_rule(monthly, d("2027-03-30"))
    assert on_rule(daily(), d("2026-11-05"))
    assert not on_rule(chore("Once", start_date="2026-11-05"), d("2026-11-05"))


@pytest.mark.django_db
def test_apply_future_edit_uses_the_given_today(family):
    c = weekly("Bins", 0, "2026-11-02", mode="fixed", assigned_to=family[0])
    old = {name: getattr(c, name) for name in services.RULE_FIELDS + services.ASSIGNMENT_FIELDS}
    c.recurrence_day = 2
    with mock.patch("django.utils.timezone.now", side_effect=AssertionError("clock read")):
        apply_future_edit(c, old, d("2026-12-01"), d("2026-11-30"))
    assert [o.date for o in ChoreOccurrence.objects.filter(chore=c)] == [d("2026-12-02")]


def test_new_service_code_does_not_read_the_clock():
    for fn in (services.on_rule, services.update_occurrence, services.apply_future_edit,
               services._future_edit_attempt):
        source = inspect.getsource(fn)
        for forbidden in ("timezone", "datetime.now", "date.today", "localdate", "_resolve_today"):
            assert forbidden not in source
