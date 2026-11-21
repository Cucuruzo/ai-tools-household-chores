import datetime
import html as htmllib
import importlib
import re

import pytest
from django.apps import apps as global_apps
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from chores.models import Chore, Member

CREATE = "/chores/create/"
HTMX = {"HTTP_HX_REQUEST": "true"}

BUILTINS = [
    # title, recurrence, recurrence_day, mode, eligibility, points
    ("Take out the trash", "weekly", 0, "rotating", "everyone", 10),
    ("Load the dishwasher", "daily", None, "rotating", "everyone", 5),
    ("Clean the bathroom", "weekly", 5, "rotating", "adults", 20),
    ("Change bed sheets", "weekly", 6, "rotating", "everyone", 20),
]
BUILTIN_TITLES_SORTED = sorted(t[0] for t in BUILTINS)

COPIED = ["title", "instructions", "start_date", "due_time", "assignment_mode",
          "eligibility", "assigned_to_id", "recurrence", "recurrence_day", "points"]


@pytest.fixture
def client():
    return Client()


def chores():
    return Chore.objects.filter(is_template=False)


def household():
    return Chore.objects.filter(is_template=True, is_builtin=False)


def builtin(title):
    return Chore.objects.get(is_builtin=True, title=title)


def make_member(name, role="adult", avatar="😀"):
    return Member.objects.create(name=name, role=role, avatar=avatar)


def make_template(title="Water plants", **kwargs):
    fields = {"title": title, "start_date": datetime.date(2020, 1, 1), "is_template": True}
    fields.update(kwargs)
    return Chore.objects.create(**fields)


def make_chore(**kwargs):
    fields = {"title": "Dishes", "start_date": datetime.date(2026, 3, 2)}
    fields.update(kwargs)
    return Chore.objects.create(**fields)


def edit_url(chore):
    return f"/chores/{chore.pk}/edit/"


