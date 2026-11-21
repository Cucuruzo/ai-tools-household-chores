"""Assign and reassign from the dashboard (#13)."""
import html
import re
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.test import Client

from chores import services, views
from chores.models import Chore, ChoreOccurrence, RotationState
from chores.services import materialize_week
from tests.test_dashboard import WED, at, cards, chore, d, member, no_writes, page, sections
from tests.test_delete import HOSTILE, OVERFLOW, SAT_NOON, hidden, row, weekly
from tests.test_toggle import HTMX, blocks, everything, fields, label, once

NEXT_MONDAY = "2026-11-23T12:00:00"
ASSIGN_FORM = re.compile(r'<form class="assign"([^>]*)>(.*?)</form>', re.S)
OPTION = re.compile(r'<option value="([^"]*)"( selected)?>(.*?)</option>', re.S)


@pytest.fixture
def client():
    return Client()


# --- Helpers ---------------------------------------------------------------------


def assign(client, pk, assignee=None, when=WED, tz=None, htmx=False, **data):
    if tz is not None:
        client.cookies["tz"] = tz
    if assignee is not None:
        data["assignee"] = assignee
    with at(when):
        return client.post(f"/dashboard/assign/{pk}/", data, **(HTMX if htmx else {}))


def pk_of(m):
    return "" if m is None else str(m.pk)


def form_of(fragment):
    m = ASSIGN_FORM.search(fragment)
    return (m.group(1), m.group(2)) if m else None


def options(fragment):
    _, inner = form_of(fragment)
    return [(v, bool(sel), html.unescape(text)) for v, sel, text in OPTION.findall(inner)]


def member_of(o):
    o.refresh_from_db()
    return o.member


def article_pk(body):
    return int(re.search(r'<article class="chore-card" id="occurrence-(\d+)">', body).group(1))


# --- The write -----------------------------------------------------------------------


@pytest.mark.django_db
def test_assign_reassign_unassign_change_only_member_and_override(client):
    mom, emma = member("Mom", role="adult"), member("Emma")
    c = weekly(points=20, end_date=d("2027-01-01"))
    o = row(c, "2026-11-18", points=10)
    other = row(c, "2026-11-25")
    chores_before, other_before, count = everything()[1], fields(other), ChoreOccurrence.objects.count()
    for target, expected in ((emma, emma), (mom, mom), (None, None)):
        before = fields(o)
        with mock.patch("chores.views.materialize_week") as materialize:
            response = assign(client, o.pk, pk_of(target))
        assert response.status_code == 302
        materialize.assert_not_called()
        after = fields(o)
        assert (after.pop("member_id"), after.pop("is_override")) == (expected.pk if expected else None, True)
        before.pop("member_id")
        before.pop("is_override")
        assert after == before
    assert everything()[1] == chores_before
    assert fields(other) == other_before
    assert ChoreOccurrence.objects.count() == count


@pytest.mark.django_db
def test_unassigned_rotating_card_reads_plain_unassigned(client):
    mom = member("Mom", role="adult")  # the kids-only pool is empty
    c = weekly("Kids only", mode="rotating", eligibility="kids")
    o = row(c, "2026-11-18")
    assert sections(page(client))["today"][0]["assignee"] == "Unassigned · needs eligible member"
    assign(client, o.pk, pk_of(mom))
    assert assign(client, o.pk, "").status_code == 302
    assert sections(page(client))["today"][0]["assignee"] == "Unassigned"


@pytest.mark.django_db
def test_adult_on_kids_only_rotating_chore_and_fixed_chore_unchanged(client):
    mom, emma = member("Mom", role="adult"), member("Emma")
    rot = weekly("Kids", mode="rotating", eligibility="kids")
    o = row(rot, "2026-11-18", member=emma)
    assert assign(client, o.pk, pk_of(mom)).status_code == 302
    assert member_of(o) == mom
    fixed = chore("Fixed", start_date="2026-11-04", recurrence="weekly", recurrence_day=2,
                  mode="fixed", assigned_to=emma)
    f = row(fixed, "2026-11-18", member=emma)
    snapshot = Chore.objects.filter(pk=fixed.pk).values().get()
    assign(client, f.pk, pk_of(mom))
    assert member_of(f) == mom
    assert Chore.objects.filter(pk=fixed.pk).values().get() == snapshot
    with at("2026-11-25T12:00:00"):
        [nxt] = [o for o in materialize_week(d("2026-11-23"), today=d("2026-11-25")) if o.chore_id == fixed.pk]
    assert nxt.member == emma


