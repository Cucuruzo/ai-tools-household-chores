"""The streak on a chore's other dashboard cards after a toggle (#37).
Today is Wed 18 Nov 2026 (UTC), current week 2026-W47 (Mon 16 - Sun 22 Nov)."""
import re

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores.models import ChoreOccurrence
from tests.test_assign import assign
from tests.test_card_streak import FLAME, dishes, shown, standard
from tests.test_dashboard import WED, blank_csrf, chore, get, member, page
from tests.test_delete import row, weekly
from tests.test_toggle import blocks, toggle

OOB = re.compile(r'<article class="chore-card" id="occurrence-(\d+)" hx-swap-oob="true">(.*?)</article>', re.S)
ARTICLE = re.compile(r'<article class="chore-card" id="occurrence-(\d+)"( hx-swap-oob="true")?>(.*?)</article>', re.S)
FORBIDDEN = re.compile(r"<(form|button|select|input|details)\b|card-block")


@pytest.fixture
def client():
    return Client()


def full(client, **kwargs):
    """The 18th's toggle posted by HTMX; the dishes example already has 21 and 22 stored."""
    return toggle(client, kwargs.pop("pk"), kwargs.pop("action"), htmx=True, **kwargs)


def example(client):
    """The issue's data plus the stored 21 and 22 of the first GET."""
    c, rows = standard()
    page(client)
    for n in (21, 22):
        rows[n] = ChoreOccurrence.objects.get(chore=c, date=f"2026-11-{n}")
    return c, rows


def oob(body):
    """[(pk, inner)] of the out-of-band articles, in response order."""
    return [(int(pk), inner) for pk, inner in OOB.findall(body)]


def ok(response):
    assert response.status_code == 200
    return response.content.decode()


# --- Same response ----------------------------------------------------------------


@pytest.mark.django_db
def test_done_sends_the_block_then_six_articles_in_date_order(client):
    c, rows = example(client)
    trash = weekly("Trash", weekday=2, start="2026-11-01")
    row(trash, "2026-11-11", "done")
    trash_row = row(trash, "2026-11-18", "done")
    body = ok(full(client, pk=rows[18].pk, action="done"))
    assert body.lstrip().startswith(f'<div class="card-block" id="card-{rows[18].pk}">')
    assert list(blocks(body)) == [rows[18].pk]
    assert shown(blocks(body)[rows[18].pk]) == 4
    found = oob(body)
    assert [pk for pk, _ in found] == [rows[n].pk for n in (16, 17, 19, 20, 21, 22)]
    assert len(re.findall(r'hx-swap-oob="true"', body)) == 6
    assert all('<p class="streak" title="Streak: 4 in a row">🔥 4</p>' in inner for _, inner in found)
    assert f"occurrence-{trash_row.pk}" not in body


@pytest.mark.django_db
def test_undo_skip_undo_refresh_every_card(client):
    c, rows = example(client)
    pk = rows[18].pk
    ok(full(client, pk=pk, action="done"))
    siblings = [rows[n].pk for n in (16, 17, 19, 20, 21, 22)]
    for action, number in (("undo", 3), ("skip", None), ("undo", 3)):
        body = ok(full(client, pk=pk, action=action))
        assert shown(blocks(body)[pk]) == number, action
        found = oob(body)
        assert [p for p, _ in found] == siblings
        assert {shown(inner) for _, inner in found} == {number}, action
        if number is None:
            assert FLAME not in body


@pytest.mark.django_db
def test_other_chores_are_not_refreshed_and_show_their_streak_on_reload(client):
    c, rows = example(client)
    trash = weekly("Trash", weekday=2, start="2026-11-01")
    row(trash, "2026-11-11", "done")
    trash_row = row(trash, "2026-11-18", "done")
    body = ok(full(client, pk=rows[18].pk, action="done"))
    assert f"occurrence-{trash_row.pk}" not in body
    reloaded = re.search(rf'<article class="chore-card" id="occurrence-{trash_row.pk}">(.*?)</article>',
                         page(client), re.S)
    assert shown(reloaded.group(1)) == 2


