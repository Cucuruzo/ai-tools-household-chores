"""An occurrence's instructions on its card and in history (#47). Today is Wed 18 Nov 2026."""
import re

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from chores.models import Chore, ChoreOccurrence
from tests.test_assign import assign
from tests.test_card_streak import standard
from tests.test_card_streak_oob import FORBIDDEN, OOB
from tests.test_dashboard import CARD, WED, at, blank_csrf, chore, d, main, member, page, part
from tests.test_history import ROW, body as history_body, main as history_main, make
from tests.test_planned_assign import bins, family, post  # noqa: F401 (fixtures)
from tests.test_toggle import BLOCK, blocks, credit, toggle

PARAGRAPH = re.compile(r'<p class="instructions">.*?</p>', re.S)
NASTY = "row <i>\nline2"
NASTY_HTML = '<p class="instructions">row &lt;i&gt;<br>line2</p>'


@pytest.fixture
def client():
    return Client()


def set_instructions(o, text):
    ChoreOccurrence.objects.filter(pk=o.pk).update(instructions=text)


def inner_of(body, pk):
    return {int(p): inner for p, inner in CARD.findall(body) if p}[pk]


def articles(body):
    return {int(pk): inner for pk, inner in re.findall(
        r'<article class="chore-card" id="occurrence-(\d+)"[^>]*>(.*?)</article>', body, re.S)}


# --- Stored cards ------------------------------------------------------------------


@pytest.mark.django_db
def test_stored_card_shows_the_escaped_row_text_between_streak_and_credit(client):
    _, rows = standard()
    set_instructions(rows[17], NASTY)
    inner = inner_of(page(client), rows[17].pk)
    assert inner.count(NASTY_HTML) == 1
    assert inner.index("</small>") < inner.index('class="streak"') < inner.index(NASTY_HTML) < inner.index('class="credit"')
    assert "<i>" not in inner


@pytest.mark.django_db
@pytest.mark.parametrize("raw, shown", [("a\r\nb", "a<br>b"), ("a\nb", "a<br>b"), ("a\rb", "a<br>b")])
def test_line_breaks_become_br(client, raw, shown):
    _, rows = standard()
    set_instructions(rows[18], raw)
    assert f'<p class="instructions">{shown}</p>' in inner_of(page(client), rows[18].pk)


@pytest.mark.django_db
def test_card_shows_the_row_not_the_chore(client):
    c, rows = standard()
    Chore.objects.filter(pk=c.pk).update(instructions="CHORETEXT")
    set_instructions(rows[18], "ROWTEXT")
    inner = inner_of(page(client), rows[18].pk)
    assert '<p class="instructions">ROWTEXT</p>' in inner
    assert "CHORETEXT" not in inner


@pytest.mark.django_db
def test_todo_overdue_done_and_skipped_cards_all_show_it(client):
    c, rows = standard()
    ChoreOccurrence.objects.filter(pk=rows[16].pk).update(status="skipped")
    old = ChoreOccurrence.objects.create(chore=c, date=d("2026-11-13"), status="todo", points=10, member=c.assigned_to)
    for o in (rows[16], rows[17], rows[18], old):
        set_instructions(o, f"text {o.pk}")
    shown = page(client)
    for o in (rows[16], rows[17], rows[18], old):
        assert f'<p class="instructions">text {o.pk}</p>' in inner_of(shown, o.pk)
    assert "Overdue" in inner_of(shown, old.pk)


@pytest.mark.django_db
def test_still_shown_after_done_skip_and_undo(client):
    _, rows = standard()
    set_instructions(rows[18], "keep")
    for action in ("done", "undo", "skip", "undo"):
        toggle(client, rows[18].pk, action)
        assert '<p class="instructions">keep</p>' in inner_of(page(client), rows[18].pk), action


# --- Planned cards -----------------------------------------------------------------


@pytest.mark.django_db
def test_planned_card_shows_the_chores_current_text(client):
    c = chore("Bins", start_date="2026-11-05", recurrence="weekly", instructions="proj & x\ny")
    body = page(client, "2026-W48")
    planned = re.search(r'<article class="chore-card" id="planned-%d-2026-11-26">(.*?)</article>' % c.pk, body, re.S)
    assert '<p class="instructions">proj &amp; x<br>y</p>' in planned.group(1)
    Chore.objects.filter(pk=c.pk).update(instructions="new text")
    planned = re.search(r'<article class="chore-card" id="planned-%d-2026-11-26">(.*?)</article>' % c.pk,
                        page(client, "2026-W48"), re.S)
    assert '<p class="instructions">new text</p>' in planned.group(1)
    assert "proj" not in planned.group(1)


# --- Empty adds nothing ------------------------------------------------------------


@pytest.mark.django_db
def test_empty_instructions_add_zero_bytes(client):
    c, rows = standard()
    chore("Bins", start_date="2026-11-05", recurrence="weekly")
    # The same page with text, minus its paragraphs, is the plain page byte for byte.
    plain = main(page(client))
    plain_future = main(page(client, "2026-W48"))
    assert "instructions" not in plain + plain_future
    Chore.objects.update(instructions="x")
    ChoreOccurrence.objects.update(instructions="y\nz")
    assert main(PARAGRAPH.sub("", page(client))) == plain
    assert main(PARAGRAPH.sub("", page(client, "2026-W48"))) == plain_future


