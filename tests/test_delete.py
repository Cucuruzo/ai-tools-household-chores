"""Chore deletion with recurrence scope (#5)."""
import datetime
import html
import importlib
import re
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import Client

from chores.forms import ChoreForm, TemplateForm
from chores.models import Chore, ChoreOccurrence, RotationState
from chores.services import materialize_week, project_occurrences
from tests.test_dashboard import WED, at, chore, d, member, page, sections

HOSTILE = "<script>alert(1)</script>"
OVERFLOW = "99999999999999999999"
SAT_NOON = "2026-11-21T12:00:00"  # Sat 21 Nov 2026 12:00 UTC; Sun 22 in Kiritimati (UTC+14)


@pytest.fixture
def client():
    return Client()


# --- Helpers ---------------------------------------------------------------------


def weekly(title="Trash", weekday=2, start="2026-11-04", recurrence="weekly", **kwargs):
    """A recurring chore (default: weekly on Wednesdays from Nov 4)."""
    return chore(title, start_date=start, recurrence=recurrence, recurrence_day=weekday, **kwargs)


def row(c, day, status="todo", **kwargs):
    kwargs.setdefault("points", c.points)
    if status == "done":
        kwargs.setdefault("done_at", datetime.datetime(2026, 11, 1, 9, tzinfo=datetime.timezone.utc))
    return ChoreOccurrence.objects.create(chore=c, date=d(day), status=status, **kwargs)


def delete(client, chore_id, when=WED, tz=None, **data):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        return client.post(f"/chores/{chore_id}/delete/", data)


def state():
    """Everything a delete could change."""
    return (
        sorted(ChoreOccurrence.objects.values_list(
            "id", "chore_id", "date", "member_id", "status", "is_cancelled", "points", "done_at")),
        sorted(Chore.objects.values_list("id", "end_date", "title", "is_template")),
        sorted(RotationState.objects.values_list("id", "chore_id", "last_member_id")),
    )


def dates(c, **filters):
    return sorted(str(o.date) for o in ChoreOccurrence.objects.filter(chore=c, **filters))


STORED = re.compile(
    r'<article class="chore-card" id="occurrence-(\d+)">.*?</article>\s*'
    r'(?:<details class="delete-confirm">(.*?)</details>)?', re.S)
PROJECTED_WITH_DELETE = re.compile(r'<article class="chore-card">(?:(?!<article).)*?</article>\s*<details', re.S)
DETAILS = re.compile(r'<details class="delete-confirm">(.*?)</details>', re.S)


def controls(body):
    """{occurrence pk: inner html of its delete <details> ('' if none)}."""
    return {int(pk): inner for pk, inner in STORED.findall(body)}


def scopes(fragment):
    return re.findall(r'<button type="submit" name="scope" value="([^"]*)"', fragment)


def hidden(fragment):
    found = dict(re.findall(r'<input type="hidden" name="([a-z]+)" value="([^"]*)">', fragment))
    found.pop("csrfmiddlewaretoken", None)
    return found


def edit_page(client, c):
    response = client.get(f"/chores/{c.pk}/edit/")
    assert response.status_code == 200
    return response.content.decode()


# --- Model and migration -------------------------------------------------------------


@pytest.mark.django_db
def test_new_fields_default():
    c = weekly()
    o = row(c, "2026-11-18")
    c.refresh_from_db()
    o.refresh_from_db()
    assert c.end_date is None
    assert o.is_cancelled is False


def test_migration_name_and_dependency():
    module = importlib.import_module("chores.migrations.0007_chore_end_date_choreoccurrence_is_cancelled")
    migration = module.Migration
    assert migration.dependencies == [("chores", "0006_choreoccurrence")]
    added = {(op.model_name, op.name) for op in migration.operations}
    assert added == {("chore", "end_date"), ("choreoccurrence", "is_cancelled")}


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", "chores", check=True, dry_run=True, verbosity=0)


@pytest.mark.django_db
def test_end_date_not_on_forms_and_not_copied(client):
    assert "end_date" not in ChoreForm.Meta.fields
    assert "end_date" not in TemplateForm.Meta.fields
    c = weekly(end_date=d("2026-11-10"))
    response = client.post(f"/chores/{c.pk}/edit/", {
        "title": "Trash 2", "start_date": "2026-11-04", "assignment_mode": "unassigned",
        "recurrence": "weekly", "recurrence_day": "2", "points": "10",
        "end_date": "2030-01-01", "save_as_template": "on",
    })
    assert response.status_code == 302
    c.refresh_from_db()
    assert c.title == "Trash 2"
    assert c.end_date == d("2026-11-10")
    template = Chore.objects.get(is_template=True, is_builtin=False)
    assert template.end_date is None


# --- Projection and materialization --------------------------------------------------


@pytest.mark.django_db
def test_projection_stops_after_end_date():
    c = weekly(end_date=d("2026-11-18"))
    this_week = project_occurrences(d("2026-11-16"), d("2026-11-22"), today=d("2026-11-18"))
    assert [o["date"] for o in this_week if o["chore_id"] == c.pk] == [d("2026-11-18")]
    later = project_occurrences(d("2026-11-23"), d("2027-03-01"), today=d("2026-11-18"))
    assert [o for o in later if o["chore_id"] == c.pk] == []
    assert materialize_week(d("2026-11-23"), today=d("2026-11-25")) == []
    assert not ChoreOccurrence.objects.filter(chore=c).exists()