@pytest.mark.django_db
def test_out_of_band_articles_hold_no_controls(client):
    c, rows = example(client)
    found = oob(ok(full(client, pk=rows[18].pk, action="done")))
    assert found and not any(FORBIDDEN.search(inner) for _, inner in found)


@pytest.mark.django_db
def test_articles_keep_their_ids_and_targets_for_later_swaps(client):
    c, rows = example(client)
    ok(full(client, pk=rows[18].pk, action="done"))
    body = page(client)
    for n in (16, 19, 22):
        pk = rows[n].pk
        assert f'id="occurrence-{pk}"' in body and f'id="card-{pk}"' in body
        if n != 16:  # a done card has no assign form
            assert f'hx-target="#occurrence-{pk}"' in blocks(body)[pk]
    # a later toggle on a sibling swaps as before
    later = ok(full(client, pk=rows[19].pk, action="done"))
    assert list(blocks(later)) == [rows[19].pk]
    assert f'id="occurrence-{rows[19].pk}"' in later


# --- Parity with a fresh GET ---------------------------------------------------------


def parity(client, response_body, week=None, data=None):
    fresh = {int(pk): (tag, inner) for pk, tag, inner in ARTICLE.findall(page(client, week, data=data))}
    assert "hx-swap-oob" not in "".join(inner for _, inner in fresh.values())
    found = [(int(pk), inner) for pk, inner in OOB.findall(response_body)]
    assert found
    for pk, inner in found:
        assert fresh[pk][1] == inner, pk
    return [pk for pk, _ in found]


@pytest.mark.django_db
def test_articles_equal_a_fresh_get_of_the_same_view(client):
    c, rows = example(client)
    ChoreOccurrence.objects.filter(pk=rows[17].pk).update(status="todo", done_at=None)
    ChoreOccurrence.objects.create(chore=c, date="2026-11-13", status="todo", points=10, member=c.assigned_to)
    body = ok(full(client, pk=rows[18].pk, action="skip"))
    pks = parity(client, body)
    assert len(pks) == 7  # includes the old overdue 13 Nov


@pytest.mark.django_db
def test_parity_with_a_filter_and_return_query_in_the_title_link(client):
    c, rows = example(client)
    emma = c.assigned_to
    body = ok(full(client, pk=rows[18].pk, action="done", member=str(emma.pk), day=""))
    parity(client, body, data={"member": str(emma.pk)})
    assert "member=%d" % emma.pk in OOB.search(body).group(2)


# --- Which siblings --------------------------------------------------------------------


@pytest.mark.django_db
def test_old_overdue_todo_row_is_sent_on_the_current_week(client):
    c, rows = example(client)
    old = ChoreOccurrence.objects.create(chore=c, date="2026-11-13", status="todo", points=10, member=c.assigned_to)
    body = ok(full(client, pk=rows[18].pk, action="done"))
    found = [pk for pk, _ in oob(body)]
    assert found[0] == old.pk and len(found) == 7
    assert "Overdue" in dict(oob(body))[old.pk]


@pytest.mark.django_db
def test_old_done_row_is_not_sent(client):
    c, rows = example(client)
    assert rows[15].pk not in [pk for pk, _ in oob(ok(full(client, pk=rows[18].pk, action="done")))]


@pytest.mark.django_db
def test_member_filter_leaves_out_other_members_cards(client):
    c, rows = example(client)
    mom = member("Mom", "adult", order=2)
    ChoreOccurrence.objects.filter(pk=rows[19].pk).update(member=mom, is_override=True)
    body = ok(full(client, pk=rows[18].pk, action="done", member=str(c.assigned_to.pk)))
    assert [pk for pk, _ in oob(body)] == [rows[n].pk for n in (16, 17, 20, 21, 22)]
    assert f"occurrence-{rows[19].pk}" not in body


@pytest.mark.django_db
def test_day_filter_leaves_the_block_alone(client):
    c, rows = example(client)
    body = ok(full(client, pk=rows[18].pk, action="done", day="3"))
    assert "hx-swap-oob" not in body and len(blocks(body)) == 1
    page_block = blocks(page(client, data={"day": "3"}))[rows[18].pk]
    assert blank_csrf(body).strip() == blank_csrf(page_block).strip()


