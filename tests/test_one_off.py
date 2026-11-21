"""One-off chores from the dashboard (#19)."""
import html
import re
from urllib.parse import quote

import pytest
from django.contrib.messages import get_messages
from django.test import Client

from chores.models import Chore, ChoreOccurrence, RotationState
from tests.test_chores import payload
from tests.test_dashboard import WED, at, d, get, member, page
from tests.test_delete import HOSTILE
from tests.test_return_filters import BACK, FORM
from tests.test_toggle import HTMX

pytestmark = pytest.mark.django_db

SAT = "2026-11-21T10:00:00"  # Sat; current week 2026-W47 (Nov 16 - 22)
SUN_NOON = "2026-11-22T12:00:00"
BASE = "/chores/create/?date={date}&recurrence=once&return_to=dashboard"
ADD = re.compile(r'<a id="add-chore" role="button" href="([^"]*)">\+ Add Chore</a>')
NO_CHORES = re.compile(r'<section id="no-chores">.*?<a href="([^"]*)">Add a chore</a>', re.S)
SELECTED = re.compile(r'<option value="([^"]*)" selected>')


@pytest.fixture
def client():
    return Client()


def hidden(fragment):
    found = dict(re.findall(r'<input type="hidden" name="([a-z_]+)" value="([^"]*)">', fragment))
    found.pop("csrfmiddlewaretoken", None)
    return found


def add_links(body):
    return [html.unescape(h) for h in ADD.findall(body)]


def add_link(client, when=SAT, **kwargs):
    [found] = add_links(page(client, when=when, **kwargs))
    return found


def create_page(client, query="", when=SAT):
    with at(when):
        response = client.get("/chores/create/" + query)
    assert response.status_code == 200
    return response.content.decode()


def recurrence_selected(body):
    select = re.search(r'<select name="recurrence".*?</select>', body, re.S).group(0)
    return SELECTED.findall(select)


def save(client, when=SAT, tz=None, **fields):
    if tz:
        client.cookies["tz"] = tz
    with at(when):
        return client.post("/chores/create/", payload(**fields))


def one_offs():
    return ChoreOccurrence.objects.filter(chore__is_template=False)


def section_titles(body, key):
    block = re.search(rf'<section id="section-{key}">(.*?)</section>', body, re.S).group(1)
    return re.findall(r'<strong><a [^>]*>([^<]*)</a>', block)


# --- "+ Add Chore" ----------------------------------------------------------------


def test_add_link_current_week_is_today(client):
    assert add_link(client) == BASE.format(date="2026-11-21")


def test_add_link_with_member_and_day_filter(client):
    m = member("Ana")
    got = add_link(client, data={"member": m.pk, "day": "3"})
    assert got == BASE.format(date="2026-11-18") + f"&member={m.pk}&day=3"


def test_add_link_future_week_is_monday(client):
    assert add_link(client, week="2026-W49") == BASE.format(date="2026-11-30") + "&week=2026-W49"


def test_add_link_future_week_day_and_unassigned(client):
    got = add_link(client, week="2026-W49", data={"day": "5", "member": "unassigned"})
    assert got == BASE.format(date="2026-12-04") + "&week=2026-W49&member=unassigned&day=5"


def test_add_link_past_week(client):
    assert add_link(client, week="2026-W45") == BASE.format(date="2026-11-02") + "&week=2026-W45"


def test_add_link_on_htmx_partial_and_history_restore(client):
    partial = page(client, when=SAT, **HTMX)
    assert partial.count('id="add-chore"') == 1 and "<html" not in partial
    restore = page(client, when=SAT, **HTMX, HTTP_HX_HISTORY_RESTORE_REQUEST="true")
    assert restore.count('id="add-chore"') == 1 and "<html" in restore
    assert page(client, when=SAT).count('id="add-chore"') == 1