@pytest.mark.django_db
def test_projection_without_end_date_unchanged():
    c = weekly()
    later = project_occurrences(d("2026-11-23"), d("2026-12-06"), today=d("2026-11-18"))
    assert [o["date"] for o in later if o["chore_id"] == c.pk] == [d("2026-11-25"), d("2026-12-02")]


@pytest.mark.django_db
def test_cancelled_rows_still_count_as_stored():
    c = weekly()
    once = chore("Once", start_date="2026-11-19")
    row(once, "2026-11-19", is_cancelled=True)
    materialize_week(d("2026-11-16"), today=d("2026-11-18"))
    ChoreOccurrence.objects.filter(chore=c).update(is_cancelled=True)
    # Run again: no IntegrityError, nothing recreated, one-off not projected again.
    assert materialize_week(d("2026-11-16"), today=d("2026-11-18")) == []
    assert ChoreOccurrence.objects.count() == 2
    projected = project_occurrences(d("2026-11-16"), d("2026-11-29"), today=d("2026-11-18"))
    assert [(o["chore_id"], o["date"]) for o in projected] == [(c.pk, d("2026-11-25"))]


# --- Dashboard hides cancelled occurrences ---------------------------------------


@pytest.mark.django_db
def test_cancelled_hidden_everywhere(client):
    c = weekly()  # Wednesdays; the cancelled Nov 18 row stops it being materialized again
    cancelled = [
        row(c, "2026-11-10", is_cancelled=True),  # past week, and Overdue from an earlier week
        row(c, "2026-11-17", status="done", is_cancelled=True),  # Done earlier this week
        row(c, "2026-11-18", is_cancelled=True),  # Today
        row(c, "2026-11-20", is_cancelled=True),  # Upcoming
        row(c, "2026-11-25", is_cancelled=True),  # stored row on a future week
    ]
    ids = {o.pk for o in cancelled}
    for week in (None, "2026-W46", "2026-W48"):
        body = page(client, week)
        shown = {card["id"] for cards_ in sections(body).values() for card in cards_}
        assert not shown & ids, week
        assert not controls(body), week
        assert "No chores this week" in body, week
    assert ChoreOccurrence.objects.count() == len(cancelled)


@pytest.mark.django_db
def test_week_of_only_cancelled_rows_is_empty(client):
    c = chore("Once", start_date="2026-11-04")
    row(c, "2026-11-04", is_cancelled=True)
    body = page(client, "2026-W45")
    assert "No chores this week" in body


@pytest.mark.django_db
def test_future_weeks_after_end_date(client):
    c = weekly(end_date=d("2026-11-24"))
    row(c, "2026-12-02", status="done")  # history kept after end_date
    assert "No chores this week" in page(client, "2026-W48")  # no Planned card on Nov 25
    w49 = sections(page(client, "2026-W49"))["week"]
    assert [(card["title"], card["status"]) for card in w49] == [("Trash", "Done")]


# --- Where Delete appears ------------------------------------------------------------


@pytest.mark.django_db
def test_edit_page_has_delete_outside_chore_form(client):
    c = weekly()
    body = edit_page(client, c)
    form_start = body.index('<form id="chore-form"')
    form_end = body.index("</form>", form_start)
    details = DETAILS.search(body)
    assert details and details.start() > form_end
    inner = details.group(1)
    assert f'<form method="post" action="/chores/{c.pk}/delete/">' in inner
    assert hidden(inner) == {}
    assert scopes(inner) == ["future"]
    assert "This and all future occurrences, starting today" in inner


@pytest.mark.django_db
def test_no_delete_on_create_or_template_edit(client):
    assert "delete-confirm" not in client.get("/chores/create/").content.decode()
    template = Chore.objects.create(title="Tpl", start_date=d("2020-01-01"), is_template=True)
    body = client.get(f"/chores/templates/{template.pk}/edit/").content.decode()
    assert "delete-confirm" not in body
    assert f"/chores/{template.pk}/delete/" not in body


@pytest.mark.django_db
def test_every_stored_card_has_delete_projected_none(client):
    c = weekly("Daily", weekday=None, recurrence="daily", start="2026-11-01")
    past = row(c, "2026-11-10")  # Overdue from an earlier week, and on W46
    future = row(chore("Later", start_date="2026-11-25"), "2026-11-25", status="skipped")
    current = page(client)
    stored = controls(current)
    assert past.pk in stored
    assert len(stored) == 1 + 7  # overdue + materialized Nov 16-22 rows
    assert all(stored.values())
    assert controls(page(client, "2026-W46")).get(past.pk)
    w48 = page(client, "2026-W48")
    assert set(controls(w48)) == {future.pk} and controls(w48)[future.pk]
    assert 'class="status">Planned' in w48
    assert not PROJECTED_WITH_DELETE.search(w48)


