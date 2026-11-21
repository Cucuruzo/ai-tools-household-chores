import datetime
import html as htmllib

import pytest
from django.test import Client

from chores.models import Chore, ChoreOccurrence, Member

LIST = "/chores/templates/"
CREATE = "/chores/create/"
HTMX = {"HTTP_HX_REQUEST": "true"}
EMPTY_TEXT = (
    'No household templates yet. Tick "Also save as a household template" '
    "when saving a chore to add one."
)
OVERFLOW = 9223372036854775808


@pytest.fixture
def client():
    return Client()


def make_template(title="Water plants", **kwargs):
    fields = {"title": title, "start_date": datetime.date(2020, 1, 1), "is_template": True}
    fields.update(kwargs)
    return Chore.objects.create(**fields)


def make_chore(**kwargs):
    fields = {"title": "Dishes", "start_date": datetime.date(2026, 3, 2)}
    fields.update(kwargs)
    return Chore.objects.create(**fields)


def edit_url(pk):
    return f"/chores/templates/{pk}/edit/"


def delete_url(pk):
    return f"/chores/templates/{pk}/delete/"


def valid_data(**overrides):
    data = {
        "title": "Water the garden",
        "instructions": "Front and back",
        "due_time": "18:30",
        "assignment_mode": "unassigned",
        "recurrence": "daily",
        "points": "20",
    }
    data.update(overrides)
    return data


def text(response):
    return htmllib.unescape(response.content.decode())


def builtin():
    return Chore.objects.filter(is_builtin=True).first()


def snapshot(chore):
    chore.refresh_from_db()
    return {f.name: getattr(chore, f.name) for f in Chore._meta.concrete_fields}


# --- entry point -----------------------------------------------------------


@pytest.mark.django_db
def test_picker_has_manage_link(client):
    response = client.get(CREATE)  # built-ins exist, so the picker is shown
    assert 'href="/chores/templates/"' in text(response)
    assert "Manage templates" in text(response)


@pytest.mark.django_db
def test_chore_edit_has_no_manage_link(client):
    chore = make_chore()
    assert "Manage templates" not in text(client.get(f"/chores/{chore.pk}/edit/"))


@pytest.mark.django_db
def test_base_has_no_manage_link(client):
    assert "Manage templates" not in text(client.get("/family/"))


# --- list ------------------------------------------------------------------


@pytest.mark.django_db
def test_list_rows_order_and_exclusions(client):
    b = make_template("banana")
    a = make_template("Apple")
    a2 = make_template("apple")
    make_chore(title="Ordinary chore")
    response = client.get(LIST)
    body = text(response)
    assert response.status_code == 200
    assert "<h1>Manage templates</h1>" in body
    assert body.index(f"template-{a.pk}") < body.index(f"template-{a2.pk}") < body.index(f"template-{b.pk}")
    assert body.count('class="template-row"') == 3
    assert "Ordinary chore" not in body
    assert "Take out the trash" not in body  # built-in
    for t in (a, a2, b):
        assert edit_url(t.pk) in body
        assert f'hx-post="{delete_url(t.pk)}"' in body
    assert body.count("Delete</button>") == 3


@pytest.mark.django_db
def test_list_delete_markup(client):
    t = make_template("Water plants")
    body = text(client.get(LIST))
    assert '<section id="templates">' in body
    assert 'class="outline contrast"' in body
    assert 'hx-confirm="Delete template Water plants? Chores created from it are kept."' in body
    assert 'hx-target="#templates" hx-select="#templates" hx-swap="outerHTML"' in body


@pytest.mark.django_db
def test_list_empty_state(client):
    body = text(client.get(LIST))
    assert EMPTY_TEXT in body
    assert 'href="/chores/create/"' in body
    assert "template-row" not in body


# --- edit ------------------------------------------------------------------


@pytest.mark.django_db
def test_edit_get_renders_prefilled_form(client):
    m = Member.objects.create(name="Ann", role="adult", avatar="x")
    t = make_template(
        "Water plants", instructions="Weekly", assignment_mode="fixed", assigned_to=m,
        recurrence="weekly", recurrence_day=2, points=10,
    )
    response = client.get(edit_url(t.pk))
    body = text(response)
    assert response.status_code == 200
    assert "<h1>Edit template</h1>" in body
    assert f'action="{edit_url(t.pk)}"' in body
    assert 'value="Water plants"' in body
    assert "Weekly" in body
    assert 'value="10"' in body
    assert "Save template" in body
    assert "Save chore" not in body
    assert 'name="template"' not in body
    assert 'name="save_as_template"' not in body
    assert 'name="start_date"' not in body
    assert "Start from a template" not in body


@pytest.mark.django_db
def test_edit_post_updates_fields_and_redirects(client):
    m = Member.objects.create(name="Ann", role="adult", avatar="x")
    t = make_template("Old", start_date=datetime.date(2020, 5, 5))
    data = valid_data(
        assignment_mode="fixed", assigned_to=str(m.pk), recurrence="weekly", recurrence_day="3"
    )
    response = client.post(edit_url(t.pk), data)
    assert response.status_code == 302
    assert response["Location"] == LIST
    t.refresh_from_db()
    assert t.title == "Water the garden"
    assert t.instructions == "Front and back"
    assert t.due_time == datetime.time(18, 30)
    assert t.assignment_mode == "fixed"
    assert t.assigned_to == m
    assert t.recurrence == "weekly"
    assert t.recurrence_day == 3
    assert t.points == 20
    assert t.start_date == datetime.date(2020, 5, 5)
    assert t.is_template is True
    assert t.is_builtin is False
    assert t.template_source is None
    assert "Template saved" in text(client.get(LIST))