@pytest.mark.django_db
def test_same_value_writes_nothing(client):
    emma = member("Emma")
    given = row(chore(start_date="2026-11-18"), "2026-11-18", member=emma)
    none = once("None", day="2026-11-19")
    before = everything()
    spy, wrapper = no_writes()
    with wrapper:
        for o, value in ((given, pk_of(emma)), (none, "")):
            plain = assign(client, o.pk, value)
            partial = assign(client, o.pk, value, htmx=True)
            assert plain.status_code == 302 and plain["Location"] == "/dashboard/"
            assert partial.status_code == 200
    assert spy.writes == []
    assert everything() == before
    assert not given.is_override and not none.is_override


@pytest.mark.django_db
def test_survives_reload_and_materialize(client):
    emma = member("Emma")
    c = weekly("Rotate", weekday=None, recurrence="daily", mode="rotating", eligibility="kids")
    page(client)
    o = ChoreOccurrence.objects.get(chore=c, date=d("2026-11-19"))
    assign(client, o.pk, "")
    page(client)
    materialize_week(d("2026-11-16"), today=d("2026-11-18"))
    o.refresh_from_db()
    assert (o.member, o.is_override) == (None, True)
    assert emma  # the rotation would otherwise have given the turn to someone


@pytest.mark.django_db
def test_no_profile_and_any_profile_may_assign(client):
    mom, emma = member("Mom", role="adult"), member("Emma")
    o = once()
    assert assign(client, o.pk, pk_of(emma)).status_code == 302  # no profile
    client.post(f"/profile/{emma.pk}/")
    assert assign(client, o.pk, pk_of(mom)).status_code == 302
    assert member_of(o) == mom


@pytest.mark.django_db
def test_assign_never_touches_completed_by_or_status(client):
    mom, emma = member("Mom", role="adult"), member("Emma")
    o = once()
    assign(client, o.pk, pk_of(emma))
    o.refresh_from_db()
    assert (o.status, o.completed_by, o.done_at) == ("todo", None, None)
    assert mom


# --- Rotation (#7) -------------------------------------------------------------------


def _rotation_run(client, target):
    """``target``: None for no reassign, else "other" or "unassign"."""
    a, b, c3 = member("A", order=0), member("B", order=1), member("C", order=2)
    c = weekly("Rotate", weekday=None, recurrence="daily", start="2026-11-16",
               mode="rotating", eligibility="kids")
    page(client)  # materializes 2026-W47
    rows = list(ChoreOccurrence.objects.filter(chore=c).order_by("date"))
    state = list(RotationState.objects.values_list("chore_id", "last_member_id"))
    if target:
        thursday = rows[3]
        was = thursday.member
        value = "" if target == "unassign" else pk_of(c3 if thursday.member != c3 else a)
        assert assign(client, thursday.pk, value).status_code == 302
        assert member_of(thursday) != was or target == "unassign"
    assert list(RotationState.objects.values_list("chore_id", "last_member_id")) == state
    projected = [card["assignee"] for card in sections(page(client, "2026-W48"))["week"]]
    assert list(RotationState.objects.values_list("chore_id", "last_member_id")) == state
    with at(NEXT_MONDAY):
        new = materialize_week(d("2026-11-23"), today=d("2026-11-23"))
    stored = [o.member.name for o in sorted(new, key=lambda o: o.date)]
    Chore.objects.filter(pk=c.pk).delete()
    for m in (a, b, c3):
        m.delete()
    return projected, stored


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["other", "unassign"])
def test_rotation_untouched_against_control_run(client, target):
    count = RotationState.objects.count()
    real = services.advance_rotation
    with mock.patch("chores.services.advance_rotation", side_effect=real) as turns:
        reassigned = _rotation_run(client, target)
        assigned_turns = turns.call_count
    with mock.patch("chores.services.advance_rotation", side_effect=real) as turns:
        control = _rotation_run(Client(), None)
        control_turns = turns.call_count
    assert reassigned == control
    assert assigned_turns == control_turns
    assert RotationState.objects.count() == count