@pytest.mark.django_db
@pytest.mark.parametrize("recurrence, status, expected", [
    ("weekly", "todo", ["this", "future"]),
    ("daily", "todo", ["this", "future"]),
    ("monthly", "todo", ["this", "future"]),
    ("weekly", "done", ["future"]),
    ("weekly", "skipped", ["future"]),
    ("once", "todo", ["future"]),
    ("once", "done", ["future"]),
])
def test_card_options(client, recurrence, status, expected):
    c = chore("Job", start_date="2026-11-19", recurrence=recurrence, recurrence_day=None)
    o = row(c, "2026-11-19", status=status)
    inner = controls(page(client))[o.pk]
    assert scopes(inner) == expected
    assert ("Delete this chore?" in inner) is (recurrence == "once")
    if expected == ["this", "future"]:
        assert ">This occurrence only</button>" in inner
        assert ">This and all future occurrences</button>" in inner
    assert 'value="this"' not in inner or status == "todo"


@pytest.mark.django_db
def test_overdue_card_offers_this(client):
    c = weekly()
    o = row(c, "2026-11-11")
    body = page(client)
    assert o.pk in [card["id"] for card in sections(body)["overdue"]]
    assert scopes(controls(body)[o.pk]) == ["this", "future"]


@pytest.mark.django_db
def test_one_off_edit_page_options(client):
    c = chore("Once", start_date="2026-11-19")
    inner = DETAILS.search(edit_page(client, c)).group(1)
    assert "Delete this chore?" in inner
    assert scopes(inner) == ["future"]
    assert '>Delete</button>' in inner


@pytest.mark.django_db
def test_confirmation_markup_no_js_no_htmx(client):
    c = weekly()
    o = row(c, "2026-11-19")
    for inner in (controls(page(client))[o.pk], DETAILS.search(edit_page(client, c)).group(1)):
        assert inner.lstrip().startswith('<summary role="button" class="outline contrast">Delete</summary>')
        assert f'<form method="post" action="/chores/{c.pk}/delete/">' in inner
        assert 'name="csrfmiddlewaretoken"' in inner
        assert re.search(r'<button type="button" class="secondary" onclick="[^"]*open = false[^"]*">Cancel</button>',
                         inner)
        for button in re.findall(r'<button type="submit"[^>]*>', inner):
            assert 'class="contrast"' in button
        assert "hx-" not in inner
    # Closed by default: nothing is open until "Delete" is clicked.
    assert "<details class=\"delete-confirm\" open" not in page(client)


@pytest.mark.django_db
def test_card_hidden_filter_inputs(client):
    mom = member("Mom", role="adult")
    c = weekly("Daily", weekday=None, recurrence="daily", start="2026-11-01")
    past = row(c, "2026-11-12", member=mom)
    plain = controls(page(client))
    assert all(hidden(inner) == {"occurrence": str(pk)} for pk, inner in plain.items())
    body = page(client, "2026-W46", data={"member": str(mom.pk), "day": "4"})
    assert hidden(controls(body)[past.pk]) == {
        "occurrence": str(past.pk), "week": "2026-W46", "member": str(mom.pk), "day": "4"}
    body = page(client, data={"member": "unassigned"})
    assert all(hidden(i).get("member") == "unassigned" and "week" not in hidden(i)
               for i in controls(body).values())


# --- "This occurrence only" ----------------------------------------------------------


@pytest.mark.django_db
def test_this_cancels_only_that_occurrence(client):
    a, b = member("A", order=0), member("B", order=1)
    c = weekly(mode="rotating", eligibility="kids")
    page(client)  # materializes Wed Nov 18 for A
    o = ChoreOccurrence.objects.get(chore=c)
    other = row(c, "2026-11-11", member=b)
    with at(WED):
        before_projection = [(p["date"], p["member_id"])
                             for p in project_occurrences(d("2026-11-23"), d("2026-12-20"))]
    before = state()
    response = delete(client, c.pk, scope="this", occurrence=o.pk)
    assert response.status_code == 302
    o.refresh_from_db()
    assert o.is_cancelled is True
    occs, chores_, rotation = state()
    assert rotation == before[2]
    assert RotationState.objects.get(chore=c).last_member == a
    assert chores_ == before[1]  # end_date untouched
    assert [x for x in occs if x[0] != o.pk] == [x for x in before[0] if x[0] != o.pk]
    other.refresh_from_db()
    assert other.is_cancelled is False
    # Not shown, not recreated.
    body = page(client)
    assert o.pk not in controls(body)
    assert materialize_week(d("2026-11-16"), today=d("2026-11-18")) == []
    with at(WED):
        after_projection = [(p["date"], p["member_id"])
                            for p in project_occurrences(d("2026-11-23"), d("2026-12-20"))]
    assert after_projection == before_projection
    assert [m for _, m in after_projection] == [b.pk, a.pk, b.pk, a.pk]


# --- "This and future" ------------------------------------------------------------------


@pytest.fixture
def history_chore(db):
    """Weekly Wednesday chore: done Nov 4, todo Nov 11 (overdue), cancelled Nov 18,
    todo Nov 25 (stored ahead), skipped Dec 2."""
    c = weekly()
    rows = {
        "done": row(c, "2026-11-04", status="done"),
        "old": row(c, "2026-11-11"),
        "cancelled": row(c, "2026-11-18", is_cancelled=True),
        "ahead": row(c, "2026-11-25"),
        "skipped": row(c, "2026-12-02", status="skipped"),
    }
    return c, rows


