import datetime
import re

import pytest
from django.contrib import admin
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from chores.models import Chore, Member

CREATE = "/chores/create/"


@pytest.fixture
def client():
    return Client()


def make_member(name, role="adult", avatar="😀"):
    return Member.objects.create(name=name, role=role, avatar=avatar)


def make_chore(**kwargs):
    fields = {"title": "Dishes", "start_date": datetime.date(2026, 3, 2)}
    fields.update(kwargs)
    return Chore.objects.create(**fields)


def edit_url(chore_or_pk):
    pk = getattr(chore_or_pk, "pk", chore_or_pk)
    return f"/chores/{pk}/edit/"


def payload(**overrides):
    data = {
        "title": "Dishes",
        "instructions": "",
        "start_date": "2026-03-02",
        "due_time": "",
        "assignment_mode": "unassigned",
        "eligibility": "",
        "assigned_to": "",
        "recurrence": "once",
        "recurrence_day": "",
        "points": "10",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def post(client, url=CREATE, **overrides):
    return client.post(url, payload(**overrides))


def html(response):
    return response.content.decode()


def section(page, field_id):
    """The wrapper <div id="field-..."> opening tag, to check its hidden flag."""
    return re.search(rf'<div id="field-{field_id}"[^>]*>', page).group(0)


def is_hidden(page, field_id):
    return " hidden" in section(page, field_id)


def visible_dynamic_fields(page):
    ids = ["eligibility", "assigned-to", "weekday", "monthday"]
    return [i for i in ids if not is_hidden(page, i)]


def select_html(page, select_id):
    return re.search(rf'<select[^>]*id="{select_id}".*?</select>', page, re.S).group(0)


def options(page, select_id):
    return re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', select_html(page, select_id))


def selected(page, select_id):
    return re.findall(r'<option value="([^"]*)" selected', select_html(page, select_id))


def assert_form_error(response, field, text=None):
    assert response.status_code == 200
    errors = response.context["form"].errors
    assert field in errors, errors
    if text:
        assert text in errors[field]
    assert "Chore saved" not in html(response)


# --- Model -----------------------------------------------------------------


@pytest.mark.django_db
def test_chore_registered_in_admin():
    assert Chore in admin.site._registry


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


def test_choices_match_architecture():
    assert [c for c, _ in Chore.AssignmentMode.choices] == ["fixed", "rotating", "unassigned"]
    assert Chore.Eligibility.choices == [
        ("kids", "Kids only"),
        ("adults", "Adults only"),
        ("everyone", "Everyone"),
    ]
    assert [c for c, _ in Chore.Recurrence.choices] == ["once", "daily", "weekly", "monthly"]
    assert [c for c, _ in Chore.POINTS_CHOICES] == [5, 10, 20]


def test_field_definitions():
    title = Chore._meta.get_field("title")
    assert title.max_length == 100
    assert Chore._meta.get_field("instructions").blank
    assert not Chore._meta.get_field("start_date").null
    assert Chore._meta.get_field("due_time").null


@pytest.mark.django_db
def test_deleting_member_keeps_fixed_chore_unassigned(client):
    ann = make_member("Ann")
    chore = make_chore(assignment_mode="fixed", assigned_to=ann)
    client.post(reverse("member_delete", args=[ann.pk]))
    chore.refresh_from_db()
    assert chore.assigned_to is None
    assert chore.assignment_mode == "fixed"


@pytest.mark.django_db
def test_deleting_template_source_sets_null():
    template = make_chore(title="Template", is_template=True)
    chore = make_chore(template_source=template)
    template.delete()
    chore.refresh_from_db()
    assert chore.template_source is None


# --- Create: GET -----------------------------------------------------------


@pytest.mark.django_db
def test_create_page_extends_base_with_profile_switcher(client):
    page = html(client.get(CREATE))
    assert '<nav id="profile-switcher"' in page


@pytest.mark.django_db
def test_create_form_field_order_and_excluded_fields(client):
    page = html(client.get(CREATE))
    names = ["title", "instructions", "start_date", "due_time", "assignment_mode",
             "eligibility", "assigned_to", "recurrence", "recurrence_day", "points"]
    positions = [page.index(f'name="{n}"') for n in names]
    assert positions == sorted(positions)
    assert 'name="is_template"' not in page
    assert 'name="template_source"' not in page


@pytest.mark.django_db
def test_create_defaults(client):
    response = client.get(CREATE)
    page = html(response)
    assert selected(page, "id_assignment_mode") == ["unassigned"]
    assert selected(page, "id_recurrence") == ["once"]
    assert selected(page, "id_points") == ["10"]
    today = timezone.localdate().isoformat()
    assert re.search(rf'<input type="date" name="start_date" value="{today}"', page)
    assert re.search(r'<input type="time" name="due_time" id="id_due_time">', page)
    assert re.search(r'<textarea name="instructions"[^>]*>\s*</textarea>', page)
    assert "Chore saved" not in page


@pytest.mark.django_db
def test_points_is_select_of_exactly_5_10_20(client):
    page = html(client.get(CREATE))
    assert options(page, "id_points") == [("5", "5"), ("10", "10"), ("20", "20")]
    assert not re.search(r'<input[^>]*name="points"', page)


@pytest.mark.django_db
def test_assigned_to_lists_members_in_order_with_nobody_default(client):
    zed = make_member("Zed", avatar="🦊")
    amy = make_member("Amy", avatar="👧")
    Member.objects.filter(pk=zed.pk).update(order=5)
    Member.objects.filter(pk=amy.pk).update(order=1)
    page = html(client.get(CREATE))
    assert options(page, "id_assigned_to") == [
        ("", "— nobody yet —"),
        (str(amy.pk), "👧 Amy"),
        (str(zed.pk), "🦊 Zed"),
    ]
    assert selected(page, "id_assigned_to") == [""]


@pytest.mark.django_db
def test_assigned_to_ties_in_order_break_by_id(client):
    a, b = make_member("A"), make_member("B")
    Member.objects.update(order=3)
    page = html(client.get(CREATE))
    assert [v for v, _ in options(page, "id_assigned_to")] == ["", str(a.pk), str(b.pk)]


@pytest.mark.django_db
def test_assigned_to_with_no_members_only_nobody(client):
    page = html(client.get(CREATE))
    assert options(page, "id_assigned_to") == [("", "— nobody yet —")]


@pytest.mark.django_db
def test_recurrence_day_selects_have_week_and_month_options(client):
    page = html(client.get(CREATE))
    assert options(page, "id_recurrence_day_week") == list(zip(
        [str(i) for i in range(7)],
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    ))
    assert options(page, "id_recurrence_day_month") == [(str(i), str(i)) for i in range(1, 32)]


# --- Dynamic fields on first render ----------------------------------------


@pytest.mark.django_db
def test_create_defaults_hide_all_dynamic_fields(client):
    assert visible_dynamic_fields(html(client.get(CREATE))) == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fields, visible",
    [
        ({"assignment_mode": "rotating", "eligibility": "kids"}, ["eligibility"]),
        ({"assignment_mode": "fixed"}, ["assigned-to"]),
        ({"assignment_mode": "unassigned"}, []),
        ({"recurrence": "weekly", "recurrence_day": 2}, ["weekday"]),
        ({"recurrence": "monthly", "recurrence_day": 15}, ["monthday"]),
        ({"recurrence": "daily"}, []),
    ],
)
def test_edit_shows_fields_for_saved_values(client, fields, visible):
    chore = make_chore(**fields)
    assert visible_dynamic_fields(html(client.get(edit_url(chore)))) == visible