@pytest.mark.django_db
def test_the_paragraph_sits_on_the_streak_line(client):
    _, rows = standard()
    set_instructions(rows[18], "hi")
    inner = inner_of(page(client), rows[18].pk)
    assert '🔥 3</p><p class="instructions">hi</p>\n' in inner


@pytest.mark.django_db
def test_planned_assign_reply_is_unchanged_when_empty(bins, family, client):  # noqa: F811
    reply = post(client, bins.pk, date="2026-12-03", assignee=str(family["ann"].pk)).content.decode()
    assert "instructions" not in reply


# --- The card stays parseable --------------------------------------------------------


@pytest.mark.django_db
def test_parsers_and_controls_are_unaffected(client):
    _, rows = standard()
    before = page(client)
    set_instructions(rows[17], NASTY)
    after = page(client)
    inner = inner_of(after, rows[17].pk)
    for key in ("title", "assignee", "date", "points", "status"):
        assert part(inner, key) == part(inner_of(before, rows[17].pk), key)
    for banned in ("<button", "<form", "<select", "<input", "<details"):
        assert banned not in inner
    block = blocks(after)[rows[17].pk]
    assert NASTY_HTML in block and "Undo" in block and block.rstrip().endswith("</div>")
    assert credit(block) == credit(blocks(before)[rows[17].pk])
    assert BLOCK.search(after)


# --- HTMX swaps --------------------------------------------------------------------


@pytest.mark.django_db
def test_toggle_block_equals_the_full_page(client):
    _, rows = standard()
    for o in rows.values():
        set_instructions(o, NASTY)
    swapped = toggle(client, rows[18].pk, "done", htmx=True).content.decode()
    assert NASTY_HTML in swapped
    assert blank_csrf(blocks(swapped)[rows[18].pk]) == blank_csrf(blocks(page(client))[rows[18].pk])


@pytest.mark.django_db
def test_out_of_band_articles_equal_the_page_and_hold_no_controls(client):
    _, rows = standard()
    page(client)
    ChoreOccurrence.objects.update(instructions="a <b>\nc")
    swapped = toggle(client, rows[18].pk, "done", htmx=True).content.decode()
    fresh = articles(page(client))
    found = OOB.findall(swapped)
    assert found
    for pk, inner in found:
        assert inner == fresh[int(pk)]
        assert 'class="instructions">a &lt;b&gt;<br>c</p>' in inner
        assert not FORBIDDEN.search(inner)


@pytest.mark.django_db
def test_assign_reply_equals_the_card_on_the_page(client):
    _, rows = standard()
    emma = member("Kid")
    set_instructions(rows[19], NASTY)
    reply = assign(client, rows[19].pk, str(emma.pk), htmx=True).content.decode()
    assert "<details" not in reply and "<form" not in reply
    assert reply.strip() == re.search(
        r'<article class="chore-card" id="occurrence-%d">.*?</article>' % rows[19].pk, page(client), re.S).group(0)


@pytest.mark.django_db
def test_planned_assign_reply_equals_the_card_on_the_page(bins, family, client):  # noqa: F811
    Chore.objects.filter(pk=bins.pk).update(instructions="plan & go\nnow")
    reply = post(client, bins.pk, date="2026-12-03", week="2026-W49",
                 assignee=str(family["ann"].pk)).content.decode()
    assert '<p class="instructions">plan &amp; go<br>now</p>' in reply
    with at(WED):
        full = client.get("/dashboard/", {"week": "2026-W49"}).content.decode()
    card = re.search(r'<article class="chore-card" id="planned-%d-2026-12-03">.*?</article>' % bins.pk, full, re.S)
    assert reply.strip() == card.group(0)


# --- History -----------------------------------------------------------------------


@pytest.mark.django_db
def test_history_row_shows_its_own_escaped_instructions(client):
    o = make(instructions="a\nb <i>")
    set_instructions(o, "a\nb <i>")
    Chore.objects.filter(pk=o.chore_id).update(instructions="changed")
    row = dict(ROW.findall(history_body(client)))[str(o.pk)]
    assert '<p class="instructions">a<br>b &lt;i&gt;</p>' in row
    assert "changed" not in row
    assert row.index('class="instructions"') < row.index('class="credit"')


@pytest.mark.django_db
def test_history_row_without_instructions_is_unchanged(client):
    o = make()
    plain = blank_csrf(history_main(history_body(client)))
    assert "instructions" not in plain
    set_instructions(o, "x")
    assert blank_csrf(history_main(PARAGRAPH.sub("", history_body(client)))) == plain


# --- Queries -----------------------------------------------------------------------


def counts(client):
    out = []
    for week in (None, "2026-W48"):
        with CaptureQueriesContext(connection) as q:
            page(client, week)
        out.append(len(q))
    with CaptureQueriesContext(connection) as q:
        history_body(client)
    return out + [len(q)]


@pytest.mark.django_db
def test_no_new_queries(client):
    _, rows = standard()
    chore("Bins", start_date="2026-11-05", recurrence="weekly")
    make()
    page(client)
    without = counts(client)
    Chore.objects.update(instructions="x\ny")
    ChoreOccurrence.objects.update(instructions="a\nb")
    assert counts(client) == without
