"""The chore streak on dashboard cards (#32). Today is Wed 18 Nov 2026."""
import re
from unittest import mock

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import progress, views
from chores.models import ChoreOccurrence
from tests.test_assign import assign
from tests.test_dashboard import CARD, WED, at, blank_csrf, chore, d, member, page, text
from tests.test_delete import row, weekly
from tests.test_toggle import blocks, toggle

STREAK = re.compile(r'<p class="streak" title="Streak: (\d+) in a row">🔥 (\d+)</p>')
FLAME = 'class="streak"'


@pytest.fixture
def client():
    return Client()


# --- Helpers ---------------------------------------------------------------------


def dishes(days=None, **kwargs):
    """Daily fixed-assignment Dishes; ``days`` maps a Nov day number to a status."""
    emma = member("Emma", order=1)
    c = chore("Dishes", start_date="2026-11-01", recurrence="daily", mode="fixed",
              assigned_to=emma, **kwargs)
    rows = {n: row(c, f"2026-11-{n:02d}", status, member=emma) for n, status in (days or {}).items()}
    return c, rows


def standard():
    """The issue's example: 15, 16, 17 done, 18, 19, 20 todo."""
    return dishes({15: "done", 16: "done", 17: "done", 18: "todo", 19: "todo", 20: "todo"})


def card_html(body):
    """{occurrence pk: the <article> inner html}."""
    return {int(pk): inner for pk, inner in CARD.findall(body) if pk}


def shown(fragment):
    """The number shown by the streak line, or None."""
    found = STREAK.findall(fragment)
    assert len(found) <= 1
    if not found:
        assert FLAME not in fragment
        return None
    assert found[0][0] == found[0][1]
    return int(found[0][1])


def numbers(body):
    return {pk: shown(inner) for pk, inner in card_html(body).items()}


def swapped(client, pk, action, **kwargs):
    response = toggle(client, pk, action, htmx=True, **kwargs)
    assert response.status_code == 200
    return shown(blocks(response.content.decode())[pk])


# --- Shown -----------------------------------------------------------------------


@pytest.mark.django_db
def test_every_dishes_card_shows_the_progress_number(client):
    c, rows = standard()
    got = numbers(page(client))
    assert {got[rows[n].pk] for n in (16, 17, 18, 19, 20)} == {3}
    assert len([n for n in got.values() if n]) >= 5
    with at(WED):
        progress_page = Client().get("/progress/").content.decode()
    assert "Dishes — 🔥 3 days in a row" in text(progress_page)


@pytest.mark.django_db
def test_streak_line_is_a_paragraph_inside_the_article_without_controls(client):
    c, rows = standard()
    body = page(client)
    inner = card_html(body)[rows[18].pk]
    assert '<p class="streak" title="Streak: 3 in a row">🔥 3</p>' in inner
    streak_line = STREAK.search(inner).group(0)
    assert not re.search(r"<(button|form|select|input)", streak_line)
    assert '<article class="chore-card" id="occurrence-%d">' % rows[18].pk in body


@pytest.mark.django_db
def test_weekly_and_monthly_streaks_equal_progress(client):
    w = weekly("Trash", weekday=2, start="2026-11-01")
    row(w, "2026-11-11", "done")
    wednesday = row(w, "2026-11-18", "done")
    m = chore("Bills", start_date="2026-08-18", recurrence="monthly", recurrence_day=18)
    for month in ("08", "09", "10", "11"):
        monthly = row(m, f"2026-{month}-18", "done")
    streaks = progress.chore_streaks(d("2026-11-18"))
    assert (streaks[w.pk], streaks[m.pk]) == (2, 4)
    got = numbers(page(client))
    assert got[wednesday.pk] == 2 and got[monthly.pk] == 4
    with at(WED):
        listing = text(Client().get("/progress/").content.decode())
    assert "Trash — 🔥 2 weeks in a row" in listing and "Bills — 🔥 4 months in a row" in listing