@pytest.mark.django_db
def test_error_rerender_shows_fields_for_chosen_mode_and_recurrence(client):
    response = post(client, title="", assignment_mode="rotating", eligibility="adults",
                    recurrence="monthly", recurrence_day="9")
    page = html(response)
    assert visible_dynamic_fields(page) == ["eligibility", "monthday"]
    assert selected(page, "id_eligibility") == ["adults"]
    assert selected(page, "id_recurrence_day_month") == ["9"]


@pytest.mark.django_db
def test_page_has_change_handlers_for_both_dropdowns(client):
    page = html(client.get(CREATE))
    assert 'form.elements["assignment_mode"].addEventListener("change", sync)' in page
    assert 'form.elements["recurrence"].addEventListener("change", sync)' in page
    assert "<noscript>" in page


# --- Create: POST, each kind -----------------------------------------------


@pytest.mark.django_db
def test_valid_create_saves_one_chore_and_redirects_with_message(client):
    response = post(client, title="Take out trash", instructions="Bins\nto curb",
                    due_time="07:30", points="20")
    assert Chore.objects.count() == 1
    chore = Chore.objects.get()
    assert response.status_code == 302
    assert response["Location"] == edit_url(chore)
    assert chore.is_template is False
    assert chore.template_source is None
    assert chore.title == "Take out trash"
    assert chore.instructions == "Bins\nto curb"
    assert chore.start_date == datetime.date(2026, 3, 2)
    assert chore.due_time == datetime.time(7, 30)
    assert chore.points == 20
    page = html(client.get(response["Location"]))
    assert "Chore saved" in page