@pytest.mark.django_db
def test_view_never_writes_rotation_state(client):
    mom = member("Mom", role="adult")
    c = weekly("Rotate", mode="rotating", eligibility="kids")
    o = row(c, "2026-11-18")
    RotationState.objects.create(chore=c, last_member=mom)
    before = everything()[2]
    assign(client, o.pk, pk_of(mom))
    assign(client, o.pk, "", htmx=True)
    assert everything()[2] == before


# --- Form on the card -------------------------------------------------------------------


@pytest.fixture
def week_of_cards(db):
    member("Mom", role="adult", avatar="👩")
    member("<b>Bo</b>", avatar="🧒")
    return {
        "overdue_old": once("Old", day="2026-11-12"),
        "overdue_today": once("Late", day="2026-11-18"),
        "today": once("Today", day="2026-11-18"),
        "upcoming": once("Soon", day="2026-11-20"),
        "done": row(chore("Done", start_date="2026-11-17"), "2026-11-17", status="done"),
        "skipped": row(chore("Skipped", start_date="2026-11-17"), "2026-11-17", status="skipped"),
    }


@pytest.mark.django_db
def test_every_stored_todo_card_has_the_form_after_status_inside_wrapper(client, week_of_cards):
    ChoreOccurrence.objects.filter(pk=week_of_cards["overdue_today"].pk).update(due_time="08:00")
    body = page(client)
    found = blocks(body)
    for key in ("overdue_old", "overdue_today", "today", "upcoming"):
        block = found[week_of_cards[key].pk]
        assert block.index('<form class="status"') < block.index('<form class="assign"')
        assert block.rstrip().endswith("</form>\n</div>") or block.rstrip().endswith("</div>")
    for key in ("done", "skipped"):
        assert form_of(found[week_of_cards[key].pk]) is None
    # outside the <article>
    assert not re.search(r'<article[^>]*>(?:(?!</article>).)*<form class="assign"', body, re.S)


@pytest.mark.django_db
def test_past_week_projected_and_planned_cards(client, week_of_cards):
    past = once("Past", day="2026-11-10")
    assert form_of(blocks(page(client, "2026-W46"))[past.pk]) is not None
    weekly("Planned", start="2026-11-04")
    body = page(client, "2026-W48")
    assert 'class="assign"' not in body
    assert 'name="assignee"' not in body
    assert "occurrence_assign" not in body


@pytest.mark.django_db
def test_form_markup_options_and_selection(client):
    mom, bo = member("Mom", role="adult", avatar="👩"), member("<b>Bo</b>", avatar="🧒")
    o = once(day="2026-11-18")
    ChoreOccurrence.objects.filter(pk=o.pk).update(member=bo)
    block = blocks(page(client))[o.pk]
    attrs, inner = form_of(block)
    url = f"/dashboard/assign/{o.pk}/"
    assert f'method="post" action="{url}"' in attrs
    assert f'hx-post="{url}"' in attrs and 'hx-trigger="change"' in attrs
    assert f'hx-target="#occurrence-{o.pk}"' in attrs and 'hx-swap="outerHTML"' in attrs
    assert 'name="csrfmiddlewaretoken"' in inner
    assert f'<label for="assign-{o.pk}">Assigned to</label>' in inner
    assert f'<select id="assign-{o.pk}" name="assignee">' in inner
    assert '<noscript><button type="submit">Assign</button></noscript>' in inner
    assert options(block) == [("", False, "Unassigned"), (str(mom.pk), False, "👩 Mom"),
                              (str(bo.pk), True, "🧒 <b>Bo</b>")]
    assert "<b>Bo</b>" not in inner and "&lt;b&gt;Bo&lt;/b&gt;" in inner


@pytest.mark.django_db
def test_unassigned_and_deleted_assignee_select_unassigned(client):
    emma = member("Emma")
    o = once(day="2026-11-18")
    ChoreOccurrence.objects.filter(pk=o.pk).update(member=emma)
    assert [s for _, s, _ in options(blocks(page(client))[o.pk])] == [False, True]
    emma.delete()
    assert [(v, s) for v, s, _ in options(blocks(page(client))[o.pk])] == [("", True)]