def payload(**overrides):
    data = {
        "title": "Dishes", "instructions": "", "start_date": "2026-03-02", "due_time": "",
        "assignment_mode": "unassigned", "eligibility": "", "assigned_to": "",
        "recurrence": "once", "recurrence_day": "", "points": "10",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def post(client, url=CREATE, **overrides):
    return client.post(url, payload(**overrides))


def page_of(response):
    return response.content.decode()


def picker(page):
    m = re.search(r'<section id="template-picker">.*?</section>', page, re.S)
    return m.group(0) if m else None


def picker_groups(page):
    """{heading: [titles]} in page order."""
    section = picker(page)
    groups = {}
    for heading, body in re.findall(r"<h3>([^<]*)</h3>\s*<ul[^>]*>(.*?)</ul>", section, re.S):
        groups[heading] = [htmllib.unescape(t) for t in re.findall(r"<a [^>]*>([^<]*)</a>", body)]
    return groups


def select_html(page, select_id):
    return re.search(rf'<select[^>]*id="{select_id}".*?</select>', page, re.S).group(0)


def selected(page, select_id):
    return re.findall(r'<option value="([^"]*)" selected', select_html(page, select_id))


def input_value(page, name):
    m = re.search(rf'<input type="[^"]*" name="{name}"(?: value="([^"]*)")?', page)
    return None if m is None else htmllib.unescape(m.group(1) or "")


def is_hidden(page, field_id):
    return " hidden" in re.search(rf'<div id="field-{field_id}"[^>]*>', page).group(0)


def submit_as_shown(page):
    """Build the POST a browser (with JS) would send for the rendered form."""
    data = {}
    for name in ["title", "start_date", "due_time", "template"]:
        data[name] = input_value(page, name) or ""
    text = re.search(r'<textarea name="instructions"[^>]*>\n?(.*?)</textarea>', page, re.S).group(1)
    data["instructions"] = htmllib.unescape(text)
    for name in ["assignment_mode", "eligibility", "assigned_to", "recurrence", "points"]:
        data[name] = (selected(page, f"id_{name}") or [""])[0]
    days = selected(page, "id_recurrence_day_week") + selected(page, "id_recurrence_day_month")
    data["recurrence_day"] = days[0] if days else ""
    return data


# --- Model / migration -----------------------------------------------------


def test_is_builtin_field_defaults_false():
    assert Chore._meta.get_field("is_builtin").default is False


@pytest.mark.django_db
def test_builtins_exist_with_exact_values():
    rows = Chore.objects.filter(is_builtin=True)
    assert rows.count() == 4
    actual = sorted(
        (c.title, c.recurrence, c.recurrence_day, c.assignment_mode, c.eligibility, c.points)
        for c in rows
    )
    assert actual == sorted(BUILTINS)
    for c in rows:
        assert c.is_template is True
        assert c.assigned_to is None
        assert c.instructions == ""
        assert c.due_time is None
        assert c.template_source is None
        assert c.start_date is not None


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


@pytest.mark.django_db
def test_data_migration_is_idempotent():
    migration = importlib.import_module("chores.migrations.0004_load_builtin_templates")
    migration.load_builtins(global_apps, None)
    migration.load_builtins(global_apps, None)
    assert Chore.objects.filter(is_builtin=True).count() == 4


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_migrate_again_reverse_and_reapply():
    call_command("migrate", verbosity=0)
    assert Chore.objects.filter(is_builtin=True).count() == 4

    household_tpl = make_template("Mine")
    made_from_builtin = make_chore(template_source=builtin("Clean the bathroom"))

    call_command("migrate", "chores", "0003_chore_is_builtin", verbosity=0)
    assert Chore.objects.filter(is_builtin=True).count() == 0
    assert Chore.objects.filter(pk=household_tpl.pk).exists()
    made_from_builtin.refresh_from_db()
    assert made_from_builtin.template_source is None

    call_command("migrate", "chores", verbosity=0)
    assert Chore.objects.filter(is_builtin=True).count() == 4
    assert sorted(Chore.objects.filter(is_builtin=True).values_list("title", flat=True)) == BUILTIN_TITLES_SORTED


@pytest.mark.django_db
def test_is_builtin_not_on_form_and_crafted_value_ignored(client):
    assert 'name="is_builtin"' not in page_of(client.get(CREATE))
    post(client, is_builtin="on", is_template="on")
    chore = chores().get()
    assert chore.is_builtin is False


# --- Picker ----------------------------------------------------------------


@pytest.mark.django_db
def test_picker_lists_builtins_sorted_and_hides_household_heading(client):
    page = page_of(client.get(CREATE))
    assert "Start from a template" in picker(page)
    assert picker_groups(page) == {"Built-in": BUILTIN_TITLES_SORTED}
    assert "Household" not in picker(page)
    assert page.index('id="template-picker"') < page.index('id="chore-form"')


@pytest.mark.django_db
def test_picker_groups_household_sorted_case_insensitive_then_id(client):
    b = make_template("banana")
    a1 = make_template("apple")
    a2 = make_template("Apple")
    c = make_template("Cherry")
    make_chore(title="Not a template")
    page = page_of(client.get(CREATE))
    groups = picker_groups(page)
    assert list(groups) == ["Built-in", "Household"]
    assert groups["Built-in"] == BUILTIN_TITLES_SORTED
    assert groups["Household"] == ["apple", "Apple", "banana", "Cherry"]
    household_ids = re.findall(r'href="/chores/create/\?template=(\d+)"', picker(page))[4:]
    assert household_ids == [str(a1.pk), str(a2.pk), str(b.pk), str(c.pk)]
    assert "Not a template" not in picker(page)


@pytest.mark.django_db
def test_picker_hidden_when_no_templates(client):
    Chore.objects.filter(is_template=True).delete()
    page = page_of(client.get(CREATE))
    assert picker(page) is None
    assert "Start from a template" not in page


@pytest.mark.django_db
def test_picker_only_household_when_builtins_deleted(client):
    Chore.objects.filter(is_builtin=True).delete()
    make_template("Mine")
    assert picker_groups(page_of(client.get(CREATE))) == {"Household": ["Mine"]}


@pytest.mark.django_db
def test_picker_not_on_edit_page(client):
    page = page_of(client.get(edit_url(make_chore())))
    assert picker(page) is None
    assert "Start from a template" not in page
    assert 'name="template"' not in page


@pytest.mark.django_db
def test_picker_links_use_href_and_hx_get(client):
    tpl = builtin("Clean the bathroom")
    section = picker(page_of(client.get(CREATE)))
    link = re.search(rf'<a href="/chores/create/\?template={tpl.pk}"[^>]*>', section).group(0)
    assert f'hx-get="/chores/create/?template={tpl.pk}"' in link
    assert 'hx-target="#chore-form-area"' in link
    assert 'hx-select="#chore-form-area"' in link


@pytest.mark.django_db
def test_picker_title_is_escaped(client):
    make_template("<b>x</b>")
    section = picker(page_of(client.get(CREATE)))
    assert "&lt;b&gt;x&lt;/b&gt;" in section
    assert "<b>x</b>" not in section


@pytest.mark.django_db
def test_htmx_pick_returns_form_area_with_script(client):
    tpl = builtin("Take out the trash")
    response = client.get(f"{CREATE}?template={tpl.pk}", **HTMX)
    assert response.status_code == 200
    area = re.search(r'<div id="chore-form-area">.*</div>', page_of(response), re.S).group(0)
    assert 'value="Take out the trash"' in area
    assert "<script>" in area  # show/hide handlers re-bind after the swap


# --- Pre-fill --------------------------------------------------------------


@pytest.mark.django_db
def test_prefill_from_builtin_clean_the_bathroom(client):
    tpl = builtin("Clean the bathroom")
    page = page_of(client.get(f"{CREATE}?template={tpl.pk}"))
    assert input_value(page, "title") == "Clean the bathroom"
    assert input_value(page, "start_date") == timezone.localdate().isoformat()
    assert input_value(page, "due_time") == ""
    assert input_value(page, "template") == str(tpl.pk)
    assert selected(page, "id_assignment_mode") == ["rotating"]
    assert selected(page, "id_eligibility") == ["adults"]
    assert selected(page, "id_recurrence") == ["weekly"]
    assert selected(page, "id_recurrence_day_week") == ["5"]
    assert ">Saturday<" in select_html(page, "id_recurrence_day_week")
    assert selected(page, "id_points") == ["20"]
    assert not is_hidden(page, "eligibility")
    assert is_hidden(page, "assigned-to")
    assert not is_hidden(page, "weekday")
    assert is_hidden(page, "monthday")


@pytest.mark.django_db
def test_prefill_from_household_fixed_template(client):
    ann = make_member("Ann")
    tpl = make_template("Feed cat", instructions="Wet food\nthen water", due_time=datetime.time(7, 15),
                        assignment_mode="fixed", assigned_to=ann, recurrence="monthly",
                        recurrence_day=12, points=5, start_date=datetime.date(2019, 5, 5))
    page = page_of(client.get(f"{CREATE}?template={tpl.pk}"))
    assert input_value(page, "title") == "Feed cat"
    assert "Wet food\nthen water</textarea>" in page
    assert input_value(page, "due_time") == "07:15"
    assert input_value(page, "start_date") == timezone.localdate().isoformat()
    assert selected(page, "id_assignment_mode") == ["fixed"]
    assert selected(page, "id_assigned_to") == [str(ann.pk)]
    assert selected(page, "id_eligibility") == [""]
    assert selected(page, "id_recurrence_day_month") == ["12"]
    assert selected(page, "id_points") == ["5"]
    assert not is_hidden(page, "assigned-to")
    assert is_hidden(page, "eligibility")


@pytest.mark.django_db
def test_prefill_household_template_with_deleted_assignee(client):
    ann = make_member("Ann")
    tpl = make_template("Feed cat", assignment_mode="fixed", assigned_to=ann)
    ann.delete()
    response = client.get(f"{CREATE}?template={tpl.pk}")
    assert response.status_code == 200
    page = page_of(response)
    assert selected(page, "id_assigned_to") == [""]
    assert "— nobody yet —" in page
    assert not response.context["form"].errors


@pytest.mark.django_db
@pytest.mark.parametrize("title", [t[0] for t in BUILTINS])
def test_each_builtin_saves_when_submitted_unchanged(client, title):
    tpl = builtin(title)
    data = submit_as_shown(page_of(client.get(f"{CREATE}?template={tpl.pk}")))
    response = client.post(CREATE, data)
    assert response.status_code == 302, response.context["form"].errors
    chore = chores().get()
    assert chore.template_source == tpl
    assert (chore.title, chore.recurrence, chore.recurrence_day, chore.assignment_mode,
            chore.eligibility, chore.points) == next(b for b in BUILTINS if b[0] == title)
    assert chore.start_date == timezone.localdate()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value",
    ["", "abc", "999999", "0", "-1", "1.0", " 1", "1e3", "9" * 40, str(2**63), "١", "%00"],
)
def test_bad_template_query_returns_default_form(client, value):
    response = client.get(CREATE, {"template": value})
    assert response.status_code == 200
    page = page_of(response)
    assert input_value(page, "title") == ""
    assert input_value(page, "template") == ""
    assert selected(page, "id_assignment_mode") == ["unassigned"]
    assert selected(page, "id_recurrence") == ["once"]
    assert selected(page, "id_points") == ["10"]