@pytest.mark.django_db
def test_future_from_card(client, history_chore):
    c, rows = history_chore
    done_before = ChoreOccurrence.objects.filter(status="done").values_list("points", "done_at").get()
    response = delete(client, c.pk, scope="future", occurrence=rows["ahead"].pk)
    assert response.status_code == 302
    c.refresh_from_db()
    assert c.end_date == d("2026-11-24")
    assert dates(c) == ["2026-11-04", "2026-11-11", "2026-11-18", "2026-12-02"]
    assert ChoreOccurrence.objects.filter(status="done").values_list("points", "done_at").get() == done_before


@pytest.mark.django_db
def test_future_removes_cancelled_todo_on_or_after_d(client, history_chore):
    c, rows = history_chore
    delete(client, c.pk, scope="future", occurrence=rows["old"].pk)
    c.refresh_from_db()
    assert c.end_date == d("2026-11-10")
    assert dates(c) == ["2026-11-04", "2026-12-02"]


@pytest.mark.django_db
def test_future_keeps_earlier_end_date(client, history_chore):
    c, rows = history_chore
    Chore.objects.filter(pk=c.pk).update(end_date=d("2026-11-01"))
    delete(client, c.pk, scope="future", occurrence=rows["ahead"].pk)
    c.refresh_from_db()
    assert c.end_date == d("2026-11-01")


@pytest.mark.django_db
def test_future_later_end_date_is_moved_earlier(client, history_chore):
    c, rows = history_chore
    Chore.objects.filter(pk=c.pk).update(end_date=d("2027-01-01"))
    delete(client, c.pk, scope="future", occurrence=rows["ahead"].pk)
    c.refresh_from_db()
    assert c.end_date == d("2026-11-24")


@pytest.mark.django_db
def test_future_without_history_deletes_chore(client):
    a = member("A")
    template = Chore.objects.create(title="Tpl", start_date=d("2020-01-01"), is_template=True)
    c = weekly(mode="rotating", eligibility="kids", template_source=template)
    page(client)
    assert RotationState.objects.filter(chore=c).exists()
    o = ChoreOccurrence.objects.get(chore=c)
    response = delete(client, c.pk, scope="future", occurrence=o.pk)
    assert response.status_code == 302
    assert not Chore.objects.filter(pk=c.pk).exists()
    assert not ChoreOccurrence.objects.filter(chore_id=c.pk).exists()
    assert not RotationState.objects.filter(chore_id=c.pk).exists()
    assert Chore.objects.filter(pk=template.pk, title="Tpl").exists()
    assert client.get(f"/chores/{c.pk}/edit/").status_code == 404
    assert a.pk


@pytest.mark.django_db
def test_future_never_writes_rotation(client):
    a, b = member("A", order=0), member("B", order=1)
    c = weekly(mode="rotating", eligibility="kids")
    row(c, "2026-11-11", status="done", member=a)
    page(client)
    last = RotationState.objects.get(chore=c).last_member_id
    o = ChoreOccurrence.objects.get(chore=c, date=d("2026-11-18"))
    delete(client, c.pk, scope="future", occurrence=o.pk)
    assert RotationState.objects.get(chore=c).last_member_id == last
    assert b.pk


@pytest.mark.django_db
def test_future_from_overdue_card_of_last_week(client):
    c = weekly("Thursday", weekday=3, start="2026-10-29")
    row(c, "2026-11-05", status="done")
    overdue = row(c, "2026-11-12")
    body = page(client)  # materializes Thu Nov 19
    assert overdue.pk in [card["id"] for card in sections(body)["overdue"]]
    assert dates(c, status="todo") == ["2026-11-12", "2026-11-19"]
    response = delete(client, c.pk, scope="future", occurrence=overdue.pk)
    assert response["Location"] == "/dashboard/"
    c.refresh_from_db()
    assert c.end_date == d("2026-11-11")
    page(client)
    assert dates(c) == ["2026-11-05"]


@pytest.mark.django_db
@pytest.mark.parametrize("tz, start", [
    ("Pacific/Kiritimati", "2026-11-22"),
    (None, "2026-11-21"),
    ("Not/AZone", "2026-11-21"),
])
def test_future_from_edit_page_uses_viewer_today(client, tz, start):
    c = weekly("Daily", weekday=None, recurrence="daily", start="2026-11-16")
    row(c, "2026-11-16", status="done")
    for day in ("2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23"):
        row(c, day)
    response = delete(client, c.pk, when=SAT_NOON, tz=tz, scope="future")
    assert response.status_code == 302
    assert response["Location"] == "/dashboard/"
    c.refresh_from_db()
    assert c.end_date == d(start) - datetime.timedelta(days=1)
    kept = [day for day in dates(c, status="todo")]
    assert kept == [day for day in ("2026-11-19", "2026-11-20", "2026-11-21") if day < start]
    assert client.get(f"/chores/{c.pk}/edit/").status_code == 200


# --- One-off chores ------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("with_row, scope", [
    (False, "future"), (True, "future"), (True, "this"), (True, None), (False, "junk"),
])
def test_one_off_without_history_is_deleted(client, with_row, scope):
    c = chore("Once", start_date="2026-11-19")
    data = {} if scope is None else {"scope": scope}
    if with_row:
        data["occurrence"] = row(c, "2026-11-19").pk
    response = delete(client, c.pk, **data)
    assert response.status_code == 302
    assert not Chore.objects.filter(pk=c.pk).exists()
    assert ChoreOccurrence.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["done", "skipped"])
