"""Undo a chore delete (#24). Today Sat 21 Nov 2026 (UTC), current week
2026-W47 (Mon 16 to Sun 22). Mom and Dad are adults (order 0, 1), Emma and
Leo kids (2, 3). "Bathroom" is weekly on Saturdays from 7 Nov, rotating over
Everyone: Sat 14 Nov stored for Mom (Overdue), Sat 21 for Dad, an override
puts Leo on Sat 28. "Dishes" is daily from 9 Nov, rotating over the Kids,
with W46 and W47 stored and an override putting Mom on Wed 25 Nov."""
import datetime
import html
import re
from unittest import mock

import pytest
from django.db import IntegrityError, models
from django.test import Client

from chores import services, undo
from chores.models import Chore, ChoreOccurrence, Member, OccurrenceException, RotationState
from chores.services import project_occurrences, set_projected_member
from tests.test_dashboard import at, blank_csrf, chore, d, get, main, member, sections

pytestmark = pytest.mark.django_db

SAT = "2026-11-21T12:00:00"
SAT_5 = "2026-11-21T12:05:00"
KIRI = "Pacific/Kiritimati"  # UTC+14: 09:58Z is Sun 23:58, 10:01Z is Mon 00:01
HTMX = {"HTTP_HX_REQUEST": "true"}
TOKEN_INPUT = re.compile(r'<input type="hidden" name="token" value="([0-9a-f]{32})">')
UNDO_FORM = re.compile(r'<form class="undo-delete"[^>]*>(.*?)</form>', re.S)
HIDDEN = re.compile(r'<input type="hidden" name="((?!csrf|token)[^"]+)" value="([^"]*)">')
NEXT = re.compile(r'name="next" value="([^"]*)"')
REFUSED = b"This delete can no longer be undone"
MODELS = (Chore, ChoreOccurrence, OccurrenceException, RotationState)


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def family():
    return {
        "mom": member("Mom", role="adult", order=0),
        "dad": member("Dad", role="adult", order=1),
        "emma": member("Emma", role="kid", order=2),
        "leo": member("Leo", role="kid", order=3),
    }


@pytest.fixture
def bathroom(client, family):
    c = chore("Bathroom", start_date="2026-11-07", recurrence="weekly", mode="rotating",
              recurrence_day=5, eligibility="everyone")
    get(client, when="2026-11-14T12:00:00")
    get(client, when=SAT)
    set_projected_member(c, d("2026-11-28"), family["leo"].pk)
    return c


def dishes_like(title="Dishes"):
    return chore(title, start_date="2026-11-09", recurrence="daily", mode="rotating", eligibility="kids",
                 instructions="Load the dishwasher")


def finish_dishes(chores, family):
    for c in chores:
        for day in ("2026-11-09", "2026-11-10"):
            row = c.occurrences.get(date=d(day))
            row.status = "done"
            row.done_at = datetime.datetime.fromisoformat(f"{day}T19:00:00+00:00")
            row.completed_by = row.member
            row.save()
        set_projected_member(c, d("2026-11-25"), family["mom"].pk)


@pytest.fixture
def dishes(client, family):
    c = dishes_like()
    get(client, when="2026-11-09T12:00:00")
    get(client, when=SAT)
    finish_dishes([c], family)
    return c


# --- Helpers ---------------------------------------------------------------------


def state():
    """Every concrete field of every row a delete or an undo could touch."""
    return {m.__name__: {r["id"]: r for r in m.objects.values()} for m in MODELS}


def post(client, url, when, tz=None, **data):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        return client.post(url, data)


def delete(client, c, when=SAT, tz=None, **data):
    response = post(client, f"/chores/{c.pk}/delete/", when, tz, **data)
    assert response.status_code == 302, response.content
    return response


def follow(client, response, when=SAT, **headers):
    with at(when):
        page = client.get(response["Location"], **headers)
    assert page.status_code == 200
    return page.content.decode()


def token_on(body):
    found = TOKEN_INPUT.findall(body)
    assert len(found) <= 1
    return found[0] if found else None


def deleted(client, c, when=SAT, tz=None, **data):
    """Delete, open the redirected page, and return the Undo token on it."""
    token = token_on(follow(client, delete(client, c, when, tz, **data), when))
    assert token is not None
    return token


def undo_post(client, token, when=SAT_5, tz=None, **data):
    return post(client, "/chores/undo-delete/", when, tz, token=token, **data)