@pytest.mark.django_db
def test_past_week_sends_only_that_weeks_cards(client):
    c, rows = dishes({n: "todo" for n in range(9, 16)} | {})
    ChoreOccurrence.objects.filter(pk=rows[15].pk).update(status="done", done_at="2026-11-15T09:00:00Z")
    ChoreOccurrence.objects.create(chore=c, date="2026-11-18", status="todo", points=10)
    body = ok(full(client, pk=rows[15].pk, action="undo", week="2026-W46"))
    found = [pk for pk, _ in oob(body)]
    assert found == [rows[n].pk for n in range(9, 15)]
    assert all("Overdue" in text for _, text in oob(body))
    parity(client, body, week="2026-W46")


@pytest.mark.django_db
def test_future_week_sends_only_stored_cards_of_that_week(client):
    c, rows = example(client)
    future = row(c, "2026-11-25", "todo", member=c.assigned_to)
    other = row(c, "2026-11-26", "todo", member=c.assigned_to)
    body = ok(full(client, pk=future.pk, action="done", week="2026-W48"))
    assert [pk for pk, _ in oob(body)] == [other.pk]
    assert "Planned" not in body
    parity(client, body, week="2026-W48")
    alone = ok(full(client, pk=other.pk, action="skip", week="2026-W49"))
    assert "hx-swap-oob" not in alone  # W49 holds only projected cards


@pytest.mark.django_db
@pytest.mark.parametrize("data", [
    {"week": "2026-W99"}, {"week": "//evil.example"}, {"member": "abc"}, {"day": "9"},
    {"member": "deleted"},
])
def test_hostile_filters_are_treated_as_absent(client, data):
    c, rows = example(client)
    if data.get("member") == "deleted":
        gone = member("Gone", order=5)
        data = {"member": str(gone.pk)}
        gone.delete()
    body = ok(full(client, pk=rows[18].pk, action="done", **data))
    assert [pk for pk, _ in oob(body)] == [rows[n].pk for n in (16, 17, 19, 20, 21, 22)]
    assert "evil" not in body and "W99" not in body and "abc" not in body


@pytest.mark.django_db
@pytest.mark.parametrize("make", ["once", "single", "filtered"])
def test_no_other_card_means_one_unchanged_block(client, make):
    if make == "once":
        c = chore("Once", start_date="2026-11-18", recurrence="once")
        pk = row(c, "2026-11-18").pk
        data = {}
    elif make == "single":
        c = weekly("Trash", weekday=2, start="2026-11-01")
        pk = row(c, "2026-11-18").pk
        data = {}
    else:
        c, rows = example(client)
        pk, data = rows[18].pk, {"day": "3"}
    body = ok(full(client, pk=pk, action="done", **data))
    assert "hx-swap-oob" not in body
    assert body.strip().startswith('<div class="card-block"') and len(blocks(body)) == 1


@pytest.mark.django_db
def test_cancelled_and_template_rows_are_never_sent(client):
    c, rows = example(client)
    ChoreOccurrence.objects.filter(pk=rows[20].pk).update(is_cancelled=True)
    body = ok(full(client, pk=rows[18].pk, action="done"))
    assert rows[20].pk not in [pk for pk, _ in oob(body)]
    c.is_template = True
    c.save()
    # the clicked row is gone from the shown rows, so there is no block at all
    assert full(client, pk=rows[18].pk, action="undo").status_code == 404


@pytest.mark.django_db
def test_repeat_toggle_also_sends_the_siblings(client):
    c, rows = example(client)
    ok(full(client, pk=rows[18].pk, action="done"))
    body = ok(full(client, pk=rows[18].pk, action="done"))
    assert len(oob(body)) == 6 and shown(blocks(body)[rows[18].pk]) == 4


# --- Assign, full pages and no JavaScript ------------------------------------------------