def test_one_off_with_history_is_kept(client, status):
    c = chore("Once", start_date="2026-11-17")
    o = row(c, "2026-11-17", status=status)
    response = delete(client, c.pk, scope="future", occurrence=o.pk)
    assert response.status_code == 302
    c.refresh_from_db()
    assert c.end_date == d("2026-11-16")
    assert dates(c) == ["2026-11-17"]
    # Still shown in its week, never projected again.
    body = page(client)
    assert [card["title"] for card in sections(body)["done-earlier"]] == ["Once"]
    assert client.get(f"/chores/{c.pk}/edit/").status_code == 200
    # An earlier end_date stays, and repeating changes nothing.
    Chore.objects.filter(pk=c.pk).update(end_date=d("2026-01-01"))
    before = state()
    assert delete(client, c.pk).status_code == 302
    assert state() == before


# --- Atomic -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_failure_rolls_back_everything(client, history_chore):
    c, rows = history_chore
    before = state()
    with mock.patch.object(Chore, "save", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            delete(client, c.pk, scope="future", occurrence=rows["ahead"].pk)
    assert state() == before


# --- Redirect and message ----------------------------------------------------------------


@pytest.mark.django_db
def test_message_shown_once_and_rows_gone(client, history_chore):
    c, rows = history_chore
    with at(WED):
        response = client.post(f"/chores/{c.pk}/delete/",
                               {"scope": "future", "occurrence": rows["old"].pk}, follow=True)
        body = response.content.decode()
        again = client.get("/dashboard/").content.decode()
    assert response.redirect_chain == [("/dashboard/", 302)]
    assert body.count('<p class="message" role="status"><mark>Chore deleted</mark></p>') == 1
    assert "Chore deleted" not in again
    assert controls(body) == {}  # Nov 11 and Nov 18 gone from this week
    w49 = page(client, "2026-W49")
    assert list(controls(w49)) == [rows["skipped"].pk]


@pytest.mark.django_db
def test_redirect_keeps_filters_from_rendered_form(client):
    mom = member("Mom", role="adult")
    c = weekly("Daily", weekday=None, recurrence="daily", start="2026-11-01")
    o = row(c, "2026-11-12", member=mom)
    body = page(client, "2026-W46", data={"member": str(mom.pk), "day": "4"})
    data = hidden(controls(body)[o.pk])
    response = delete(client, c.pk, scope="this", **data)
    assert response["Location"] == f"/dashboard/?week=2026-W46&member={mom.pk}&day=4"


@pytest.mark.django_db
def test_redirect_overdue_from_current_week_goes_to_current_week(client):
    c = weekly()
    o = row(c, "2026-11-11")
    data = hidden(controls(page(client))[o.pk])
    assert data == {"occurrence": str(o.pk)}
    assert delete(client, c.pk, scope="this", **data)["Location"] == "/dashboard/"


@pytest.mark.django_db
@pytest.mark.parametrize("extra, expected", [
    ({}, "/dashboard/"),
    ({"week": "2026-W46", "member": "unassigned", "day": "7"}, "/dashboard/?week=2026-W46&member=unassigned&day=7"),
    ({"week": "2026-W99"}, "/dashboard/"),
    ({"week": "2026-W47", "day": "9"}, "/dashboard/?week=2026-W47"),
    ({"member": "abc"}, "/dashboard/"),
    ({"member": "GONE"}, "/dashboard/"),
    ({"member": OVERFLOW, "day": "0"}, "/dashboard/"),
    ({"week": "//evil.example/", "member": HOSTILE, "day": HOSTILE}, "/dashboard/"),
    ({"week": "https://evil.example/2026-W46"}, "/dashboard/"),
])
def test_redirect_normalizes_values(client, extra, expected):
    gone = member("Gone")
    gone_pk = gone.pk
    gone.delete()
    extra = {k: (str(gone_pk) if v == "GONE" else v) for k, v in extra.items()}
    c = weekly()
    o = row(c, "2026-11-11", status="done")
    response = delete(client, c.pk, scope="future", occurrence=o.pk, **extra)
    assert response.status_code == 302
    assert response["Location"] == expected


@pytest.mark.django_db
def test_redirect_from_edit_page(client, history_chore):
    c, _ = history_chore
    assert delete(client, c.pk, scope="future")["Location"] == "/dashboard/"


# --- Errors ------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "put", "patch", "delete", "head", "options"])
@pytest.mark.parametrize("pk", ["1", OVERFLOW, "0"])
def test_non_post_is_405(client, history_chore, method, pk):
    c, _ = history_chore
    before = state()
    url = f"/chores/{c.pk if pk == '1' else pk}/delete/"
    assert getattr(client, method)(url).status_code == 405
    assert state() == before


@pytest.mark.django_db
def test_post_without_csrf_is_403(history_chore):
    c, rows = history_chore
    before = state()
    strict = Client(enforce_csrf_checks=True)
    response = strict.post(f"/chores/{c.pk}/delete/", {"scope": "this", "occurrence": rows["old"].pk})
    assert response.status_code == 403
    assert state() == before


@pytest.mark.django_db
@pytest.mark.parametrize("pk", ["0", OVERFLOW, "9223372036854775808", "424242"])
def test_bad_chore_pk_is_404(client, history_chore, pk):
    before = state()
    assert delete(client, pk, scope="future").status_code == 404
    assert state() == before


@pytest.mark.django_db
def test_templates_are_404(client):
    builtin = Chore.objects.filter(is_builtin=True).first()
    assert builtin is not None
    household = Chore.objects.create(title="Mine", start_date=d("2020-01-01"), is_template=True)
    derived = weekly(template_source=household)
    before = state()
    for template in (builtin, household):
        for scope in ("future", "this"):
            assert delete(client, template.pk, scope=scope).status_code == 404
    assert state() == before
    assert delete(client, derived.pk, scope="future").status_code == 302
    household.refresh_from_db()
    assert household.title == "Mine" and household.is_template


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["", "abc", "-1", "1.5", "0", OVERFLOW, " 1", "١", HOSTILE,
                                   "UNKNOWN", "OTHER", "CANCELLED"])