@pytest.mark.django_db
def test_past_and_future_weeks_show_todays_streak(client):
    c, rows = standard()
    future = row(c, "2026-11-25", "todo")
    assert numbers(page(client, "2026-W46"))[rows[15].pk] == 3
    got = numbers(page(client, "2026-W48"))
    assert got[future.pk] == 3
    assert FLAME not in "".join(
        inner for pk, inner in card_html(page(client, "2026-W48")).items() if pk != future.pk
    )


# --- Not shown -------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("days", [
    {16: "skipped", 17: "done"},                  # streak 1
    {17: "skipped"},                              # streak 0
    {17: "todo"},                                 # overdue, streak 0
    {},                                           # no counted rows
])
def test_streak_below_two_shows_nothing(client, days):
    dishes(days)
    body = page(client)
    assert FLAME not in body and "🔥" not in body


@pytest.mark.django_db
def test_one_off_chore_shows_nothing(client):
    once = chore("Once", start_date="2026-11-17", recurrence="once")
    done = row(once, "2026-11-17", "done")
    assert FLAME not in card_html(page(client))[done.pk]
    assert progress.chore_streaks(d("2026-11-18"))[once.pk] == 1


@pytest.mark.django_db
def test_projected_cards_show_nothing(client):
    c, rows = standard()
    body = page(client, "2026-W48")
    planned = [inner for pk, inner in CARD.findall(body) if not pk]
    assert planned and all("Planned" in text(inner) for inner in planned)
    assert all(FLAME not in inner for inner in planned)


@pytest.mark.django_db
def test_ended_chore_shows_nothing_and_is_not_on_progress(client):
    c, rows = standard()
    c.end_date = d("2026-11-17")
    c.save()
    assert numbers(page(client, "2026-W46"))[rows[15].pk] is None
    assert FLAME not in page(client)
    with at(WED):
        assert "Dishes" not in text(Client().get("/progress/").content.decode())


@pytest.mark.django_db
def test_chore_ending_today_still_shows_its_streak(client):
    c, rows = standard()
    c.end_date = d("2026-11-18")
    c.save()
    assert numbers(page(client))[rows[18].pk] == 3


# --- Toggle swaps -----------------------------------------------------------------


@pytest.mark.django_db
def test_toggle_done_undo_skip_undo_transitions(client):
    c, rows = standard()
    pk = rows[18].pk
    assert [swapped(client, pk, a) for a in ("done", "undo", "skip", "undo")] == [4, 3, None, 3]


@pytest.mark.django_db
def test_done_after_an_overdue_gap_then_filling_the_gap(client):
    c, rows = dishes({15: "done", 16: "done", 17: "todo", 18: "todo"})
    assert swapped(client, rows[18].pk, "done") is None
    assert swapped(client, rows[17].pk, "done") == 4


@pytest.mark.django_db
def test_undo_into_an_overdue_todo_resets_the_streak(client):
    c, rows = dishes({15: "skipped", 16: "done", 17: "done", 18: "todo"})
    assert shown(blocks(page(client))[rows[18].pk]) == 2
    assert swapped(client, rows[17].pk, "undo") is None


@pytest.mark.django_db
def test_swapped_block_equals_full_page_block_with_a_streak(client):
    c, rows = dishes({15: "skipped", 16: "done", 17: "done", 18: "todo"})
    pk = rows[18].pk
    assert shown(blocks(page(client))[pk]) == 2
    for action, number in (("done", 3), ("undo", 2), ("skip", None), ("undo", 2)):
        block = blocks(toggle(client, pk, action, htmx=True).content.decode())[pk]
        full = blocks(page(client))[pk]
        assert blank_csrf(block).strip() == blank_csrf(full).strip(), action
        assert shown(block) == number, action


@pytest.mark.django_db
def test_a_reload_shows_the_new_number_on_other_cards(client):
    c, rows = standard()
    assert swapped(client, rows[18].pk, "done") == 4
    assert numbers(page(client))[rows[19].pk] == 4  # a reload refreshes the others


@pytest.mark.django_db
def test_no_js_toggle_redirects_and_every_card_shows_the_new_number(client):
    c, rows = standard()
    response = toggle(client, rows[18].pk, "done")
    assert response.status_code == 302 and response["Location"] == "/dashboard/"
    got = numbers(page(client))
    assert {got[rows[n].pk] for n in (16, 17, 18, 19, 20)} == {4}


