"""Each stored occurrence keeps its own title and instructions (#39).

Examples use #20's ``dishes`` fixture: today = Wed 2026-11-18 (UTC), current
week Mon 16 - Sun 22 Nov, daily "Dishes" (instructions empty), start 1 Nov,
fixed Mom, 10 pts, rows 16 done, 17 skipped, 18 todo, 19 todo override
(Emma), 20 todo, 21 done, 22 todo.
"""
import datetime
import html
import importlib
import re

import pytest
from django.db import connection, migrations, models
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores.models import Chore, ChoreOccurrence, OccurrenceException
from chores.services import create_occurrence, materialize_one_off, materialize_week, update_occurrence
from tests.test_dashboard import WED, WRITE_SQL, at, cards, chore, d, member, page
from tests.test_edit_scope import (
    CHORE_FORM, OCC_FORM, body, daily, dishes, future, messages_of, nov, post, projected, rows_of,
    snapshot, this, values, weekly,
)
from tests.test_one_off_edit import edit as one_off_edit
from tests.test_one_off_edit import stored, vacuum

MIGRATION = "0010_choreoccurrence_title_instructions"
DAY_CELL = re.compile(r'<a class="day-number"[^>]*>(\d+)</a>(.*?)</td>', re.S)
DOT_TITLE = re.compile(r'<span class="dot [^"]*" title="([^"]*) · [^"]*"></span>')


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def family():
    return member("Mom", "adult", 0), member("Emma", "kid", 1), member("Leo", "kid", 2)


# --- Helpers ---------------------------------------------------------------------


def labels(o):
    return tuple(ChoreOccurrence.objects.filter(pk=o.pk).values_list("title", "instructions").get())


def card_titles(text):
    """``{occurrence pk: card title}`` for the stored cards in a dashboard body."""
    return {c["id"]: c["title"] for c in cards(text) if c["id"] is not None}


def history(client):
    with at(WED):
        response = client.get("/history/")
    assert response.status_code == 200
    return response.content.decode()


def history_titles(text):
    return [html.unescape(t) for t in re.findall(r'<strong class="title">(.*?)</strong>', text)]


def calendar_dots(client, when=WED):
    """``{day of the month: [dot title]}`` on the current month."""
    with at(when):
        text = client.get("/calendar/").content.decode()
    return {int(day): DOT_TITLE.findall(inner) for day, inner in DAY_CELL.findall(text)}


def occurrence_writes(queries):
    return [q["sql"] for q in queries
            if WRITE_SQL.match(q["sql"]) and '"chores_choreoccurrence"' in q["sql"]]


# --- Model and migration -----------------------------------------------------------


def test_fields_are_non_null_without_default():
    title = ChoreOccurrence._meta.get_field("title")
    instructions = ChoreOccurrence._meta.get_field("instructions")
    assert isinstance(title, models.CharField) and title.max_length == 100
    assert not title.null and not title.blank and not title.has_default()
    assert isinstance(instructions, models.TextField)
    assert not instructions.null and instructions.blank and not instructions.has_default()