@pytest.mark.parametrize("scope", ["this", "future", "junk"])
def test_bad_occurrence_is_404(client, history_chore, value, scope):
    c, rows = history_chore
    other = row(weekly("Other"), "2026-11-18")
    value = {"UNKNOWN": "424242", "OTHER": str(other.pk),
             "CANCELLED": str(rows["cancelled"].pk)}.get(value, value)
    before = state()
    response = delete(client, c.pk, scope=scope, occurrence=value)
    assert response.status_code == 404
    assert HOSTILE not in response.content.decode()
    assert state() == before


@pytest.mark.django_db
@pytest.mark.parametrize("scope", [None, "", "This", "FUTURE", "all", " this", "this ", HOSTILE])
def test_bad_scope_on_recurring_is_400(client, history_chore, scope):
    c, rows = history_chore
    before = state()
    for occurrence in (None, rows["old"].pk):
        data = {} if scope is None else {"scope": scope}
        if occurrence:
            data["occurrence"] = occurrence
        response = delete(client, c.pk, **data)
        assert response.status_code == 400
        assert HOSTILE not in response.content.decode()
        assert html.escape(HOSTILE) not in response.content.decode()
    assert state() == before


@pytest.mark.django_db
@pytest.mark.parametrize("key", [None, "done", "skipped"])
def test_this_needs_a_todo_occurrence(client, history_chore, key):
    c, rows = history_chore
    data = {"scope": "this"}
    if key:
        data["occurrence"] = rows[key].pk
    before = state()
    assert delete(client, c.pk, **data).status_code == 400
    assert state() == before


@pytest.mark.django_db
def test_error_order(client, history_chore):
    c, rows = history_chore
    template = Chore.objects.create(title="Tpl", start_date=d("2020-01-01"), is_template=True)
    # Chore lookup before occurrence; occurrence before scope; scope before "this" checks.
    assert delete(client, template.pk, scope="junk", occurrence="abc").status_code == 404
    assert delete(client, c.pk, scope="junk", occurrence="abc").status_code == 404
    assert delete(client, c.pk, scope="junk", occurrence=rows["done"].pk).status_code == 400
    assert client.get(f"/chores/{OVERFLOW}/delete/").status_code == 405


# --- Repeated posts -----------------------------------------------------------------------


@pytest.mark.django_db
def test_repeat_this_is_404(client, history_chore):
    c, rows = history_chore
    assert delete(client, c.pk, scope="this", occurrence=rows["old"].pk).status_code == 302
    before = state()
    assert delete(client, c.pk, scope="this", occurrence=rows["old"].pk).status_code == 404
    assert state() == before


@pytest.mark.django_db
def test_repeat_future_from_todo_card_is_404(client, history_chore):
    c, rows = history_chore
    assert delete(client, c.pk, scope="future", occurrence=rows["ahead"].pk).status_code == 302
    before = state()
    assert delete(client, c.pk, scope="future", occurrence=rows["ahead"].pk).status_code == 404
    assert state() == before


@pytest.mark.django_db
def test_repeat_future_on_deleted_chore_is_404(client):
    c = weekly()
    o = row(c, "2026-11-18")
    assert delete(client, c.pk, scope="future", occurrence=o.pk).status_code == 302
    assert delete(client, c.pk, scope="future", occurrence=o.pk).status_code == 404
    assert delete(client, c.pk, scope="future").status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize("from_history", [True, False])
def test_repeat_future_on_kept_chore_changes_nothing(client, history_chore, from_history):
    c, rows = history_chore
    data = {"scope": "future"}
    if from_history:
        data["occurrence"] = rows["skipped"].pk
    assert delete(client, c.pk, **data).status_code == 302
    before = state()
    assert delete(client, c.pk, **data).status_code == 302
    assert state() == before


# --- No history: dates before D stay (#42) -------------------------------------------------

from django.db import connection  # noqa: E402
from django.utils import timezone  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from chores.views import _delete_from  # noqa: E402

TODAY = d("2026-11-18")


def daily_dishes():
    return weekly("Dishes", weekday=None, recurrence="daily", start="2026-11-16")


def dishes_week(client):
    """Daily Dishes from Mon 11-16 with the current week stored by the dashboard."""
    c = daily_dishes()
    page(client)
    assert dates(c) == [f"2026-11-{n}" for n in range(16, 23)]
    return c