def undone(client, token, when=SAT_5, **data):
    response = undo_post(client, token, when, **data)
    assert response.status_code == 302, response.content
    return response


def slot(client):
    return client.session.get(undo.SLOT)


def set_slot(client, value):
    session = client.session
    session[undo.SLOT] = value
    session.save()


def row(c, day):
    return ChoreOccurrence.objects.get(chore=c, date=d(day))


def members_of(week_monday, week_sunday, today, c):
    return [(str(o["date"]), o["member_id"], o["is_override"])
            for o in project_occurrences(d(week_monday), d(week_sunday), today=d(today))
            if o["chore_id"] == c.pk]


def card(body, title):
    return [(key, x) for key, found in sections(body).items() for x in found if x["title"] == title]


# --- Guard -----------------------------------------------------------------------


def _cascades(model):
    return {r.name for r in model._meta.related_objects if r.on_delete is models.CASCADE}


def test_only_occurrences_rotation_and_exceptions_cascade_from_a_chore():
    assert _cascades(Chore) == {"occurrences", "rotation_state", "exceptions"}
    assert _cascades(ChoreOccurrence) == set()
    assert _cascades(OccurrenceException) == set()


def test_snapshot_copies_every_concrete_field_including_the_row_title(dishes):
    snap = undo.snapshot(dishes)
    names = {f.attname for f in ChoreOccurrence._meta.concrete_fields}
    assert {"title", "instructions", "id"} <= names
    assert all(set(o) == names for o in snap["occurrences"])
    assert set(snap["chore"]) == {f.attname for f in Chore._meta.concrete_fields}
    assert snap["occurrences"][0]["instructions"] == "Load the dishwasher"


# --- Each kind and source --------------------------------------------------------


def test_cancel_a_stored_row(client, family):
    trash = chore("Trash", start_date="2026-11-04", recurrence="weekly", mode="rotating",
                  recurrence_day=2, eligibility="kids")
    get(client, when="2026-11-11T12:00:00")
    get(client, when=SAT)
    wed = row(trash, "2026-11-18")
    assert wed.member == family["leo"]
    before = state()
    token = deleted(client, trash, occurrence=wed.pk, scope="this")
    between = state()
    assert between["RotationState"] == before["RotationState"]
    assert slot(client)["kind"] == "cancel" and slot(client)["occurrence_id"] == wed.pk
    body = follow(client, undone(client, token), SAT_5)
    assert state() == before
    assert (wed.pk, "😀 Leo") in [(x["id"], x["assignee"]) for key, x in card(body, "Trash") if key == "overdue"]


def test_planned_date_without_prior_exception(client, dishes):
    before = state()
    projected = members_of("2026-11-23", "2026-11-29", "2026-11-21", dishes)
    token = deleted(client, dishes, date="2026-11-26", scope="this", week="2026-W48")
    assert slot(client)["exception"] is None
    body = follow(client, undone(client, token, week="2026-W48"), SAT_5)
    assert state() == before
    assert not OccurrenceException.objects.filter(chore=dishes, date=d("2026-11-26")).exists()
    assert members_of("2026-11-23", "2026-11-29", "2026-11-21", dishes) == projected
    assert any(x["date"] and "26" in x["date"] for _, x in card(body, "Dishes"))


def test_planned_date_with_prior_override(client, family, dishes):
    before = state()
    token = deleted(client, dishes, date="2026-11-25", scope="this", week="2026-W48")
    assert slot(client)["exception"]["member_id"] == family["mom"].pk
    body = follow(client, undone(client, token, week="2026-W48"), SAT_5)
    assert state() == before
    wed = [x for _, x in card(body, "Dishes") if "25" in (x["date"] or "")]
    assert [x["assignee"] for x in wed] == ["😀 Mom"]


def test_trim_from_a_stored_card(client, family, dishes):
    before = state()
    w48 = members_of("2026-11-23", "2026-11-29", "2026-11-21", dishes)
    ids = [row(dishes, day).pk for day in ("2026-11-20", "2026-11-21", "2026-11-22")]
    token = deleted(client, dishes, occurrence=ids[0], scope="future")
    s = slot(client)
    assert s["kind"] == "trim" and s["end_date_after"] == "2026-11-19"
    assert sorted(r["id"] for r in s["rows"]) == ids
    assert [e["date"] for e in s["exceptions"]] == ["2026-11-25"]
    undone(client, token)
    assert state() == before
    assert Chore.objects.get(pk=dishes.pk).end_date is None
    assert members_of("2026-11-23", "2026-11-29", "2026-11-21", dishes) == w48
    assert ("2026-11-25", family["mom"].pk, True) in w48