def test_add_link_is_a_plain_link(client):
    body = page(client, when=SAT)
    tag = re.search(r'<a id="add-chore"[^>]*>', body).group(0)
    assert "hx-" not in tag


def test_add_link_today_is_viewer_date(client):
    assert add_link(client, SUN_NOON) == BASE.format(date="2026-11-22")
    assert add_link(client, SUN_NOON, tz="Pacific/Kiritimati") == BASE.format(date="2026-11-23")


@pytest.mark.parametrize("data", [
    {"week": "2026-W99"}, {"week": "//evil.example"}, {"member": "abc"},
    {"member": "1\r\nX"}, {"member": "GONE"}, {"day": "9"},
])
def test_add_link_drops_invalid_values(client, data):
    gone = member("Gone")
    pk = gone.pk
    gone.delete()
    data = {k: str(pk) if v == "GONE" else v for k, v in data.items()}
    body = page(client, when=SAT, data=data)
    assert add_links(body) == [BASE.format(date="2026-11-21")]
    assert "evil.example" not in body


def test_empty_state_link_matches_add_link(client):
    body = page(client, when=SAT, week="2026-W49", data={"day": "5"})
    assert 'id="no-chores"' in body
    [empty] = NO_CHORES.findall(body)
    assert html.unescape(empty) == add_links(body)[0]


# --- ?recurrence= pre-fill -----------------------------------------------------------


@pytest.mark.parametrize("value", ["once", "daily", "weekly", "monthly"])
def test_recurrence_prefill(client, value):
    assert recurrence_selected(create_page(client, f"?recurrence={value}")) == [value]


def test_default_recurrence_is_once(client):
    assert recurrence_selected(create_page(client)) == ["once"]


@pytest.mark.parametrize("raw", [
    "", "ONCE", "Once", "%20once", "once%20", "once%00", "once%0a", "yearly", "%D9%A1",
    quote(HOSTILE), "x" * 10000,
])
def test_hostile_recurrence_falls_back_to_once(client, raw):
    body = create_page(client, f"?recurrence={raw}")
    assert recurrence_selected(body) == ["once"]
    assert "<script>alert" not in body and "xxxxxxxxxx" not in body
    assert "yearly" not in body and "\u0661" not in body


def test_last_recurrence_wins(client):
    assert recurrence_selected(create_page(client, "?recurrence=daily&recurrence=bogus")) == ["once"]
    assert recurrence_selected(create_page(client, "?recurrence=bogus&recurrence=daily")) == ["daily"]


def test_recurrence_prefill_keeps_other_defaults(client):
    body = create_page(client, "?recurrence=daily&date=2026-11-18")
    assert 'value="2026-11-18"' in body
    assert recurrence_selected(body) == ["daily"]
    assert 'name="title"' in body and 'value="10" selected' in body


def test_template_wins_over_recurrence(client):
    weekly = Chore.objects.filter(is_template=True, recurrence="weekly").first()
    body = create_page(client, f"?template={weekly.pk}&date=2026-11-18&recurrence=once")
    assert recurrence_selected(body) == ["weekly"]
    assert 'value="2026-11-18"' in body


def test_recurrence_ignored_on_edit(client):
    c = Chore.objects.create(title="Dishes", start_date=d("2026-11-16"))
    with at(SAT):
        body = client.get(f"/chores/{c.pk}/edit/?recurrence=daily").content.decode()
    assert recurrence_selected(body) == ["once"]


def test_recurrence_ignored_on_post(client):
    with at(SAT):
        client.post("/chores/create/?recurrence=daily", payload(recurrence="once"))
    assert Chore.objects.get(title="Dishes", is_template=False).recurrence == "once"


# --- return_to -----------------------------------------------------------------------


def test_create_without_return_to_is_unchanged(client):
    body = create_page(client, "?date=2026-11-18&member=3&day=3")
    assert hidden(FORM.search(body).group(1)) == {}
    assert not BACK.findall(body)
    assert "return_to" not in body