@pytest.mark.django_db
def test_crafted_is_template_and_template_source_are_ignored(client):
    template = make_chore(title="T", is_template=True)
    post(client, is_template="on", template_source=str(template.pk))
    chore = Chore.objects.get(is_template=False)
    assert chore.template_source is None


@pytest.mark.django_db
def test_create_fixed_with_assignee(client):
    ann = make_member("Ann")
    post(client, assignment_mode="fixed", assigned_to=str(ann.pk))
    chore = Chore.objects.get()
    assert (chore.assignment_mode, chore.assigned_to, chore.eligibility) == ("fixed", ann, None)


@pytest.mark.django_db
def test_create_fixed_without_assignee_is_allowed(client):
    response = post(client, assignment_mode="fixed", assigned_to="")
    assert response.status_code == 302
    chore = Chore.objects.get()
    assert (chore.assignment_mode, chore.assigned_to) == ("fixed", None)


@pytest.mark.django_db
@pytest.mark.parametrize("eligibility", ["kids", "adults", "everyone"])
def test_create_rotating_for_each_eligibility(client, eligibility):
    post(client, assignment_mode="rotating", eligibility=eligibility)
    chore = Chore.objects.get()
    assert (chore.assignment_mode, chore.eligibility, chore.assigned_to) == ("rotating", eligibility, None)


@pytest.mark.django_db
def test_rotating_saves_with_no_eligible_members(client):
    make_member("Dad", role="adult")
    response = post(client, assignment_mode="rotating", eligibility="kids")
    assert response.status_code == 302
    assert Chore.objects.get().eligibility == "kids"


@pytest.mark.django_db
def test_create_unassigned(client):
    post(client, assignment_mode="unassigned")
    chore = Chore.objects.get()
    assert (chore.assignment_mode, chore.eligibility, chore.assigned_to) == ("unassigned", None, None)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "recurrence, day, stored",
    [("once", "", None), ("daily", "", None), ("weekly", "0", 0), ("weekly", "6", 6),
     ("monthly", "1", 1), ("monthly", "31", 31)],
)
def test_create_each_recurrence(client, recurrence, day, stored):
    response = post(client, recurrence=recurrence, recurrence_day=day)
    assert response.status_code == 302
    chore = Chore.objects.get()
    assert (chore.recurrence, chore.recurrence_day) == (recurrence, stored)


@pytest.mark.django_db
def test_weekly_day_may_differ_from_start_weekday(client):
    # 2026-03-02 is a Monday; Friday is fine.
    response = post(client, recurrence="weekly", recurrence_day="4")
    assert response.status_code == 302
    assert Chore.objects.get().recurrence_day == 4


@pytest.mark.django_db
def test_without_js_both_day_selects_posted_picks_matching_one(client):
    data = payload(recurrence="monthly")
    data["recurrence_day"] = ["2", "20"]  # weekday select first, month select second
    client.post(CREATE, data)
    assert Chore.objects.get().recurrence_day == 20
    data = payload(title="B", recurrence="weekly")
    data["recurrence_day"] = ["2", "20"]
    client.post(CREATE, data)
    assert Chore.objects.get(title="B").recurrence_day == 2


# --- Validation ------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("title", ["", "   ", "\t\n"])
def test_empty_or_whitespace_title_rejected(client, title):
    assert_form_error(post(client, title=title), "title")
    assert Chore.objects.count() == 0


@pytest.mark.django_db
def test_missing_title_key_rejected(client):
    assert_form_error(post(client, title=None), "title")
    assert Chore.objects.count() == 0


@pytest.mark.django_db
def test_title_is_stripped(client):
    post(client, title="  Dishes  ")
    assert Chore.objects.get().title == "Dishes"


@pytest.mark.django_db
def test_title_length_limit(client):
    assert_form_error(post(client, title="x" * 101), "title")
    assert Chore.objects.count() == 0
    assert post(client, title="x" * 100).status_code == 302
    assert Chore.objects.get().title == "x" * 100


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value",
    ["", None, "2026-02-30", "2026-13-01", "not-a-date", "99999-01-01", "0000-01-01",
     "2026-1-1-1", "9" * 50],
)
def test_bad_start_date_rejected(client, value):
    assert_form_error(post(client, start_date=value), "start_date")
    assert Chore.objects.count() == 0