@pytest.mark.django_db
def test_edit_post_ignores_start_date_in_post(client):
    t = make_template("Old", start_date=datetime.date(2020, 5, 5))
    client.post(edit_url(t.pk), valid_data(start_date="2031-01-01", save_as_template="on"))
    t.refresh_from_db()
    assert t.start_date == datetime.date(2020, 5, 5)
    assert Chore.objects.filter(is_template=True, is_builtin=False).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"title": ""}, "This field is required"),
        ({"assignment_mode": "rotating"}, "Choose who is eligible"),
        ({"recurrence": "weekly"}, "Choose a day of the week"),
        ({"recurrence": "monthly"}, "Choose a day of the month"),
    ],
)
def test_edit_post_errors_save_nothing(client, overrides, message):
    t = make_template("Old")
    before = snapshot(t)
    response = client.post(edit_url(t.pk), valid_data(**overrides))
    assert response.status_code == 200
    assert message in text(response)
    assert "Edit template" in text(response)
    assert snapshot(t) == before


@pytest.mark.django_db
def test_edit_keeps_derived_chores(client):
    t = make_template("Old", points=3)
    c = make_chore(title="Mine", template_source=t, points=4)
    before = snapshot(c)
    client.post(edit_url(t.pk), valid_data())
    assert snapshot(c) == before
    assert c.template_source_id == t.pk


@pytest.mark.django_db
def test_picker_shows_edited_title_and_prefills(client):
    t = make_template("Old name")
    client.post(edit_url(t.pk), valid_data(title="New name", points="5"))
    body = text(client.get(CREATE))
    assert "New name" in body
    assert "Old name" not in body
    prefilled = text(client.get(f"{CREATE}?template={t.pk}"))
    assert 'value="New name"' in prefilled
    assert 'value="5"' in prefilled


# --- delete ----------------------------------------------------------------


@pytest.mark.django_db
def test_delete_non_htmx_redirects(client):
    t = make_template("Gone")
    response = client.post(delete_url(t.pk))
    assert response.status_code == 302
    assert response["Location"] == LIST
    assert not Chore.objects.filter(pk=t.pk).exists()
    assert "Template deleted" in text(client.get(LIST))


@pytest.mark.django_db
def test_delete_htmx_returns_list(client):
    t = make_template("Gone")
    other = make_template("Stays")
    response = client.post(delete_url(t.pk), **HTMX)
    body = text(response)
    assert response.status_code == 200
    assert '<section id="templates">' in body
    assert "Gone" not in body.replace("Template deleted", "")
    assert "Stays" in body
    assert "Template deleted" in body
    assert Chore.objects.filter(pk=other.pk).exists()


@pytest.mark.django_db
def test_delete_keeps_derived_chore_and_occurrences(client):
    t = make_template("Gone")
    c = make_chore(title="Mine", template_source=t, points=4)
    occ = ChoreOccurrence.objects.create(chore=c, date=datetime.date(2026, 3, 2), points=4)
    before = snapshot(c)
    before.pop("template_source")
    client.post(delete_url(t.pk))
    after = snapshot(c)
    assert after.pop("template_source") is None
    assert after == before
    assert ChoreOccurrence.objects.filter(pk=occ.pk).exists()


@pytest.mark.django_db
def test_deleted_template_gone_from_picker_and_query_id_blank(client):
    t = make_template("Gone")
    client.post(delete_url(t.pk))
    assert "Gone" not in text(client.get(CREATE))
    response = client.get(f"{CREATE}?template={t.pk}")
    assert response.status_code == 200
    assert 'name="title"' in text(response)
    assert 'value="Gone"' not in text(response)


# --- 404s ------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["builtin", "chore", "unknown", "zero", "overflow"])
def test_bad_targets_404(client, kind):
    chore = make_chore()
    pk = {
        "builtin": builtin().pk,
        "chore": chore.pk,
        "unknown": 999999,
        "zero": 0,
        "overflow": OVERFLOW,
    }[kind]
    target = Chore.objects.filter(pk=pk).first() if pk < OVERFLOW else None
    before = snapshot(target) if target else None
    assert client.get(edit_url(pk)).status_code == 404
    assert client.post(edit_url(pk), valid_data()).status_code == 404
    assert client.post(delete_url(pk)).status_code == 404
    assert client.post(delete_url(pk), **HTMX).status_code == 404
    if target:
        assert snapshot(target) == before


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["abc/edit/", "abc/delete/", "-1/edit/", "1.5/edit/"])
def test_non_numeric_paths_404(client, path):
    assert client.get(f"{LIST}{path}").status_code == 404
    assert client.post(f"{LIST}{path}").status_code == 404


# --- 405s ------------------------------------------------------------------


@pytest.mark.django_db
def test_method_not_allowed(client):
    t = make_template("T")
    assert client.get(delete_url(t.pk)).status_code == 405
    assert client.post(LIST).status_code == 405
    assert client.put(edit_url(t.pk)).status_code == 405
    assert client.delete(edit_url(t.pk)).status_code == 405
    assert Chore.objects.filter(pk=t.pk).exists()