@pytest.mark.django_db
def test_template_query_with_non_template_chore_returns_default_form(client):
    chore = make_chore(title="Real chore", points=20)
    page = page_of(client.get(CREATE, {"template": str(chore.pk)}))
    assert input_value(page, "title") == ""
    assert input_value(page, "template") == ""
    assert selected(page, "id_points") == ["10"]


@pytest.mark.django_db
def test_repeated_template_query_param_does_not_error(client):
    tpl = builtin("Take out the trash")
    response = client.get(f"{CREATE}?template=abc&template={tpl.pk}")
    assert response.status_code == 200


# --- Saving from a template ------------------------------------------------


@pytest.mark.django_db
def test_save_from_template_sets_source_and_allows_changes(client):
    tpl = builtin("Clean the bathroom")
    before = Chore.objects.filter(pk=tpl.pk).values().get()
    response = post(client, template=str(tpl.pk), title="Scrub tub", assignment_mode="rotating",
                    eligibility="everyone", recurrence="weekly", recurrence_day="2", points="5")
    assert response.status_code == 302
    chore = chores().get()
    assert response["Location"] == edit_url(chore)
    assert (chore.title, chore.eligibility, chore.recurrence_day, chore.points) == ("Scrub tub", "everyone", 2, 5)
    assert chore.template_source == tpl
    assert (chore.is_template, chore.is_builtin) == (False, False)
    assert Chore.objects.filter(pk=tpl.pk).values().get() == before