@pytest.mark.django_db
def test_hx_swap_oob_only_appears_in_toggle_responses(client):
    c, rows = example(client)
    mom = member("Mom", "adult", order=2)
    assert "hx-swap-oob" not in page(client)
    assert "hx-swap-oob" not in page(client, "2026-W48")
    partial = get(client, HTTP_HX_REQUEST="true").content.decode()
    assert not OOB.search(partial)
    swap = ok(assign(client, rows[19].pk, str(mom.pk), htmx=True))
    assert "hx-swap-oob" not in swap and swap.strip().startswith("<article")
    retarget = assign(client, rows[19].pk, str(c.assigned_to.pk), htmx=True, member=str(c.assigned_to.pk))
    assert not OOB.search(retarget.content.decode())
    ok(full(client, pk=rows[18].pk, action="done"))
    assert not OOB.search(page(client))


@pytest.mark.django_db
def test_no_js_toggle_still_redirects(client):
    c, rows = example(client)
    response = toggle(client, rows[18].pk, "done", member=str(c.assigned_to.pk))
    assert response.status_code == 302
    assert response["Location"] == f"/dashboard/?member={c.assigned_to.pk}"
    assert not OOB.search(response.content.decode())


# --- Queries and clock ---------------------------------------------------------------------


def toggle_queries(client, pk):
    with CaptureQueriesContext(connection) as queries:
        ok(toggle(client, pk, "skip", htmx=True))
    return len(queries)


@pytest.mark.django_db
def test_query_count_is_the_same_for_0_1_and_6_siblings(client):
    emma = member("Emma", order=1)

    def daily(title, days):
        c = chore(title, start_date="2026-11-16", recurrence="daily", mode="fixed", assigned_to=emma)
        return [row(c, f"2026-11-{n}", member=emma) for n in days]

    zero = daily("Zero", [18])
    one = daily("One", [18, 19])
    six = daily("Six", [16, 17, 18, 19, 20, 21, 22])
    counts = [toggle_queries(client, rows_[2 if len(rows_) == 7 else 0].pk) for rows_ in (zero, one, six)]
    assert len(counts) == 3 and len(set(counts)) == 1, counts


@pytest.mark.django_db
def test_query_count_with_a_rotating_unassigned_chore(client):
    member("Ann", order=1)
    member("Ben", order=2)

    def spin(title, days):
        c = chore(title, start_date="2026-11-16", recurrence="daily", mode="rotating", eligibility="everyone")
        return [row(c, f"2026-11-{n}") for n in days]

    base = toggle_queries(client, spin("Alone", [18])[0].pk)
    six = spin("Spin", [16, 17, 18, 19, 20, 21, 22])
    assert toggle_queries(client, six[2].pk) == base


@pytest.mark.django_db
def test_stored_cards_and_streaks_run_once_with_the_viewers_today(client):
    from unittest import mock

    from chores import progress, views
    from tests.test_dashboard import d

    c, rows = example(client)
    with mock.patch("chores.views._stored_cards", wraps=views._stored_cards) as stored, \
            mock.patch("chores.progress.chore_streaks", wraps=progress.chore_streaks) as streaks:
        ok(full(client, pk=rows[19].pk, action="done", when="2026-11-21T12:00:00", tz="Pacific/Kiritimati"))
    assert stored.call_count == 1
    assert streaks.call_args_list == [mock.call(d("2026-11-22"))]


@pytest.mark.django_db
def test_viewers_zone_decides_the_week(client):
    c, rows = example(client)
    for n in range(16, 23):
        ChoreOccurrence.objects.filter(chore=c, date=f"2026-11-{n:02d}").update(
            status="done", done_at="2026-11-01T09:00:00Z")
    w48 = [row(c, f"2026-11-{n}", member=c.assigned_to) for n in (23, 24, 25)]
    body = ok(full(client, pk=w48[0].pk, action="done", when="2026-11-22T12:00:00", tz="Pacific/Kiritimati"))
    assert [pk for pk, _ in oob(body)] == [w48[1].pk, w48[2].pk]
    w47 = {r.pk for r in ChoreOccurrence.objects.filter(chore=c, date__lt="2026-11-23")}
    assert not w47 & {pk for pk, _ in oob(body)}