def test_migration_file_operations():
    migration = importlib.import_module(f"chores.migrations.{MIGRATION}").Migration
    assert migration.dependencies == [("chores", "0009_occurrenceexception")]
    add_title, add_instructions, backfill = migration.operations
    assert (type(add_title), add_title.name, add_title.preserve_default) == (migrations.AddField, "title", False)
    assert (add_title.field.max_length, add_title.field.default) == (100, "")
    assert (type(add_instructions), add_instructions.name, add_instructions.preserve_default) == (
        migrations.AddField, "instructions", False)
    assert add_instructions.field.blank and add_instructions.field.default == ""
    assert isinstance(backfill, migrations.RunPython)
    assert backfill.reverse_code is migrations.RunPython.noop


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_migration_backfills_every_row_and_reverses():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("chores", "0009_occurrenceexception")])
        old = executor.loader.project_state([("chores", "0009_occurrenceexception")]).apps
        OldChore, OldRow = old.get_model("chores", "Chore"), old.get_model("chores", "ChoreOccurrence")
        bins = OldChore.objects.create(title="Bins", instructions="Blue bin", start_date=d("2026-11-01"))
        dish = OldChore.objects.create(title="Dishes", start_date=d("2026-11-01"))
        made = {
            "done": OldRow.objects.create(chore=bins, date=d("2026-11-16"), status="done", points=10),
            "skipped": OldRow.objects.create(chore=bins, date=d("2026-11-17"), status="skipped", points=10),
            "todo": OldRow.objects.create(chore=dish, date=d("2026-11-18"), points=10),
            "cancelled": OldRow.objects.create(chore=dish, date=d("2026-11-19"), points=10, is_cancelled=True),
        }

        executor = MigrationExecutor(connection)
        executor.migrate([("chores", MIGRATION)])
        new = executor.loader.project_state([("chores", MIGRATION)]).apps
        NewRow = new.get_model("chores", "ChoreOccurrence")
        got = {k: tuple(NewRow.objects.filter(pk=o.pk).values_list("title", "instructions").get())
               for k, o in made.items()}
        assert got == {
            "done": ("Bins", "Blue bin"), "skipped": ("Bins", "Blue bin"),
            "todo": ("Dishes", ""), "cancelled": ("Dishes", ""),
        }

        executor = MigrationExecutor(connection)
        executor.migrate([("chores", "0009_occurrenceexception")])
        with connection.cursor() as cursor:
            columns = {c.name for c in connection.introspection.get_table_description(
                cursor, "chores_choreoccurrence")}
        assert "title" not in columns and "instructions" not in columns
        assert OldRow.objects.count() == 4
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db
def test_save_fills_an_empty_title_on_insert_only():
    c = chore("Bins", instructions="Blue bin")
    filled = ChoreOccurrence.objects.create(chore=c, date=d("2026-11-16"), points=10)
    assert labels(filled) == ("Bins", "Blue bin")
    own = ChoreOccurrence.objects.create(chore=c, date=d("2026-11-17"), points=10, title="Own")
    assert labels(own) == ("Own", "")  # a title was given: nothing is copied
    filled.title, filled.instructions = "", ""
    filled.save()
    assert labels(filled) == ("", "")  # an update never copies


# --- Every stored row gets a snapshot ------------------------------------------------


@pytest.mark.django_db
def test_materialize_week_snapshots_every_row(family):
    bins = weekly("Bins", 3, "2026-11-01", instructions="Blue bin")
    dish = daily(instructions="Hot water")
    OccurrenceException.objects.create(chore=dish, date=d(nov(19)), is_override=True, member=family[1])
    OccurrenceException.objects.create(chore=dish, date=d(nov(20)), is_cancelled=True)
    materialize_week(d("2026-11-16"), today=d("2026-11-18"))
    assert {labels(o) for o in bins.occurrences.all()} == {("Bins", "Blue bin")}
    assert bins.occurrences.count() == 1
    rows = rows_of(dish)
    assert sorted(rows) == list(range(16, 23))
    assert values(rows[19])["is_override"] and values(rows[20])["is_cancelled"]
    assert {labels(o) for o in rows.values()} == {("Dishes", "Hot water")}


@pytest.mark.django_db
def test_create_occurrence_and_materialize_one_off_snapshot():
    c = daily("Bins", instructions="Blue bin")
    assert labels(create_occurrence(c, d(nov(16)))) == ("Bins", "Blue bin")
    once = chore("Vacuum", start_date=nov(20), instructions="Under the bed")
    assert labels(materialize_one_off(once, d("2026-11-18"))) == ("Vacuum", "Under the bed")


@pytest.mark.django_db
def test_row_stored_after_a_rename_has_the_new_title():
    c = daily()
    before = create_occurrence(c, d(nov(16)))
    Chore.objects.filter(pk=c.pk).update(title="Wash up")
    c.refresh_from_db()
    after = create_occurrence(c, d(nov(17)))
    assert labels(before) == ("Dishes", "") and labels(after) == ("Wash up", "")


# --- This and future ----------------------------------------------------------------


@pytest.mark.django_db
def test_rename_from_the_20th_relabels_only_followers(client, family):
    c, rows = dishes(family)
    future(client, c, rows[20], title="Wash up", points="20")
    assert {day: values(o)["title"] for day, o in rows.items()} == {
        16: "Dishes", 17: "Dishes", 18: "Dishes", 19: "Dishes", 20: "Wash up", 21: "Dishes", 22: "Wash up",
    }
    shown = card_titles(page(client))
    assert {day: shown[o.pk] for day, o in rows.items()} == {
        16: "Dishes", 17: "Dishes", 18: "Dishes", 19: "Dishes", 20: "Wash up", 21: "Dishes", 22: "Wash up",
    }
    text = history(client)
    assert history_titles(text) == ["Dishes", "Dishes", "Dishes"]  # 21 done, 17 skipped, 16 done
    assert "Wash up" not in text
    assert {p["title"] for p in projected(c, "2026-11-23", "2026-11-29")} == {"Wash up"}
    w48 = [c_["title"] for c_ in cards(page(client, "2026-W48"))]
    assert w48 and set(w48) == {"Wash up"}