def calendar_dots(client, title, month=None):
    """{day number: dot count for ``title``} from the calendar page."""
    with at(WED):
        response = client.get("/calendar/", {"month": month} if month else {})
    assert response.status_code == 200
    found = {}
    for cell in re.findall(r"<td[^>]*>(.*?)</td>", response.content.decode(), re.S):
        day = re.search(r'class="day-number"[^>]*>(\d+)<', cell)
        n = len(re.findall(rf'title="{title} · ', cell))
        if day and n:
            found[int(day.group(1))] = n
    return found


def screen_titles(body):
    return {key: [card["title"] for card in cards_] for key, cards_ in sections(body).items()}


@pytest.mark.django_db
def test_a_card_in_current_week_keeps_rows_before_d(client):
    c = dishes_week(client)
    before = {o.date: (o.pk, o.member_id, o.points) for o in c.occurrences.all()}
    friday = c.occurrences.get(date=d("2026-11-20"))
    assert delete(client, c.pk, scope="future", occurrence=friday.pk).status_code == 302
    c.refresh_from_db()
    assert c.end_date == d("2026-11-19")
    assert dates(c) == [f"2026-11-{n}" for n in range(16, 20)]
    for o in c.occurrences.all():
        assert (o.pk, o.member_id, o.points) == before[o.date]
    assert client.get(f"/chores/{c.pk}/edit/").status_code == 200


@pytest.mark.django_db
def test_a_screens_afterwards(client):
    c = dishes_week(client)
    pk = c.pk
    with at(WED):
        history_before = no_csrf(client.get("/history/").content.decode())
        progress_before = no_csrf(client.get("/progress/").content.decode())
    friday = c.occurrences.get(date=d("2026-11-20"))
    delete(client, pk, scope="future", occurrence=friday.pk)

    body = page(client)
    shown = sections(body)
    assert [(card["title"], card["date"]) for card in shown["overdue"]] == [
        ("Dishes", shown["overdue"][0]["date"]), ("Dishes", shown["overdue"][1]["date"])]
    assert len(shown["overdue"]) == 2
    assert [card["id"] for card in shown["overdue"]] == list(
        c.occurrences.filter(date__in=[d("2026-11-16"), d("2026-11-17")]).order_by("date").values_list("pk", flat=True))
    assert len(shown["today"]) == 1 and len(shown["upcoming"]) == 1
    assert shown["upcoming"][0]["id"] == c.occurrences.get(date=d("2026-11-19")).pk
    for key in ("overdue", "today", "upcoming"):
        for card in shown[key]:
            fragment = controls(body)[card["id"]]
            assert scopes(fragment) == ["this", "future"]

    snap = snapshot_rows()
    w48 = page(client, "2026-W48")
    assert "Dishes" not in {t for ts in screen_titles(w48).values() for t in ts}
    assert "Planned" not in w48 or "Dishes" not in w48
    assert snapshot_rows() == snap
    assert dates(c) == [f"2026-11-{n}" for n in range(16, 20)]

    assert calendar_dots(client, "Dishes") == {16: 1, 17: 1, 18: 1, 19: 1}
    assert calendar_dots(client, "Dishes", "2026-12") == {}
    with at(WED):
        calendar = client.get("/calendar/").content.decode()
    assert calendar.count("dot-overdue") >= 2

    with at(WED):
        assert no_csrf(client.get("/history/").content.decode()) == history_before
        progress = no_csrf(client.get("/progress/").content.decode())
    assert progress == progress_before
    assert f'id="streak-{pk}"' not in progress


def no_csrf(body):
    return re.sub(r'csrf[A-Za-z_]*"?[:=] ?[\'"][A-Za-z0-9]+[\'"]', "csrf", body, flags=re.I)


def snapshot_rows():
    return sorted(ChoreOccurrence.objects.values_list("id", "chore_id", "date", "status", "member_id"))


@pytest.mark.django_db
def test_b_overdue_rotating_then_nothing_left(client):
    a, b = member("A", order=0), member("B", order=1)
    c = weekly("Trash", mode="rotating", eligibility="kids")
    old = row(c, "2026-11-11", member=a)
    RotationState.objects.update_or_create(chore=c, defaults={"last_member": a})
    body = page(client)  # stores 11-18
    assert [card["id"] for card in sections(body)["overdue"]] == [old.pk]
    last = RotationState.objects.get(chore=c).last_member_id
    wed = c.occurrences.get(date=d("2026-11-18"))

    assert delete(client, c.pk, scope="future", occurrence=wed.pk).status_code == 302
    c.refresh_from_db()
    assert c.end_date == d("2026-11-17")
    assert dates(c) == ["2026-11-11"]
    old.refresh_from_db()
    assert old.member_id == a.pk
    assert [card["id"] for card in sections(page(client))["overdue"]] == [old.pk]
    assert RotationState.objects.get(chore=c).last_member_id == last  # kept, not written

    # Repeat from the edit page (D = 11-18) writes nothing and answers 302.
    before = state()
    assert delete(client, c.pk, scope="future").status_code == 302
    assert state() == before

    assert delete(client, c.pk, scope="future", occurrence=old.pk).status_code == 302
    assert not Chore.objects.filter(pk=c.pk).exists()
    assert not RotationState.objects.filter(chore_id=c.pk).exists()
    assert not ChoreOccurrence.objects.filter(chore_id=c.pk).exists()
    assert client.get(f"/chores/{c.pk}/edit/").status_code == 404
    assert delete(client, c.pk, scope="future", occurrence=old.pk).status_code == 404
    assert b.pk