def test_return_to_adds_hidden_inputs_and_back_link(client):
    m = member("Ana")
    body = create_page(client, f"?return_to=dashboard&week=2026-W49&member={m.pk}&day=3")
    assert hidden(FORM.search(body).group(1)) == {
        "return_to": "dashboard", "week": "2026-W49", "member": str(m.pk), "day": "3"}
    assert html.unescape(BACK.findall(body)[0]) == f"/dashboard/?week=2026-W49&member={m.pk}&day=3"


def test_return_to_without_filters_has_only_return_input(client):
    body = create_page(client, "?return_to=dashboard")
    assert hidden(FORM.search(body).group(1)) == {"return_to": "dashboard"}
    assert html.unescape(BACK.findall(body)[0]) == "/dashboard/"


@pytest.mark.parametrize("raw", [
    "Dashboard", "%2Fdashboard%2F", "%2F%2Fevil.example", "https%3A%2F%2Fevil.example",
    "dashboard%00", quote(HOSTILE),
])
def test_hostile_return_to_counts_as_absent(client, raw):
    body = create_page(client, f"?return_to={raw}")
    assert "return_to" not in body and not BACK.findall(body)
    assert "evil.example" not in body and "<script>alert" not in body


def test_picker_links_keep_date_and_return(client):
    m = member("Ana")
    body = create_page(client, f"?date=2026-11-18&return_to=dashboard&member={m.pk}&day=3")
    tpl = Chore.objects.filter(is_template=True).first()
    want = f"/chores/create/?template={tpl.pk}&date=2026-11-18&return_to=dashboard&member={m.pk}&day=3"
    links = [html.unescape(h) for h in re.findall(r'(?:href|hx-get)="(/chores/create/\?template=[^"]*)"', body)]
    assert want in links
    assert all(l.endswith(f"&return_to=dashboard&member={m.pk}&day=3") for l in links)
    assert all("recurrence" not in l for l in links)


def test_picker_links_without_return_are_as_before(client):
    body = create_page(client, "?date=2026-11-18")
    links = re.findall(r'href="(/chores/create/\?template=[^"]*)"', body)
    assert links and all(l.endswith("&amp;date=2026-11-18") for l in links)


def test_template_page_keeps_hidden_inputs(client):
    tpl = Chore.objects.filter(is_template=True).first()
    body = create_page(client, f"?template={tpl.pk}&return_to=dashboard&week=2026-W49")
    assert hidden(FORM.search(body).group(1))["return_to"] == "dashboard"
    assert hidden(FORM.search(body).group(1))["week"] == "2026-W49"


def test_post_with_return_to_redirects_to_dashboard_and_flashes_once(client):
    m = member("Ana")
    response = save(client, return_to="dashboard", week="2026-W49", member=str(m.pk))
    assert response.status_code == 302
    assert response["Location"] == f"/dashboard/?week=2026-W49&member={m.pk}"
    body = page(client, week="2026-W49", data={"member": m.pk})
    assert body.count("Chore saved") == 1
    assert "Chore saved" not in page(client, week="2026-W49", data={"member": m.pk})


def test_post_with_return_to_current_week_goes_to_plain_dashboard(client):
    assert save(client, return_to="dashboard")["Location"] == "/dashboard/"


def test_save_as_template_message_on_dashboard(client):
    save(client, return_to="dashboard", save_as_template="on")
    assert "Chore saved and template added" in page(client, when=SAT)


def test_post_without_return_to_goes_to_edit_page(client):
    response = save(client)
    c = Chore.objects.get(title="Dishes", is_template=False)
    assert response["Location"] == f"/chores/{c.pk}/edit/"
    assert save(client, return_to="Dashboard", title="Other")["Location"].startswith("/chores/")