@pytest.mark.django_db
def test_rename_from_the_18th_leaves_the_override_row(client, family):
    c, rows = dishes(family)
    future(client, c, rows[18], title="Wash up")
    titles = {day: values(o)["title"] for day, o in rows.items()}
    assert titles == {16: "Dishes", 17: "Dishes", 18: "Wash up", 19: "Dishes", 20: "Wash up",
                      21: "Dishes", 22: "Wash up"}


@pytest.mark.django_db
def test_instructions_only_rewrites_only_follower_instructions(client, family):
    c, rows = dishes(family)
    ChoreOccurrence.objects.filter(pk=rows[22].pk).update(title="Kept")  # crafted: differs from the chore
    with CaptureQueriesContext(connection) as queries:
        future(client, c, rows[20], instructions="Hot water")
    follower_writes = [s for s in occurrence_writes(queries) if s.startswith("UPDATE")]
    assert len(follower_writes) == 2
    assert all('"instructions"' in s for s in follower_writes)
    assert sum('"title"' in s for s in follower_writes) == 1  # only row 22, whose title differed
    assert {day: labels(o) for day, o in rows.items()} == {
        16: ("Dishes", ""), 17: ("Dishes", ""), 18: ("Dishes", ""), 19: ("Dishes", ""),
        20: ("Dishes", "Hot water"), 21: ("Dishes", ""), 22: ("Dishes", "Hot water"),
    }


@pytest.mark.django_db
def test_unchanged_future_resave_writes_no_title_or_instructions(client, family):
    c, rows = dishes(family)
    before = {day: labels(o) for day, o in rows.items()}
    with CaptureQueriesContext(connection) as queries:
        future(client, c, rows[20])
    writes = occurrence_writes(queries)
    assert not any('"title"' in s or '"instructions"' in s for s in writes)
    assert {day: labels(o) for day, o in rows.items()} == before


# --- This occurrence only --------------------------------------------------------------


@pytest.mark.django_db
def test_this_occurrence_title_and_instructions(client, family):
    c, rows = dishes(family)
    target = rows[20]
    chores_before, rotation_before = snapshot()[1:]
    others_before = [values(o) for day, o in rows.items() if day != 20]
    response = this(client, c, target, title="Dishes (guests)", instructions="Use the big pot")
    assert response.status_code == 302
    assert response["Location"].endswith(f"?occurrence={target.pk}")
    assert "Occurrence saved" in messages_of(response)
    got = values(target)
    assert (got["title"], got["instructions"], got["is_override"]) == ("Dishes (guests)", "Use the big pot", True)
    assert snapshot()[1:] == (chores_before, rotation_before)
    assert [values(o) for day, o in rows.items() if day != 20] == others_before
    assert card_titles(page(client))[target.pk] == "Dishes (guests)"
    form = OCC_FORM.search(body(client, c, f"?occurrence={target.pk}")).group(1)
    assert 'value="Dishes (guests)"' in form
    assert re.search(r'<textarea name="instructions"[^>]*id="occ_instructions"[^>]*>\s*Use the big pot</textarea>', form)


@pytest.mark.django_db
def test_form_order_and_prefill_from_the_row(client, family):
    c, rows = dishes(family)
    ChoreOccurrence.objects.filter(pk=rows[20].pk).update(title="Row title", instructions="Row notes")
    form = OCC_FORM.search(body(client, c, f"?occurrence={rows[20].pk}")).group(1)
    order = [form.index(s) for s in ('<label for="occ_title">Title</label>', 'id="occ_title"',
                                    '<label for="occ_instructions">Instructions</label>',
                                    'id="occ_instructions"', '<label for="occ_due_time">')]
    assert order == sorted(order)
    assert 'value="Row title"' in form and "Row notes" in form
    title_input = re.search(r'<input[^>]*id="occ_title"[^>]*>', form).group(0)
    assert 'maxlength="100"' in title_input and "required" in title_input
    instructions_input = re.search(r'<textarea[^>]*id="occ_instructions"[^>]*>', form).group(0)
    assert 'rows="3"' in instructions_input and "required" not in instructions_input