def test_trim_from_the_edit_page_keeps_the_chore(client, bathroom):
    """#42: the 11-14 to-do row is left, so the chore stays (kind trim)."""
    before = state()
    token = deleted(client, bathroom, scope="future")
    s = slot(client)
    assert s["kind"] == "trim"
    assert [r["date"] for r in s["rows"]] == ["2026-11-21"]
    assert [e["date"] for e in s["exceptions"]] == ["2026-11-28"]
    undone(client, token)
    assert state() == before
    assert Chore.objects.get(pk=bathroom.pk).end_date is None


def test_trim_from_a_planned_card(client, family, dishes):
    set_projected_member(dishes, d("2026-11-27"), family["emma"].pk)
    before = state()
    token = deleted(client, dishes, date="2026-11-26", scope="future", week="2026-W48")
    s = slot(client)
    assert s["kind"] == "trim" and s["rows"] == [] and s["end_date_after"] == "2026-11-25"
    assert [e["date"] for e in s["exceptions"]] == ["2026-11-27"]
    undone(client, token, week="2026-W48")
    assert state() == before


def test_trim_one_off_with_history_only_puts_end_date_back(client, family):
    c = chore("Windows", start_date="2026-11-18")
    get(client, when=SAT)
    ChoreOccurrence.objects.filter(chore=c).update(status="done", completed_by=family["mom"])
    before = state()
    token = deleted(client, c, scope="future")
    s = slot(client)
    assert s["kind"] == "trim" and s["rows"] == [] and s["exceptions"] == []
    assert Chore.objects.get(pk=c.pk).end_date == d("2026-11-17")
    undone(client, token)
    assert state() == before


def test_whole_chore_from_the_overdue_card(client, family, bathroom):
    before = state()
    w48_50 = members_of("2026-11-23", "2026-12-13", "2026-11-21", bathroom)
    assert [(day, m) for day, m, _ in w48_50] == [
        ("2026-11-28", family["leo"].pk), ("2026-12-05", family["leo"].pk), ("2026-12-12", family["mom"].pk)]
    token = deleted(client, bathroom, occurrence=row(bathroom, "2026-11-14").pk, scope="future")
    assert not Chore.objects.filter(pk=bathroom.pk).exists()
    s = slot(client)
    assert s["kind"] == "chore"
    assert len(s["occurrences"]) == 2 and len(s["exceptions"]) == 1
    assert s["rotation"]["last_member_id"] == family["dad"].pk
    body = follow(client, undone(client, token), SAT_5)
    assert state() == before
    with at(SAT_5):
        assert client.get(f"/chores/{bathroom.pk}/edit/").status_code == 200
    found = {key: x["assignee"] for key, x in card(body, "Bathroom")}
    assert found == {"overdue": "😀 Mom", "today": "😀 Dad"}
    assert RotationState.objects.get(chore_id=bathroom.pk).last_member == family["dad"]
    assert members_of("2026-11-23", "2026-12-13", "2026-11-21", bathroom) == w48_50


def test_whole_chore_from_a_planned_card(client, family):
    c = chore("Gift", start_date="2026-11-27", mode="rotating", eligibility="kids")
    set_projected_member(c, d("2026-11-27"), family["leo"].pk)
    before = state()
    token = deleted(client, c, date="2026-11-27", scope="this", week="2026-W48")
    assert slot(client)["kind"] == "chore"
    body = follow(client, undone(client, token, week="2026-W48"), SAT_5)
    assert state() == before
    assert [x["assignee"] for _, x in card(body, "Gift")] == ["😀 Leo"]


def test_whole_one_off_from_its_stored_card(client):
    c = chore("Vacuum", start_date="2026-11-21")
    get(client, when=SAT)
    before = state()
    token = deleted(client, c, occurrence=c.occurrences.get().pk)
    assert slot(client)["kind"] == "chore"
    undone(client, token)
    assert state() == before


# --- The slot ---------------------------------------------------------------------