@pytest.mark.django_db
def test_past_start_date_allowed(client):
    response = post(client, start_date="2000-01-01")
    assert response.status_code == 302
    assert Chore.objects.get().start_date == datetime.date(2000, 1, 1)


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["25:00", "12:60", "noon", "9" * 50, "-1:00"])
def test_bad_due_time_rejected(client, value):
    assert_form_error(post(client, due_time=value), "due_time")
    assert Chore.objects.count() == 0


@pytest.mark.django_db
def test_empty_due_time_saved_as_null(client):
    post(client, due_time="")
    assert Chore.objects.get().due_time is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field, value",
    [
        ("points", "15"),
        ("points", "0"),
        ("points", "-10"),
        ("points", "10.0"),
        ("points", "abc"),
        ("points", "9" * 50),
        ("points", ""),
        ("assignment_mode", "sometimes"),
        ("assignment_mode", ""),
        ("recurrence", "yearly"),
        ("recurrence", ""),
        ("eligibility", "pets"),
    ],
)
def test_unknown_choices_rejected(client, field, value):
    assert_form_error(post(client, **{field: value}), field)
    assert Chore.objects.count() == 0


@pytest.mark.django_db
def test_unknown_eligibility_rejected_even_when_mode_hides_it(client):
    response = post(client, assignment_mode="fixed", eligibility="pets")
    assert_form_error(response, "eligibility")
    assert not is_hidden(html(response), "eligibility")  # error is visible
    assert Chore.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value",
    ["999", "0", "-1", "abc", "1.5", "9" * 30, str(2**63), "-" + "9" * 30, "١"],
)
def test_bad_assigned_to_rejected(client, value):
    make_member("Ann")
    response = post(client, assignment_mode="fixed", assigned_to=value)
    assert_form_error(response, "assigned_to")
    assert Chore.objects.count() == 0


@pytest.mark.django_db
def test_rotating_without_eligibility_rejected(client):
    assert_form_error(post(client, assignment_mode="rotating", eligibility=""),
                      "eligibility", "Choose who is eligible")
    assert Chore.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("day", ["", None, "7", "-1", "abc", "9" * 50, "3.5"])
def test_weekly_bad_day_rejected(client, day):
    response = post(client, recurrence="weekly", recurrence_day=day)
    assert_form_error(response, "recurrence_day", "Choose a day of the week")
    assert "Choose a day of the week" in html(response)
    assert Chore.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("day", ["", None, "0", "32", "-5", "abc", "9" * 50])
def test_monthly_bad_day_rejected(client, day):
    response = post(client, recurrence="monthly", recurrence_day=day)
    assert_form_error(response, "recurrence_day", "Choose a day of the month")
    assert "Choose a day of the month" in html(response)
    assert Chore.objects.count() == 0


@pytest.mark.django_db
def test_empty_post_rerenders_with_errors(client):
    response = client.post(CREATE, {})
    assert response.status_code == 200
    assert "title" in response.context["form"].errors
    assert Chore.objects.count() == 0


# --- Field clearing --------------------------------------------------------


@pytest.mark.django_db
def test_fixed_clears_eligibility(client):
    ann = make_member("Ann")
    post(client, assignment_mode="fixed", assigned_to=str(ann.pk), eligibility="kids")
    chore = Chore.objects.get()
    assert (chore.eligibility, chore.assigned_to) == (None, ann)


@pytest.mark.django_db
def test_rotating_clears_assigned_to(client):
    ann = make_member("Ann")
    post(client, assignment_mode="rotating", eligibility="everyone", assigned_to=str(ann.pk))
    chore = Chore.objects.get()
    assert (chore.eligibility, chore.assigned_to) == ("everyone", None)


@pytest.mark.django_db
def test_unassigned_clears_both(client):
    ann = make_member("Ann")
    post(client, assignment_mode="unassigned", eligibility="kids", assigned_to=str(ann.pk))
    chore = Chore.objects.get()
    assert (chore.eligibility, chore.assigned_to) == (None, None)


@pytest.mark.django_db
@pytest.mark.parametrize("recurrence", ["once", "daily"])
@pytest.mark.parametrize("day", ["3", "99", "garbage"])
def test_once_and_daily_clear_recurrence_day(client, recurrence, day):
    response = post(client, recurrence=recurrence, recurrence_day=day)
    assert response.status_code == 302
    assert Chore.objects.get().recurrence_day is None