@pytest.mark.django_db
@pytest.mark.parametrize("tz, d_day, kept", [
    (None, "2026-11-18", ["2026-11-16", "2026-11-17"]),
    ("Pacific/Kiritimati", "2026-11-19", ["2026-11-16", "2026-11-17", "2026-11-18"]),
])
def test_c_edit_page_uses_viewer_today_once(client, tz, d_day, kept):
    c = dishes_week(client)
    with mock.patch("chores.views.timezone.localdate", wraps=timezone.localdate) as spy_:
        response = delete(client, c.pk, tz=tz, scope="future")
    assert response.status_code == 302
    assert spy_.call_count == 1
    c.refresh_from_db()
    assert c.end_date == d(d_day) - datetime.timedelta(days=1)
    assert dates(c) == kept
    overdue = [card["date"] for card in sections(page(client, tz=tz))["overdue"]]
    assert len(overdue) == len([k for k in kept if k < str(TODAY if tz is None else d("2026-11-19"))])


@pytest.mark.django_db
def test_d_only_cancelled_rows_before_d(client):
    c = weekly("Trash")
    cancelled = row(c, "2026-11-11", is_cancelled=True)
    wed = row(c, "2026-11-18")
    assert delete(client, c.pk, scope="future", occurrence=wed.pk).status_code == 302
    assert not Chore.objects.filter(pk=c.pk).exists()
    assert not ChoreOccurrence.objects.filter(pk=cancelled.pk).exists()


@pytest.mark.django_db
def test_e_projected_dates_before_d_nothing_stored(db):
    bins = weekly("Bins", weekday=3, start="2026-11-26")
    _delete_from(bins, d("2026-12-10"), TODAY)
    bins.refresh_from_db()
    assert bins.end_date == d("2026-12-09")
    projected = [(o["chore_id"], str(o["date"])) for o in
                 project_occurrences(d("2026-11-23"), d("2026-12-20"), today=TODAY)]
    assert projected == [(bins.pk, "2026-11-26"), (bins.pk, "2026-12-03")]

    other = weekly("Bins2", weekday=3, start="2026-11-26")
    _delete_from(other, d("2026-11-26"), TODAY)
    assert not Chore.objects.filter(pk=other.pk).exists()


@pytest.mark.django_db
def test_e_edit_page_before_start_date_deletes_chore(client):
    bins = weekly("Bins", weekday=3, start="2026-11-26")
    assert delete(client, bins.pk, scope="future").status_code == 302
    assert not Chore.objects.filter(pk=bins.pk).exists()


@pytest.mark.django_db
def test_f_current_week_not_materialized_yet(client):
    c = daily_dishes()
    assert ChoreOccurrence.objects.count() == 0
    assert delete(client, c.pk, scope="future").status_code == 302
    c.refresh_from_db()
    assert c.end_date == d("2026-11-17")
    assert client.get(f"/chores/{c.pk}/edit/").status_code == 200
    body = page(client)
    assert dates(c) == ["2026-11-16", "2026-11-17"]
    assert [card["title"] for card in sections(body)["overdue"]] == ["Dishes", "Dishes"]


@pytest.mark.django_db
def test_future_start_chore_with_projection_before_d_is_trimmed_not_deleted(client):
    c = weekly("Bins", weekday=3, start="2026-11-26")
    before = state()
    _delete_from(c, d("2026-12-03"), TODAY)
    c.refresh_from_db()
    assert c.end_date == d("2026-12-02")
    assert state() != before


@pytest.mark.django_db
def test_kept_chore_does_not_write_rotation(client):
    a, b = member("A", order=0), member("B", order=1)
    c = weekly("Trash", mode="rotating", eligibility="kids")
    row(c, "2026-11-11", member=a)
    RotationState.objects.update_or_create(chore=c, defaults={"last_member": b})
    wed = row(c, "2026-11-18", member=b)
    delete(client, c.pk, scope="future", occurrence=wed.pk)
    assert RotationState.objects.get(chore=c).last_member_id == b.pk


@pytest.mark.django_db
def test_one_off_with_overdue_row_is_still_deleted(client):
    c = chore("Once", start_date="2026-11-16")
    o = row(c, "2026-11-16")
    assert delete(client, c.pk, scope="future", occurrence=o.pk).status_code == 302
    assert not Chore.objects.filter(pk=c.pk).exists()


@pytest.mark.django_db
def test_no_history_failure_rolls_back_everything(client):
    c = dishes_week(client)
    friday = c.occurrences.get(date=d("2026-11-20"))
    before = state()
    with mock.patch.object(Chore, "save", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            delete(client, c.pk, scope="future", occurrence=friday.pk)
    assert state() == before


@pytest.mark.django_db
def test_query_count_does_not_depend_on_stored_rows(client):
    counts = []
    for extra in (0, 14):
        ChoreOccurrence.objects.all().delete()
        Chore.objects.filter(title="Dishes").delete()
        c = dishes_week(client)
        for n in range(extra):
            row(c, str(d("2026-11-23") + datetime.timedelta(days=n)))
        friday = c.occurrences.get(date=d("2026-11-20"))
        with CaptureQueriesContext(connection) as queries:
            delete(client, c.pk, scope="future", occurrence=friday.pk)
        counts.append(len(queries))
    assert counts[0] == counts[1]
