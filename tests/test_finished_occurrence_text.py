"""Title and instructions of a done or skipped row on its edit page (#52)."""
import re

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from tests.test_dashboard import WED, at
from tests.test_edit_scope import SECTION, body, daily, get, nov, row

FINISHED = ["done", "skipped"]
FORM_TAGS = re.compile(r"<(form|input|textarea|select|button)\b")


@pytest.fixture
def client():
    return Client()


def section(client, c, o):
    return SECTION.search(body(client, c, f"?occurrence={o.pk}")).group(1)


@pytest.mark.django_db
@pytest.mark.parametrize("status", FINISHED)
def test_shows_the_rows_own_title_not_the_chores_current_one(client, status):
    c = daily("Dishes")
    o = row(c, nov(17), status)
    c.title = "Laundry"
    c.save()
    text = body(client, c, f"?occurrence={o.pk}")
    assert '<p id="occurrence-title"><strong>Dishes</strong></p>' in section(client, c, o)
    assert "Laundry" in text  # the chore form itself carries the new title


@pytest.mark.django_db
@pytest.mark.parametrize("status", FINISHED)
def test_instructions_keep_line_breaks(client, status):
    c = daily()
    o = row(c, nov(17), status, title="Dishes", instructions="Line 1\r\nLine 2\nLine 3")
    assert (
        '<p id="occurrence-instructions" class="instructions">Line 1<br>Line 2<br>Line 3</p>'
        in section(client, c, o)
    )


@pytest.mark.django_db
@pytest.mark.parametrize("status", FINISHED)
def test_instructions_are_the_rows_snapshot(client, status):
    c = daily(instructions="Old")
    o = row(c, nov(17), status, title="Dishes", instructions="Old")
    c.instructions = "New"
    c.save()
    s = section(client, c, o)
    assert ">Old</p>" in s and "New" not in s


@pytest.mark.django_db
@pytest.mark.parametrize("status", FINISHED)
def test_empty_instructions_show_no_paragraph(client, status):
    c = daily()
    o = row(c, nov(17), status, title="Dishes", instructions="")
    assert 'id="occurrence-instructions"' not in body(client, c, f"?occurrence={o.pk}")


@pytest.mark.django_db
@pytest.mark.parametrize("status", FINISHED)
def test_markup_is_escaped(client, status):
    c = daily()
    o = row(c, nov(17), status, title="<b>Mop & go</b>", instructions="<script>x</script>")
    text = body(client, c, f"?occurrence={o.pk}")
    assert "&lt;b&gt;Mop &amp; go&lt;/b&gt;" in text
    assert "&lt;script&gt;x&lt;/script&gt;" in text
    assert "<script>x</script>" not in text
    assert "<b>Mop" not in text


@pytest.mark.django_db
@pytest.mark.parametrize("status", FINISHED)
def test_nothing_is_editable(client, status):
    c = daily()
    o = row(c, nov(17), status, title="Dishes", instructions="Rinse")
    s = section(client, c, o)
    assert not FORM_TAGS.search(s)
    assert 'id="occurrence-form"' not in body(client, c, f"?occurrence={o.pk}")


@pytest.mark.django_db
@pytest.mark.parametrize("status", FINISHED)
def test_status_sentence_is_unchanged_and_after_the_text(client, status):
    c = daily()
    o = row(c, nov(17), status, title="Dishes", instructions="Rinse")
    s = section(client, c, o)
    sentence = f"<p>This occurrence is {status}. Undo it on the dashboard to change it on its own.</p>"
    assert s.index("occurrence-title") < s.index("occurrence-instructions") < s.index(sentence)


@pytest.mark.django_db
def test_todo_row_page_is_unchanged(client):
    c = daily()
    o = row(c, nov(20), title="Dishes", instructions="Rinse")
    text = body(client, c, f"?occurrence={o.pk}")
    assert 'id="occurrence-form"' in text
    assert 'id="occ_title"' in text and 'id="occ_instructions"' in text
    assert 'id="occurrence-title"' not in text
    assert 'id="occurrence-instructions"' not in text


@pytest.mark.django_db
def test_recurring_page_without_occurrence_has_no_section(client):
    c = daily()
    row(c, nov(17), "done")
    text = body(client, c)
    assert 'id="this-occurrence"' not in text and 'id="occurrence-title"' not in text


@pytest.mark.django_db
def test_one_off_page_has_no_section(client):
    from tests.test_dashboard import chore

    c = chore("Once", start_date=nov(18))
    o = row(c, nov(18), "done")
    text = body(client, c, f"?occurrence={o.pk}")
    assert 'id="this-occurrence"' not in text and 'id="occurrence-title"' not in text


@pytest.mark.django_db
def test_query_count_stays_at_four(client):
    c = daily()
    o = row(c, nov(17), "done", title="Dishes", instructions="Rinse")
    with CaptureQueriesContext(connection) as queries:
        response = get(client, c, f"?occurrence={o.pk}")
    assert response.status_code == 200
    assert len(queries) == 4


@pytest.mark.django_db
def test_plain_html_without_javascript(client):
    c = daily()
    o = row(c, nov(17), "done", title="Dishes", instructions="Rinse")
    s = section(client, c, o)
    assert "<script" not in s and "hx-" not in s