# --- Assign swaps -------------------------------------------------------------------


@pytest.mark.django_db
def test_assign_swap_keeps_the_streak(client):
    c, rows = standard()
    mom = member("Mom", "adult", order=2)
    response = assign(client, rows[19].pk, str(mom.pk), htmx=True)
    body = response.content.decode()
    assert body.strip().startswith("<article") and shown(body) == 3
    assert "HX-Retarget" not in response


@pytest.mark.django_db
def test_assign_hidden_by_member_filter_rerenders_the_list_with_streaks(client):
    c, rows = standard()
    emma = c.assigned_to
    mom = member("Mom", "adult", order=2)
    response = assign(client, rows[19].pk, str(mom.pk), htmx=True, member=str(emma.pk))
    assert response["HX-Retarget"] == "#dashboard-list"
    got = numbers(response.content.decode())
    assert rows[19].pk not in got
    assert got[rows[18].pk] == got[rows[20].pk] == 3


# --- Queries and clock -----------------------------------------------------------------


def query_count(client):
    with CaptureQueriesContext(connection) as queries:
        page(client)
    return len(queries)


@pytest.mark.django_db
def test_query_count_is_the_same_for_one_and_ten_chores(client):
    emma = member("Emma", order=1)
    one = chore("Dishes", start_date="2026-11-01", recurrence="daily", mode="fixed", assigned_to=emma)
    for n in (15, 16, 17):
        row(one, f"2026-11-{n:02d}", "done", member=emma)
    page(client)  # materialize the week once
    base = query_count(client)
    for i in range(9):
        c = chore(f"Chore {i}", start_date="2026-11-01", recurrence="daily", mode="fixed", assigned_to=emma)
        for n in range(9, 19):
            row(c, f"2026-11-{n:02d}", "done", member=emma)
    page(client)
    assert query_count(client) == base


def spy_streaks():
    return mock.patch("chores.progress.chore_streaks", wraps=progress.chore_streaks)


@pytest.mark.django_db
@pytest.mark.parametrize("tz, expected", [(None, "2026-11-21"), ("Pacific/Kiritimati", "2026-11-22")])
def test_dashboard_calls_chore_streaks_once_with_its_today(client, tz, expected):
    standard()
    with spy_streaks() as streaks:
        page(client, when="2026-11-21T12:00:00", tz=tz)
    assert streaks.call_args_list == [mock.call(d(expected))]


@pytest.mark.django_db
def test_other_weeks_also_call_it_once(client):
    standard()
    for week in ("2026-W46", "2026-W48"):
        with spy_streaks() as streaks:
            page(client, week)
        assert streaks.call_count == 1


@pytest.mark.django_db
@pytest.mark.parametrize("tz, expected", [(None, "2026-11-21"), ("Pacific/Kiritimati", "2026-11-22")])
def test_swaps_call_chore_streaks_once_with_their_today(client, tz, expected):
    c, rows = standard()
    pk = rows[19].pk
    when = "2026-11-21T12:00:00"
    with spy_streaks() as streaks:
        toggle(client, pk, "done", when=when, tz=tz, htmx=True)
    assert streaks.call_args_list == [mock.call(d(expected))]
    with spy_streaks() as streaks:
        assign(client, rows[20].pk, "", when=when, tz=tz, htmx=True)
    assert streaks.call_args_list == [mock.call(d(expected))]


@pytest.mark.django_db
def test_stored_cards_without_today_shows_nothing():
    c, rows = standard()
    with spy_streaks() as streaks:
        cards = views._stored_cards(ChoreOccurrence.objects.all(), views._PoolCache())
    assert streaks.call_count == 0 and {card.streak for card in cards} == {0}


def test_views_does_not_import_progress_at_module_level():
    import inspect
    source = inspect.getsource(views)
    assert not re.search(r"^(from \. import progress|from \.progress|import chores\.progress)", source, re.M)