def test_slot_shape(client, dishes):
    deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    s = slot(client)
    assert s["v"] == 1 and re.fullmatch(r"[0-9a-f]{32}", s["token"])
    assert s["created"] == "2026-11-21T12:00:00+00:00"
    assert s["chore_id"] == dishes.pk and s["kind"] == "cancel"


def test_slot_created_is_utc_whatever_the_viewer_zone(client, dishes):
    deleted(client, dishes, tz=KIRI, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    assert slot(client)["created"] == "2026-11-21T12:00:00+00:00"


def test_failed_delete_writes_no_slot(client, dishes):
    with mock.patch.object(Chore, "save", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            post(client, f"/chores/{dishes.pk}/delete/", SAT, occurrence=row(dishes, "2026-11-20").pk,
                 scope="future")
    assert slot(client) is None


@pytest.mark.parametrize("data", [
    {"scope": "bogus"},
    {"scope": "this"},
    {"date": "nope", "scope": "this"},
    {"date": "2026-11-26", "occurrence": "1", "scope": "this"},
    {"date": "2026-11-08", "scope": "this"},  # before start_date: not a planned date, 404
    {"occurrence": "99999999", "scope": "this"},
])
def test_delete_errors_leave_the_slot(client, dishes, data):
    deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    kept = slot(client)
    response = post(client, f"/chores/{dishes.pk}/delete/", SAT, **data)
    assert response.status_code in (400, 404)
    assert slot(client) == kept


def test_delete_error_405_and_template_404_leave_the_slot(client, dishes):
    deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    kept = slot(client)
    with at(SAT):
        assert client.get(f"/chores/{dishes.pk}/delete/").status_code == 405
    template = Chore.objects.filter(is_template=True).first()
    assert post(client, f"/chores/{template.pk}/delete/", SAT, scope="future").status_code == 404
    assert slot(client) == kept


def test_nothing_written_removes_the_slot_and_shows_no_undo(client, dishes):
    deleted(client, dishes, scope="future")
    assert slot(client) is not None
    body = follow(client, delete(client, dishes, scope="future"))
    assert "<mark>Chore deleted</mark>" in body
    assert token_on(body) is None and "undo-delete" not in body
    assert slot(client) is None


def test_latest_delete_replaces_the_slot(client, dishes):
    first = deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    second = deleted(client, dishes, occurrence=row(dishes, "2026-11-21").pk, scope="this")
    assert first != second
    assert undo_post(client, first).status_code == 404
    undone(client, second)
    assert not row(dishes, "2026-11-21").is_cancelled
    assert row(dishes, "2026-11-20").is_cancelled


# --- Rendering --------------------------------------------------------------------


def test_undo_form_on_the_redirected_page_with_the_filters(client, family, dishes):
    leo = family["leo"]
    leo_row = dishes.occurrences.filter(date__lt=d("2026-11-16"), member=leo, status="todo").first()
    response = delete(client, dishes, occurrence=leo_row.pk, scope="this", week="2026-W46", member=leo.pk)
    assert response["Location"] == f"/dashboard/?week=2026-W46&member={leo.pk}"
    body = follow(client, response)
    assert '<p class="message" role="status"><mark>Chore deleted</mark></p>' in body
    form = UNDO_FORM.findall(body)
    assert len(form) == 1
    assert 'action="/chores/undo-delete/"' in body and "hx-post" not in UNDO_FORM.search(body).group(0)
    assert 'name="csrfmiddlewaretoken"' in form[0]
    assert '<button type="submit" class="secondary">Undo</button>' in form[0]
    assert HIDDEN.findall(form[0]) == [("week", "2026-W46"), ("member", str(leo.pk))]
    token = token_on(body)
    back = undone(client, token, week="2026-W46", member=leo.pk)
    assert back["Location"] == f"/dashboard/?week=2026-W46&member={leo.pk}"
    restored = follow(client, back, SAT_5)
    assert restored.count("<mark>Chore restored</mark>") == 1
    assert "undo-delete" not in restored and token_on(restored) is None
    assert "Chore restored" not in follow(client, back, SAT_5)


def test_undo_form_carries_the_day_filter(client, dishes):
    response = delete(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this", day="5")
    form = UNDO_FORM.search(follow(client, response)).group(1)
    assert HIDDEN.findall(form) == [("day", "5")]


def test_undo_form_gone_after_a_reload(client, dishes):
    response = delete(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    assert token_on(follow(client, response)) is not None
    again = follow(client, response)
    assert token_on(again) is None and "Chore deleted" not in again
    assert slot(client) is not None  # the dashboard only reads it


def test_htmx_partial_has_no_undo_and_does_not_consume_it(client, dishes):
    response = delete(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    partial = follow(client, response, **HTMX)
    assert "undo-delete" not in partial and "Chore deleted" not in partial
    assert slot(client)["token"] not in partial
    assert token_on(follow(client, response)) == slot(client)["token"]


def test_no_undo_once_the_slot_expired(client, dishes):
    response = delete(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    body = follow(client, response, "2026-11-21T12:10:01")
    assert "<mark>Chore deleted</mark>" in body and token_on(body) is None


def test_no_undo_on_another_message(client, dishes):
    set_slot(client, None)
    deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    with at(SAT):
        response = client.post(f"/dashboard/toggle/{row(dishes, '2026-11-21').pk}/", {"action": "done"})
    body = follow(client, response)
    assert token_on(body) is None


def test_restored_message_renders_like_any_message(client, dishes):
    token = deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    body = follow(client, undone(client, token), SAT_5)
    plain = follow(client, undone(client, deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk,
                                                  scope="this")), SAT_5)
    with at(SAT_5):
        quiet = client.get("/dashboard/").content.decode()
    line = '\n  <p class="message" role="status"><mark>Chore restored</mark></p>\n'
    assert main(body) == main(plain)
    assert main(body).count(line) == 1
    assert main(body).replace(line, "", 1) == main(quiet)


def test_switcher_next_matches_a_plain_get_and_has_no_token(client, family, dishes):
    leo = family["leo"]
    response = delete(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this",
                      week="2026-W47", member=leo.pk, day="5")
    body = follow(client, response)
    token = token_on(body)
    found = {html.unescape(v) for v in NEXT.findall(body)}
    assert len(found) == 1
    other = Client()
    with at(SAT):
        plain = other.get(response["Location"]).content.decode()
    assert found == {html.unescape(v) for v in NEXT.findall(plain)}
    assert token not in found.pop() and token not in response["Location"]


# --- POST /chores/undo-delete/ errors ---------------------------------------------


def test_get_is_405(client):
    with at(SAT):
        assert client.get("/chores/undo-delete/").status_code == 405


def test_post_without_csrf_is_403(family, dishes):
    browser = Client(enforce_csrf_checks=True)
    with at(SAT):
        browser.get("/dashboard/")
    set_slot(browser, None)
    before = state()
    response = post(browser, "/chores/undo-delete/", SAT, token="0" * 32)
    assert response.status_code == 403
    assert state() == before


@pytest.mark.parametrize("tokens", [
    [], [""], ["abc"], ["a" * 33], ["A" * 32], ["<script>alert(1)</script>"], ["f" * 10_000],
    ["é" * 32], ["٣" * 32], ["0" * 31 + "\n"],
])
def test_malformed_token_is_400_and_never_echoed(client, dishes, tokens):
    real = deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    kept, before = slot(client), state()
    with at(SAT_5):
        response = client.post("/chores/undo-delete/", {"token": tokens} if tokens else {})
    assert response.status_code == 400
    for t in tokens:
        if t:
            assert t.encode() not in response.content
    assert slot(client) == kept and state() == before
    undone(client, real)


def test_token_given_twice_is_400(client, dishes):
    real = deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    with at(SAT_5):
        response = client.post("/chores/undo-delete/", {"token": [real, real]})
    assert response.status_code == 400 and real.encode() not in response.content
    assert slot(client)["token"] == real


def test_no_slot_is_404(client, dishes):
    assert undo_post(client, "0" * 32).status_code == 404


def test_wrong_token_is_404_and_keeps_the_slot(client, dishes):
    real = deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    kept, before = slot(client), state()
    wrong = "0" * 32 if real != "0" * 32 else "1" * 32
    response = undo_post(client, wrong)
    assert response.status_code == 404 and wrong.encode() not in response.content
    assert slot(client) == kept and state() == before
    undone(client, real)


@pytest.mark.parametrize("value", [
    "not a dict",
    ["0" * 32],
    {"v": 2, "token": "0" * 32, "created": "2026-11-21T12:00:00+00:00", "kind": "cancel",
     "chore_id": 1, "occurrence_id": 1},
    {"v": 1, "token": "0" * 32, "created": "2026-11-21T12:00:00+00:00", "kind": "cancel", "chore_id": 1},
    {"v": 1, "token": "0" * 32, "created": "2026-11-21T12:00:00+00:00", "kind": "erase",
     "chore_id": 1, "occurrence_id": 1},
    {"v": 1, "token": "0" * 32, "kind": "cancel", "chore_id": 1, "occurrence_id": 1},
    {"v": 1, "token": "0" * 32, "created": "yesterday", "kind": "cancel", "chore_id": 1, "occurrence_id": 1},
    {"v": 1, "created": "2026-11-21T12:00:00+00:00", "kind": "cancel", "chore_id": 1, "occurrence_id": 1},
])
def test_malformed_slot_is_404_and_removed(client, dishes, value):
    set_slot(client, value)
    before = state()
    assert undo_post(client, "0" * 32).status_code == 404
    assert slot(client) is None and state() == before


@pytest.mark.parametrize("when, status", [
    ("2026-11-21T12:10:00", 302), ("2026-11-21T12:10:01", 404),
])
def test_window_is_600_seconds(client, dishes, when, status):
    target = row(dishes, "2026-11-20")
    token = deleted(client, dishes, occurrence=target.pk, scope="this")
    assert undo_post(client, token, when).status_code == status
    assert slot(client) is None
    target.refresh_from_db()
    assert target.is_cancelled is (status == 404)


def test_window_ignores_the_viewer_zone(client, dishes):
    token = deleted(client, dishes, tz=KIRI, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    del client.cookies["tz"]
    assert undo_post(client, token, "2026-11-21T12:10:00").status_code == 302
    assert not row(dishes, "2026-11-20").is_cancelled


def test_replay_is_404(client, dishes):
    token = deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    undone(client, token)
    after = state()
    assert undo_post(client, token).status_code == 404
    assert state() == after


def test_other_session_is_404(client, dishes):
    token = deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    before = state()
    assert undo_post(Client(), token).status_code == 404
    assert state() == before and slot(client)["token"] == token


@pytest.mark.parametrize("data, location", [
    ({"week": "2026-W46", "member": "leo", "day": "4"}, "/dashboard/?week=2026-W46&member={leo}&day=4"),
    ({"week": "2026-W99", "member": "999"}, "/dashboard/"),
])
def test_success_redirect_normalizes_filters(client, family, dishes, data, location):
    data = {k: (str(family["leo"].pk) if v == "leo" else v) for k, v in data.items()}
    token = deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="this")
    assert undone(client, token, **data)["Location"] == location.format(leo=family["leo"].pk)


# --- Restore refused --------------------------------------------------------------


def test_conflict_with_a_later_delete_from_another_browser(client, dishes):
    token = deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="future")
    other = Client()
    delete(other, dishes, occurrence=row(dishes, "2026-11-18").pk, scope="future")
    before = state()
    response = undo_post(client, token)
    assert response.status_code == 400 and response.content == REFUSED
    assert state() == before
    assert Chore.objects.get(pk=dishes.pk).end_date == d("2026-11-17")
    assert slot(client) is None


def test_edited_chore_blocks_a_trim_undo(client, dishes):
    token = deleted(client, dishes, occurrence=row(dishes, "2026-11-20").pk, scope="future")
    Chore.objects.filter(pk=dishes.pk).update(points=20)
    before = state()
    assert undo_post(client, token).status_code == 400
    assert state() == before and slot(client) is None


def test_cancelled_row_restored_elsewhere_is_refused(client, dishes):
    target = row(dishes, "2026-11-20")
    token = deleted(client, dishes, occurrence=target.pk, scope="this")
    ChoreOccurrence.objects.filter(pk=target.pk).update(is_cancelled=False)
    before = state()
    assert undo_post(client, token).status_code == 400
    assert state() == before


def test_restored_chore_id_taken_is_refused(client, bathroom):
    token = deleted(client, bathroom, occurrence=row(bathroom, "2026-11-14").pk, scope="future")
    s = slot(client)
    Chore.objects.create(id=s["chore_id"], title="Squatter", start_date=d("2026-11-21"))
    before = state()
    assert undo_post(client, token).status_code == 400
    assert state() == before


def test_planned_exception_materialized_in_between_is_refused(client, dishes):
    """Kiritimati: W48 becomes current and stores 11-23 as a cancelled row."""
    token = deleted(client, dishes, "2026-11-22T09:58:00", tz=KIRI, date="2026-11-23", scope="this",
                    week="2026-W48")
    get(client, when="2026-11-22T10:01:00", tz=KIRI)
    stored = row(dishes, "2026-11-23")
    assert stored.is_cancelled
    before = state()
    response = undo_post(client, token, "2026-11-22T10:05:00")
    assert response.status_code == 400
    assert state() == before and row(dishes, "2026-11-23").is_cancelled


def test_integrity_error_rolls_back_the_whole_undo(client, bathroom):
    token = deleted(client, bathroom, occurrence=row(bathroom, "2026-11-14").pk, scope="future")
    before = state()
    with mock.patch.object(RotationState, "save", side_effect=IntegrityError("duplicate")):
        response = undo_post(client, token)
    assert response.status_code == 400
    assert state() == before
    assert not Chore.objects.filter(pk=bathroom.pk).exists()
    assert not ChoreOccurrence.objects.filter(chore_id=bathroom.pk).exists()
    assert not OccurrenceException.objects.filter(chore_id=bathroom.pk).exists()
    assert not RotationState.objects.filter(chore_id=bathroom.pk).exists()


# --- What undo never does ---------------------------------------------------------


def test_undo_never_calls_rotation_or_projection_writers(client, bathroom):
    token = deleted(client, bathroom, occurrence=row(bathroom, "2026-11-14").pk, scope="future")
    boom = AssertionError("must not be called")
    names = ["advance_rotation", "create_occurrence", "materialize_week", "set_projected_member",
             "cancel_projected_date"]
    patches = [mock.patch.object(services, n, side_effect=boom) for n in names]
    patches += [mock.patch(f"chores.views.{n}", side_effect=boom)
                for n in ("materialize_week", "set_projected_member", "cancel_projected_date")]
    patches.append(mock.patch("django.utils.timezone.localdate", side_effect=boom))
    for p in patches:
        p.start()
    try:
        assert undo_post(client, token).status_code == 302
    finally:
        for p in patches:
            p.stop()


# --- Rows that point at removed members or templates -------------------------------


def test_removed_member_comes_back_as_null(client, family, bathroom):
    token = deleted(client, bathroom, occurrence=row(bathroom, "2026-11-14").pk, scope="future")
    with at(SAT):
        assert client.post(f"/family/{family['dad'].pk}/delete/").status_code == 302
    undone(client, token)
    assert row(bathroom, "2026-11-21").member is None
    assert row(bathroom, "2026-11-14").member == family["mom"]
    assert RotationState.objects.get(chore_id=bathroom.pk).last_member is None


def test_removed_template_source_comes_back_as_null(client):
    template = Chore.objects.create(title="Mop", start_date=d("2026-11-01"), is_template=True)
    c = chore("Mop floor", start_date="2026-11-21", template_source=template)
    get(client, when=SAT)
    token = deleted(client, c, occurrence=c.occurrences.get().pk)
    template.delete()
    undone(client, token)
    assert Chore.objects.get(pk=c.pk).template_source_id is None
    assert c.occurrences.count() == 1


# --- Time passing between delete and undo ------------------------------------------


def test_week_became_current_in_between(client, family):
    """Kiritimati: trim at local Sun 23:58, W48 stored at Mon 00:01, undo."""
    dishes = dishes_like()
    control = dishes_like(title="Control")
    get(client, when="2026-11-09T12:00:00")
    get(client, when=SAT)
    finish_dishes([dishes, control], family)
    token = deleted(client, dishes, "2026-11-22T09:58:00", tz=KIRI, scope="future")
    s = slot(client)
    assert [r["date"] for r in s["rows"]] == ["2026-11-22"]
    assert [e["date"] for e in s["exceptions"]] == ["2026-11-25"]
    assert s["end_date_after"] == "2026-11-21"
    get(client, when="2026-11-22T10:01:00", tz=KIRI)
    assert control.occurrences.filter(date__gte=d("2026-11-23")).count() == 7
    assert not dishes.occurrences.filter(date__gte=d("2026-11-23")).exists()
    others = {r["id"]: r for r in ChoreOccurrence.objects.filter(chore=control).values()}
    undone(client, token, "2026-11-22T10:05:00")
    assert row(dishes, "2026-11-22").pk == s["rows"][0]["id"]
    assert OccurrenceException.objects.get(chore=dishes).date == d("2026-11-25")
    assert Chore.objects.get(pk=dishes.pk).end_date is None
    assert {r["id"]: r for r in ChoreOccurrence.objects.filter(chore=control).values()} == others
    body = blank_csrf(get(client, when="2026-11-22T10:06:00", tz=KIRI).content.decode())
    assert any(x["title"] == "Dishes" for x in sections(body)["overdue"])

    def week(c):
        return [(str(o.date), o.member_id, o.is_override)
                for o in c.occurrences.filter(date__gte=d("2026-11-23")).order_by("date")]

    assert len(week(dishes)) == 7 and week(dishes) == week(control)
    assert (RotationState.objects.get(chore=dishes).last_member_id
            == RotationState.objects.get(chore=control).last_member_id)


# --- #43 ---

from tests.test_dashboard import WED  # noqa: E402

WED_5 = "2026-11-18T12:05:00"


def trash43(start="2026-11-04", **kwargs):
    return chore("Trash", start_date=start, recurrence="weekly", recurrence_day=2, **kwargs)


def test_43_a_undo_restores_the_chore_deleted_by_this_only(client):
    c = trash43()
    old = ChoreOccurrence.objects.create(chore=c, date=d("2026-11-11"), points=c.points)
    wed = ChoreOccurrence.objects.create(chore=c, date=d("2026-11-18"), points=c.points)
    follow(client, delete(client, c, WED, occurrence=wed.pk, scope="future"), WED)
    assert slot(client)["kind"] == "trim"
    before = state()
    token = deleted(client, c, WED, occurrence=old.pk, scope="this")
    assert not Chore.objects.filter(pk=c.pk).exists()
    s = slot(client)
    assert s["kind"] == "chore" and s["chore"]["end_date"] == "2026-11-17"
    assert [(o["id"], o["is_cancelled"]) for o in s["occurrences"]] == [(old.pk, False)]
    body = follow(client, undone(client, token, WED_5), WED_5)
    assert state() == before
    assert "Chore restored" in body
    assert [(key, x["id"]) for key, x in card(body, "Trash")] == [("overdue", old.pk)]


def test_43_b_undo_puts_back_a_planned_only_chore_without_an_exception(client):
    c = trash43(start="2026-11-25")
    follow(client, delete(client, c, WED, date="2026-12-02", scope="future", week="2026-W49"), WED)
    assert Chore.objects.get(pk=c.pk).end_date == d("2026-12-01")
    before = state()
    token = deleted(client, c, WED, date="2026-11-25", scope="this", week="2026-W48")
    assert not Chore.objects.filter(pk=c.pk).exists()
    s = slot(client)
    assert s["kind"] == "chore" and s["exceptions"] == []
    follow(client, undone(client, token, WED_5, week="2026-W48"), WED_5)
    assert state() == before
    assert not OccurrenceException.objects.filter(chore_id=c.pk).exists()
    assert members_of("2026-11-23", "2026-11-29", "2026-11-18", c) == [("2026-11-25", None, False)]


def test_43_e_undo_restores_the_state_before_the_second_this_only(client):
    a = member("A", order=0)
    c = trash43(start="2026-11-25", end_date=d("2026-12-08"))
    override = OccurrenceException.objects.create(chore=c, date=d("2026-12-02"), is_override=True, member=a)
    follow(client, delete(client, c, WED, date="2026-11-25", scope="this", week="2026-W48"), WED)
    assert slot(client)["kind"] == "planned"
    before = state()
    token = deleted(client, c, WED, date="2026-12-02", scope="this", week="2026-W49")
    assert not Chore.objects.filter(pk=c.pk).exists()
    assert slot(client)["kind"] == "chore"
    follow(client, undone(client, token, WED_5, week="2026-W49"), WED_5)
    assert state() == before
    restored = {str(e.date): e for e in OccurrenceException.objects.filter(chore_id=c.pk)}
    assert restored["2026-11-25"].is_cancelled is True
    assert restored["2026-12-02"].pk == override.pk
    assert (restored["2026-12-02"].is_override, restored["2026-12-02"].member_id,
            restored["2026-12-02"].is_cancelled) == (True, a.pk, False)
    assert members_of("2026-11-30", "2026-12-06", "2026-11-18", c) == [("2026-12-02", a.pk, True)]