@pytest.mark.django_db
def test_later_rename_keeps_the_this_occurrence_title(client, family):
    c, rows = dishes(family)
    this(client, c, rows[20], title="Dishes (guests)")
    future(client, c, rows[18], title="Wash up")
    assert values(rows[20])["title"] == "Dishes (guests)"
    assert values(rows[22])["title"] == "Wash up"


@pytest.mark.django_db
@pytest.mark.parametrize("title", ["", "   ", "x" * 101], ids=["blank", "spaces", "101-chars"])
def test_invalid_title_writes_nothing(client, family, title):
    c, rows = dishes(family)
    before = snapshot()
    response = this(client, c, rows[20], title=title)
    assert response.status_code == 200
    form = OCC_FORM.search(response.content.decode()).group(1)
    assert 'class="errorlist"' in form
    assert snapshot() == before


@pytest.mark.django_db
def test_100_char_title_is_valid_and_whitespace_is_stripped(client, family):
    c, rows = dishes(family)
    assert this(client, c, rows[20], title="x" * 100).status_code == 302
    assert values(rows[20])["title"] == "x" * 100
    rows[20].refresh_from_db()
    assert this(client, c, rows[20], title="  Padded  ").status_code == 302
    assert values(rows[20])["title"] == "Padded"


@pytest.mark.django_db
def test_blank_instructions_clear_them(client, family):
    c, rows = dishes(family)
    ChoreOccurrence.objects.filter(pk=rows[20].pk).update(instructions="Old notes")
    rows[20].refresh_from_db()
    assert this(client, c, rows[20], instructions="").status_code == 302
    assert labels(rows[20]) == ("Dishes", "")
    assert values(rows[20])["is_override"]


@pytest.mark.django_db
def test_equal_values_write_nothing(client, family):
    c, rows = dishes(family)
    before = snapshot()
    with CaptureQueriesContext(connection) as queries:
        assert this(client, c, rows[20]).status_code == 302
    assert occurrence_writes(queries) == []
    assert snapshot() == before
    o = rows[20]
    with CaptureQueriesContext(connection) as queries:
        assert update_occurrence(o, o.due_time, o.points, o.title, o.instructions) is True
    assert queries.captured_queries == []


@pytest.mark.django_db
def test_update_occurrence_is_one_conditional_update(family):
    c, rows = dishes(family)
    o = rows[20]
    with CaptureQueriesContext(connection) as queries:
        assert update_occurrence(o, o.due_time, o.points, "New", "Notes") is True
    [sql] = [q["sql"] for q in queries.captured_queries]
    assert sql.startswith('UPDATE "chores_choreoccurrence" SET')
    assert '"title"' in sql and '"instructions"' in sql and '"is_override"' in sql
    assert (labels(o), values(o)["is_override"]) == (("New", "Notes"), True)
    ChoreOccurrence.objects.filter(pk=o.pk).update(status="done")
    assert update_occurrence(o, o.due_time, o.points, "Other", "") is False
    assert labels(o) == ("New", "Notes")


@pytest.mark.django_db
@pytest.mark.parametrize("day", [16, 17], ids=["done", "skipped"])
def test_done_or_skipped_row_gets_no_form(client, family, day):
    c, rows = dishes(family)
    text = body(client, c, f"?occurrence={rows[day].pk}")
    assert OCC_FORM.search(text) is None
    assert "Undo it on the dashboard to change it on its own." in text
    before = snapshot()
    assert this(client, c, rows[day], title="Changed").status_code == 400
    assert snapshot() == before


@pytest.mark.django_db
def test_markup_in_a_title_is_escaped(client, family):
    c, rows = dishes(family)
    this(client, c, rows[20], title="<b>x</b>")
    escaped = "&lt;b&gt;x&lt;/b&gt;"
    card = re.search(rf'id="occurrence-{rows[20].pk}">(.*?)</article>', page(client), re.S).group(1)
    assert escaped in card and "<b>x</b>" not in card
    dots = calendar_dots(client)
    assert escaped in dots[20]
    ChoreOccurrence.objects.filter(pk=rows[20].pk).update(
        status="done", done_at=datetime.datetime(2026, 11, 18, 9, tzinfo=datetime.timezone.utc))
    text = history(client)
    assert escaped in text and "<b>x</b>" not in text


