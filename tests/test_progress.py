"""Streaks and badges (#16)."""
import datetime
import html
import inspect
import re

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores import progress
from chores.models import Chore, ChoreOccurrence, Member, RotationState
from tests.test_dashboard import at, chore, d, instant, member, t

SAT = "2026-11-21T12:00:00"  # Sat 21 Nov 2026; the week is Mon 16 - Sun 22
TODAY = d("2026-11-21")
DONE, TODO, SKIPPED = "done", "todo", "skipped"


@pytest.fixture
def client():
    return Client()


# --- Helpers ---------------------------------------------------------------------


def day(n):
    return d(f"2026-11-{n:02d}")


def dishes(**kwargs):
    return chore("Dishes", start_date="2026-11-01", recurrence="daily", **kwargs)


def row(c, n, status=DONE, by=None, done_at=None, **kwargs):
    """One stored row of chore ``c`` on 2026-11-``n``."""
    if status == DONE and done_at is None:
        done_at = instant(f"2026-11-{n:02d}T09:00:00")
    return ChoreOccurrence.objects.create(
        chore=c, date=day(n), status=status, points=10, completed_by=by,
        done_at=done_at if status == DONE else None, **kwargs,
    )


def run(c, days, status=DONE, by=None, **kwargs):
    return [row(c, n, status, by, **kwargs) for n in days]


def mom_emma():
    return member("Mom", "adult", order=1), member("Emma", "kid", order=2)


def streak(c, today=TODAY):
    return progress.chore_streaks(today).get(c.pk, 0)


def walk_counters(today=TODAY):
    last = {}
    for chore_id, counter, _row in progress._walk(progress._rows(), today):
        last[chore_id] = counter
    return last


def get(client, when=SAT, tz=None):
    if tz is not None:
        client.cookies["tz"] = tz
    with at(when):
        return client.get("/progress/")


def body(client, **kwargs):
    response = get(client, **kwargs)
    assert response.status_code == 200
    return response.content.decode()