@pytest.mark.django_db
@pytest.mark.parametrize("query, expected", [
    ("", {}),
    ("?week=2026-W46&member=1&day=4", {"week": "2026-W46", "member": "1", "day": "4"}),
    ("?member=unassigned", {"member": "unassigned"}),
])
def test_hidden_filter_inputs(client, query, expected):
    mom = member("Mom")
    old = "W46" in query
    o = row(chore("Old", start_date="2026-11-12"), "2026-11-12", member=mom) if old else once()
    if old:
        query, expected = query.replace("member=1", f"member={mom.pk}"), {**expected, "member": str(mom.pk)}
    params = dict(p.split("=") for p in query[1:].split("&")) if query else {}
    with at(WED):
        body = client.get("/dashboard/", params).content.decode()
    assert hidden(form_of(blocks(body)[o.pk])[1]) == expected


@pytest.mark.django_db
def test_card_only_renders_just_the_article():
    from django.template.loader import render_to_string
    card = views._stored_cards(ChoreOccurrence.objects.filter(pk=once().pk), views._PoolCache())[0]
    out = render_to_string("chores/_chore_card.html", {"card": card, "card_only": True})
    assert out.strip().startswith('<article class="chore-card"') and out.strip().endswith("</article>")
    assert "<form" not in out and "<details" not in out and "card-block" not in out


# --- Errors ------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "put", "delete", "patch"])
def test_non_post_is_405(client, method):
    o = once()
    before = everything()
    assert getattr(client, method)(f"/dashboard/assign/{o.pk}/").status_code == 405
    assert everything() == before


@pytest.mark.django_db
def test_post_without_csrf_is_403():
    emma = member("Emma")
    o = once()
    before = everything()
    with at(WED):
        response = Client(enforce_csrf_checks=True).post(
            f"/dashboard/assign/{o.pk}/", {"assignee": pk_of(emma)}, **HTMX)
    assert response.status_code == 403
    assert everything() == before


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["0", OVERFLOW, "9223372036854775808", "424242", "abc", "-1", "1%00"])
def test_bad_occurrence_pk_is_404(client, path):
    member("Emma")
    once()
    before = everything()
    with at(WED):
        response = client.post(f"/dashboard/assign/{path}/", {"assignee": ""})
    assert response.status_code == 404
    assert everything() == before


@pytest.mark.django_db
def test_cancelled_deleted_and_template_rows_are_404(client):
    emma = member("Emma")
    cancelled = row(weekly(), "2026-11-18", is_cancelled=True)
    gone = once()
    gone_pk = gone.pk
    Chore.objects.filter(pk=gone.chore_id).delete()
    t_row = row(chore("Template", start_date="2026-11-18", is_template=True), "2026-11-18")
    before = everything()
    for pk in (cancelled.pk, gone_pk, t_row.pk):
        for htmx in (False, True):
            assert assign(client, pk, pk_of(emma), htmx=htmx).status_code == 404
    assert everything() == before


@pytest.mark.django_db
@pytest.mark.parametrize("value", [
    None, "abc", "-1", "0", "1.5", " 5", "5 ", "٥", OVERFLOW, "x" * 10000, HOSTILE, "424242",
])
def test_bad_assignee_is_400_never_echoed(client, value):
    emma = member("Emma")
    o = row(chore(start_date="2026-11-18"), "2026-11-18", member=emma)
    before = everything()
    for htmx in (False, True):
        response = assign(client, o.pk, value, htmx=htmx)
        assert response.status_code == 400
        body = response.content.decode()
        assert len(body) < 100
        if value:
            assert value not in body and html.escape(value) not in body
    assert everything() == before


@pytest.mark.django_db
def test_deleted_member_is_400_not_404(client):
    emma = member("Emma")
    o = once()
    value = pk_of(emma)
    emma.delete()
    assert assign(client, o.pk, value).status_code == 400
    assert member_of(o) is None and not o.is_override


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["done", "skipped"])
def test_done_or_skipped_is_400(client, status):
    emma = member("Emma")
    o = row(chore(start_date="2026-11-18"), "2026-11-18", status=status)
    before = everything()
    for value in (pk_of(emma), ""):
        for htmx in (False, True):
            response = assign(client, o.pk, value, htmx=htmx)
            assert response.status_code == 400
            assert "undo" in response.content.decode().lower()
    assert everything() == before