def test_invalid_post_rerenders_with_hidden_inputs_and_back_link(client):
    m = member("Ana")
    response = save(client, title="", return_to="dashboard", week="2026-W49", member=str(m.pk), day="3")
    assert response.status_code == 200
    body = response.content.decode()
    assert hidden(FORM.search(body).group(1)) == {
        "return_to": "dashboard", "week": "2026-W49", "member": str(m.pk), "day": "3"}
    assert html.unescape(BACK.findall(body)[0]) == f"/dashboard/?week=2026-W49&member={m.pk}&day=3"
    assert not Chore.objects.filter(is_template=False).exists()


def test_hostile_post_values_are_dropped(client):
    response = save(client, return_to="dashboard", week="//evil.example", member="1\r\nX", day=HOSTILE)
    assert response.status_code == 302
    assert response["Location"] == "/dashboard/"


# --- Saving a one-off shows it straight away -----------------------------------------


def titles(client, key, when=SAT, **kwargs):
    return section_titles(page(client, when=when, **kwargs), key)


@pytest.mark.parametrize("start, key", [
    ("2026-11-21", "today"), ("2026-11-22", "upcoming"), ("2026-11-16", "overdue"),
    ("2026-11-02", "overdue"),
])
def test_one_off_this_week_or_earlier_is_stored_and_shown(client, start, key):
    save(client, return_to="dashboard", start_date=start)
    [row] = one_offs()
    assert row.date == d(start) and row.status == "todo" and row.is_override is False
    assert titles(client, key) == ["Dishes"]


def test_due_time_passed_is_overdue_and_copied(client):
    save(client, start_date="2026-11-21", due_time="08:00", points="20")
    [row] = one_offs()
    assert (str(row.due_time), row.points) == ("08:00:00", 20)
    assert titles(client, "overdue") == ["Dishes"] and titles(client, "today") == []


def test_past_week_one_off_is_overdue_on_its_week(client):
    save(client, start_date="2026-11-02")
    assert one_offs().count() == 1
    body = page(client, week="2026-W45", when=SAT)
    assert "Dishes" in body and "Overdue" in body


def test_future_week_one_off_waits_then_is_stored_once(client):
    save(client, start_date="2026-11-30")
    assert one_offs().count() == 0
    assert "Planned" in page(client, week="2026-W49", when=SAT)
    page(client, week="2026-W49", when=SAT)
    assert one_offs().count() == 0
    with at("2026-11-30T09:00:00"):
        client.get("/dashboard/")
        client.get("/dashboard/")
    assert one_offs().count() == 1


def test_no_duplicates_after_dashboard_and_calendar(client):
    save(client, start_date="2026-11-21")
    for _ in range(2):
        page(client, when=SAT)
        with at(SAT):
            assert client.get("/calendar/").status_code == 200
    assert one_offs().count() == 1


def test_kiritimati_stores_monday_one_off_but_utc_does_not(client):
    save(client, SUN_NOON, start_date="2026-11-23")
    assert one_offs().count() == 0
    save(client, SUN_NOON, tz="Pacific/Kiritimati", start_date="2026-11-23", title="Kiri")
    assert [o.chore.title for o in one_offs()] == ["Kiri"]


def test_save_as_template_copy_has_no_occurrence(client):
    save(client, start_date="2026-11-21", save_as_template="on")
    assert one_offs().count() == 1
    copy = Chore.objects.get(title="Dishes", is_template=True, is_builtin=False)
    assert copy.occurrences.count() == 0


def test_from_template_once_stores_one_row(client):
    tpl = Chore.objects.create(title="Tpl", start_date=d("2026-11-01"), is_template=True)
    save(client, start_date="2026-11-21", template=str(tpl.pk), title="Tpl")
    assert one_offs().count() == 1 and tpl.occurrences.count() == 0
    assert Chore.objects.get(title="Tpl", is_template=False).template_source == tpl