def text(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def streak_lines(page):
    return [text(m) for m in re.findall(r'<li class="streak" id="streak-\d+">(.*?)</li>', page, re.S)]


def badge_row(page, pk):
    m = re.search(rf'<li class="member-badges" id="badges-{pk}">(.*?)</li>', page, re.S)
    return m.group(1)


def badge_types(page, pk):
    return re.findall(r'data-badge="([a-z_0-9]+)"', badge_row(page, pk))


# --- Streak rule -----------------------------------------------------------------


@pytest.mark.django_db
def test_done_then_todo_today_is_neutral():
    c = dishes()
    run(c, range(15, 21))
    row(c, 21, TODO)
    assert streak(c) == 6


@pytest.mark.django_db
def test_all_done_counts_seven():
    c = dishes()
    run(c, range(15, 22))
    assert streak(c) == 7


@pytest.mark.django_db
def test_skipped_stops_the_streak():
    c = dishes()
    run(c, [15, 16, 17, 19, 20, 21])
    row(c, 18, SKIPPED)
    assert streak(c) == 3


@pytest.mark.django_db
def test_past_todo_stops_the_streak():
    c = dishes()
    run(c, range(15, 20))
    row(c, 20, TODO)
    row(c, 21)
    assert streak(c) == 1


@pytest.mark.django_db
def test_todo_today_past_due_time_does_not_stop_it():
    c = dishes()
    run(c, range(15, 21))
    row(c, 21, TODO, due_time=t("08:00"))
    assert streak(c) == 6  # the dashboard calls it Overdue, the date is today


@pytest.mark.django_db
@pytest.mark.parametrize("status", [TODO, DONE])
def test_cancelled_date_is_passed_over(status):
    c = dishes()
    run(c, [15, 16, 17, 18, 20, 21])
    row(c, 19, status, is_cancelled=True)
    assert streak(c) == 6


@pytest.mark.django_db
def test_gap_without_rows_is_passed_over():
    c = dishes()
    run(c, range(2, 9))
    run(c, range(16, 22))
    assert streak(c) == 13


@pytest.mark.django_db
def test_future_todo_neutral_and_future_done_counts():
    c = dishes()
    run(c, range(16, 21))
    row(c, 21, TODO)
    row(c, 22)
    assert streak(c) == 6


@pytest.mark.django_db
def test_future_skip_stops_it():
    c = dishes()
    run(c, range(16, 22))
    row(c, 22, SKIPPED)
    assert streak(c) == 0


@pytest.mark.django_db
def test_no_rows_means_missing_from_the_dict():
    c = dishes()
    assert c.pk not in progress.chore_streaks(TODAY)


@pytest.mark.django_db
def test_credit_and_assignee_are_ignored():
    mom, emma = mom_emma()
    c = dishes()
    row(c, 15, by=mom, member=emma)
    row(c, 16, by=emma)
    row(c, 17, by=None, member=None)
    row(c, 18, by=None, member=mom)
    assert streak(c) == 4
    row(c, 19, TODO, member=None)
    row(c, 20)
    assert streak(c) == 1


@pytest.mark.django_db
def test_streak_is_per_chore_in_its_own_occurrences():
    weekly = chore("Bins", start_date="2026-10-05", recurrence="weekly", recurrence_day=0)
    for date in ("2026-11-02", "2026-11-09", "2026-11-16"):
        ChoreOccurrence.objects.create(
            chore=weekly, date=d(date), status=DONE, points=10, done_at=instant(date + "T09:00:00")
        )
    c = dishes()
    run(c, [20, 21])
    assert progress.chore_streaks(TODAY) == {weekly.pk: 3, c.pk: 2}


@pytest.mark.django_db
def test_template_rows_are_ignored():
    tpl = chore("Tpl", recurrence="daily", is_template=True)
    run(tpl, range(15, 22))
    assert progress.chore_streaks(TODAY) == {}


@pytest.mark.django_db
def test_streaks_use_one_query():
    for i in range(5):
        run(chore(f"C{i}", recurrence="daily"), range(15, 18))
    with CaptureQueriesContext(connection) as queries:
        progress.chore_streaks(TODAY)
    assert len(queries) == 1


@pytest.mark.django_db
def test_kiritimati_changes_the_streak(client):
    c = dishes()
    run(c, range(16, 21))
    row(c, 21, TODO)
    assert streak_lines(body(client)) == ["Dishes — 🔥 5 days in a row"]
    # Local date there is 2026-11-22, so the 21st is in the past.
    page = body(client, tz="Pacific/Kiritimati")
    assert streak_lines(page) == []
    assert 'id="no-streaks"' in page


# --- Undo, skip ------------------------------------------------------------------


def undo(occurrence):
    occurrence.status, occurrence.done_at, occurrence.completed_by = TODO, None, None
    occurrence.save()


def redo(occurrence, by=None):
    occurrence.status, occurrence.done_at, occurrence.completed_by = DONE, instant(SAT), by
    occurrence.save()


@pytest.mark.django_db
def test_undo_today_lowers_to_six_and_undo_17th_to_four():
    c = dishes()
    rows = {r.date.day: r for r in run(c, range(15, 22))}
    assert streak(c) == 7
    undo(rows[21])
    assert streak(c) == 6
    redo(rows[21])
    undo(rows[17])
    assert streak(c) == 4


@pytest.mark.django_db
def test_skip_and_undone_skip_to_past_todo_still_stop():
    c = dishes()
    rows = {r.date.day: r for r in run(c, range(15, 22))}
    rows[18].status, rows[18].done_at = SKIPPED, None
    rows[18].save()
    assert streak(c) == 3
    rows[18].status = TODO
    rows[18].save()
    assert streak(c) == 3


# --- Badges: streak_7 ------------------------------------------------------------


@pytest.mark.django_db
def test_streak_7_goes_to_the_seventh_row_credit():
    mom, emma = mom_emma()
    c = dishes()
    run(c, range(15, 21), by=emma)
    row(c, 21, by=mom)
    assert progress.member_badges(TODAY) == {mom.pk: ["streak_7"], emma.pk: []}


@pytest.mark.django_db
def test_six_in_a_row_gives_nothing():
    mom, _ = mom_emma()
    run(dishes(), range(15, 21), by=mom)
    assert "streak_7" not in progress.member_badges(TODAY)[mom.pk]


@pytest.mark.django_db
def test_uncredited_seventh_gives_it_to_nobody_but_eighth_earns_it():
    mom, emma = mom_emma()
    c = dishes()
    run(c, range(15, 21), by=mom)
    row(c, 21, by=None)
    assert "streak_7" not in progress.member_badges(TODAY)[mom.pk]
    row(c, 22, by=emma)
    assert progress.member_badges(TODAY) == {mom.pk: [], emma.pk: ["streak_7"]}


@pytest.mark.django_db
def test_runs_are_per_chore():
    mom, _ = mom_emma()
    run(dishes(), range(15, 19), by=mom)
    bins = chore("Bins", recurrence="daily")
    run(bins, range(15, 18), by=mom)
    assert progress.member_badges(TODAY)[mom.pk] == []


@pytest.mark.django_db
def test_one_off_chore_never_earns_it():
    mom, _ = mom_emma()
    c = chore("Once")
    row(c, 21, by=mom)
    assert progress.member_badges(TODAY)[mom.pk] == []


@pytest.mark.django_db
def test_walk_counter_equals_chore_streaks_on_every_example():
    cases = [
        (range(15, 21), {21: TODO}),
        (range(15, 22), {}),
        ([15, 16, 17, 19, 20, 21], {18: SKIPPED}),
        ([15, 16, 17, 18, 19, 21], {20: TODO}),
        ([15, 16, 17, 18, 20, 21], {19: TODO}),
        (list(range(2, 9)) + list(range(16, 22)), {}),
        (range(16, 21), {21: TODO, 22: DONE}),
        (range(16, 22), {22: SKIPPED}),
    ]
    for done, others in cases:
        ChoreOccurrence.objects.all().delete()
        Chore.objects.all().delete()
        c = dishes()
        run(c, done)
        for n, status in others.items():
            row(c, n, status)
        assert walk_counters().get(c.pk) == streak(c)


# --- Badges: hero and completed_50 -----------------------------------------------


def done_at_rows(by, stamps):
    """One done row per stamp, each on its own one-off chore (no streaks)."""
    return [
        ChoreOccurrence.objects.create(
            chore=chore(f"H{Chore.objects.count()}"), date=day(1), status=DONE, points=5,
            completed_by=by, done_at=instant(stamp),
        )
        for stamp in stamps
    ]


WEEK_STAMPS = ["2026-11-16T00:00:00"] + [f"2026-11-{n}T12:00:00" for n in (17, 18, 19, 20, 21)] + [
    "2026-11-22T23:59:59", "2026-11-18T08:00:00", "2026-11-19T08:00:00", "2026-11-20T08:00:00",
]


@pytest.mark.django_db
def test_hero_at_ten_but_not_nine():
    mom, _ = mom_emma()
    assert len(WEEK_STAMPS) == 10
    done_at_rows(mom, WEEK_STAMPS[:9])
    assert progress.member_badges(TODAY)[mom.pk] == []
    done_at_rows(mom, WEEK_STAMPS[9:])
    assert progress.member_badges(TODAY)[mom.pk] == ["hero"]


@pytest.mark.django_db
def test_hero_ten_in_week_with_edges_of_the_week():
    mom, _ = mom_emma()
    done_at_rows(mom, WEEK_STAMPS)
    assert progress.member_badges(TODAY)[mom.pk] == ["hero"]


@pytest.mark.django_db
def test_hero_needs_one_week_not_two_halves():
    mom, _ = mom_emma()
    done_at_rows(mom, ["2026-11-10T10:00:00"] * 5 + ["2026-11-17T10:00:00"] * 5)
    assert progress.member_badges(TODAY)[mom.pk] == []


@pytest.mark.django_db
def test_hero_week_boundary_sunday_vs_monday():
    mom, _ = mom_emma()
    done_at_rows(mom, ["2026-11-15T23:59:59"] * 5 + ["2026-11-16T00:00:00"] * 5)
    assert progress.member_badges(TODAY)[mom.pk] == []


@pytest.mark.django_db
def test_hero_goes_by_done_at_not_date_and_any_week():
    mom, _ = mom_emma()
    for i in range(10):  # all dated in the week of 2026-11-09, done 16-21 Nov
        ChoreOccurrence.objects.create(
            chore=chore(f"L{i}"), date=d("2026-11-10"), status=DONE, points=5,
            completed_by=mom, done_at=instant(f"2026-11-{16 + i % 6}T10:00:00"),
        )
    assert progress.member_badges(TODAY)[mom.pk] == ["hero"]
    # Any week counts, not only the current one.
    assert progress.member_badges(d("2027-06-01"))[mom.pk] == ["hero"]


@pytest.mark.django_db
def test_hero_ignores_null_done_at_and_uncredited():
    mom, _ = mom_emma()
    done_at_rows(mom, ["2026-11-18T10:00:00"] * 9)
    c = chore("NoStamp", recurrence="daily")
    ChoreOccurrence.objects.create(chore=c, date=day(1), status=DONE, points=5, completed_by=mom)
    done_at_rows(None, ["2026-11-18T10:00:00"] * 5)
    assert progress.member_badges(TODAY) == {mom.pk: []}


@pytest.mark.django_db
def test_hero_in_the_viewers_zone(client):
    mom, _ = mom_emma()
    done_at_rows(mom, ["2026-11-18T10:00:00"] * 9 + ["2026-11-15T11:00:00"])
    assert badge_types(body(client), mom.pk) == []
    assert badge_types(body(client, tz="Pacific/Kiritimati"), mom.pk) == ["hero"]


@pytest.mark.django_db
def test_completed_50_threshold_and_what_does_not_count():
    mom, _ = mom_emma()
    c = chore("Many", recurrence="daily")
    for i in range(49):
        ChoreOccurrence.objects.create(
            chore=c, date=d("2026-01-01") + datetime.timedelta(days=i), status=DONE, points=5,
            completed_by=mom, done_at=instant(f"2026-0{1 + i // 28}-{1 + i % 28:02d}T10:00:00"),
        )
    other = chore("Other")
    for status, cancelled in ((SKIPPED, False), (TODO, False), (DONE, True)):
        ChoreOccurrence.objects.create(
            chore=other, date=day(1) + datetime.timedelta(days=len(other.occurrences.all())),
            status=status, points=5, completed_by=mom if status == DONE else None,
            is_cancelled=cancelled, done_at=instant(SAT) if status == DONE else None,
        )
    assert "completed_50" not in progress.member_badges(TODAY)[mom.pk]
    ChoreOccurrence.objects.create(
        chore=c, date=d("2027-01-01"), status=DONE, points=5, completed_by=mom, done_at=instant(SAT)
    )
    assert "completed_50" in progress.member_badges(TODAY)[mom.pk]


@pytest.mark.django_db
def test_template_rows_give_no_badge():
    mom, _ = mom_emma()
    tpl = chore("Tpl", recurrence="daily", is_template=True)
    run(tpl, range(15, 22), by=mom)
    assert progress.member_badges(TODAY) == {}


@pytest.mark.django_db
def test_badge_order_is_fixed():
    mom, _ = mom_emma()
    c = dishes()
    run(c, range(1, 22), by=mom)  # 21 done, 7 in a row
    for i in range(29):
        row(chore(f"X{i}"), 21, by=mom)
    assert progress.member_badges(TODAY)[mom.pk] == ["streak_7", "hero", "completed_50"]


@pytest.mark.django_db
def test_broken_streak_keeps_its_badge():
    mom, emma = mom_emma()
    c = dishes()
    run(c, range(8, 14), by=mom)
    row(c, 14, by=emma)
    row(c, 15, SKIPPED)
    run(c, range(16, 22), by=mom)
    assert streak(c) == 6
    assert progress.member_badges(TODAY)[emma.pk] == ["streak_7"]


# --- Undo and redo of badges -----------------------------------------------------


@pytest.mark.django_db
def test_undo_and_redo_streak_7_badge():
    mom, emma = mom_emma()
    c = dishes()
    rows = run(c, range(15, 21), by=emma)
    last = row(c, 21, by=mom)
    undo(last)
    assert progress.member_badges(TODAY).get(mom.pk, []) == []
    redo(last, by=mom)
    assert progress.member_badges(TODAY)[mom.pk] == ["streak_7"]
    assert rows


@pytest.mark.django_db
def test_undo_and_redo_completed_50_and_extra_row_changes_nothing():
    mom, _ = mom_emma()
    c = chore("Many", recurrence="daily")
    rows = [
        ChoreOccurrence.objects.create(
            chore=c, date=d("2026-01-01") + datetime.timedelta(days=i), status=DONE, points=5,
            completed_by=mom, done_at=instant(f"2026-0{1 + i // 28}-{1 + i % 28:02d}T10:00:00"),
        )
        for i in range(51)
    ]
    assert "completed_50" in progress.member_badges(TODAY)[mom.pk]
    undo(rows[50])  # the 51st: not needed
    assert "completed_50" in progress.member_badges(TODAY)[mom.pk]
    undo(rows[0])
    assert "completed_50" not in progress.member_badges(TODAY)[mom.pk]
    redo(rows[0], by=mom)
    assert "completed_50" in progress.member_badges(TODAY)[mom.pk]


@pytest.mark.django_db
def test_undo_and_redo_hero():
    mom, _ = mom_emma()
    rows = done_at_rows(mom, WEEK_STAMPS)
    undo(rows[3])
    assert progress.member_badges(TODAY)[mom.pk] == []
    rows[3].done_at = instant(WEEK_STAMPS[3])
    rows[3].status, rows[3].completed_by = DONE, mom
    rows[3].save()
    assert progress.member_badges(TODAY)[mom.pk] == ["hero"]


# --- Deleted member --------------------------------------------------------------


@pytest.mark.django_db
def test_deleted_member_loses_badges_streak_unchanged(client):
    mom, emma = mom_emma()
    c = dishes()
    run(c, range(15, 21), by=emma)
    row(c, 21, by=mom)
    before = streak(c)
    mom.delete()
    assert streak(c) == before == 7
    assert progress.member_badges(TODAY) == {emma.pk: []}
    new_mom = member("Mom", "adult", order=3)
    page = body(client)
    assert "No badges yet" in badge_row(page, new_mom.pk)
    assert badge_types(page, emma.pk) == []


@pytest.mark.django_db
def test_deleting_a_member_leaves_other_members_badges():
    mom, emma = mom_emma()
    c = dishes()
    run(c, range(14, 20), by=mom)
    row(c, 20, by=emma)
    mom.delete()
    assert progress.member_badges(TODAY) == {emma.pk: ["streak_7"]}


# --- The page --------------------------------------------------------------------


@pytest.mark.django_db
def test_page_markup_units_and_order(client):
    mom, emma = mom_emma()
    run(dishes(), range(15, 21))
    bins = chore("bins", start_date="2026-11-02", recurrence="weekly", recurrence_day=0)
    row(bins, 16)
    run(chore("Alpha", recurrence="monthly"), [21])
    page = body(client)
    assert "<title>Progress · Household Chores</title>" in page
    assert "<h1>Progress</h1>" in page
    assert '<section id="streaks">' in page and "<h2>Streaks</h2>" in page
    assert streak_lines(page) == [
        "Alpha — 🔥 1 month in a row",
        "bins — 🔥 1 week in a row",
        "Dishes — 🔥 6 days in a row",
    ]
    assert 'id="profile-switcher"' in page
    assert "<script" in page and "hx-" not in page.split("<main")[1]
    assert re.search(rf'id="streak-{bins.pk}"', page)
    assert [int(x) for x in re.findall(r'class="member-badges" id="badges-(\d+)"', page)] == [mom.pk, emma.pk]
    assert 'class="avatar"' in badge_row(page, mom.pk) and 'class="name">Mom<' in badge_row(page, mom.pk)
    assert 'class="no-badges">No badges yet<' in badge_row(page, emma.pk)


@pytest.mark.django_db
def test_streaks_not_sorted_by_streak_and_members_not_by_badges(client):
    mom, emma = mom_emma()
    run(chore("Zebra", recurrence="daily"), range(14, 22), by=emma)  # emma earns streak_7
    run(chore("Apple", recurrence="daily"), [21])
    page = body(client)
    assert streak_lines(page)[0].startswith("Apple")
    assert [int(x) for x in re.findall(r'id="badges-(\d+)"', page)] == [mom.pk, emma.pk]
    row_html = badge_row(page, emma.pk)
    assert '<span class="badge" data-badge="streak_7">🔥 7 in a Row</span>' in row_html


@pytest.mark.django_db
def test_badge_labels_and_icons_on_the_page(client):
    mom, _ = mom_emma()
    c = dishes()
    run(c, range(1, 22), by=mom)
    for i in range(29):
        row(chore(f"X{i}"), 21, by=mom)
    page = body(client)
    assert badge_types(page, mom.pk) == ["streak_7", "hero", "completed_50"]
    assert "🔥 7 in a Row" in text(badge_row(page, mom.pk))
    assert "🦸 Chore Hero" in text(badge_row(page, mom.pk))
    assert "⭐ 50 Chores Completed" in text(badge_row(page, mom.pk))


@pytest.mark.django_db
def test_exclusions_once_zero_template_and_ended(client):
    row(chore("Single"), 21)
    row(chore("Broken", recurrence="daily"), 20, TODO)
    tpl = chore("Tpl", recurrence="daily", is_template=True)
    row(tpl, 21)
    row(chore("Ended", recurrence="daily", end_date=d("2026-11-20")), 21)
    run(chore("Ends today", recurrence="daily", end_date=TODAY), [21])
    assert streak_lines(body(client)) == ["Ends today — 🔥 1 day in a row"]


@pytest.mark.django_db
def test_ended_chore_still_counts_for_badges():
    mom, _ = mom_emma()
    c = dishes(end_date=d("2026-11-20"))
    run(c, range(15, 22), by=mom)
    assert progress.member_badges(TODAY)[mom.pk] == ["streak_7"]


@pytest.mark.django_db
def test_empty_states(client):
    page = body(client)
    assert '<p id="no-streaks">No streaks yet. Mark a chore done to start one.</p>' in page
    assert '<p id="no-members-badges">' in page
    assert "Add your family to see badges" in page
    assert 'href="/family/"' in page.split('id="no-members-badges"')[1]
    # Streaks still show without members.
    run(dishes(), [21])
    assert streak_lines(body(client)) == ["Dishes — 🔥 1 day in a row"]


@pytest.mark.django_db
def test_escaping(client):
    member("<i>Bo</i>", order=1)
    run(chore("<b>Dishes</b>", recurrence="daily"), [21])
    page = body(client)
    assert "<b>Dishes</b>" not in page and "<i>Bo</i>" not in page
    assert "&lt;b&gt;Dishes&lt;/b&gt;" in page and "&lt;i&gt;Bo&lt;/i&gt;" in page
    assert streak_lines(page) == ["<b>Dishes</b> — 🔥 1 day in a row"]


@pytest.mark.django_db
def test_no_competitive_wording(client):
    mom, _ = mom_emma()
    run(dishes(), range(1, 22), by=mom)
    page = body(client)
    main = page.split("<main", 1)[1]
    visible = text(main).lower()
    for word in ("winner", "loser", "rank", "leader", "1st", "top", "best", "place"):
        assert not re.search(rf"\b{word}", visible), word
    assert "🏆" not in page and "🥇" not in page


@pytest.mark.django_db
def test_post_is_405(client):
    assert client.post("/progress/").status_code == 405


@pytest.mark.django_db
def test_get_writes_nothing_and_does_not_materialize(client):
    mom_emma()
    chore("Bins", start_date="2026-11-02", recurrence="weekly", recurrence_day=0)
    before = (Member.objects.count(), Chore.objects.count(),
              ChoreOccurrence.objects.count(), RotationState.objects.count())
    monday = "2026-11-16T09:00:00"
    assert get(client, when=monday).status_code == 200
    after = (Member.objects.count(), Chore.objects.count(),
             ChoreOccurrence.objects.count(), RotationState.objects.count())
    assert before == after


@pytest.mark.django_db
def test_query_count_is_constant(client):
    def count():
        with CaptureQueriesContext(connection) as queries:
            assert get(client).status_code == 200
        assert not any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries)
        return len(queries)

    mom = member("Mom", order=1)
    run(chore("One", recurrence="daily"), range(19, 22), by=mom)
    small = count()
    for i in range(4):
        member(f"M{i}", order=10 + i)
    for i in range(5):
        run(chore(f"C{i}", recurrence="daily"), range(1, 23), by=mom)
    assert ChoreOccurrence.objects.count() > 100
    assert count() == small


@pytest.mark.django_db
def test_view_reads_the_clock_once_and_passes_today(monkeypatch, client):
    seen = {}
    monkeypatch.setattr(progress, "chore_streaks", lambda today: seen.setdefault("streaks", today) and {})
    monkeypatch.setattr(progress, "member_badges", lambda today: seen.setdefault("badges", today) and {})
    with at(SAT) as patched:
        assert client.get("/progress/").status_code == 200
    assert seen == {"streaks": TODAY, "badges": TODAY}
    # One clock read, in the view.
    with at(SAT) as baseline:  # the base template's own clock read, on a page that reads none
        client.get("/family/")
    assert patched.call_count == baseline.call_count + 1


def test_progress_module_does_not_read_the_clock():
    source = inspect.getsource(progress)
    for forbidden in ("timezone.now", "datetime.now", "date.today", "localtime()", "localdate()", "TIME_ZONE"):
        assert forbidden not in source


def test_no_migration_after_0008():
    from pathlib import Path

    names = sorted(p.name for p in Path(progress.__file__).parent.joinpath("migrations").glob("0*.py"))
    assert names[-1] == "0008_choreoccurrence_completed_by.py"


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)