@pytest.mark.django_db
def test_error_order(client):
    o = row(chore(start_date="2026-11-18"), "2026-11-18", status="done")
    assert assign(client, 424242, "bad").status_code == 404  # occurrence before value
    assert assign(client, o.pk, "bad").status_code == 400
    assert assign(client, o.pk, "").status_code == 400  # done


@pytest.mark.django_db
def test_last_assignee_wins(client):
    a, b = member("A"), member("B")
    o = once()
    with at(WED):
        client.post(f"/dashboard/assign/{o.pk}/", {"assignee": [pk_of(a), pk_of(b)]})
    assert member_of(o) == b


def _race(change):
    real = views.get_object_or_404

    def racing(*args, **kwargs):
        found = real(*args, **kwargs)
        change(found)
        return found
    return mock.patch("chores.views.get_object_or_404", side_effect=racing)


@pytest.mark.django_db
@pytest.mark.parametrize("change, expected", [
    (lambda o: ChoreOccurrence.objects.filter(pk=o.pk).update(status="done"), 400),
    (lambda o: ChoreOccurrence.objects.filter(pk=o.pk).update(status="skipped"), 400),
    (lambda o: ChoreOccurrence.objects.filter(pk=o.pk).update(is_cancelled=True), 404),
    (lambda o: Chore.objects.filter(pk=o.chore_id).delete(), 404),
])
def test_lost_race_never_changes_the_row(client, change, expected):
    emma = member("Emma")
    o = once()
    with _race(change):
        response = assign(client, o.pk, pk_of(emma), htmx=True)
    assert response.status_code == expected
    if ChoreOccurrence.objects.filter(pk=o.pk).exists():
        o.refresh_from_db()
        assert (o.member, o.is_override) == (None, False)


@pytest.mark.django_db
def test_write_is_one_conditional_update(client):
    emma = member("Emma")
    o = once()
    with mock.patch.object(ChoreOccurrence, "save", side_effect=AssertionError("no save")):
        assert assign(client, o.pk, pk_of(emma)).status_code == 302
    assert member_of(o) == emma


@pytest.mark.django_db
def test_no_messages_on_any_path(client):
    emma = member("Emma")
    o = once()
    responses = [
        assign(client, o.pk, pk_of(emma), htmx=True),
        assign(client, o.pk, pk_of(emma), htmx=True),
        assign(client, o.pk, "bad", htmx=True),
        assign(client, o.pk, ""),
        assign(client, 424242, ""),
    ]
    for response in responses:
        assert list(get_messages(response.wsgi_request)) == []
    with at(WED):
        after = client.get("/dashboard/")
    assert 'class="message"' not in after.content.decode()


# --- Responses ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_htmx_reply_is_only_the_card_and_matches_reload(client):
    emma = member("Emma", avatar="🧒")
    o = once(points=15)
    response = assign(client, o.pk, pk_of(emma), htmx=True)
    assert response.status_code == 200
    body = response.content.decode()
    assert body.strip().startswith(f'<article class="chore-card" id="occurrence-{o.pk}">')
    assert body.strip().endswith("</article>")
    for banned in ("<html", "<details", "<form"):
        assert banned not in body
    assert "HX-Retarget" not in response
    [card] = sections(page(client))["today"]
    [swapped] = cards(body)
    for key in ("assignee", "points", "status", "title", "date"):
        assert swapped[key] == card[key]
    assert card["assignee"] == "🧒 Emma" and card["points"] == "15 pts"


@pytest.mark.django_db
def test_htmx_label_uses_viewer_zone(client):
    o = once(day="2026-11-21")
    emma = member("Emma")
    response = assign(client, o.pk, pk_of(emma), when=SAT_NOON, htmx=True, tz="Pacific/Kiritimati")
    assert label(response.content.decode()) == "Overdue"
    other = Client()
    again = assign(other, o.pk, "", when=SAT_NOON, htmx=True)
    assert label(again.content.decode()) == "To do"


@pytest.mark.django_db
def test_swap_leaves_one_of_each_control_on_the_page(client):
    emma = member("Emma")
    o = once()
    block = blocks(page(client))[o.pk]
    assert block.count('<details class="delete-confirm">') == 1
    assert block.count('<form class="status"') == 1 and block.count('<form class="assign"') == 1
    assign(client, o.pk, pk_of(emma), htmx=True)
    block = blocks(page(client))[o.pk]
    assert block.count('<form class="assign"') == 1