# --- Re-render after error -------------------------------------------------


@pytest.mark.django_db
def test_rerender_keeps_typed_values(client):
    ann = make_member("Ann")
    response = post(client, title="x" * 101, instructions="Line 1\nLine 2",
                    start_date="2026-05-06", due_time="18:45", assignment_mode="fixed",
                    assigned_to=str(ann.pk), recurrence="weekly", recurrence_day="5", points="5")
    page = html(response)
    assert f'value="{"x" * 101}"' in page
    assert "Line 1\nLine 2</textarea>" in page
    assert 'value="2026-05-06"' in page
    assert 'value="18:45"' in page
    assert selected(page, "id_assignment_mode") == ["fixed"]
    assert selected(page, "id_assigned_to") == [str(ann.pk)]
    assert selected(page, "id_recurrence") == ["weekly"]
    assert selected(page, "id_recurrence_day_week") == ["5"]
    assert selected(page, "id_points") == ["5"]
    assert visible_dynamic_fields(page) == ["assigned-to", "weekday"]
    assert "Chore saved" not in page


@pytest.mark.django_db
def test_rerender_keeps_malformed_date_text(client):
    page = html(post(client, start_date="2026-02-30"))
    assert 'value="2026-02-30"' in page


@pytest.mark.django_db
def test_html_title_saved_as_is_and_escaped(client):
    post(client, title="<b>x</b>")
    chore = Chore.objects.get()
    assert chore.title == "<b>x</b>"
    page = html(client.get(edit_url(chore)))
    assert 'value="&lt;b&gt;x&lt;/b&gt;"' in page
    assert "<b>x</b>" not in page


# --- Edit ------------------------------------------------------------------


@pytest.mark.django_db
def test_edit_prefills_every_saved_value(client):
    ann = make_member("Ann")
    chore = make_chore(title="Mow", instructions="Front\nand back", start_date=datetime.date(2026, 7, 4),
                       due_time=datetime.time(9, 5), assignment_mode="fixed", assigned_to=ann,
                       recurrence="monthly", recurrence_day=17, points=20)
    page = html(client.get(edit_url(chore)))
    assert 'value="Mow"' in page
    assert "Front\nand back</textarea>" in page
    assert 'value="2026-07-04"' in page
    assert 'value="09:05"' in page
    assert selected(page, "id_assignment_mode") == ["fixed"]
    assert selected(page, "id_assigned_to") == [str(ann.pk)]
    assert selected(page, "id_recurrence") == ["monthly"]
    assert selected(page, "id_recurrence_day_month") == ["17"]
    assert selected(page, "id_points") == ["20"]
    assert f'action="{edit_url(chore)}"' in page
    assert "Chore saved" not in page


@pytest.mark.django_db
def test_edit_prefills_weekly_day_and_eligibility(client):
    chore = make_chore(assignment_mode="rotating", eligibility="adults", recurrence="weekly", recurrence_day=6)
    page = html(client.get(edit_url(chore)))
    assert selected(page, "id_eligibility") == ["adults"]
    assert selected(page, "id_recurrence_day_week") == ["6"]


@pytest.mark.django_db
def test_edit_fixed_with_deleted_assignee_shows_nobody(client):
    ann = make_member("Ann")
    chore = make_chore(assignment_mode="fixed", assigned_to=ann)
    ann.delete()
    response = client.get(edit_url(chore))
    assert response.status_code == 200
    page = html(response)
    assert selected(page, "id_assigned_to") == [""]
    assert not response.context["form"].errors


@pytest.mark.django_db
def test_edit_updates_in_place(client):
    chore = make_chore()
    response = post(client, edit_url(chore), title="Wash up", points="5")
    assert response.status_code == 302
    assert response["Location"] == edit_url(chore)
    assert Chore.objects.count() == 1
    chore.refresh_from_db()
    assert (chore.title, chore.points) == ("Wash up", 5)
    assert "Chore saved" in html(client.get(edit_url(chore)))


@pytest.mark.django_db
def test_edit_preserves_template_source(client):
    template = make_chore(title="T", is_template=True)
    chore = make_chore(template_source=template)
    post(client, edit_url(chore), title="Changed")
    chore.refresh_from_db()
    assert chore.template_source == template
    assert chore.is_template is False