@pytest.mark.django_db
def test_save_from_household_template_sets_source(client):
    tpl = make_template("Mine")
    post(client, template=str(tpl.pk))
    assert chores().get().template_source == tpl


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value",
    [None, "", "abc", "999999", "-1", "1.0", "9" * 40, str(2**63), "\x00", "١", "nontemplate"],
)
def test_bad_template_post_value_saves_with_null_source(client, value):
    if value == "nontemplate":
        value = str(make_chore(title="Other").pk)
    response = post(client, title="New", template=value)
    assert response.status_code == 302
    assert chores().get(title="New").template_source is None


@pytest.mark.django_db
def test_invalid_post_keeps_template_id_and_corrected_submit_sets_source(client):
    tpl = builtin("Load the dishwasher")
    response = post(client, template=str(tpl.pk), title="")
    assert response.status_code == 200
    assert chores().count() == 0
    page = page_of(response)
    assert input_value(page, "template") == str(tpl.pk)
    data = submit_as_shown(page)
    data["title"] = "Dishwasher"
    assert client.post(CREATE, data).status_code == 302
    assert chores().get().template_source == tpl


@pytest.mark.django_db
def test_editing_chore_from_template_leaves_template_and_source(client):
    tpl = builtin("Take out the trash")
    before = Chore.objects.filter(pk=tpl.pk).values().get()
    post(client, template=str(tpl.pk), title="Trash")
    chore = chores().get()
    other = builtin("Change bed sheets")
    response = post(client, edit_url(chore), title="Bins", points="20", template=str(other.pk))
    assert response.status_code == 302
    chore.refresh_from_db()
    assert chore.title == "Bins"
    assert chore.template_source == tpl  # edit never changes the source
    assert Chore.objects.filter(pk=tpl.pk).values().get() == before


@pytest.mark.django_db
def test_deleting_chore_from_template_leaves_template(client):
    tpl = builtin("Take out the trash")
    post(client, template=str(tpl.pk))
    chores().get().delete()
    assert Chore.objects.filter(pk=tpl.pk, is_template=True, is_builtin=True).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["builtin", "household"])
def test_templates_still_404_on_edit(client, kind):
    tpl = builtin("Take out the trash") if kind == "builtin" else make_template("Mine")
    assert client.get(edit_url(tpl)).status_code == 404
    assert post(client, edit_url(tpl), title="Hacked").status_code == 404
    tpl.refresh_from_db()
    assert tpl.title != "Hacked"


# --- Save as template ------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("on_edit", [False, True])
def test_checkbox_shown_unticked(client, on_edit):
    url = edit_url(make_chore()) if on_edit else CREATE
    page = page_of(client.get(url))
    box = re.search(r'<input type="checkbox" name="save_as_template"[^>]*>', page).group(0)
    assert "checked" not in box
    assert "Also save as a household template" in page