@pytest.mark.django_db
@pytest.mark.parametrize("filter_value, target, mismatch", [
    ("emma", "leo", True),
    ("emma", "emma", False),
    ("leo", "leo", False),
    ("unassigned", "mom", True),
    ("emma", "", True),
])
def test_member_filter_mismatch_retargets_list(client, filter_value, target, mismatch):
    people = {"mom": member("Mom", role="adult"), "emma": member("Emma"), "leo": member("Leo")}
    o = row(chore("Mine", start_date="2026-11-18"), "2026-11-18",
            member=None if filter_value == "unassigned" else people["emma" if filter_value != "leo" else "leo"])
    keep = row(chore("Other", start_date="2026-11-18"), "2026-11-18", member=people["emma"])
    fvalue = "unassigned" if filter_value == "unassigned" else str(people[filter_value].pk)
    value = pk_of(people.get(target))
    response = assign(client, o.pk, value, htmx=True, member=fvalue, day="3")
    assert response.status_code == 200
    body = response.content.decode()
    if not mismatch:
        assert "HX-Retarget" not in response and body.strip().startswith("<article")
        return
    assert response["HX-Retarget"] == "#dashboard-list" and response["HX-Reswap"] == "outerHTML"
    assert body.lstrip().startswith('<div id="dashboard-list">')
    with at(WED):
        expected = Client().get("/dashboard/", {"member": fvalue, "day": "3"}, **HTMX).content.decode()
    strip = lambda s: re.sub(r'(name="csrfmiddlewaretoken" value=")[^"]*"', r'\1"', s)
    oob = re.compile(r'<nav id="profile-switcher" hx-swap-oob="true".*?</nav>', re.S)
    expected = oob.sub("", expected)
    assert strip(body) == strip(expected)
    assert f"occurrence-{o.pk}" not in body
    if fvalue != "unassigned":
        assert (f"occurrence-{keep.pk}" in body) == (filter_value == "emma")


@pytest.mark.django_db
def test_mismatch_emptied_section_says_nothing_here(client):
    emma, leo = member("Emma"), member("Leo")
    o = row(chore(start_date="2026-11-18"), "2026-11-18", member=emma)
    response = assign(client, o.pk, pk_of(leo), htmx=True, member=str(emma.pk))
    assert 'class="nothing-here"' in response.content.decode()
    assert f"occurrence-{o.pk}" not in response.content.decode()


@pytest.mark.django_db
def test_no_member_filter_always_swaps_in_place(client):
    emma = member("Emma")
    o = once()
    response = assign(client, o.pk, pk_of(emma), htmx=True, day="3", week="2026-W47")
    assert "HX-Retarget" not in response
    assert response.content.decode().strip().startswith("<article")


@pytest.mark.django_db
def test_stale_filters_in_htmx_are_ignored(client):
    emma = member("Emma")
    o = once()
    response = assign(client, o.pk, pk_of(emma), htmx=True, member="999999", week="2026-W99", day="9")
    assert response.status_code == 200 and "HX-Retarget" not in response


# --- No JavaScript ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("data, location", [
    ({"week": "2026-W46", "member": "{m}", "day": "4"}, "/dashboard/?week=2026-W46&member={m}&day=4"),
    ({}, "/dashboard/"),
    ({"member": "unassigned"}, "/dashboard/?member=unassigned"),
    ({"week": "2026-W99", "member": "abc", "day": "9"}, "/dashboard/"),
    ({"member": "999999"}, "/dashboard/"),
    ({"week": HOSTILE, "member": HOSTILE, "day": HOSTILE}, "/dashboard/"),
])
def test_redirect_keeps_normalized_filters(client, data, location):
    emma = member("Emma")
    o = once("Old", day="2026-11-12")
    data = {k: v.format(m=emma.pk) for k, v in data.items()}
    response = assign(client, o.pk, pk_of(emma), **data)
    assert response.status_code == 302
    assert response["Location"] == location.format(m=emma.pk)
    assert member_of(o) == emma


@pytest.mark.django_db
def test_overdue_card_from_earlier_week_assigned_from_current_week(client):
    emma = member("Emma")
    o = once("Old", day="2026-11-12")
    assert form_of(blocks(page(client))[o.pk]) is not None
    assert assign(client, o.pk, pk_of(emma))["Location"] == "/dashboard/"