@pytest.mark.django_db
def test_edit_fixed_to_rotating_clears_assigned_to(client):
    ann = make_member("Ann")
    chore = make_chore(assignment_mode="fixed", assigned_to=ann)
    post(client, edit_url(chore), assignment_mode="rotating", eligibility="everyone", assigned_to=str(ann.pk))
    chore.refresh_from_db()
    assert (chore.assignment_mode, chore.assigned_to, chore.eligibility) == ("rotating", None, "everyone")


@pytest.mark.django_db
def test_edit_rotating_to_fixed_clears_eligibility(client):
    chore = make_chore(assignment_mode="rotating", eligibility="kids")
    post(client, edit_url(chore), assignment_mode="fixed", eligibility="kids")
    chore.refresh_from_db()
    assert (chore.assignment_mode, chore.eligibility) == ("fixed", None)


@pytest.mark.django_db
def test_edit_to_unassigned_clears_both(client):
    ann = make_member("Ann")
    chore = make_chore(assignment_mode="fixed", assigned_to=ann)
    post(client, edit_url(chore), assignment_mode="unassigned", assigned_to=str(ann.pk), eligibility="kids")
    chore.refresh_from_db()
    assert (chore.assigned_to, chore.eligibility) == (None, None)


@pytest.mark.django_db
@pytest.mark.parametrize("old", [("weekly", 3), ("monthly", 28)])
@pytest.mark.parametrize("new", ["once", "daily"])
def test_edit_recurrence_change_clears_day(client, old, new):
    chore = make_chore(recurrence=old[0], recurrence_day=old[1])
    post(client, edit_url(chore), recurrence=new, recurrence_day=str(old[1]))
    chore.refresh_from_db()
    assert (chore.recurrence, chore.recurrence_day) == (new, None)


@pytest.mark.django_db
def test_edit_omitting_hidden_fields_still_clears_them(client):
    ann = make_member("Ann")
    chore = make_chore(assignment_mode="fixed", assigned_to=ann, recurrence="weekly", recurrence_day=2)
    post(client, edit_url(chore), assignment_mode="unassigned", assigned_to=None, eligibility=None,
         recurrence="daily", recurrence_day=None)
    chore.refresh_from_db()
    assert (chore.assigned_to, chore.recurrence_day) == (None, None)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "overrides",
    [
        {"title": ""},
        {"start_date": "2026-02-30"},
        {"points": "15"},
        {"assigned_to": "999999"},
        {"assigned_to": "9" * 30},
        {"assignment_mode": "rotating", "eligibility": ""},
        {"recurrence": "monthly", "recurrence_day": "32"},
    ],
)
def test_invalid_edit_leaves_record_unchanged(client, overrides):
    ann = make_member("Ann")
    chore = make_chore(title="Keep", assignment_mode="fixed", assigned_to=ann,
                       recurrence="weekly", recurrence_day=1, points=20)
    before = Chore.objects.filter(pk=chore.pk).values().get()
    data = {"title": "Changed", "assignment_mode": "fixed", "assigned_to": str(ann.pk),
            "recurrence": "weekly", "recurrence_day": "1", "points": "20"}
    data.update(overrides)
    response = post(client, edit_url(chore), **data)
    assert response.status_code == 200
    assert response.context["form"].errors
    assert "Chore saved" not in html(response)
    assert Chore.objects.filter(pk=chore.pk).values().get() == before


@pytest.mark.django_db
@pytest.mark.parametrize("pk", ["999", "0", "9" * 30, str(2**63)])
def test_unknown_id_404(client, pk):
    assert client.get(edit_url(pk)).status_code == 404
    assert post(client, edit_url(pk)).status_code == 404
    assert Chore.objects.count() == 0


@pytest.mark.django_db
def test_non_numeric_id_404(client):
    assert client.get("/chores/abc/edit/").status_code == 404
    assert client.get("/chores/-1/edit/").status_code == 404


@pytest.mark.django_db
def test_template_chore_404_for_get_and_post(client):
    template = make_chore(title="Template", is_template=True)
    assert client.get(edit_url(template)).status_code == 404
    assert post(client, edit_url(template), title="Hacked").status_code == 404
    template.refresh_from_db()
    assert template.title == "Template"


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["put", "delete", "patch"])
def test_other_methods_not_allowed(client, method):
    chore = make_chore()
    assert getattr(client, method)(CREATE).status_code == 405
    assert getattr(client, method)(edit_url(chore)).status_code == 405