# --- One-off edit ------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("is_override", [False, True], ids=["plain", "override"])
def test_one_off_todo_row_follows_the_chore(client, family, is_override):
    c, r = vacuum(family)
    ChoreOccurrence.objects.filter(pk=r.pk).update(is_override=is_override)
    one_off_edit(client, c, title="Hoover", instructions="Under the bed")
    assert labels(r) == ("Hoover", "Under the bed")
    assert values(r)["is_override"] is is_override


@pytest.mark.django_db
def test_one_off_unchanged_resave_writes_no_row(client, family):
    c, r = vacuum(family)
    with CaptureQueriesContext(connection) as queries:
        one_off_edit(client, c)
    assert occurrence_writes(queries) == []


@pytest.mark.django_db
def test_done_one_off_keeps_its_old_title(client, family):
    mom = family[0]
    c = chore("Vacuum", start_date=nov(18), mode="fixed", assigned_to=mom, points=10)
    r = stored(c, nov(18), "done", who=mom, completed_by=mom,
               done_at=datetime.datetime(2026, 11, 18, 9, tzinfo=datetime.timezone.utc))
    one_off_edit(client, c, title="Hoover")
    assert c.title == "Hoover" and labels(r) == ("Vacuum", "")
    assert card_titles(page(client))[r.pk] == "Vacuum"
    assert history_titles(history(client)) == ["Vacuum"]
    assert 'value="Hoover"' in CHORE_FORM.search(body(client, c)).group(1)


# --- Screens that read the title ----------------------------------------------------------


@pytest.mark.django_db
def test_dashboard_orders_by_the_row_title(client):
    alpha, beta = chore("Alpha", start_date=nov(18)), chore("Beta", start_date=nov(18))
    a = ChoreOccurrence.objects.create(chore=alpha, date=d(nov(18)), points=10, title="Zeta")
    b = ChoreOccurrence.objects.create(chore=beta, date=d(nov(18)), points=10, title="Gamma")
    shown = [x["id"] for x in cards(page(client)) if x["id"] in (a.pk, b.pk)]
    assert shown == [b.pk, a.pk]
    assert card_titles(page(client)) == {a.pk: "Zeta", b.pk: "Gamma"}


@pytest.mark.django_db
def test_history_orders_by_the_row_title(client):
    alpha, beta = chore("Alpha", start_date=nov(17)), chore("Beta", start_date=nov(17))
    done_at = datetime.datetime(2026, 11, 17, 9, tzinfo=datetime.timezone.utc)
    ChoreOccurrence.objects.create(chore=alpha, date=d(nov(17)), points=10, title="Zeta",
                                   status="done", done_at=done_at)
    ChoreOccurrence.objects.create(chore=beta, date=d(nov(17)), points=10, title="Gamma",
                                   status="done", done_at=done_at)
    assert history_titles(history(client)) == ["Gamma", "Zeta"]


@pytest.mark.django_db
def test_calendar_shows_row_titles_and_planned_chore_titles(client, family):
    c, rows = dishes(family)
    future(client, c, rows[20], title="Wash up")
    dots = calendar_dots(client)
    for day in (16, 17, 18, 19, 21):
        assert dots[day] == ["Dishes"], day
    for day in (20, 22, 23, 30):
        assert dots[day] == ["Wash up"], day


@pytest.mark.django_db
def test_progress_lists_the_current_chore_title(client, family):
    c, rows = dishes(family)
    for day in (18, 19, 20):
        ChoreOccurrence.objects.filter(pk=rows[day].pk).update(
            status="done", done_at=datetime.datetime(2026, 11, day, 9, tzinfo=datetime.timezone.utc))
    ChoreOccurrence.objects.filter(pk=rows[17].pk).update(
        status="done", done_at=datetime.datetime(2026, 11, 17, 9, tzinfo=datetime.timezone.utc))
    Chore.objects.filter(pk=c.pk).update(title="Wash up")
    with at("2026-11-20T23:00:00"):
        text = Client().get("/progress/").content.decode()
    line = re.search(rf'<li class="streak" id="streak-{c.pk}">(.*?)</li>', text, re.S).group(1)
    assert line.startswith("Wash up")
    assert "Dishes" not in text