@pytest.mark.django_db
def test_save_as_template_on_create_adds_two_rows(client):
    ann = make_member("Ann")
    total = Chore.objects.count()
    response = post(client, title="Feed cat", instructions="Wet\nfood", start_date="2026-04-01",
                    due_time="08:00", assignment_mode="fixed", assigned_to=str(ann.pk),
                    eligibility="kids", recurrence="weekly", recurrence_day="3", points="20",
                    save_as_template="on")
    assert response.status_code == 302
    assert Chore.objects.count() == total + 2
    chore = chores().get()
    copy = household().get()
    assert response["Location"] == edit_url(chore)
    for field in COPIED:
        assert getattr(copy, field) == getattr(chore, field), field
    assert copy.eligibility is None  # #4 field clearing applies to the copy too
    assert (copy.is_template, copy.is_builtin, copy.template_source) == (True, False, None)
    assert "Chore saved and template added" in page_of(client.get(response["Location"]))


@pytest.mark.django_db
def test_save_as_template_on_edit_adds_one_row_and_updates_in_place(client):
    chore = make_chore(title="Old", recurrence="monthly", recurrence_day=3)
    total = Chore.objects.count()
    response = post(client, edit_url(chore), title="New", assignment_mode="rotating",
                    eligibility="adults", recurrence="daily", recurrence_day="3",
                    save_as_template="on")
    assert response.status_code == 302
    assert response["Location"] == edit_url(chore)
    assert Chore.objects.count() == total + 1
    chore.refresh_from_db()
    copy = household().get()
    assert chore.title == "New"
    for field in COPIED:
        assert getattr(copy, field) == getattr(chore, field), field
    assert copy.recurrence_day is None
    assert copy.template_source is None
    page = page_of(client.get(edit_url(chore)))
    assert "Chore saved and template added" in page


@pytest.mark.django_db
@pytest.mark.parametrize("on_edit", [False, True])
def test_unticked_creates_no_template(client, on_edit):
    url = edit_url(make_chore()) if on_edit else CREATE
    response = post(client, url, title="Plain")
    assert response.status_code == 302
    assert household().count() == 0
    page = page_of(client.get(response["Location"]))
    assert "Chore saved" in page
    assert "template added" not in page


@pytest.mark.django_db
@pytest.mark.parametrize("on_edit", [False, True])
def test_ticked_invalid_post_saves_nothing_and_keeps_box_ticked(client, on_edit):
    chore = make_chore(title="Keep") if on_edit else None
    url = edit_url(chore) if on_edit else CREATE
    total = Chore.objects.count()
    response = post(client, url, title="", save_as_template="on")
    assert response.status_code == 200
    assert Chore.objects.count() == total
    box = re.search(r'<input type="checkbox" name="save_as_template"[^>]*>', page_of(response)).group(0)
    assert "checked" in box
    if chore:
        chore.refresh_from_db()
        assert chore.title == "Keep"


@pytest.mark.django_db
def test_new_template_listed_under_household(client):
    post(client, title="Walk dog", save_as_template="on")
    groups = picker_groups(page_of(client.get(CREATE)))
    assert groups["Household"] == ["Walk dog"]
    assert groups["Built-in"] == BUILTIN_TITLES_SORTED


@pytest.mark.django_db
def test_saving_as_template_twice_lists_both_by_id(client):
    chore = make_chore(title="Walk dog")
    post(client, edit_url(chore), title="Walk dog", save_as_template="on")
    post(client, edit_url(chore), title="Walk dog", save_as_template="on")
    copies = list(household().order_by("id"))
    assert [c.title for c in copies] == ["Walk dog", "Walk dog"]
    section = picker(page_of(client.get(CREATE)))
    household_ids = re.findall(r'href="/chores/create/\?template=(\d+)"', section)[4:]
    assert household_ids == [str(c.pk) for c in copies]


@pytest.mark.django_db
def test_from_template_and_save_as_template(client):
    tpl = builtin("Change bed sheets")
    post(client, template=str(tpl.pk), title="Sheets", assignment_mode="rotating",
         eligibility="everyone", recurrence="weekly", recurrence_day="6", points="20",
         save_as_template="on")
    chore = chores().get()
    copy = household().get()
    assert chore.template_source == tpl
    assert copy.template_source is None
    assert copy.title == "Sheets"