@pytest.mark.parametrize("fields", [
    {"recurrence": "daily"}, {"recurrence": "weekly", "recurrence_day": "2"},
    {"recurrence": "monthly", "recurrence_day": "20"},
])
def test_recurring_gets_no_row_on_save(client, fields):
    save(client, start_date="2026-11-17", **fields)
    assert ChoreOccurrence.objects.count() == 0
    page(client, when=SAT)
    assert ChoreOccurrence.objects.count() > 0


def test_editing_creates_nothing(client):
    save(client, start_date="2026-11-30")
    c = Chore.objects.get(title="Dishes", is_template=False)
    with at(SAT):
        client.post(f"/chores/{c.pk}/edit/", payload(start_date="2026-11-21"))
    assert ChoreOccurrence.objects.count() == 0
    save(client, start_date="2026-11-21", title="Now")
    now = Chore.objects.get(title="Now")
    before = ChoreOccurrence.objects.count()
    with at(SAT):
        client.post(f"/chores/{now.pk}/edit/", payload(title="Now", start_date="2026-11-22"))
    assert ChoreOccurrence.objects.count() == before


def test_fixed_assignee_and_unassigned(client):
    ana = member("Ana")
    save(client, assignment_mode="fixed", assigned_to=str(ana.pk), start_date="2026-11-21")
    save(client, assignment_mode="fixed", start_date="2026-11-21", title="Nobody")
    save(client, start_date="2026-11-21", title="Free")
    got = {o.chore.title: o.member for o in one_offs()}
    assert got == {"Dishes": ana, "Nobody": None, "Free": None}
    assert RotationState.objects.count() == 0


def test_rotating_takes_first_in_pool_and_sets_state(client):
    first = member("Ana", role="adult", order=1)
    member("Bo", role="adult", order=2)
    save(client, assignment_mode="rotating", eligibility="adults", start_date="2026-11-21")
    [row] = one_offs()
    assert row.member == first
    assert RotationState.objects.get(chore=row.chore).last_member == first


def test_rotating_with_empty_pool_is_unassigned(client):
    member("Kid", role="kid")
    save(client, return_to="dashboard", assignment_mode="rotating", eligibility="adults",
         start_date="2026-11-21")
    [row] = one_offs()
    assert row.member is None
    assert not RotationState.objects.filter(chore=row.chore, last_member__isnull=False).exists()
    assert "needs eligible member" in page(client, when=SAT)


def test_failure_in_materializing_rolls_back_chore(client, monkeypatch):
    def boom(chore, today):
        raise RuntimeError("boom")
    monkeypatch.setattr("chores.views.materialize_one_off", boom)
    client.raise_request_exception = False
    save(client, start_date="2026-11-21")
    assert not Chore.objects.filter(title="Dishes", is_template=False).exists()


def test_recurring_chores_are_unchanged_by_a_this_week_start(client):
    save(client, start_date="2026-11-21", recurrence="daily")
    assert ChoreOccurrence.objects.count() == 0


# --- #20's Once <-> repeating guard ----------------------------------------------------

GUARD = "can't switch between Once and a repeating schedule"


def test_this_week_one_off_cannot_switch_to_repeating(client):
    save(client, start_date="2026-11-21")
    c = Chore.objects.get(title="Dishes", is_template=False)
    with at(SAT):
        response = client.post(f"/chores/{c.pk}/edit/", payload(start_date="2026-11-21", recurrence="daily"))
    assert response.status_code == 200
    assert GUARD in html.unescape(response.content.decode())
    c.refresh_from_db()
    assert c.recurrence == "once"


def test_future_one_off_can_switch_to_repeating(client):
    save(client, start_date="2026-11-30")
    c = Chore.objects.get(title="Dishes", is_template=False)
    with at(SAT):
        response = client.post(f"/chores/{c.pk}/edit/", payload(start_date="2026-11-30", recurrence="daily"))
    assert response.status_code == 302
    c.refresh_from_db()
    assert c.recurrence == "daily"
